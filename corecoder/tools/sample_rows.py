"""Sample rows from a loaded table for schema discovery or manual review."""

from __future__ import annotations

from .base import Tool
from ..db.workspace import get_workspace
from ..db.embeddings import DEFAULT_EMBED_MODEL, diverse_sample_indices, encode_texts
from .sql_query import format_table

_DEFAULT_PREVIEW = 10


class SampleRowsTool(Tool):
    name = "sample_rows"
    description = (
        "Sample rows from a loaded table for exploration or taxonomy discovery. "
        "Use random sampling for quick looks and diverse sampling for open-ended "
        "semantic discovery over larger sets. Diverse mode uses embeddings to cover "
        "more of the semantic space."
    )
    parameters = {
        "type": "object",
        "properties": {
            "table": {"type": "string", "description": "Source table name"},
            "sample_size": {
                "type": "integer",
                "description": "How many rows to sample",
            },
            "where": {
                "type": "string",
                "description": (
                    "Optional SQL WHERE clause to filter rows before sampling. "
                    "Do NOT include the word WHERE."
                ),
            },
            "method": {
                "type": "string",
                "enum": ["random", "diverse", "stratified"],
                "description": "Sampling strategy. Phase one fully supports random only.",
            },
            "columns": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional subset of columns to include in the preview",
            },
            "stratify_by": {
                "type": "string",
                "description": "Column used when method='stratified'",
            },
            "text_column": {
                "type": "string",
                "description": "Text column used for diverse sampling embeddings",
            },
            "embedding_model": {
                "type": "string",
                "description": f"Sentence-transformer model for diverse sampling. Default: {DEFAULT_EMBED_MODEL}",
            },
            "new_table": {
                "type": "string",
                "description": (
                    "Optional output table name. If provided, the sampled rows are "
                    "materialized as a new table so later discovery runs explicitly "
                    "on the sample table."
                ),
            },
        },
        "required": ["table", "sample_size"],
    }

    def execute(
        self,
        table: str,
        sample_size: int,
        where: str | None = None,
        method: str = "random",
        columns: list[str] | None = None,
        stratify_by: str | None = None,
        text_column: str | None = None,
        embedding_model: str = DEFAULT_EMBED_MODEL,
        new_table: str | None = None,
    ) -> str:
        ws = get_workspace()
        if table not in ws.tables:
            return f"Error: table '{table}' not loaded. Call load_table first."
        if sample_size <= 0:
            return "Error: sample_size must be positive"
        if new_table and not _is_ident(new_table):
            return "Error: new_table must be a valid SQL identifier"

        existing = ws.tables[table]["columns"]
        if columns:
            missing = [c for c in columns if c not in existing]
            if missing:
                return f"Error: columns not found in {table}: {missing}"
        else:
            # show _rid plus a manageable subset of user-facing columns
            columns = [c for c in existing if c != "_rid"][:5]

        col_list = ", ".join(f'"{c}"' for c in (["_rid"] + columns))
        from_clause = f'FROM "{table}"'
        where_clause = f" WHERE {where}" if where else ""

        note = ""
        if method == "random":
            order_clause = " ORDER BY random()"
        elif method == "stratified":
            if not stratify_by:
                return "Error: stratify_by is required when method='stratified'"
            if stratify_by not in existing:
                return f"Error: column '{stratify_by}' not in table '{table}'"
            order_clause = f' ORDER BY "{stratify_by}", random()'
            note = "Stratified mode currently preserves strata ordering but does not enforce equal quotas."
        elif method == "diverse":
            order_clause = None
            if text_column is None:
                return "Error: text_column is required when method='diverse'"
            if text_column not in existing:
                return f"Error: column '{text_column}' not in table '{table}'"
            note = f"Diverse sampling via embeddings on column '{text_column}'"
        else:
            return "Error: method must be one of random, diverse, stratified"

        total_q = f'SELECT COUNT(*) {from_clause}{where_clause}'
        total = ws.conn.execute(total_q).fetchone()[0]
        if total == 0:
            return "Error: filter returned 0 rows - nothing to sample"

        if method == "diverse":
            sample_q = f'SELECT _rid, "{text_column}", {col_list} {from_clause}{where_clause}'
            raw_rows = ws.conn.execute(sample_q).fetchall()
            if not raw_rows:
                return "Error: filter returned 0 rows - nothing to sample"
            texts = ["" if r[1] is None else str(r[1]) for r in raw_rows]
            try:
                vecs = encode_texts(texts, model_name=embedding_model)
            except Exception as e:
                return f"Embedding error: {e}"
            picked = diverse_sample_indices(vecs, int(sample_size))
            rows = [raw_rows[i][2:] for i in picked]
            sampled_rids = [raw_rows[i][0] for i in picked]
        else:
            sample_q = (
                f"SELECT {col_list} {from_clause}{where_clause}"
                f"{order_clause} LIMIT ?"
            )
            rows = ws.conn.execute(sample_q, [int(sample_size)]).fetchall()
            sampled_rids = [r[0] for r in rows]
        shown = rows[:_DEFAULT_PREVIEW]
        preview = format_table(["_rid"] + columns, shown, truncated=len(rows) > len(shown))

        lines = [
            f"Sampled {len(rows)} rows from '{table}' (population={total}, method={method})",
        ]
        if where:
            lines.append(f"Filter: {where}")
        if note:
            lines.append(note)
        lines += [
            f"Columns: {['_rid'] + columns}",
            "Preview:",
            preview,
        ]
        if new_table and sampled_rids:
            staged_rows = ws.conn.execute(
                f'SELECT * FROM "{table}" WHERE _rid = ANY(?) ORDER BY _rid',
                [sampled_rids],
            ).fetchall()
            all_cols = [r[0] for r in ws.conn.execute(f'DESCRIBE "{table}"').fetchall()]
            try:
                ws.conn.execute(
                    f'CREATE OR REPLACE TABLE "{new_table}" AS '
                    f'SELECT * FROM "{table}" WHERE 1=0'
                )
                ws.conn.executemany(
                    f'INSERT INTO "{new_table}" VALUES ({", ".join(["?"] * len(all_cols))})',
                    staged_rows,
                )
            except Exception as e:
                return f"Error materializing sampled rows into '{new_table}': {e}"
            ws.register_table(
                new_table,
                f"{table}[sample:{method}]",
                len(staged_rows),
                all_cols,
            )
            lines += [
                "",
                f"Materialized sample table: {new_table}",
            ]
        return "\n".join(lines)


def _is_ident(name: str) -> bool:
    return bool(name) and name.replace("_", "").isalnum() and not name[0].isdigit()
