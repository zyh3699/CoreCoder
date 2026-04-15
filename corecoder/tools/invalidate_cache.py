"""Invalidate selected AI-DB cache entries and optionally matching workspace columns."""

from .base import Tool
from ..db.workspace import get_workspace


class InvalidateCacheTool(Tool):
    name = "invalidate_cache"
    description = (
        "Delete selected AI-DB cache entries and optionally remove matching columns "
        "from the in-memory workspace tables. Use this when the user wants to rerun "
        "only part of a workflow."
    )
    parameters = {
        "type": "object",
        "properties": {
            "table": {"type": "string", "description": "Optional table name filter"},
            "column": {"type": "string", "description": "Optional column name filter"},
            "tool_name": {"type": "string", "description": "Optional tool name filter"},
            "goal": {"type": "string", "description": "Optional goal filter"},
            "model": {"type": "string", "description": "Optional model filter"},
            "workspace_only": {
                "type": "boolean",
                "description": "Only drop matching workspace columns; keep persistent cache entries",
            },
            "cache_only": {
                "type": "boolean",
                "description": "Only delete persistent cache entries; keep workspace columns",
            },
        },
        "required": [],
    }

    def execute(
        self,
        table: str | None = None,
        column: str | None = None,
        tool_name: str | None = None,
        goal: str | None = None,
        model: str | None = None,
        workspace_only: bool = False,
        cache_only: bool = False,
    ) -> str:
        if workspace_only and cache_only:
            return "Error: workspace_only and cache_only cannot both be true"
        ws = get_workspace()
        entries = ws.cache.list_entries(
            table=table,
            column=column,
            tool_name=tool_name,
            goal=goal,
            model=model,
        )
        if not entries:
            return "No matching cache entries."

        dropped = []
        if not cache_only:
            # Deduplicate table/column pairs before dropping.
            seen = set()
            for e in entries:
                pair = (e["table_name"], e["column_name"])
                if pair in seen:
                    continue
                seen.add(pair)
                if e["column_name"] in {"_rid"}:
                    continue
                if e["table_name"] not in ws.tables:
                    continue
                if e["column_name"] not in ws.tables[e["table_name"]]["columns"]:
                    continue
                try:
                    ws.conn.execute(
                        f'ALTER TABLE "{e["table_name"]}" DROP COLUMN IF EXISTS "{e["column_name"]}"'
                    )
                    ws.refresh_table(e["table_name"])
                    dropped.append(f'{e["table_name"]}.{e["column_name"]}')
                except Exception:
                    pass

        deleted = 0
        if not workspace_only:
            deleted = ws.cache.delete_entries(
                table=table,
                column=column,
                tool_name=tool_name,
                goal=goal,
                model=model,
            )

        lines = [
            f"Matched cache groups: {len(entries)}",
            f"Deleted cache rows: {deleted}" if not workspace_only else "Deleted cache rows: 0 (workspace_only)",
            f"Dropped workspace columns: {len(dropped)}" if not cache_only else "Dropped workspace columns: 0 (cache_only)",
        ]
        if dropped:
            lines += ["Columns dropped:", *[f"  - {x}" for x in dropped[:50]]]
        return "\n".join(lines)
