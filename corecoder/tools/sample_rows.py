"""Sample rows from a loaded table for schema discovery or manual review."""

from __future__ import annotations

from .base import Tool
from ..db.workspace import get_workspace
from .sql_query import format_table

_DEFAULT_PREVIEW = 10


class SampleRowsTool(Tool):
    name = "sample_rows"
    description = (
        "Sample rows from a loaded table for exploration or taxonomy discovery. "
        "Supports random sampling now, with placeholders for diverse and stratified "
        "sampling workflows."
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
    ) -> str:
        ws = get_workspace()
        if table not in ws.tables:
            return f"Error: table '{table}' not loaded. Call load_table first."
        if sample_size <= 0:
            return "Error: sample_size must be positive"

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
            order_clause = " ORDER BY random()"
            note = "Diverse sampling will use embeddings in a later phase. Falling back to random sampling for now."
        else:
            return "Error: method must be one of random, diverse, stratified"

        total_q = f'SELECT COUNT(*) {from_clause}{where_clause}'
        total = ws.conn.execute(total_q).fetchone()[0]
        if total == 0:
            return "Error: filter returned 0 rows - nothing to sample"

        sample_q = (
            f"SELECT {col_list} {from_clause}{where_clause}"
            f"{order_clause} LIMIT ?"
        )
        rows = ws.conn.execute(sample_q, [int(sample_size)]).fetchall()
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
        return "\n".join(lines)
