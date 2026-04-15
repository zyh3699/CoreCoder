"""Run DuckDB SQL against workspace tables.

This is the ONLY legal path for numeric aggregation (count / sum / avg /
group by).  The system prompt tells the LLM never to compute numbers in its
own head; every statistic must come back through this tool.
"""

from .base import Tool
from ..db.workspace import get_workspace

_MAX_CELL = 200
_DEFAULT_LIMIT = 50


class SqlQueryTool(Tool):
    name = "sql_query"
    description = (
        "Execute a SQL query against loaded tables (DuckDB dialect). "
        "Use this for ALL numeric aggregation: COUNT, SUM, AVG, GROUP BY, etc. "
        "The model must never compute statistics itself - every number must "
        "come from this tool. Derived columns materialized by derive_column "
        "are queryable here."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "SQL to execute. DuckDB dialect. Semicolon optional.",
            },
            "limit": {
                "type": "integer",
                "description": f"Max rows to include in the output preview. Default {_DEFAULT_LIMIT}.",
            },
        },
        "required": ["query"],
    }

    def execute(self, query: str, limit: int = _DEFAULT_LIMIT) -> str:
        ws = get_workspace()
        q = query.strip().rstrip(";")
        if not q:
            return "Error: empty query"

        try:
            cursor = ws.conn.execute(q)
        except Exception as e:
            return f"SQL error: {e}"

        if cursor.description is None:
            # DDL / DML with no resultset
            ws_tables = list(ws.tables.keys())
            for t in ws_tables:
                try:
                    ws.refresh_table(t)
                except Exception:
                    pass
            return "OK (no rows returned)"

        cols = [d[0] for d in cursor.description]
        rows = cursor.fetchmany(limit + 1)
        truncated = len(rows) > limit
        rows = rows[:limit]
        return format_table(cols, rows, truncated)


def format_table(cols, rows, truncated: bool) -> str:
    if not rows:
        return f"columns: {cols}\n(0 rows)"

    str_rows = [[_fmt_cell(v) for v in r] for r in rows]
    widths = [len(c) for c in cols]
    for r in str_rows:
        for i, v in enumerate(r):
            if len(v) > widths[i]:
                widths[i] = len(v)

    sep = "  "
    out = [sep.join(c.ljust(widths[i]) for i, c in enumerate(cols))]
    out.append(sep.join("-" * w for w in widths))
    for r in str_rows:
        out.append(sep.join(r[i].ljust(widths[i]) for i in range(len(cols))))
    if truncated:
        out.append(f"... (truncated at {len(rows)} rows)")
    else:
        out.append(f"({len(rows)} row{'s' if len(rows) != 1 else ''})")
    return "\n".join(out)


def _fmt_cell(v) -> str:
    if v is None:
        return "NULL"
    s = str(v)
    if len(s) > _MAX_CELL:
        s = s[: _MAX_CELL - 3] + "..."
    return s
