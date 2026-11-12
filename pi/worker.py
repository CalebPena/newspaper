import argparse
import copy
import fcntl
import json
import logging
import os
import re
import select
import signal
import sqlite3
import stat
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from components import NewspaperRenderer, save_receipt

VERSION = "0.1.0"
LOGGER = logging.getLogger("newspaper-worker")


class WorkerError(Exception):
    pass


class APIError(WorkerError):
    def __init__(self, status):
        super().__init__(f"Server returned HTTP {status}")
        self.status = status


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


class API:
    def __init__(self, url, key):
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("NEWSPAPER_SERVER_URL must be an HTTPS URL without credentials")
        if len(key) < 32:
            raise ValueError("PRINTER_API_KEY must contain at least 32 characters")
        self.url = url.rstrip("/")
        self.key = key
        self.opener = urllib.request.build_opener(NoRedirects)

    def post(self, path, payload):
        request = urllib.request.Request(
            self.url + path,
            data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"},
        )
        try:
            with self.opener.open(request, timeout=30) as response:
                data = response.read(2 * 1024 * 1024 + 1)
        except urllib.error.HTTPError as error:
            raise APIError(error.code) from None
        except (OSError, urllib.error.URLError) as error:
            raise WorkerError("Unable to reach newspaper server") from error
        if len(data) > 2 * 1024 * 1024:
            raise WorkerError("Server response exceeds 2 MiB")
        return json.loads(data)

    def update(self, job_id, token, action, error=None):
        return self.post(
            f"/printer/editions/{job_id}/{action}",
            {"claimToken": token, "error": error},
        )

    def heartbeat(self, error=None):
        return self.post("/printer/heartbeat", {"softwareVersion": VERSION, "error": error})

    def download(self, url, destination):
        if urllib.parse.urlparse(url).scheme != "https":
            raise WorkerError("Image download must use HTTPS")
        try:
            with self.opener.open(url, timeout=30) as response:
                data = response.read(4 * 1024 * 1024 + 1)
        except (OSError, urllib.error.URLError) as error:
            raise WorkerError("Unable to download an edition image") from error
        if len(data) > 4 * 1024 * 1024:
            raise WorkerError("Image download exceeds 4 MiB")
        destination.write_bytes(data)


