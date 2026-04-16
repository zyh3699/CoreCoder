"""Embed a text column and materialize the vectors as JSON strings."""

from __future__ import annotations

import json

from .base import Tool
from ..db.workspace import get_workspace
from ..db.embeddings import DEFAULT_EMBED_MODEL, encode_texts


class EmbedColumnTool(Tool):
    name = "embed_column"
    description = (
        "Generate local embeddings for a text column and write them back as a JSON "
        "string column. Useful for reuse across diverse sampling, nearest-neighbor "
        "assignment, and drill-down workflows. Prefer this for large tables or "
        "multi-step analyses where the same text slice will be reused."
    )
    parameters = {
        "type": "object",
        "properties": {
            "table": {"type": "string", "description": "Source table name"},
            "source_column": {"type": "string", "description": "Text column to embed"},
            "new_column": {"type": "string", "description": "Name of the embedding output column"},
            "where": {
                "type": "string",
                "description": "Optional SQL WHERE clause to restrict rows before embedding",
            },
            "model_name": {
                "type": "string",
                "description": f"Sentence-transformer model to use. Default: {DEFAULT_EMBED_MODEL}",
            },
            "rerun_mode": {
                "type": "string",
                "enum": ["reuse", "refresh", "no_write_cache"],
                "description": "Whether to reuse cache, refresh vectors, or skip writing new cache entries.",
            },
        },
        "required": ["table", "source_column", "new_column"],
    }

    def execute(
        self,
        table: str,
        source_column: str,
        new_column: str,
        where: str | None = None,
        model_name: str = DEFAULT_EMBED_MODEL,
        rerun_mode: str = "reuse",
    ) -> str:
        ws = get_workspace()
        if table not in ws.tables:
            return f"Error: table '{table}' not loaded. Call load_table first."
        if source_column not in ws.tables[table]["columns"]:
            return f"Error: column '{source_column}' not in table '{table}'"
        if rerun_mode not in {"reuse", "refresh", "no_write_cache"}:
            return "Error: rerun_mode must be one of reuse, refresh, no_write_cache"

        where_clause = f" WHERE {where}" if where else ""
        rows = ws.conn.execute(
            f'SELECT _rid, "{source_column}" FROM "{table}"{where_clause} ORDER BY _rid'
        ).fetchall()
        if not rows:
            return "Error: filter returned 0 rows - nothing to embed"

        schema_str = json.dumps({"model": model_name}, sort_keys=True)
        schema_hash = ws.cache.hash_text(schema_str)
        hits: dict[int, str] = {}
        work: list[tuple[int, str, str]] = []
        for rid, text in rows:
            text_str = "" if text is None else str(text)
            key = ws.cache.make_key(table, text_str, model_name, schema_str, model_name)
            cached = None if rerun_mode != "reuse" else ws.cache.get(key)
            if cached is not None:
                hits[rid] = json.dumps(cached, ensure_ascii=False)
            else:
                work.append((rid, text_str, key))

        new_vals: dict[int, str] = {}
        if work:
            texts = [text for _, text, _ in work]
            try:
                vectors = encode_texts(texts, model_name=model_name)
            except Exception as e:
                return f"Embedding error: {e}"
            for (rid, _, key), vec in zip(work, vectors):
                vec_json = json.dumps(vec, ensure_ascii=False)
                new_vals[rid] = vec_json
                if rerun_mode != "no_write_cache":
                    ws.cache.put(
                        key,
                        table,
                        new_column,
                        str(rid),
                        vec,
                        model_name,
                        tool_name=self.name,
                        goal=new_column,
                        schema_hash=schema_hash,
                        prompt_preview=model_name,
                        source_columns=source_column,
                    )

        results = {**hits, **new_vals}
        try:
            ws.conn.execute(
                "CREATE OR REPLACE TEMP TABLE __embed_staging (_rid BIGINT, val VARCHAR)"
            )
            ws.conn.executemany(
                "INSERT INTO __embed_staging VALUES (?, ?)",
                [(rid, results.get(rid)) for rid, _ in rows],
            )
            ws.conn.execute(f'ALTER TABLE "{table}" DROP COLUMN IF EXISTS "{new_column}"')
            ws.conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{new_column}" VARCHAR')
            ws.conn.execute(
                f'UPDATE "{table}" SET "{new_column}" = s.val '
                f'FROM __embed_staging s WHERE "{table}"._rid = s._rid'
            )
            ws.conn.execute("DROP TABLE IF EXISTS __embed_staging")
        except Exception as e:
            return f"Error writing embedding column: {e}"

        ws.refresh_table(table)
        return (
            f'Wrote embedding column "{new_column}" into "{table}": {len(rows)} rows\n'
            f"  cache hits: {len(hits)}   embedded: {len(new_vals)}\n"
            f"Model: {model_name}"
        )
