"""Classify a text column using a local model (no LLM, no token cost).

Currently supports:
  - sentiment-zh : Erlangshen-Roberta-330M-Sentiment (binary CN sentiment -> pos/neg)

Results are cached by the same key scheme as derive_column, so re-running on
an unchanged table is free.  Batch inference makes this ~10x faster than
one-shot LLM calls.
"""

from __future__ import annotations

import json
from pathlib import Path

from .base import Tool
from ..db.workspace import get_workspace

_SUPPORTED = ["sentiment-zh"]


class ClassifyColumnTool(Tool):
    name = "classify_column"
    description = (
        "Classify a text column using a local model — no LLM calls, no token cost, "
        "fast batch inference.  Results are cached and written as a real SQL column. "
        f"Supported model_name values: {_SUPPORTED}."
    )
    parameters = {
        "type": "object",
        "properties": {
            "table": {"type": "string", "description": "Source table name"},
            "source_column": {
                "type": "string",
                "description": "Text column to classify",
            },
            "new_column": {
                "type": "string",
                "description": "Name of the output column to add",
            },
            "model_name": {
                "type": "string",
                "enum": _SUPPORTED,
                "description": "Which built-in model to use",
            },
            "batch_size": {
                "type": "integer",
                "description": "Rows per inference batch (default 32)",
            },
            "rerun_mode": {
                "type": "string",
                "enum": ["reuse", "refresh", "no_write_cache"],
                "description": "Whether to reuse cache, refresh results, or skip writing new cache entries.",
            },
        },
        "required": ["table", "source_column", "new_column", "model_name"],
    }

    # loaded lazily on first use
    _erlangshen: tuple | None = None

    def execute(
        self,
        table: str,
        source_column: str,
        new_column: str,
        model_name: str,
        batch_size: int = 32,
        rerun_mode: str = "reuse",
    ) -> str:
        ws = get_workspace()
        if table not in ws.tables:
            return f"Error: table '{table}' not loaded. Call load_table first."

        cols = ws.tables[table]["columns"]
        if source_column not in cols:
            return f"Error: column '{source_column}' not in table '{table}'"

        if model_name not in _SUPPORTED:
            return f"Error: unknown model '{model_name}'. Supported: {_SUPPORTED}"
        if rerun_mode not in {"reuse", "refresh", "no_write_cache"}:
            return "Error: rerun_mode must be one of reuse, refresh, no_write_cache"

        # fetch rows
        rows = ws.conn.execute(
            f'SELECT _rid, "{source_column}" FROM "{table}" ORDER BY _rid'
        ).fetchall()

        schema_str = json.dumps({"model": model_name}, sort_keys=True)
        schema_hash = ws.cache.hash_text(schema_str)

        # partition into cache hits vs. work
        hits: dict[int, str] = {}
        work: list[tuple[int, str]] = []  # (rid, text)
        for rid, text in rows:
            text_str = "" if text is None else str(text)
            key = ws.cache.make_key(table, text_str, model_name, schema_str, model_name)
            cached = None if rerun_mode != "reuse" else ws.cache.get(key)
            if cached is not None:
                hits[rid] = cached
            else:
                work.append((rid, text_str))

        # batch inference for cache misses
        new_labels: dict[int, str] = {}
        if work:
            infer = self._get_infer_fn(model_name)
            if isinstance(infer, str):
                return infer  # error message

            rids = [r for r, _ in work]
            texts = [t for _, t in work]

            for i in range(0, len(texts), batch_size):
                batch_rids = rids[i : i + batch_size]
                batch_texts = texts[i : i + batch_size]
                try:
                    labels = infer(batch_texts)
                except Exception as e:
                    return f"Inference error: {e}"
                for rid, text_str, label in zip(batch_rids, batch_texts, labels):
                    new_labels[rid] = label
                    key = ws.cache.make_key(
                        table, text_str, model_name, schema_str, model_name
                    )
                    if rerun_mode != "no_write_cache":
                        ws.cache.put(
                            key,
                            table,
                            new_column,
                            str(rid),
                            label,
                            model_name,
                            tool_name=self.name,
                            goal=new_column,
                            schema_hash=schema_hash,
                            prompt_preview=model_name,
                            source_columns=source_column,
                        )

        results = {**hits, **new_labels}

        # write column back via _rid
        try:
            ws.conn.execute(
                "CREATE OR REPLACE TEMP TABLE __cls_staging (_rid BIGINT, val VARCHAR)"
            )
            ws.conn.executemany(
                "INSERT INTO __cls_staging VALUES (?, ?)",
                [(rid, results.get(rid)) for rid, _ in rows],
            )
            ws.conn.execute(
                f'ALTER TABLE "{table}" DROP COLUMN IF EXISTS "{new_column}"'
            )
            ws.conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{new_column}" VARCHAR')
            ws.conn.execute(
                f'UPDATE "{table}" SET "{new_column}" = s.val '
                f'FROM __cls_staging s WHERE "{table}"._rid = s._rid'
            )
            ws.conn.execute("DROP TABLE IF EXISTS __cls_staging")
        except Exception as e:
            return f"Error writing column: {e}"

        ws.refresh_table(table)

        from collections import Counter
        dist = Counter(results.values()).most_common()
        dist_str = ", ".join(f"{k}={v}" for k, v in dist)
        return (
            f"Wrote column \"{new_column}\" into \"{table}\": {len(rows)} rows\n"
            f"  cache hits: {len(hits)}   inferred: {len(new_labels)}\n"
            f"Distribution: {dist_str}\n\n"
            f"Now queryable, e.g.:\n"
            f'  SELECT "{new_column}", COUNT(*) FROM "{table}" GROUP BY 1 ORDER BY 2 DESC'
        )

    def _get_infer_fn(self, model_name: str):
        if model_name == "sentiment-zh":
            return self._load_erlangshen()
        return f"Error: unknown model '{model_name}'"

    def _load_erlangshen(self):
        if ClassifyColumnTool._erlangshen is not None:
            _, infer_fn = ClassifyColumnTool._erlangshen
            return infer_fn

        try:
            from transformers import AutoTokenizer, AutoModelForSequenceClassification
            import torch
        except ImportError:
            return "Error: transformers and torch required: pip install transformers torch"

        _HF_NAME = "sanshizhang/Chinese-Sentiment-Analysis-Fund-Direction"
        tokenizer = AutoTokenizer.from_pretrained(_HF_NAME)
        model = AutoModelForSequenceClassification.from_pretrained(_HF_NAME)
        model.eval()

        def infer(texts: list[str]) -> list[str]:
            import torch
            enc = tokenizer(
                texts,
                return_tensors="pt",
                padding=True,
                truncation=True,
                max_length=512,
            )
            with torch.no_grad():
                logits = model(**enc).logits
            indices = torch.argmax(logits, dim=-1).tolist()
            return [model.config.id2label[i] for i in indices]

        ClassifyColumnTool._erlangshen = (model, infer)
        return infer
