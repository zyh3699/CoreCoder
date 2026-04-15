"""Assign a confirmed taxonomy to rows and materialize it as a real column."""

from __future__ import annotations

import concurrent.futures
import json
from collections import Counter

from .base import Tool
from ..db.workspace import get_workspace
from ..db.embeddings import DEFAULT_EMBED_MODEL, cosine_similarity, encode_texts

_MAX_WORKERS = 8


class AssignTaxonomyTool(Tool):
    name = "assign_taxonomy"
    description = (
        "Apply a confirmed taxonomy to rows and materialize the result as a real SQL "
        "column. Phase one uses LLM-only assignment and keeps the interface ready for "
        "future embed-then-LLM routing."
    )
    parameters = {
        "type": "object",
        "properties": {
            "table": {"type": "string", "description": "Source table name"},
            "text_column": {
                "type": "string",
                "description": "Column containing the primary text to classify",
            },
            "new_column": {
                "type": "string",
                "description": "Name of the output column to add",
            },
            "new_column_parent": {
                "type": "string",
                "description": "Parent-level output column when using a hierarchical taxonomy",
            },
            "new_column_child": {
                "type": "string",
                "description": "Child-level output column when using a hierarchical taxonomy",
            },
            "goal": {
                "type": "string",
                "description": "Semantic dimension being assigned, e.g. negative_problem_angles",
            },
            "taxonomy": {
                "type": "array",
                "items": {},
                "description": (
                    "Closed set of labels to assign. For flat taxonomies, pass strings. "
                    "For hierarchical taxonomies, pass objects with parent, child, and optional definition."
                ),
            },
            "taxonomy_shape": {
                "type": "string",
                "enum": ["flat", "hierarchical"],
                "description": "Whether the taxonomy is flat or parent/child hierarchical.",
            },
            "category_definitions": {
                "type": "object",
                "description": "Optional mapping from label to definition",
            },
            "where": {
                "type": "string",
                "description": (
                    "Optional SQL WHERE clause to restrict which rows are assigned. "
                    "Rows outside the filter keep NULL."
                ),
            },
            "routing_mode": {
                "type": "string",
                "enum": ["llm_only", "embed_then_llm", "embed_only"],
                "description": "Phase one fully supports llm_only; the other modes fall back with a note.",
            },
            "embedding_model": {
                "type": "string",
                "description": f"Sentence-transformer model used when routing_mode is embedding-based. Default: {DEFAULT_EMBED_MODEL}",
            },
            "confidence_threshold": {
                "type": "number",
                "description": "Minimum cosine similarity for high-confidence embedding assignment. Default 0.55.",
            },
            "sample_size": {
                "type": "integer",
                "description": "Dry-run mode: classify this many rows only and do not write the column.",
            },
            "rerun_mode": {
                "type": "string",
                "enum": ["reuse", "refresh", "no_write_cache"],
                "description": "Whether to reuse cache, refresh labels, or skip writing new cache entries.",
            },
        },
        "required": ["table", "text_column", "new_column", "goal", "taxonomy"],
    }

    def execute(
        self,
        table: str,
        text_column: str,
        new_column: str,
        goal: str,
        taxonomy: list,
        taxonomy_shape: str = "flat",
        category_definitions: dict | None = None,
        new_column_parent: str | None = None,
        new_column_child: str | None = None,
        where: str | None = None,
        routing_mode: str = "llm_only",
        embedding_model: str = DEFAULT_EMBED_MODEL,
        confidence_threshold: float = 0.55,
        sample_size: int | None = None,
        rerun_mode: str = "reuse",
    ) -> str:
        ws = get_workspace()
        if table not in ws.tables:
            return f"Error: table '{table}' not loaded. Call load_table first."
        if ws.llm is None:
            return "Error: no LLM attached to workspace (cannot assign taxonomy)"
        if text_column not in ws.tables[table]["columns"]:
            return f"Error: column '{text_column}' not in table '{table}'"
        if not taxonomy:
            return "Error: taxonomy must not be empty"
        if taxonomy_shape not in {"flat", "hierarchical"}:
            return "Error: taxonomy_shape must be one of flat, hierarchical"
        if rerun_mode not in {"reuse", "refresh", "no_write_cache"}:
            return "Error: rerun_mode must be one of reuse, refresh, no_write_cache"

        note = ""
        target_polarity = _infer_target_polarity(goal, where)
        if routing_mode == "llm_only":
            pass
        elif routing_mode in {"embed_then_llm", "embed_only"}:
            note = (
                f"Routing mode '{routing_mode}' will use embeddings first with model "
                f"'{embedding_model}' and threshold {confidence_threshold:.2f}."
            )
        else:
            return "Error: routing_mode must be one of llm_only, embed_then_llm, embed_only"

        parsed = _parse_taxonomy(taxonomy, taxonomy_shape, category_definitions or {})
        if isinstance(parsed, str):
            return parsed
        if taxonomy_shape == "flat":
            if not _is_ident(new_column):
                return "Error: new_column must be a valid SQL identifier"
        else:
            if not new_column_parent or not new_column_child:
                return "Error: new_column_parent and new_column_child are required for hierarchical taxonomies"
            if not _is_ident(new_column_parent) or not _is_ident(new_column_child):
                return "Error: new_column_parent and new_column_child must be valid SQL identifiers"

        schema_str = json.dumps(
            {
                "goal": goal,
                "taxonomy_shape": taxonomy_shape,
                "labels": parsed["cache_payload"],
                "definitions": parsed["definitions"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        model = ws.llm.model
        schema_hash = ws.cache.hash_text(schema_str)
        source_cols_str = text_column

        where_clause = f" WHERE {where}" if where else ""
        base_q = f'SELECT _rid, "{text_column}" FROM "{table}"{where_clause} ORDER BY _rid'
        if sample_size:
            rows = ws.conn.execute(base_q + " LIMIT ?", [int(sample_size)]).fetchall()
        else:
            rows = ws.conn.execute(base_q).fetchall()
        if not rows:
            return "Error: filter returned 0 rows - nothing to assign"

        hits: dict[int, object] = {}
        work: list[tuple[int, str, str]] = []
        for rid, text in rows:
            text_str = "" if text is None else str(text)
            key = ws.cache.make_key(table, text_str, goal, schema_str, model)
            cached = None if rerun_mode != "reuse" else ws.cache.get(key)
            if cached is not None:
                hits[rid] = cached
            else:
                work.append((rid, text_str, key))

        results = dict(hits)
        errors = 0
        embed_assigned = 0
        if work and routing_mode in {"embed_then_llm", "embed_only"}:
            try:
                work_texts = [text for _, text, _ in work]
                row_vecs = encode_texts(work_texts, model_name=embedding_model)
                proto_texts = _prototype_texts(parsed)
                proto_vecs = encode_texts(proto_texts, model_name=embedding_model)
                proto_items = parsed["labels"] if parsed["shape"] == "flat" else parsed["items"]
                remaining: list[tuple[int, str, str]] = []
                for (rid, text, key), row_vec in zip(work, row_vecs):
                    sims = [cosine_similarity(row_vec, pv) for pv in proto_vecs]
                    best_idx = max(range(len(sims)), key=sims.__getitem__)
                    best_score = sims[best_idx]
                    if best_score >= confidence_threshold:
                        if parsed["shape"] == "flat":
                            value = proto_items[best_idx]
                        else:
                            item = proto_items[best_idx]
                            value = {"parent": item["parent"], "child": item["child"]}
                        results[rid] = value
                        embed_assigned += 1
                        if rerun_mode != "no_write_cache":
                            cache_col = new_column if taxonomy_shape == "flat" else f"{new_column_parent}|{new_column_child}"
                            ws.cache.put(
                                key,
                                table,
                                cache_col,
                                str(rid),
                                value,
                                model,
                                tool_name=self.name,
                                goal=goal,
                                schema_hash=schema_hash,
                                prompt_preview=f"{goal} [embed]",
                                source_columns=source_cols_str,
                            )
                    else:
                        remaining.append((rid, text, key))
                work = remaining
            except Exception as e:
                note += f" Embedding routing fallback to llm_only due to: {e}"

        if routing_mode == "embed_only":
            for rid, _, _ in work:
                results[rid] = None
            errors += len(work)
            work = []

        if work:
            with concurrent.futures.ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
                future_map = {
                    pool.submit(
                        self._label_one,
                        ws.llm,
                        text,
                        goal,
                        parsed,
                        target_polarity,
                    ): (rid, key)
                    for rid, text, key in work
                }
                for fut in concurrent.futures.as_completed(future_map):
                    rid, key = future_map[fut]
                    try:
                        value = fut.result()
                    except Exception:
                        value = None
                    results[rid] = value
                    if value is None:
                        errors += 1
                    elif rerun_mode != "no_write_cache":
                        cache_col = new_column if taxonomy_shape == "flat" else f"{new_column_parent}|{new_column_child}"
                        ws.cache.put(
                            key,
                            table,
                            cache_col,
                            str(rid),
                            value,
                            model,
                            tool_name=self.name,
                            goal=goal,
                            schema_hash=schema_hash,
                            prompt_preview=goal[:160],
                            source_columns=source_cols_str,
                        )

        if taxonomy_shape == "flat":
            dist = Counter("NULL" if results.get(rid) is None else results[rid] for rid, _ in rows)
        else:
            dist = Counter(
                "NULL" if results.get(rid) is None else results[rid].get("child", "NULL")
                for rid, _ in rows
            )
        if sample_size:
            lines = [
                f"[DRY RUN - {len(rows)} rows previewed, column NOT written]",
                f"Goal: {goal}",
                f"  cache hits: {len(hits)}   embed assigns: {embed_assigned}   llm calls: {len(work)}   errors: {errors}",
            ]
            if note:
                lines.append(note)
            lines += [
                "Label distribution: " + ", ".join(f"{k}={v}" for k, v in dist.most_common()),
                "",
                "Sample assignments:",
            ]
            for rid, text in rows[:10]:
                snippet = ("" if text is None else str(text)).replace("\n", " ")
                if len(snippet) > 100:
                    snippet = snippet[:97] + "..."
                lines.append(f"  _rid={rid} -> {results.get(rid)!r}   {snippet}")
            return "\n".join(lines)

        try:
            if taxonomy_shape == "flat":
                ws.conn.execute(
                    "CREATE OR REPLACE TEMP TABLE __taxonomy_staging (_rid BIGINT, val VARCHAR)"
                )
                ws.conn.executemany(
                    "INSERT INTO __taxonomy_staging VALUES (?, ?)",
                    [(rid, results.get(rid)) for rid, _ in rows],
                )
                ws.conn.execute(f'ALTER TABLE "{table}" DROP COLUMN IF EXISTS "{new_column}"')
                ws.conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{new_column}" VARCHAR')
                ws.conn.execute(
                    f'UPDATE "{table}" SET "{new_column}" = s.val '
                    f'FROM __taxonomy_staging s WHERE "{table}"._rid = s._rid'
                )
            else:
                ws.conn.execute(
                    "CREATE OR REPLACE TEMP TABLE __taxonomy_staging "
                    "(_rid BIGINT, parent_val VARCHAR, child_val VARCHAR)"
                )
                ws.conn.executemany(
                    "INSERT INTO __taxonomy_staging VALUES (?, ?, ?)",
                    [
                        (
                            rid,
                            None if results.get(rid) is None else results[rid].get("parent"),
                            None if results.get(rid) is None else results[rid].get("child"),
                        )
                        for rid, _ in rows
                    ],
                )
                for col, src in ((new_column_parent, "parent_val"), (new_column_child, "child_val")):
                    ws.conn.execute(f'ALTER TABLE "{table}" DROP COLUMN IF EXISTS "{col}"')
                    ws.conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{col}" VARCHAR')
                    ws.conn.execute(
                        f'UPDATE "{table}" SET "{col}" = s.{src} '
                        f'FROM __taxonomy_staging s WHERE "{table}"._rid = s._rid'
                    )
            ws.conn.execute("DROP TABLE IF EXISTS __taxonomy_staging")
        except Exception as e:
            return f"Error writing column: {e}"

        ws.refresh_table(table)
        if taxonomy_shape == "flat":
            lines = [
                f'Wrote column "{new_column}" into "{table}": {len(rows)} rows',
                f"Goal: {goal}",
                f"  cache hits: {len(hits)}   embed assigns: {embed_assigned}   llm calls: {len(work)}   errors (NULL): {errors}",
            ]
        else:
            parent_dist = Counter(
                "NULL" if results.get(rid) is None else results[rid].get("parent", "NULL")
                for rid, _ in rows
            )
            lines = [
                f'Wrote hierarchical columns "{new_column_parent}" and "{new_column_child}" into "{table}": {len(rows)} rows',
                f"Goal: {goal}",
                f"  cache hits: {len(hits)}   embed assigns: {embed_assigned}   llm calls: {len(work)}   errors (NULL): {errors}",
                "Parent distribution: " + ", ".join(f"{k}={v}" for k, v in parent_dist.most_common()),
            ]
        if note:
            lines.append(note)
        lines += [
            "Label distribution: " + ", ".join(f"{k}={v}" for k, v in dist.most_common()),
            "",
            "Now queryable via sql_query, e.g.:",
        ]
        if taxonomy_shape == "flat":
            lines.append(f'  SELECT "{new_column}", COUNT(*) FROM "{table}" GROUP BY 1 ORDER BY 2 DESC')
        else:
            lines.append(
                f'  SELECT "{new_column_parent}", "{new_column_child}", COUNT(*) '
                f'FROM "{table}" GROUP BY 1, 2 ORDER BY 3 DESC'
            )
        return "\n".join(lines)

    @staticmethod
    def _label_one(llm, text: str, goal: str, parsed: dict, target_polarity: str | None) -> object | None:
        system = (
            "You assign one row into a closed taxonomy for analytics. "
            "Output ONLY a single valid JSON object with key 'value'."
        )
        if parsed["shape"] == "flat":
            schema_note = (
                f'Respond with JSON: {{"value": <one of {parsed["labels"]}>}}. '
                "Pick exactly one label. If none fits, choose 'other' when available. "
                "Use 'other' only as a last resort after considering the most specific non-other label."
            )
            defs = "\n".join(
                f"- {label}: {parsed['definitions'][label]}"
                for label in parsed["labels"]
                if label in parsed["definitions"]
            ) or "(no category definitions provided)"
            user = (
                f"Goal: {goal}\n"
                f"Target polarity: {target_polarity or 'unspecified'}\n"
                f"Allowed labels: {parsed['labels']}\n"
                f"Definitions:\n{defs}\n\n"
                "If the row is clearly outside the target polarity, respond with "
                'JSON {"value": "__skip__"} instead of forcing a bad label.\n\n'
                f"{schema_note}\n\n"
                f"Row text:\n{text}"
            )
        else:
            schema_note = (
                'Respond with JSON: {"value": {"parent": <parent_label>, "child": <child_label>}}. '
                "Pick exactly one parent/child pair from the allowed taxonomy. "
                "If nothing fits, choose the most appropriate 'other' branch when available. "
                "Use the 'other' branch only as a last resort after considering specific branches."
            )
            defs = "\n".join(
                f"- {item['parent']} -> {item['child']}: {item['definition']}"
                for item in parsed["items"]
            )
            user = (
                f"Goal: {goal}\n"
                f"Target polarity: {target_polarity or 'unspecified'}\n"
                f"Allowed taxonomy pairs:\n{defs}\n\n"
                "If the row is clearly outside the target polarity, respond with "
                'JSON {"value": "__skip__"} instead of forcing a bad label.\n\n'
                f"{schema_note}\n\n"
                f"Row text:\n{text}"
            )
        raw = llm.complete_json(system, user)
        obj = json.loads(raw)
        val = obj.get("value") if isinstance(obj, dict) else None
        if parsed["shape"] == "flat":
            if val == "__skip__":
                return None
            return val if val in parsed["labels"] else None
        if val == "__skip__":
            return None
        if not isinstance(val, dict):
            return None
        parent = str(val.get("parent", "")).strip()
        child = str(val.get("child", "")).strip()
        pair = (parent, child)
        if pair not in parsed["pairs"]:
            return None
        return {"parent": parent, "child": child}


def _is_ident(name: str) -> bool:
    return bool(name) and name.replace("_", "").isalnum() and not name[0].isdigit()


def _parse_taxonomy(taxonomy: list, taxonomy_shape: str, category_definitions: dict) -> dict | str:
    if taxonomy_shape == "flat":
        labels = [str(x).strip() for x in taxonomy if str(x).strip()]
        if not labels:
            return "Error: taxonomy must contain at least one non-empty label"
        return {
            "shape": "flat",
            "labels": labels,
            "definitions": category_definitions,
            "cache_payload": labels,
        }

    items = []
    pairs = set()
    for entry in taxonomy:
        if not isinstance(entry, dict):
            return "Error: hierarchical taxonomy entries must be objects with parent and child"
        parent = str(entry.get("parent", "")).strip()
        child = str(entry.get("child", "")).strip()
        definition = str(entry.get("definition", "")).strip()
        if not parent or not child:
            return "Error: hierarchical taxonomy entries require parent and child"
        items.append({"parent": parent, "child": child, "definition": definition})
        pairs.add((parent, child))
    if not items:
        return "Error: hierarchical taxonomy must contain at least one parent/child entry"
    return {
        "shape": "hierarchical",
        "items": items,
        "pairs": pairs,
        "definitions": category_definitions,
        "cache_payload": items,
    }


def _prototype_texts(parsed: dict) -> list[str]:
    if parsed["shape"] == "flat":
        texts = []
        for label in parsed["labels"]:
            definition = parsed["definitions"].get(label, "")
            texts.append(f"{label}. {definition}".strip())
        return texts
    return [
        f"{item['parent']}. {item['child']}. {item['definition']}".strip()
        for item in parsed["items"]
    ]


def _infer_target_polarity(goal: str, where: str | None) -> str | None:
    text = " ".join([goal, where or ""]).lower()
    if any(tok in text for tok in ["negative", "complaint", "neg", "label_0", "吐槽", "负面", "差评"]):
        return "negative"
    if any(tok in text for tok in ["positive", "praise", "selling", "pos", "label_2", "label_1", "正面", "卖点", "好评"]):
        return "positive"
    return None
