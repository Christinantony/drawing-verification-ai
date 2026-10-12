"""Local evidence, audit log, and versioned cache. Originals remain read-only."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, allow_nan=False)


def digest(value):
    return hashlib.sha256(dumps(value).encode()).hexdigest()


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS runs (
              id TEXT PRIMARY KEY, started TEXT NOT NULL, manifest TEXT NOT NULL, result TEXT);
            CREATE TABLE IF NOT EXISTS events (
              seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
              kind TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS evidence (
              run_id TEXT NOT NULL, id TEXT NOT NULL, payload TEXT NOT NULL,
              PRIMARY KEY (run_id, id));
            CREATE TABLE IF NOT EXISTS relations (
              run_id TEXT NOT NULL, source TEXT NOT NULL, target TEXT NOT NULL, kind TEXT NOT NULL,
              PRIMARY KEY (run_id, source, target, kind));
        """)
        self.db.commit()

    def start(self, manifest):
        run = uuid.uuid4().hex
        with self.db:
            self.db.execute("INSERT INTO runs VALUES (?,?,?,NULL)",
                            (run, datetime.now(timezone.utc).isoformat(), dumps(manifest)))
        return run

    def event(self, run, kind, payload):
        with self.db:
            self.db.execute("INSERT INTO events(run_id,kind,payload) VALUES (?,?,?)",
                            (run, kind, dumps(payload)))

    def evidence(self, run, item):
        with self.db:
            self.db.execute("INSERT INTO evidence VALUES (?,?,?)", (run, item["id"], dumps(item)))

    def relate(self, run, source, target, kind):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO relations VALUES (?,?,?,?)", (run, source, target, kind))

    def cached(self, key):
        row = self.db.execute("SELECT value FROM cache WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def cache(self, key, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO cache VALUES (?,?)", (key, dumps(value)))

    def finish(self, run, result):
        with self.db:
            self.db.execute("UPDATE runs SET result=? WHERE id=?", (dumps(result), run))

    def close(self):
        self.db.close()
