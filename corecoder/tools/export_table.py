"""Export a SQL query result to a file (Excel or CSV).

The typical use: run your analysis via sql_query, then call export_table with
the same SQL to persist the result.  The output path is returned so the user
knows where to find it.
"""

from pathlib import Path

from .base import Tool
from ..db.workspace import get_workspace


class ExportTableTool(Tool):
    name = "export_table"
    description = (
        "Run a SQL query and write the result to an Excel (.xlsx) or CSV file. "
        "Use this as the final step to hand off analysis results to the user."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "SQL query whose result will be exported",
            },
            "output_path": {
                "type": "string",
                "description": (
                    "Destination file path.  Extension determines format: "
                    ".xlsx for Excel, .csv for CSV."
                ),
            },
        },
        "required": ["query", "output_path"],
    }

    def execute(self, query: str, output_path: str) -> str:
        try:
            import pandas as pd
        except ImportError:
            return "Error: pandas is required for export: pip install pandas openpyxl"

        ws = get_workspace()
        q = query.strip().rstrip(";")
        if not q:
            return "Error: empty query"

        try:
            df = ws.conn.execute(q).df()
        except Exception as e:
            return f"SQL error: {e}"

        if df.empty:
            return "Query returned 0 rows - nothing to export"

        out = Path(output_path).expanduser().resolve()
        out.parent.mkdir(parents=True, exist_ok=True)

        try:
            if out.suffix.lower() in (".xlsx", ".xls"):
                df.to_excel(out, index=False, engine="openpyxl")
            else:
                df.to_csv(out, index=False)
        except Exception as e:
            return f"Error writing {out}: {e}"

        return (
            f"Exported {len(df)} rows × {len(df.columns)} columns → {out}\n"
            f"Columns: {list(df.columns)}"
        )
