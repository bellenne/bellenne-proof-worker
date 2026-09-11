from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path


class LocalState:
    """Crash recovery journal only; Core owns scheduling and business states."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS executions (
                job_id TEXT NOT NULL, attempt INTEGER NOT NULL, payload TEXT NOT NULL,
                stage TEXT NOT NULL, artifact TEXT, error TEXT, retries INTEGER NOT NULL DEFAULT 0,
                updated REAL NOT NULL, upload_intent INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(job_id, attempt)
            );
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        columns = {row[1] for row in self.connection.execute("PRAGMA table_info(executions)")}
        if "upload_intent" not in columns:
            with self.connection:
                self.connection.execute("ALTER TABLE executions ADD COLUMN upload_intent INTEGER NOT NULL DEFAULT 0")

    def set_flag(self, key: str, value) -> None:
        with self.connection:
            self.connection.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, json.dumps(value)))

    def get_flag(self, key: str, default=None):
        row = self.connection.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def claim(self, payload: dict) -> dict:
        job_id, attempt = payload["id"], payload["attempt"]
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO executions(job_id,attempt,payload,stage,updated) VALUES(?,?,?,?,?)",
                (job_id, attempt, json.dumps(payload), "CLAIMED", time.time()),
            )
            self.connection.execute(
                "UPDATE executions SET payload=?, updated=? WHERE job_id=? AND attempt=?",
                (json.dumps(payload), time.time(), job_id, attempt),
            )
        return self.get(job_id, attempt)

    def get(self, job_id: str, attempt: int) -> dict | None:
        row = self.connection.execute("SELECT * FROM executions WHERE job_id=? AND attempt=?", (job_id, attempt)).fetchone()
        if row is None:
            return None
        value = dict(row)
        for field in ("payload", "artifact", "error"):
            value[field] = json.loads(value[field]) if value[field] else None
        return value

    def update(self, job_id: str, attempt: int, **fields):
        if not fields or not fields.keys() <= {"stage", "artifact", "error", "retries", "upload_intent"}:
            raise ValueError("Unknown journal field")
        encoded = {k: json.dumps(v) if k in {"artifact", "error"} and v is not None else v for k, v in fields.items()}
        encoded["updated"] = time.time()
        sql = ",".join(f"{k}=?" for k in encoded)
        with self.connection:
            self.connection.execute(f"UPDATE executions SET {sql} WHERE job_id=? AND attempt=?", (*encoded.values(), job_id, attempt))

    def unfinished(self) -> list[dict]:
        rows = self.connection.execute("SELECT job_id,attempt FROM executions WHERE stage NOT IN ('DONE','FAILED','DETACHED') ORDER BY updated").fetchall()
        return [self.get(row[0], row[1]) for row in rows]

    def detach_except(self, job_id: str | None, attempt: int | None):
        for record in self.unfinished():
            if (record["job_id"], record["attempt"]) != (job_id, attempt):
                self.update(record["job_id"], record["attempt"], stage="DETACHED")

    def close(self):
        self.connection.close()
