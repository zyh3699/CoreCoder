"""Materialize an LLM judgement as a real SQL column.

The core trick of the AI-DB: ask the model once per row (cached, parallel),
then let every later question go through SQL over the labelled column. This
is what separates "a database" from "a chatbot reading a CSV".

Flow:
  1. Fetch the source columns for every row (via _rid ordering).
  2. For each row, compute the cache key; hit -> reuse; miss -> queue.
  3. Run cache-miss labels in parallel via response_format=json_object.
  4. Validate each value against the declared output_type (enum/num/bool/str).
  5. Rebuild the table with the new column, joined back by _rid.
  6. Return a label-distribution summary so the operator can eyeball it.

sample_size triggers dry-run mode: only N rows, no write, preview + dist.
"""

from __future__ import annotations

import concurrent.futures
import json
from collections import Counter

from .base import Tool
from ..db.workspace import get_workspace

_MAX_WORKERS = 8
_PREVIEW_ROWS = 10

_SQL_TYPE = {
    "enum": "VARCHAR",
    "string": "VARCHAR",
    "number": "DOUBLE",
    "boolean": "BOOLEAN",
}


class DeriveColumnTool(Tool):
    name = "derive_column"
    description = (
        "Add a new column to a table by running an LLM prompt over every row, "
        "then materializing the result as a real SQL column.  Use this for "
        "sentiment, topic, category, named-entity extraction, or any other "
        "per-row judgement that later needs to be aggregated.  Results are "
        "cached by (row content, prompt, schema, model) so re-running is free. "
        "ALWAYS run once with sample_size=20 first to sanity-check the labels "
        "before labelling the whole table."
    )
    parameters = {
        "type": "object",
        "properties": {
            "table": {"type": "string", "description": "Source table name"},
            "new_column": {
                "type": "string",
                "description": "Name of the column to add (must be a valid SQL identifier)",
            },
            "source_columns": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Which columns to feed into the prompt as context for each row",
            },
            "prompt": {
                "type": "string",
                "description": (
                    "The labelling instruction.  The row's source columns are "
                    "appended as JSON after the prompt.  Be specific about "
                    "what the label means."
                ),
            },
            "output_type": {
                "type": "string",
                "enum": ["enum", "string", "number", "boolean"],
                "description": (
                    "Output type.  'enum' is strongly preferred for anything "
                    "categorical - it keeps the column group-by-able."
                ),
            },
            "enum_values": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Allowed labels when output_type='enum'. Include 'other' as a catch-all.",
            },
            "sample_size": {
                "type": "integer",
                "description": (
                    "Dry-run mode: only label this many rows and DO NOT write "
                    "the column.  Use this first to check the taxonomy."
                ),
            },
            "rerun_mode": {
                "type": "string",
                "enum": ["reuse", "refresh", "no_write_cache"],
                "description": "Whether to reuse cache, refresh labels, or skip writing new cache entries.",
            },
        },
        "required": ["table", "new_column", "source_columns", "prompt", "output_type"],
    }

    def execute(
        self,
        table: str,
        new_column: str,
        source_columns: list[str],
        prompt: str,
        output_type: str,
        enum_values: list[str] | None = None,
        sample_size: int | None = None,
        rerun_mode: str = "reuse",
    ) -> str:
        if not _is_ident(table) or not _is_ident(new_column):
            return "Error: table and new_column must be valid SQL identifiers"

        if output_type not in _SQL_TYPE:
            return f"Error: output_type must be one of {list(_SQL_TYPE)}"
        if output_type == "enum" and not enum_values:
            return "Error: enum_values required when output_type='enum'"

        ws = get_workspace()
        if table not in ws.tables:
            return f"Error: table '{table}' not loaded. Call load_table first."
        if ws.llm is None:
            return "Error: no LLM attached to workspace (cannot call derive_column)"

        existing_cols = ws.tables[table]["columns"]
        missing = [c for c in source_columns if c not in existing_cols]
        if missing:
            return f"Error: source columns not found in {table}: {missing}"

        schema_str = json.dumps(
            {"type": output_type, "enum": enum_values}, sort_keys=True, ensure_ascii=False
        )
        if rerun_mode not in {"reuse", "refresh", "no_write_cache"}:
            return "Error: rerun_mode must be one of reuse, refresh, no_write_cache"
        model = ws.llm.model
        schema_hash = ws.cache.hash_text(schema_str)
        source_cols_str = ",".join(source_columns)

        col_list = ", ".join(f'"{c}"' for c in source_columns)
        base_q = f'SELECT _rid, {col_list} FROM "{table}" ORDER BY _rid'
        if sample_size:
            rows = ws.conn.execute(base_q + " LIMIT ?", [int(sample_size)]).fetchall()
        else:
            rows = ws.conn.execute(base_q).fetchall()

        # partition into cache hits vs. work
        hits: dict[int, object] = {}
        work: list[tuple[int, str, str]] = []  # (rid, content_json, cache_key)
        for r in rows:
            rid = r[0]
            content = dict(zip(source_columns, r[1:]))
            content_json = json.dumps(content, ensure_ascii=False, sort_keys=True, default=str)
            key = ws.cache.make_key(table, content_json, prompt, schema_str, model)
            cached = None if rerun_mode != "reuse" else ws.cache.get(key)
            if cached is not None:
                hits[rid] = cached
            else:
                work.append((rid, content_json, key))

        # parallel LLM calls for cache misses
        results = dict(hits)
        errors = 0
        if work:
            with concurrent.futures.ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
                future_to_row = {
                    pool.submit(
                        self._label_one,
                        ws.llm,
                        content_json,
                        prompt,
                        output_type,
                        enum_values,
                    ): (rid, content_json, key)
                    for (rid, content_json, key) in work
                }
                for fut in concurrent.futures.as_completed(future_to_row):
                    rid, content_json, key = future_to_row[fut]
                    try:
                        value = fut.result()
                    except Exception:
                        value = None
                    results[rid] = value
                    if value is None:
                        errors += 1
                    elif rerun_mode != "no_write_cache":
                        ws.cache.put(
                            key,
                            table,
                            new_column,
                            str(rid),
                            value,
                            model,
                            tool_name=self.name,
                            goal=new_column,
                            schema_hash=schema_hash,
                            prompt_preview=prompt[:160],
                            source_columns=source_cols_str,
                        )

        values_list = [results.get(r[0]) for r in rows]
        dist = _distribution(values_list)

        if sample_size:
            return _render_preview(
                rows, source_columns, results, dist,
                n_cached=len(hits), n_llm=len(work), n_errors=errors,
            )

        # write the column back: rebuild table via JOIN on _rid
        sql_type = _SQL_TYPE[output_type]
        try:
            ws.conn.execute(
                "CREATE OR REPLACE TEMP TABLE __derive_staging (_rid BIGINT, val "
                + sql_type + ")"
            )
            ws.conn.executemany(
                "INSERT INTO __derive_staging VALUES (?, ?)",
                [(rid, results.get(rid)) for rid in (r[0] for r in rows)],
            )
            # drop then add keeps this idempotent across re-runs
            ws.conn.execute(
                f'ALTER TABLE "{table}" DROP COLUMN IF EXISTS "{new_column}"'
            )
            ws.conn.execute(
                f'ALTER TABLE "{table}" ADD COLUMN "{new_column}" {sql_type}'
            )
            ws.conn.execute(
                f'UPDATE "{table}" SET "{new_column}" = s.val '
                f'FROM __derive_staging s WHERE "{table}"._rid = s._rid'
            )
            ws.conn.execute("DROP TABLE IF EXISTS __derive_staging")
        except Exception as e:
            return f"Error materializing column: {e}"

        ws.refresh_table(table)
        return _render_final(
            table, new_column, total=len(rows), cached=len(hits),
            llm_calls=len(work), errors=errors, dist=dist,
        )

    # ------------------------------------------------------------------

    @staticmethod
    def _label_one(llm, content_json, prompt, output_type, enum_values):
        if output_type == "enum":
            schema_note = (
                f'Respond with JSON: {{"value": <one of {enum_values}>}}. '
                f"Pick exactly one. If none fit, pick 'other' if available."
            )
        elif output_type == "number":
            schema_note = 'Respond with JSON: {"value": <number>}.'
        elif output_type == "boolean":
            schema_note = 'Respond with JSON: {"value": <true|false>}.'
        else:
            schema_note = 'Respond with JSON: {"value": <short string>}.'

        system = (
            "You label rows for a database.  Output ONLY a single valid JSON "
            "object with a 'value' field.  No prose, no code fences."
        )
        user = f"{prompt}\n\n{schema_note}\n\nRow:\n{content_json}"

        raw = llm.complete_json(system, user)
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            return None
        val = obj.get("value") if isinstance(obj, dict) else None

        if output_type == "enum":
            return val if val in (enum_values or []) else None
        if output_type == "number":
            return val if isinstance(val, (int, float)) and not isinstance(val, bool) else None
        if output_type == "boolean":
            return val if isinstance(val, bool) else None
        return val if isinstance(val, str) else None


