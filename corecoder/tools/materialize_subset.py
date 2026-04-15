"""Materialize a filtered subset as a new table for drill-down analysis."""

from __future__ import annotations

from .base import Tool
from ..db.workspace import get_workspace
from .sql_query import format_table


class MaterializeSubsetTool(Tool):
    name = "materialize_subset"
    description = (
        "Create a new table from a filtered subset of an existing table. Use this "
        "when the user wants to keep drilling into one angle or segment without "
        "polluting the original full table with more columns."
    )
    parameters = {
        "type": "object",
        "properties": {
            "source_table": {"type": "string", "description": "Existing source table"},
            "new_table": {"type": "string", "description": "Name of the subset table to create"},
            "where": {
                "type": "string",
                "description": "SQL WHERE clause defining the subset. Do NOT include WHERE.",
            },
            "columns": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional subset of columns to keep. Defaults to all columns.",
            },
        },
        "required": ["source_table", "new_table", "where"],
    }

    def execute(
        self,
        source_table: str,
        new_table: str,
        where: str,
        columns: list[str] | None = None,
    ) -> str:
        if not _is_ident(new_table):
            return "Error: new_table must be a valid SQL identifier"
        ws = get_workspace()
        if source_table not in ws.tables:
            return f"Error: table '{source_table}' not loaded. Call load_table first."
        if columns:
            missing = [c for c in columns if c not in ws.tables[source_table]["columns"]]
            if missing:
                return f"Error: columns not found in {source_table}: {missing}"
            col_list = ", ".join(f'"{c}"' for c in columns)
        else:
            col_list = "*"

        try:
            ws.conn.execute(
                f'CREATE OR REPLACE TABLE "{new_table}" AS '
                f'SELECT {col_list} FROM "{source_table}" WHERE {where}'
            )
        except Exception as e:
            return f"Error materializing subset: {e}"

        n = ws.conn.execute(f'SELECT COUNT(*) FROM "{new_table}"').fetchone()[0]
        cols = [r[0] for r in ws.conn.execute(f'DESCRIBE "{new_table}"').fetchall()]
        ws.register_table(new_table, f"{source_table}[{where}]", n, cols)
        preview_rows = ws.conn.execute(f'SELECT * FROM "{new_table}" LIMIT 3').fetchall()
        preview = format_table(cols, preview_rows, truncated=False)
        return (
            f"Materialized subset '{new_table}' from '{source_table}': {n} rows, {len(cols)} columns\n"
            f"Columns: {cols}\n"
            f"Preview:\n{preview}"
        )


def _is_ident(name: str) -> bool:
    return bool(name) and name.replace("_", "").isalnum() and not name[0].isdigit()
