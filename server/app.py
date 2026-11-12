import base64
import binascii
import os
import secrets
from contextlib import asynccontextmanager

from a2wsgi import WSGIMiddleware
from flask import Flask, jsonify, request
from mcp.server.mcpserver import MCPServer
from pydantic import ValidationError
from sqlalchemy import text
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount
from werkzeug.exceptions import HTTPException

from server.database import Base, connect_database
from server.schema import ClaimUpdate, CreateEdition, Heartbeat
from server.service import NewspaperService, ServiceError
from server.storage import ImageStorage


class BearerAuth:
    def __init__(self, app, key):
        self.app = app
        self.key = key

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope["headers"])
            authorization = headers.get(b"authorization", b"")
            if not secrets.compare_digest(authorization, f"Bearer {self.key}".encode()):
                response = JSONResponse({"error": "Unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def create_app():
    creator_key = os.environ["CREATOR_API_KEY"]
    printer_key = os.environ["PRINTER_API_KEY"]
    if min(len(creator_key), len(printer_key)) < 32 or creator_key == printer_key:
        raise ValueError("Use distinct creator and printer keys of at least 32 characters")
    printer_id = os.getenv("PRINTER_ID", "diesel")
    engine, sessions = connect_database()
    service = NewspaperService(sessions, ImageStorage(), printer_id)
    api = Flask(__name__)
    api.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024

    @api.before_request
    def authorize():
        if request.path == "/health":
            return None
        key = printer_key if request.path.startswith("/printer/") else creator_key
        authorization = request.headers.get("Authorization", "")
        if not secrets.compare_digest(authorization.encode(), f"Bearer {key}".encode()):
            return jsonify(error="Unauthorized"), 401
        if request.content_length and request.path != "/assets" and request.content_length > 512 * 1024:
            return jsonify(error="JSON request exceeds 512 KiB"), 413

    @api.errorhandler(ServiceError)
    def service_error(error):
        return jsonify(error=str(error)), error.status

    @api.errorhandler(ValidationError)
    def validation_error(error):
        return jsonify(error="Invalid request", details=error.errors(include_input=False, include_context=False)), 422

    @api.errorhandler(ValueError)
    def value_error(error):
        return jsonify(error=str(error)), 400

    @api.errorhandler(HTTPException)
    def http_error(error):
        return jsonify(error=error.description), error.code

    @api.get("/health")
    def health():
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return jsonify(ok=True)

    @api.get("/schema")
    def schema():
        return jsonify(service.schema())

    @api.post("/editions")
    def create_edition_route():
        return jsonify(service.create(request.get_json())), 201

    @api.get("/editions")
    def list_editions_route():
        return jsonify(editions=service.list_editions(int(request.args.get("limit", "20"))))

    @api.get("/editions/<edition_id>")
    def get_edition_route(edition_id):
        return jsonify(service.get(edition_id))

    @api.post("/editions/<edition_id>/cancel")
    def cancel_edition_route(edition_id):
        return jsonify(service.cancel(edition_id))

    @api.post("/assets")
    def upload_image_route():
        if "image" not in request.files:
            raise ServiceError("Upload a multipart file named image")
        return jsonify(service.upload_image(request.files["image"].read(8 * 1024 * 1024 + 1))), 201

    @api.get("/printers/status")
    def printer_status_route():
        return jsonify(service.printer_status())

    @api.post("/printer/claim")
    def claim_route():
        return jsonify(job=service.claim())

    @api.post("/printer/heartbeat")
    def heartbeat_route():
        payload = Heartbeat.model_validate(request.get_json())
        return jsonify(service.heartbeat(payload.softwareVersion, payload.error))

    @api.post("/printer/editions/<edition_id>/<action>")
    def update_route(edition_id, action):
        payload = ClaimUpdate.model_validate(request.get_json())
        return jsonify(service.transition(edition_id, action, payload.claimToken, payload.error))

    mcp = MCPServer("Morning Newspaper", instructions=(
        "Create individual newspaper print jobs, not recurring schedules. First read get_document_schema. "
        "Use ISO 8601 times with explicit UTC offsets. Reuse the same idempotencyKey when retrying a creation. "
        "Upload images first and reference assetId, never local paths. A completed job means the Pi reported "
        "sending it to the printer, not independently confirmed physical output. Never automatically retry uncertain jobs."
    ))

    @mcp.tool()
    def get_document_schema() -> dict:
        """Get the supported versioned component JSON schema, including chessboards and nested lists."""
        return service.schema()

    @mcp.tool()
    def create_edition(edition: CreateEdition) -> dict:
        """Schedule an immutable edition. printAt needs a timezone offset; expiresAt sets the late-print cutoff."""
        return service.create(edition.model_dump(mode="json", by_alias=True, exclude_none=True))

    @mcp.tool()
    def list_editions(limit: int = 20) -> list[dict]:
        """List recent editions and their print states. Limit is 1 to 100."""
        return service.list_editions(limit)

    @mcp.tool()
    def get_edition(edition_id: str) -> dict:
        """Read an edition, its content, and current print state."""
        return service.get(edition_id)

    @mcp.tool()
    def cancel_edition(edition_id: str) -> dict:
        """Cancel an edition only if the printer has not claimed it yet."""
        return service.cancel(edition_id)

    @mcp.tool()
    def get_printer_status() -> dict:
        """Get the configured printer's last contact, software version, and error."""
        return service.printer_status()

    @mcp.tool()
    def upload_image(image_base64: str) -> dict:
        """Upload a base64-encoded image (no data URL prefix). Returns assetId for image components."""
        if len(image_base64) > 12 * 1024 * 1024:
            raise ValueError("Image exceeds upload limit")
        try:
            data = base64.b64decode(image_base64, validate=True)
        except binascii.Error as error:
            raise ValueError("Invalid base64 image") from error
        return service.upload_image(data)

    mcp_app = mcp.streamable_http_app(
        streamable_http_path="/", json_response=True, stateless_http=True,
        host="0.0.0.0", max_request_body_size=12 * 1024 * 1024,
    )

    @asynccontextmanager
    async def lifespan(app):
        Base.metadata.create_all(engine)
        service.initialize_printer()
        async with mcp.session_manager.run():
            yield
        engine.dispose()

    return Starlette(
        routes=[Mount("/mcp", app=BearerAuth(mcp_app, creator_key)), Mount("/", app=WSGIMiddleware(api))],
        lifespan=lifespan,
    )