class Journal:
    def __init__(self, path):
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                token TEXT NOT NULL,
                state TEXT NOT NULL,
                error TEXT,
                updated_at REAL NOT NULL
            )
        """)
        self.connection.commit()

    def get(self, job_id):
        return self.connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()

    def record(self, job_id, token, state, error=None):
        with self.connection:
            self.connection.execute(
                """INSERT INTO jobs VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET token=excluded.token,
                   state=excluded.state, error=excluded.error, updated_at=excluded.updated_at""",
                (job_id, token, state, error, time.time()),
            )

    def recover(self):
        for job in self.connection.execute("SELECT * FROM jobs WHERE state IN ('preparing', 'starting', 'printing')").fetchall():
            state = "fail_pending" if job["state"] == "preparing" else "uncertain_pending"
            self.record(job["id"], job["token"], state, "Worker stopped during this job; not automatically reprinting")

    def pending(self):
        return self.connection.execute("SELECT * FROM jobs WHERE state LIKE '%_pending' ORDER BY updated_at").fetchall()


def resolve_images(node, filenames):
    if isinstance(node, dict):
        if "assetId" in node:
            node["src"] = filenames[node.pop("assetId")]
        for value in node.values():
            resolve_images(value, filenames)
    elif isinstance(node, list):
        for value in node:
            resolve_images(value, filenames)


def printer_ready(device):
    try:
        mode = device.stat().st_mode
        if not stat.S_ISCHR(mode) or not os.access(device, os.W_OK):
            return "Printer device is not a writable character device"
    except OSError:
        return "USB printer is disconnected or switched off"
    return None


def send_receipt(device, receipt):
    descriptor = os.open(device, os.O_WRONLY | os.O_NONBLOCK | os.O_NOCTTY)
    try:
        if not stat.S_ISCHR(os.fstat(descriptor).st_mode):
            raise WorkerError("Printer device is not a character device")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        poller = select.poll()
        poller.register(descriptor, select.POLLOUT | select.POLLERR | select.POLLHUP)
        deadline = time.monotonic() + 120
        with receipt.open("rb") as stream:
            while chunk := stream.read(4096):
                remaining = memoryview(chunk)
                while remaining:
                    if time.monotonic() >= deadline:
                        raise WorkerError("Printer write exceeded two minutes")
                    events = poller.poll(1000)
                    if not events:
                        continue
                    if events[0][1] & (select.POLLERR | select.POLLHUP | select.POLLNVAL):
                        raise WorkerError("Printer disconnected or reported a write error")
                    try:
                        count = os.write(descriptor, remaining)
                    except BlockingIOError:
                        continue
                    if count == 0:
                        raise WorkerError("Printer accepted no data")
                    remaining = remaining[count:]
    finally:
        os.close(descriptor)


class Worker:
    def __init__(self, api, state_directory, device):
        self.api = api
        self.directory = state_directory
        self.device = device
        self.journal = Journal(state_directory / "jobs.sqlite3")
        self.journal.recover()

    def acknowledge(self):
        for job in self.journal.pending():
            action = {"complete_pending": "complete", "fail_pending": "fail", "uncertain_pending": "uncertain"}[job["state"]]
            try:
                self.api.update(job["id"], job["token"], action, job["error"])
            except APIError as error:
                if action == "uncertain" and error.status == 409:
                    try:
                        self.api.update(job["id"], job["token"], "fail", job["error"])
                    except APIError as fallback:
                        if fallback.status not in (404, 409):
                            raise
                elif error.status not in (404, 409):
                    raise
                LOGGER.warning("Job %s acknowledgment conflicted; it will not be reprinted", job["id"])
                self.journal.record(job["id"], job["token"], "closed", job["error"])
                continue
            self.journal.record(job["id"], job["token"], {"complete": "completed", "fail": "failed", "uncertain": "uncertain"}[action], job["error"])

    def prepare(self, job, directory):
        if job["document"].get("schemaVersion", 1) != 1:
            raise WorkerError("Unsupported newspaper schema version")
        filenames = {}
        for asset in job["assets"]:
            asset_id = asset["id"]
            if not re.fullmatch(r"[a-f0-9]{32}", asset_id):
                raise WorkerError("Invalid image asset ID")
            self.api.update(job["id"], job["claimToken"], "renew")
            filename = f"{asset_id}.png"
            self.api.download(asset["url"], directory / filename)
            filenames[asset_id] = filename
        document = copy.deepcopy(job["document"])
        resolve_images(document, filenames)
        renderer = NewspaperRenderer(document.get("theme"), directory)
        image = renderer.render(document)
        if image.width != 576 or image.height > 16000:
            raise WorkerError("Edition exceeds the 576-dot width or two-meter paper limit")
        receipt = directory / "edition.escpos"
        save_receipt(image, receipt)
        self.api.update(job["id"], job["claimToken"], "renew")
        return receipt

    def process(self, job):
        job_id, token = job["id"], job["claimToken"]
        if not re.fullmatch(r"[a-f0-9]{32}", job_id):
            raise WorkerError("Invalid edition ID")
        if self.journal.get(job_id) is not None:
            self.journal.record(job_id, token, "fail_pending", "Edition already exists in local history; refusing a duplicate print")
            return
        self.journal.record(job_id, token, "preparing")
        try:
            with tempfile.TemporaryDirectory(prefix="edition-", dir=self.directory) as temporary:
                receipt = self.prepare(job, Path(temporary))
                if error := printer_ready(self.device):
                    raise WorkerError(error)
                self.journal.record(job_id, token, "starting")
                self.api.update(job_id, token, "start")
                self.journal.record(job_id, token, "printing")
                send_receipt(self.device, receipt)
                self.journal.record(job_id, token, "complete_pending")
                LOGGER.info("Job %s sent to printer", job_id)
        except Exception as error:  # noqa: BLE001 - Persist the outcome before any failure can trigger a restart.
            state = self.journal.get(job_id)["state"]
            pending = "fail_pending" if state == "preparing" else "uncertain_pending"
            message = str(error) if isinstance(error, WorkerError) else f"{type(error).__name__} while preparing or printing edition"
            self.journal.record(job_id, token, pending, message[:2000])
            LOGGER.error("Job %s: %s", job_id, message)

    def tick(self):
        self.acknowledge()
        error = printer_ready(self.device)
        self.api.heartbeat(error)
        if error:
            LOGGER.warning(error)
            return
        job = self.api.post("/printer/claim", {})["job"]
        if job:
            self.process(job)
            self.acknowledge()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="Check configuration and send a heartbeat without claiming or printing")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    os.umask(0o077)
    api = API(os.environ["NEWSPAPER_SERVER_URL"], os.environ["PRINTER_API_KEY"])
    device = Path(os.getenv("PRINTER_DEVICE", "/dev/usb/lp0"))
    if args.check:
        NewspaperRenderer().render({"components": [
            {"type": "chessboard", "fen": "7k/8/5KQ1/8/8/8/8/8 w - - 0 1"},
            {"type": "qr", "url": "https://example.com"},
        ]})
        error = printer_ready(device)
        api.heartbeat(error)
        LOGGER.info("Server authentication works. Printer: %s", error or "ready")
        return
    directory = Path(os.getenv("NEWSPAPER_STATE_DIR", "/var/lib/newspaper"))
    directory.mkdir(parents=True, exist_ok=True)
    interval = max(5, int(os.getenv("POLL_SECONDS", "60")))
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    with (directory / "worker.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        worker = Worker(api, directory, device)
        LOGGER.info("Newspaper worker %s started; polling every %ss", VERSION, interval)
        while not stopped.is_set():
            try:
                worker.tick()
            except Exception as error:  # noqa: BLE001 - Keep the device polling after an unexpected job failure.
                message = str(error) if isinstance(error, WorkerError) else type(error).__name__
                LOGGER.error("Worker cycle failed: %s", message)
            stopped.wait(interval)


if __name__ == "__main__":
    main()
