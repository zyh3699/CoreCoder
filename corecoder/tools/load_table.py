"""Load a tabular file into the workspace so sql_query / derive_column can see it.

Supports CSV, Parquet, and JSON(L) via DuckDB's native readers.  Every loaded
table gets a synthetic `_rid` primary key (row_number over the input order)
which derive_column relies on to write results back.
"""

from pathlib import Path

from .base import Tool
from ..db.workspace import get_workspace


class LoadTableTool(Tool):
    name = "load_table"
    description = (
        "Load a CSV / Parquet / JSON / Excel (.xlsx) file into the workspace as a "
        "queryable table.  Excel files are detected automatically even if named .csv. "
        "Must be called before sql_query or derive_column can touch the data. "
        "Returns the schema and a 3-row preview so you can see what you're working with."
    )
    parameters = {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Path to the source file"},
            "table_name": {
                "type": "string",
                "description": "Name to register the table under (must be a valid SQL identifier)",
            },
        },
        "required": ["file_path", "table_name"],
    }

    def execute(self, file_path: str, table_name: str) -> str:
        if not _is_safe_ident(table_name):
            return f"Error: table_name '{table_name}' is not a valid identifier"

        p = Path(file_path).expanduser().resolve()
        if not p.exists():
            return f"Error: {file_path} not found"

        ws = get_workspace()

        # Excel files (xlsx/xlsm) need a separate path even if named .csv
        if _is_xlsx(p):
            try:
                _load_xlsx(ws.conn, p, table_name)
            except Exception as e:
                return f"Error loading Excel file {file_path}: {e}"
        else:
            reader = _reader_for(p)
            if reader is None:
                return (
                    f"Error: unsupported file type '{p.suffix}'. "
                    f"Use .csv, .parquet, .json/.jsonl, or .xlsx"
                )
            try:
                ws.conn.execute(
                    f'CREATE OR REPLACE TABLE "{table_name}" AS '
                    f"SELECT row_number() OVER () AS _rid, * FROM {reader}(?)",
                    [str(p)],
                )
            except Exception as e:
                return f"Error loading {file_path}: {e}"

        n = ws.conn.execute(f'SELECT COUNT(*) FROM "{table_name}"').fetchone()[0]
        cols = [r[0] for r in ws.conn.execute(f'DESCRIBE "{table_name}"').fetchall()]
        ws.register_table(table_name, str(p), n, cols)

        preview = _preview(ws.conn, table_name, 3)
        return (
            f"Loaded '{table_name}': {n} rows, {len(cols)} columns\n"
            f"Columns: {cols}\n"
            f"Preview:\n{preview}"
        )


def _reader_for(path: Path) -> str | None:
    """Return the DuckDB reader function name, or None for xlsx (handled separately)."""
    s = path.suffix.lower()
    if s in (".csv", ".tsv"):
        return "read_csv_auto"
    if s in (".parquet", ".pq"):
        return "read_parquet"
    if s in (".json", ".jsonl", ".ndjson"):
        return "read_json_auto"
    return None


def _is_xlsx(path: Path) -> bool:
    """True if the file is an Excel workbook, regardless of extension."""
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"PK\x03\x04"  # ZIP magic = xlsx/xlsm
    except OSError:
        return False


def _load_xlsx(conn, path: Path, table_name: str):
    """Load an Excel file via pandas+openpyxl, then register in DuckDB."""
    try:
        import pandas as pd
    except ImportError:
        raise RuntimeError(
            "pandas is required to load Excel files: pip install pandas openpyxl"
        )
    df = pd.read_excel(path, engine="openpyxl")
    conn.register("__xlsx_tmp", df)
    conn.execute(
        f'CREATE OR REPLACE TABLE "{table_name}" AS '
        f"SELECT row_number() OVER () AS _rid, * FROM __xlsx_tmp"
    )
    conn.unregister("__xlsx_tmp")


def _is_safe_ident(name: str) -> bool:
    return name.replace("_", "").isalnum() and not name[0].isdigit()


def _preview(conn, table: str, n: int) -> str:
    cursor = conn.execute(f'SELECT * FROM "{table}" LIMIT {int(n)}')
    cols = [d[0] for d in cursor.description]
    rows = cursor.fetchall()
    from .sql_query import format_table  # local import avoids circular
    return format_table(cols, rows, truncated=False)