def _is_ident(name: str) -> bool:
    return bool(name) and name.replace("_", "").isalnum() and not name[0].isdigit()


def _distribution(values) -> list[tuple[str, int]]:
    c = Counter("NULL" if v is None else str(v) for v in values)
    return c.most_common()


def _render_preview(rows, source_columns, results, dist, n_cached, n_llm, n_errors):
    lines = [
        f"[DRY RUN - {len(rows)} rows previewed, column NOT written]",
        f"  cache hits: {n_cached}   llm calls: {n_llm}   errors: {n_errors}",
        "",
        "Sample labels:",
    ]
    for r in rows[:_PREVIEW_ROWS]:
        rid = r[0]
        content = dict(zip(source_columns, r[1:]))
        snippet = json.dumps(content, ensure_ascii=False, default=str)
        if len(snippet) > 120:
            snippet = snippet[:117] + "..."
        lines.append(f"  _rid={rid}  -> {results.get(rid)!r}   {snippet}")
    lines.append("")
    lines.append("Label distribution: " + ", ".join(f"{k}={v}" for k, v in dist))
    lines.append("")
    lines.append(
        "If the labels look right, re-run without sample_size to label the full table."
    )
    return "\n".join(lines)


def _render_final(table, new_column, total, cached, llm_calls, errors, dist):
    lines = [
        f"Wrote column \"{new_column}\" into \"{table}\": {total} rows",
        f"  cache hits: {cached}   llm calls: {llm_calls}   errors (NULL): {errors}",
        "Label distribution: " + ", ".join(f"{k}={v}" for k, v in dist),
        "",
        f"Now queryable via sql_query, e.g.:",
        f'  SELECT "{new_column}", COUNT(*) FROM "{table}" GROUP BY 1 ORDER BY 2 DESC',
    ]
    return "\n".join(lines)
