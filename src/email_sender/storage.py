import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


logger = logging.getLogger("email_sender")


class Store:
    def __init__(self, database: Path):
        self.database = database
        database.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS email_requests (
                    request_id TEXT PRIMARY KEY, owner TEXT NOT NULL,
                    created REAL NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                    digest TEXT, attempts INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS email_audit (
                    request_id TEXT NOT NULL, created REAL NOT NULL,
                    event TEXT NOT NULL, status TEXT NOT NULL
                );
            """)

    @contextmanager
    def connection(self):
        connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def register(self, request_id: str, owner: str, limit: int = 20) -> None:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT owner FROM email_requests WHERE request_id=?", (request_id,)).fetchone()
            if existing:
                if existing["owner"] != owner:
                    raise PermissionError("Request belongs to a different user")
                return
            count = connection.execute("SELECT COUNT(*) FROM email_requests WHERE owner=? AND created>?", (owner, time.time() - 3600)).fetchone()[0]
            if count >= limit:
                raise ValueError("Hourly request limit reached")
            connection.execute("INSERT INTO email_requests(request_id,owner,created) VALUES(?,?,?)", (request_id, owner, time.time()))

    def get(self, request_id: str, owner: str) -> dict:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM email_requests WHERE request_id=?", (request_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown request ID")
        if row["owner"] != owner:
            raise PermissionError("Request belongs to a different user")
        return dict(row)

    def claim(self, request_id: str, owner: str, digest: str) -> str:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM email_requests WHERE request_id=?", (request_id,)).fetchone()
            if not row or row["owner"] != owner:
                raise PermissionError("Request ownership could not be verified")
            if row["digest"] and row["digest"] != digest:
                return "blocked"
            if row["status"] != "pending":
                return row["status"]
            connection.execute("UPDATE email_requests SET status='sending',digest=? WHERE request_id=?", (digest, request_id))
        self.audit(request_id, "send_claimed", "sending")
        return "claimed"

    def finish(self, request_id: str, status: str, attempts: int = 0) -> None:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT status FROM email_requests WHERE request_id=?", (request_id,)).fetchone()
            transitions = {"pending": {"failed", "cancelled", "declined"}, "sending": {"accepted", "failed", "unknown"}}
            if not row or (row["status"] != status and status not in transitions.get(row["status"], set())):
                raise ValueError("Request status transition is not permitted")
            connection.execute("UPDATE email_requests SET status=?,attempts=? WHERE request_id=?", (status, attempts, request_id))
        self.audit(request_id, "request_finished", status)

    def audit(self, request_id: str, event: str, status: str) -> None:
        with self.connection() as connection:
            connection.execute("INSERT INTO email_audit VALUES(?,?,?,?)", (request_id, time.time(), event, status))
        logger.info(json.dumps({"event": event, "request_id": request_id, "status": status}))