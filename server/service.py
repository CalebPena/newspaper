import hashlib
import json
import secrets
from datetime import UTC, timedelta
from uuid import uuid4

from sqlalchemy import select

from server.database import Asset, Edition, Printer, now
from server.schema import CreateEdition, Document, asset_ids


class ServiceError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def utc(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def iso(value):
    return utc(value).isoformat() if value else None


class NewspaperService:
    def __init__(self, sessions, storage, printer_id):
        self.sessions = sessions
        self.storage = storage
        self.printer_id = printer_id

    def initialize_printer(self):
        with self.sessions.begin() as session:
            if session.get(Printer, self.printer_id) is None:
                session.add(Printer(id=self.printer_id))

    def printer_lock(self, session):
        return session.scalar(select(Printer).where(Printer.id == self.printer_id).with_for_update())

    def reconcile(self, session):
        timestamp = now()
        for edition in session.scalars(select(Edition).where(
            Edition.printer_id == self.printer_id,
            Edition.status.in_(["scheduled", "claimed", "printing"]),
        )):
            previous_status = edition.status
            if edition.status == "scheduled" and edition.expires_at and utc(edition.expires_at) <= timestamp:
                edition.status = "expired"
            elif edition.lease_until and utc(edition.lease_until) <= timestamp:
                edition.status = "uncertain" if edition.status == "printing" else "scheduled"
                edition.error = "Printing lease expired; inspect the printer before retrying" if edition.status == "uncertain" else None
                edition.lease_until = None
                if edition.status == "scheduled":
                    edition.claim_token = None
                    if edition.expires_at and utc(edition.expires_at) <= timestamp:
                        edition.status = "expired"
            if edition.status != previous_status:
                edition.updated_at = timestamp
        session.flush()

    def serialize(self, edition, include_document=False):
        result = {
            "id": edition.id, "printerId": edition.printer_id, "status": edition.status,
            "printAt": iso(edition.print_at), "expiresAt": iso(edition.expires_at),
            "createdAt": iso(edition.created_at), "updatedAt": iso(edition.updated_at),
            "error": edition.error,
        }
        if include_document:
            result["document"] = edition.document
        return result

    def create(self, payload):
        request = CreateEdition.model_validate(payload)
        normalized = request.model_dump(mode="json", by_alias=True, exclude_none=True)
        digest = hashlib.sha256(json.dumps(normalized, sort_keys=True).encode()).hexdigest()
        with self.sessions.begin() as session:
            self.printer_lock(session)
            existing = session.scalar(select(Edition).where(Edition.idempotency_key == request.idempotencyKey))
            if existing:
                if existing.request_hash != digest:
                    raise ServiceError("Idempotency key was already used for a different edition", 409)
                return self.serialize(existing, True)
            if request.expiresAt and request.expiresAt <= now():
                raise ServiceError("Edition has already expired")
            for asset_id in set(asset_ids(normalized["document"])):
                if session.get(Asset, asset_id) is None:
                    raise ServiceError(f"Unknown image asset: {asset_id}")
            edition = Edition(
                id=uuid4().hex, printer_id=self.printer_id,
                idempotency_key=request.idempotencyKey, request_hash=digest,
                document=normalized["document"], print_at=request.printAt.astimezone(UTC),
                expires_at=request.expiresAt.astimezone(UTC) if request.expiresAt else None,
                status="scheduled",
            )
            session.add(edition)
            session.flush()
            return self.serialize(edition, True)

    def list_editions(self, limit=20):
        if not 1 <= limit <= 100:
            raise ServiceError("limit must be between 1 and 100")
        with self.sessions.begin() as session:
            self.printer_lock(session)
            self.reconcile(session)
            editions = session.scalars(select(Edition).where(Edition.printer_id == self.printer_id).order_by(Edition.created_at.desc()).limit(limit))
            return [self.serialize(edition) for edition in editions]

    def edition(self, session, edition_id):
        edition = session.get(Edition, edition_id)
        if edition is None or edition.printer_id != self.printer_id:
            raise ServiceError("Edition not found", 404)
        return edition

    def get(self, edition_id):
        with self.sessions.begin() as session:
            self.printer_lock(session)
            self.reconcile(session)
            return self.serialize(self.edition(session, edition_id), True)

    def cancel(self, edition_id):
        with self.sessions.begin() as session:
            self.printer_lock(session)
            self.reconcile(session)
            edition = self.edition(session, edition_id)
            if edition.status == "cancelled":
                return self.serialize(edition)
            if edition.status != "scheduled":
                raise ServiceError("Only unclaimed scheduled editions can be cancelled", 409)
            edition.status = "cancelled"
            edition.updated_at = now()
            return self.serialize(edition)

    def claim(self):
        with self.sessions.begin() as session:
            printer = self.printer_lock(session)
            printer.last_seen = now()
            self.reconcile(session)
            active = session.scalar(select(Edition.id).where(
                Edition.printer_id == self.printer_id, Edition.status.in_(["claimed", "printing"])
            ).limit(1))
            if active:
                return None
            edition = session.scalar(select(Edition).where(
                Edition.printer_id == self.printer_id, Edition.status == "scheduled", Edition.print_at <= now(),
            ).order_by(Edition.print_at, Edition.created_at).limit(1))
            if edition is None:
                return None
            edition.status = "claimed"
            edition.claim_token = secrets.token_urlsafe(32)
            edition.lease_until = now() + timedelta(minutes=5)
            edition.updated_at = now()
            assets = []
            for asset_id in sorted(set(asset_ids(edition.document))):
                asset = session.get(Asset, asset_id)
                assets.append({"id": asset.id, "url": self.storage.download_url(asset), "filename": f"{asset.id}.png"})
            return self.serialize(edition, True) | {
                "claimToken": edition.claim_token, "leaseUntil": iso(edition.lease_until), "assets": assets,
            }

    def transition(self, edition_id, action, token, error=None):
        with self.sessions.begin() as session:
            printer = self.printer_lock(session)
            printer.last_seen = now()
            self.reconcile(session)
            edition = self.edition(session, edition_id)
            if not edition.claim_token or not secrets.compare_digest(edition.claim_token, token):
                raise ServiceError("Invalid or expired claim token", 409)
            target = {"start": "printing", "complete": "completed", "fail": "failed"}.get(action)
            if target and edition.status == target:
                return self.serialize(edition)
            allowed = {
                "start": {"claimed"}, "complete": {"printing", "uncertain"},
                "fail": {"claimed"}, "uncertain": {"printing", "uncertain"},
                "renew": {"claimed", "printing"},
            }
            if action not in allowed or edition.status not in allowed[action]:
                raise ServiceError("Invalid job transition", 409)
            if action == "renew":
                edition.lease_until = now() + timedelta(minutes=5)
            else:
                edition.status = target or "uncertain"
                edition.error = error
                edition.lease_until = now() + timedelta(minutes=5) if action == "start" else None
            edition.updated_at = now()
            return self.serialize(edition) | {"leaseUntil": iso(edition.lease_until)}

    def heartbeat(self, software_version=None, error=None):
        with self.sessions.begin() as session:
            printer = self.printer_lock(session)
            printer.last_seen = now()
            printer.software_version = software_version
            printer.error = error
        return {"ok": True, "serverTime": iso(now())}

    def printer_status(self):
        with self.sessions.begin() as session:
            printer = self.printer_lock(session)
            self.reconcile(session)
            return {
                "id": printer.id, "lastSeen": iso(printer.last_seen),
                "softwareVersion": printer.software_version, "error": printer.error,
                "online": bool(printer.last_seen and now() - utc(printer.last_seen) < timedelta(minutes=2)),
            }

    def upload_image(self, data):
        asset = self.storage.upload(data)
        with self.sessions.begin() as session:
            session.add(asset)
        return {"assetId": asset.id, "width": asset.width, "height": asset.height, "bytes": asset.size}

    def schema(self):
        return Document.model_json_schema()
