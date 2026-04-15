"""Persistent cache for LLM-derived column values.

Cache key = sha256(table, row_content_json, prompt, schema, model). When the
same prompt is run again over the same rows with the same model, every call
is a hit and the column rematerializes for free.  Cache lives in SQLite so
it survives across sessions and can be inspected with any SQLite browser.
"""

import hashlib
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any


class DerivedCache:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS derived (
                cache_key  TEXT PRIMARY KEY,
                table_name TEXT,
                column_name TEXT,
                row_key    TEXT,
                value      TEXT,
                model      TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tc ON derived(table_name, column_name)"
        )
        self.conn.commit()

    @staticmethod
    def make_key(table: str, row_content: str, prompt: str, schema: str, model: str) -> str:
        h = hashlib.sha256()
        for part in (table, row_content, prompt, schema, model):
            h.update(part.encode("utf-8"))
            h.update(b"\x00")
        return h.hexdigest()

    def get(self, key: str) -> Any | None:
        row = self.conn.execute(
            "SELECT value FROM derived WHERE cache_key=?", (key,)
        ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row[0])
        except json.JSONDecodeError:
            return None

    def put(self, key: str, table: str, column: str, row_key: str, value: Any, model: str):
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO derived"
                "(cache_key,table_name,column_name,row_key,value,model)"
                " VALUES (?,?,?,?,?,?)",
                (key, table, column, row_key, json.dumps(value, ensure_ascii=False), model),
            )
            self.conn.commit()

    def stats(self, table: str, column: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM derived WHERE table_name=? AND column_name=?",
            (table, column),
        ).fetchone()
        return row[0] if row else 0
