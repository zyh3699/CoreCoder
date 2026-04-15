"""Inspect AI-DB cache metadata."""

from .base import Tool
from ..db.workspace import get_workspace


class CacheStatusTool(Tool):
    name = "cache_status"
    description = (
        "Inspect cached AI-DB results so the user can see which derived columns or "
        "analysis layers already exist and may be reused."
    )
    parameters = {
        "type": "object",
        "properties": {
            "table": {"type": "string", "description": "Optional table name filter"},
            "column": {"type": "string", "description": "Optional column name filter"},
            "tool_name": {"type": "string", "description": "Optional tool name filter"},
            "goal": {"type": "string", "description": "Optional goal filter"},
            "model": {"type": "string", "description": "Optional model filter"},
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
    ) -> str:
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
        lines = [f"Matching cache groups: {len(entries)}", ""]
        for e in entries[:50]:
            lines += [
                f"- table={e['table_name']}  column={e['column_name']}  rows={e['count']}",
                f"  tool={e['tool_name'] or '?'}  goal={e['goal'] or '?'}  model={e['model'] or '?'}",
                f"  schema={e['schema_hash'][:12] if e['schema_hash'] else '?'}  source_columns={e['source_columns'] or '?'}",
                f"  created={e['created_min']} .. {e['created_max']}",
                "",
            ]
        if len(entries) > 50:
            lines.append(f"... ({len(entries)} groups total)")
        return "\n".join(lines).rstrip()
