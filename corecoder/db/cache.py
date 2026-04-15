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
                tool_name  TEXT DEFAULT '',
                goal       TEXT DEFAULT '',
                schema_hash TEXT DEFAULT '',
                prompt_preview TEXT DEFAULT '',
                source_columns TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tc ON derived(table_name, column_name)"
        )
        self._ensure_columns()
        self.conn.commit()

    def _ensure_columns(self):
        cols = {
            row[1]
            for row in self.conn.execute("PRAGMA table_info(derived)").fetchall()
        }
        wanted = {
            "tool_name": "TEXT DEFAULT ''",
            "goal": "TEXT DEFAULT ''",
            "schema_hash": "TEXT DEFAULT ''",
            "prompt_preview": "TEXT DEFAULT ''",
            "source_columns": "TEXT DEFAULT ''",
        }
        for name, ddl in wanted.items():
            if name not in cols:
                self.conn.execute(f"ALTER TABLE derived ADD COLUMN {name} {ddl}")

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

    @staticmethod
    def hash_text(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def put(
        self,
        key: str,
        table: str,
        column: str,
        row_key: str,
        value: Any,
        model: str,
        tool_name: str = "",
        goal: str = "",
        schema_hash: str = "",
        prompt_preview: str = "",
        source_columns: str = "",
    ):
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO derived"
                "(cache_key,table_name,column_name,row_key,value,model,tool_name,goal,schema_hash,prompt_preview,source_columns)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    key,
                    table,
                    column,
                    row_key,
                    json.dumps(value, ensure_ascii=False),
                    model,
                    tool_name,
                    goal,
                    schema_hash,
                    prompt_preview,
                    source_columns,
                ),
            )
            self.conn.commit()

    def stats(self, table: str, column: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM derived WHERE table_name=? AND column_name=?",
            (table, column),
        ).fetchone()
        return row[0] if row else 0

    def list_entries(
        self,
        table: str | None = None,
        column: str | None = None,
        tool_name: str | None = None,
        goal: str | None = None,
        model: str | None = None,
    ) -> list[dict]:
        clauses = []
        params: list[str] = []
        for field, value in (
            ("table_name", table),
            ("column_name", column),
            ("tool_name", tool_name),
            ("goal", goal),
            ("model", model),
        ):
            if value:
                clauses.append(f"{field}=?")
                params.append(value)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self.conn.execute(
            "SELECT table_name, column_name, tool_name, goal, model, schema_hash, "
            "source_columns, MIN(created_at), MAX(created_at), COUNT(*) "
            f"FROM derived{where} "
            "GROUP BY table_name, column_name, tool_name, goal, model, schema_hash, source_columns "
            "ORDER BY COUNT(*) DESC, MAX(created_at) DESC",
            params,
        ).fetchall()
        out = []
        for row in rows:
            out.append(
                {
                    "table_name": row[0],
                    "column_name": row[1],
                    "tool_name": row[2],
                    "goal": row[3],
                    "model": row[4],
                    "schema_hash": row[5],
                    "source_columns": row[6],
                    "created_min": row[7],
                    "created_max": row[8],
                    "count": row[9],
                }
            )
        return out

    def delete_entries(
        self,
        table: str | None = None,
        column: str | None = None,
        tool_name: str | None = None,
        goal: str | None = None,
        model: str | None = None,
    ) -> int:
        clauses = []
        params: list[str] = []
        for field, value in (
            ("table_name", table),
            ("column_name", column),
            ("tool_name", tool_name),
            ("goal", goal),
            ("model", model),
        ):
            if value:
                clauses.append(f"{field}=?")
                params.append(value)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._lock:
            cur = self.conn.execute(f"DELETE FROM derived{where}", params)
            self.conn.commit()
        return cur.rowcount
