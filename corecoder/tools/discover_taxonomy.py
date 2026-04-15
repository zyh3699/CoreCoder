"""Discover a closed taxonomy from sampled rows."""

from __future__ import annotations

import json

from .base import Tool
from ..db.workspace import get_workspace


class DiscoverTaxonomyTool(Tool):
    name = "discover_taxonomy"
    description = (
        "Inspect a sample of rows and propose a closed taxonomy for a semantic "
        "analysis goal such as complaint angles, positive selling points, "
        "usage scenarios, or purchase motives. Returns candidate categories, "
        "definitions, and a suggested follow-up assignment prompt."
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
                    "What semantic dimension to discover, e.g. "
                    "'negative_problem_angles' or 'usage_scenarios'"
                ),
            },
            "where": {
                "type": "string",
                "description": (
                    "Optional SQL WHERE clause to filter rows before discovery. "
                    "Do NOT include the word WHERE."
                ),
            },
            "sample_size": {
                "type": "integer",
                "description": "How many rows to inspect when discovering the taxonomy",
            },
            "sampling_method": {
                "type": "string",
                "enum": ["random", "diverse", "stratified"],
                "description": "Sampling strategy. Phase one discovery fully supports random now.",
            },
            "max_categories": {
                "type": "integer",
                "description": "Maximum number of non-other categories to propose",
            },
            "taxonomy_shape": {
                "type": "string",
                "enum": ["flat", "hierarchical"],
                "description": "Whether to propose a flat taxonomy or a parent/child hierarchy.",
            },
            "granularity_preference": {
                "type": "string",
                "enum": ["broad", "fine", "both"],
                "description": "Whether to emphasize broad categories, fine-grained angles, or both.",
            },
            "max_parent_categories": {
                "type": "integer",
                "description": "Maximum number of parent categories when taxonomy_shape='hierarchical'",
            },
            "max_child_categories_per_parent": {
                "type": "integer",
                "description": "Maximum number of child categories to propose under each parent category",
            },
            "include_other": {
                "type": "boolean",
                "description": "Whether to include an 'other' fallback category",
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
        sample_size: int = 30,
        sampling_method: str = "random",
        max_categories: int = 8,
        taxonomy_shape: str = "flat",
        granularity_preference: str = "both",
        max_parent_categories: int = 5,
        max_child_categories_per_parent: int = 5,
        include_other: bool = True,
    ) -> str:
        ws = get_workspace()
        if table not in ws.tables:
            return f"Error: table '{table}' not loaded. Call load_table first."
        if ws.llm is None:
            return "Error: no LLM attached to workspace (cannot discover taxonomy)"
        if text_column not in ws.tables[table]["columns"]:
            return f"Error: column '{text_column}' not in table '{table}'"
        if sample_size <= 0:
            return "Error: sample_size must be positive"
        if max_categories <= 0:
            return "Error: max_categories must be positive"
        if taxonomy_shape not in {"flat", "hierarchical"}:
            return "Error: taxonomy_shape must be one of flat, hierarchical"
        if granularity_preference not in {"broad", "fine", "both"}:
            return "Error: granularity_preference must be one of broad, fine, both"

        note = ""
        order_clause = " ORDER BY random()"
        if sampling_method == "diverse":
            note = "Diverse sampling will use embeddings in a later phase. Falling back to random sampling for now."
        elif sampling_method == "stratified":
            note = "Stratified sampling for taxonomy discovery will be added in a later phase. Falling back to random sampling for now."
        elif sampling_method != "random":
            return "Error: sampling_method must be one of random, diverse, stratified"

        where_clause = f" WHERE {where}" if where else ""
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
            "You are designing a closed taxonomy for tabular analysis. "
            "Output ONLY valid JSON with keys: categories, recommended_column_name, "
            "assignment_prompt, notes. "
            "If taxonomy_shape='flat', categories must be an array of objects with "
            "keys label and definition. "
            "If taxonomy_shape='hierarchical', categories must be an array of objects "
            "with keys parent_label, parent_definition, children, where children is an "
            "array of objects with keys child_label and child_definition. "
            "Use short labels, mutually distinguishable definitions, and focus on the "
            "requested goal rather than generic topics."
        )
        user = json.dumps(
            {
                "goal": goal,
                "max_categories": max_categories,
                "taxonomy_shape": taxonomy_shape,
                "granularity_preference": granularity_preference,
                "max_parent_categories": max_parent_categories,
                "max_child_categories_per_parent": max_child_categories_per_parent,
                "include_other": include_other,
                "text_column": text_column,
                "rows": sample_payload,
                "requirements": [
                    "Propose categories that are useful for later SQL aggregation.",
                    "Prefer business/actionable angles over entity names or loose keywords.",
                    "Keep categories single-axis and easy to distinguish.",
                    "If include_other is true, include an 'other' catch-all category.",
                    "If taxonomy_shape is hierarchical, make parent categories broad enough for overview reporting and child categories specific enough for drill-down analysis.",
                    "Write assignment_prompt as a direct instruction for labeling one row into exactly one category.",
                ],
            },
            ensure_ascii=False,
        )

        try:
            raw = ws.llm.complete_json(system, user)
            obj = json.loads(raw)
        except Exception as e:
            return f"Error: taxonomy discovery failed: {e}"

        categories = obj.get("categories")
        if not isinstance(categories, list) or not categories:
            return "Error: taxonomy discovery returned no categories"

        rendered, labels = _render_categories(categories, taxonomy_shape)
        if not rendered:
            return "Error: taxonomy discovery returned malformed categories"

        recommended_column = str(obj.get("recommended_column_name") or f"{goal}_label")
        assignment_prompt = str(obj.get("assignment_prompt") or "").strip()
        notes = obj.get("notes")
        lines = [
            f"Discovered candidate taxonomy for goal '{goal}' from {len(rows)} sampled rows",
        ]
        if where:
            lines.append(f"Filter: {where}")
        if note:
            lines.append(note)
        lines += [
            f"Recommended column: {recommended_column}",
            "Candidate categories:",
            *rendered,
            "",
            "Suggested taxonomy payload:",
            json.dumps(labels, ensure_ascii=False),
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
            "If this taxonomy looks right, confirm it and then run assign_taxonomy over the full target set.",
        ]
        return "\n".join(lines)


def _render_categories(categories, taxonomy_shape: str) -> tuple[list[str], list]:
    rendered: list[str] = []
    payload: list = []

    if taxonomy_shape == "flat":
        for item in categories:
            if not isinstance(item, dict):
                continue
            label = str(item.get("label", "")).strip()
            definition = str(item.get("definition", "")).strip()
            if not label:
                continue
            payload.append(label)
            rendered.append(f"- {label}: {definition or '(no definition provided)'}")
        return rendered, payload

    for item in categories:
        if not isinstance(item, dict):
            continue
        parent_label = str(item.get("parent_label", "")).strip()
        parent_definition = str(item.get("parent_definition", "")).strip()
        children = item.get("children")
        if not parent_label or not isinstance(children, list):
            continue
        child_payload = []
        rendered.append(
            f"- {parent_label}: {parent_definition or '(no definition provided)'}"
        )
        for child in children:
            if not isinstance(child, dict):
                continue
            child_label = str(child.get("child_label", "")).strip()
            child_definition = str(child.get("child_definition", "")).strip()
            if not child_label:
                continue
            child_payload.append(
                {
                    "parent": parent_label,
                    "child": child_label,
                    "definition": child_definition,
                }
            )
            rendered.append(
                f"  - {child_label}: {child_definition or '(no definition provided)'}"
            )
        payload.extend(child_payload)
    return rendered, payload
