"""Discover canonical issue phrases from sampled text rows."""

from __future__ import annotations

import json

from .base import Tool
from ..db.workspace import get_workspace


class DiscoverIssuePhrasesTool(Tool):
    name = "discover_issue_phrases"
    description = (
        "Inspect a sample of rows and propose concrete canonical phrases such as "
        "'pilling', 'stinging', or 'not worth the price'. Use this when the user "
        "wants the final talking angles to be short, user-language issue phrases "
        "rather than broad analytical categories. Best paired with diverse sampling "
        "and later assign_taxonomy over the relevant subset. Prefer running it on a "
        "materialized sample table instead of a large raw table."
    )
    parameters = {
        "type": "object",
        "properties": {
            "table": {"type": "string", "description": "Source table name"},
            "text_column": {
                "type": "string",
                "description": "Column containing the primary text to inspect",
            },
            "goal": {
                "type": "string",
                "description": (
                    "What phrase layer to discover, e.g. "
                    "'negative_issue_phrases' or 'positive_selling_phrases'"
                ),
            },
            "where": {
                "type": "string",
                "description": (
                    "Optional SQL WHERE clause to filter rows before discovery. "
                    "Do NOT include the word WHERE."
                ),
            },
            "parent_angle_column": {
                "type": "string",
                "description": "Optional broad-angle column used for drill-down phrase discovery",
            },
            "parent_angle_value": {
                "type": "string",
                "description": "Optional broad-angle value to focus on, e.g. usage_issues",
            },
            "sample_size": {
                "type": "integer",
                "description": "How many rows to inspect when discovering canonical phrases",
            },
            "sampling_method": {
                "type": "string",
                "enum": ["random", "diverse", "stratified"],
                "description": "Sampling strategy. Phase one fully supports random now.",
            },
            "max_phrases": {
                "type": "integer",
                "description": "Maximum number of canonical phrases to propose",
            },
            "include_other": {
                "type": "boolean",
                "description": "Whether to include an 'other' fallback phrase bucket",
            },
            "phrase_style": {
                "type": "string",
                "enum": ["canonical_issue", "canonical_praise", "canonical_scenario"],
                "description": "What kind of canonical phrase to discover",
            },
        },
        "required": ["table", "text_column", "goal"],
    }

    def execute(
        self,
        table: str,
        text_column: str,
        goal: str,
        where: str | None = None,
        parent_angle_column: str | None = None,
        parent_angle_value: str | None = None,
        sample_size: int = 40,
        sampling_method: str = "random",
        max_phrases: int = 10,
        include_other: bool = True,
        phrase_style: str = "canonical_issue",
    ) -> str:
        ws = get_workspace()
        if table not in ws.tables:
            return f"Error: table '{table}' not loaded. Call load_table first."
        if ws.llm is None:
            return "Error: no LLM attached to workspace (cannot discover issue phrases)"
        if text_column not in ws.tables[table]["columns"]:
            return f"Error: column '{text_column}' not in table '{table}'"
        if sample_size <= 0:
            return "Error: sample_size must be positive"
        if max_phrases <= 0:
            return "Error: max_phrases must be positive"
        if phrase_style not in {"canonical_issue", "canonical_praise", "canonical_scenario"}:
            return "Error: phrase_style must be one of canonical_issue, canonical_praise, canonical_scenario"
        if parent_angle_column and parent_angle_column not in ws.tables[table]["columns"]:
            return f"Error: column '{parent_angle_column}' not in table '{table}'"
        if parent_angle_value and not parent_angle_column:
            return "Error: parent_angle_column is required when parent_angle_value is provided"
        target_polarity = _infer_target_polarity(goal, where, parent_angle_value)

        note = ""
        order_clause = " ORDER BY random()"
        if sampling_method == "diverse":
            note = "Diverse sampling will use embeddings in a later phase. Falling back to random sampling for now."
        elif sampling_method == "stratified":
            note = "Stratified sampling for phrase discovery will be added in a later phase. Falling back to random sampling for now."
        elif sampling_method != "random":
            return "Error: sampling_method must be one of random, diverse, stratified"

        filters = []
        if where:
            filters.append(f"({where})")
        if parent_angle_column and parent_angle_value:
            escaped = parent_angle_value.replace("'", "''")
            filters.append(f'"{parent_angle_column}" = \'{escaped}\'')
        where_clause = f" WHERE {' AND '.join(filters)}" if filters else ""

        rows = ws.conn.execute(
            f'SELECT _rid, "{text_column}" FROM "{table}"{where_clause}{order_clause} LIMIT ?',
            [int(sample_size)],
        ).fetchall()
        if not rows:
            return "Error: filter returned 0 rows - nothing to inspect"

        sample_payload = [
            {"_rid": rid, text_column: "" if text is None else str(text)[:500]}
            for rid, text in rows
        ]
        system = (
            "You are discovering concrete canonical phrases for analytics. "
            "Output ONLY valid JSON with keys: phrases, recommended_column_name, "
            "assignment_prompt, notes. phrases must be an array of objects with keys: "
            "canonical_phrase, definition, variants, example_rids, parent_angle. "
            "canonical_phrase should be short, concrete, and close to the user's own "
            "language - not a broad analyst category."
        )
        user = json.dumps(
            {
                "goal": goal,
                "phrase_style": phrase_style,
                "target_polarity": target_polarity,
                "max_phrases": max_phrases,
                "include_other": include_other,
                "text_column": text_column,
                "parent_angle_column": parent_angle_column,
                "parent_angle_value": parent_angle_value,
                "rows": sample_payload,
                "requirements": [
                    "Prefer concrete, repeatable phrases that could be used directly in a chart or table.",
                    "Use short canonical phrases, usually 2-6 words or a short phrase in the source language.",
                    "Group near-synonyms and wording variants under one canonical phrase.",
                    "Do not output broad buckets like general commentary, product mention, or user reaction unless the goal explicitly asks for them.",
                    "If target_polarity is provided, ignore sampled rows that are clearly outside that polarity instead of letting them shape the discovered phrases.",
                    "For canonical_issue, prefer actionable problems and concrete user-language failure modes rather than abstract summaries.",
                    "Cover as much of the on-target sample as possible with concrete phrases before using 'other'.",
                    "If include_other is true, add an 'other' fallback phrase only after proposing the concrete recurring phrases, and only for residual minority cases.",
                    "Do not let 'other' stand in for generic non-specific chatter; if rows lack a clear issue, leave them unrepresented rather than turning that into a dominant phrase.",
                    "Return example_rids that point to the sampled rows supporting each phrase.",
                ],
            },
            ensure_ascii=False,
        )

        try:
            raw = ws.llm.complete_json(system, user)
            obj = json.loads(raw)
        except Exception as e:
            return f"Error: issue phrase discovery failed: {e}"

        phrases = obj.get("phrases")
        if not isinstance(phrases, list) or not phrases:
            return "Error: issue phrase discovery returned no phrases"

        rendered = []
        payload = []
        for item in phrases:
            if not isinstance(item, dict):
                continue
            canonical = str(item.get("canonical_phrase", "")).strip()
            definition = str(item.get("definition", "")).strip()
            variants = item.get("variants", [])
            example_rids = item.get("example_rids", [])
            parent = str(item.get("parent_angle", "")).strip()
            if not canonical:
                continue
            payload.append(
                {
                    "label": canonical,
                    "definition": definition,
                    "variants": variants if isinstance(variants, list) else [],
                    "parent_angle": parent,
                }
            )
            pieces = [f"- {canonical}: {definition or '(no definition provided)'}"]
            if parent:
                pieces.append(f"parent={parent}")
            if isinstance(variants, list) and variants:
                pieces.append(f"variants={variants[:5]}")
            if isinstance(example_rids, list) and example_rids:
                pieces.append(f"example_rids={example_rids[:5]}")
            rendered.append("  ".join(pieces))

        if not rendered:
            return "Error: issue phrase discovery returned malformed phrases"

        recommended_column = str(obj.get("recommended_column_name") or "issue_phrase")
        assignment_prompt = str(obj.get("assignment_prompt") or "").strip()
        notes = obj.get("notes")
        lines = [
            f"Discovered candidate issue phrases for goal '{goal}' from {len(rows)} sampled rows",
        ]
        if where:
            lines.append(f"Filter: {where}")
        if parent_angle_column and parent_angle_value:
            lines.append(f"Focused on {parent_angle_column} = {parent_angle_value}")
        if note:
            lines.append(note)
        lines += [
            f"Recommended column: {recommended_column}",
            "Candidate canonical phrases:",
            *rendered,
            "",
            "Suggested taxonomy payload:",
            json.dumps(payload, ensure_ascii=False),
        ]
        if assignment_prompt:
            lines += [
                "",
                "Suggested assignment prompt:",
                assignment_prompt,
            ]
        if notes:
            lines += [
                "",
                "Notes:",
                str(notes),
            ]
        lines += [
            "",
            "If these canonical phrases look right, convert them into a closed taxonomy and run assign_taxonomy over the full target set.",
        ]
        return "\n".join(lines)


def _infer_target_polarity(goal: str, where: str | None, parent_angle_value: str | None) -> str | None:
    text = " ".join(x for x in [goal, where or "", parent_angle_value or ""]).lower()
    if any(tok in text for tok in ["negative", "complaint", "pain", "neg", "label_0", "吐槽", "负面", "差评"]):
        return "negative"
    if any(tok in text for tok in ["positive", "praise", "selling", "pos", "label_2", "label_1", "正面", "卖点", "好评"]):
        return "positive"
    return None
