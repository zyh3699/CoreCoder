"""Discover topics in a text column using BERTopic (no per-row LLM calls).

Flow:
  1. Pull rows from DuckDB (with optional WHERE filter).
  2. Embed with a local Sentence Transformer (zero API cost).
  3. Cluster with BERTopic -> each cluster = one topic candidate.
  4. Call LLM *once per topic* (not once per row) to generate a short label.
  5. Write topic_id (int) and topic_label (str) back as two new SQL columns.
  6. Return a distribution table so the operator can see the breakdown.

Typical usage: filter to negative posts first, then discover what they discuss.
  discover_topics(table="posts", text_column="content",
                  where="sentiment = 'neg'", n_topics=8)
"""

from __future__ import annotations

import json

from .base import Tool
from ..db.workspace import get_workspace

_DEFAULT_EMBED_MODEL = "shibing624/text2vec-base-chinese"
_OUTLIER_LABEL = "other"


class DiscoverTopicsTool(Tool):
    name = "discover_topics"
    description = (
        "Discover discussion topics in a text column via BERTopic (local embeddings + "
        "clustering).  Calls the LLM only once per discovered topic to generate a "
        "human-readable label — NOT once per row.  Writes topic_id and topic_label "
        "columns back to the table so sql_query can aggregate them. "
        "Use where= to focus on a subset (e.g. WHERE sentiment='neg')."
    )
    parameters = {
        "type": "object",
        "properties": {
            "table": {"type": "string", "description": "Source table name"},
            "text_column": {
                "type": "string",
                "description": "Column containing the text to analyse",
            },
            "where": {
                "type": "string",
                "description": (
                    "Optional SQL WHERE clause to filter rows before analysis "
                    "(e.g. \"sentiment = 'neg'\"). Do NOT include the word WHERE."
                ),
            },
            "n_topics": {
                "type": "integer",
                "description": (
                    "Approximate number of topics to discover (default: auto). "
                    "BERTopic may return slightly more or fewer."
                ),
            },
            "topic_id_column": {
                "type": "string",
                "description": "Column name for the numeric topic id (default: topic_id)",
            },
            "topic_label_column": {
                "type": "string",
                "description": "Column name for the LLM-named topic label (default: topic_label)",
            },
            "embed_model": {
                "type": "string",
                "description": (
                    f"HuggingFace sentence-transformer model for embeddings. "
                    f"Default: {_DEFAULT_EMBED_MODEL}"
                ),
            },
        },
        "required": ["table", "text_column"],
    }

    # module-level cache so the embedding model is only loaded once
    _embed_cache: dict[str, object] = {}

    def execute(
        self,
        table: str,
        text_column: str,
        where: str | None = None,
        n_topics: int | None = None,
        topic_id_column: str = "topic_id",
        topic_label_column: str = "topic_label",
        embed_model: str = _DEFAULT_EMBED_MODEL,
    ) -> str:
        try:
            from bertopic import BERTopic
            from sentence_transformers import SentenceTransformer
        except ImportError:
            return (
                "Error: bertopic and sentence-transformers are required: "
                "pip install bertopic sentence-transformers"
            )

        ws = get_workspace()
        if table not in ws.tables:
            return f"Error: table '{table}' not loaded. Call load_table first."
        if text_column not in ws.tables[table]["columns"]:
            return f"Error: column '{text_column}' not in table '{table}'"

        # 1. fetch rows
        q = f'SELECT _rid, "{text_column}" FROM "{table}"'
        if where:
            q += f" WHERE {where}"
        q += " ORDER BY _rid"
        rows = ws.conn.execute(q).fetchall()

        if not rows:
            return "Error: WHERE filter returned 0 rows — nothing to cluster"
        if len(rows) < 5:
            return f"Error: only {len(rows)} rows after filter — need at least 5 for clustering"

        rids = [r[0] for r in rows]
        texts = [str(r[1]) if r[1] is not None else "" for r in rows]

        # 2. embed (cached per model name so we don't reload on each call)
        if embed_model not in self._embed_cache:
            self._embed_cache[embed_model] = SentenceTransformer(embed_model)
        embedder = self._embed_cache[embed_model]
        embeddings = embedder.encode(texts, show_progress_bar=False)

        # 3. BERTopic clustering
        nr_topics = n_topics if n_topics else "auto"
        topic_model = BERTopic(
            language="multilingual",
            nr_topics=nr_topics,
            verbose=False,
            calculate_probabilities=False,
        )
        topic_ids, _ = topic_model.fit_transform(texts, embeddings)
        topic_info = topic_model.get_topic_info()

        # 4. name each topic with one LLM call per topic
        topic_labels = _build_labels(topic_model, topic_info, ws.llm)

        # 5. write columns back (only the filtered rows get labels;
        #    rows outside the WHERE keep NULL so the user can tell them apart)
        _rid_to_id = dict(zip(rids, topic_ids))
        _rid_to_label = {
            rid: topic_labels.get(tid, _OUTLIER_LABEL)
            for rid, tid in _rid_to_id.items()
        }

        try:
            ws.conn.execute(
                "CREATE OR REPLACE TEMP TABLE __topic_staging "
                "(_rid BIGINT, t_id INTEGER, t_label VARCHAR)"
            )
            ws.conn.executemany(
                "INSERT INTO __topic_staging VALUES (?, ?, ?)",
                [
                    (rid, _rid_to_id[rid], _rid_to_label[rid])
                    for rid in rids
                ],
            )
            for col, src in ((topic_id_column, "t_id"), (topic_label_column, "t_label")):
                ws.conn.execute(
                    f'ALTER TABLE "{table}" DROP COLUMN IF EXISTS "{col}"'
                )
                ws.conn.execute(
                    f'ALTER TABLE "{table}" ADD COLUMN "{col}" '
                    + ("INTEGER" if src == "t_id" else "VARCHAR")
                )
                ws.conn.execute(
                    f'UPDATE "{table}" SET "{col}" = s.{src} '
                    f'FROM __topic_staging s WHERE "{table}"._rid = s._rid'
                )
            ws.conn.execute("DROP TABLE IF EXISTS __topic_staging")
        except Exception as e:
            return f"Error writing columns: {e}"

        ws.refresh_table(table)

        # 6. build distribution summary
        dist_rows = ws.conn.execute(
            f'SELECT "{topic_label_column}", COUNT(*) AS n '
            f'FROM "{table}" WHERE "{topic_label_column}" IS NOT NULL '
            f'GROUP BY 1 ORDER BY 2 DESC'
        ).fetchall()

        lines = [
            f"Discovered {len(topic_labels)} topics across {len(rows)} rows",
            f"Wrote columns: '{topic_id_column}' (int), '{topic_label_column}' (str)",
            "",
            "Topic distribution:",
        ]
        total = sum(r[1] for r in dist_rows)
        for label, n in dist_rows:
            pct = n * 100 / total if total else 0
            lines.append(f"  {label:<30}  {n:>5}  ({pct:.1f}%)")

        lines += [
            "",
            "Query example:",
            f'  SELECT "{topic_label_column}", COUNT(*) AS n',
            f'  FROM "{table}"',
            f'  WHERE "{topic_label_column}" != \'{_OUTLIER_LABEL}\'',
            f'  GROUP BY 1 ORDER BY 2 DESC',
        ]
        return "\n".join(lines)


def _build_labels(
    topic_model,
    topic_info,
    llm,
) -> dict[int, str]:
    """Return {topic_id: label_str}.  -1 (outlier) always maps to 'other'."""
    labels: dict[int, str] = {-1: _OUTLIER_LABEL}

    for _, row in topic_info.iterrows():
        tid = int(row["Topic"])
        if tid == -1:
            continue

        # BERTopic gives us top keywords for free — use them as the label
        # unless we have an LLM to generate something more readable
        keywords = [kw for kw, _ in topic_model.get_topic(tid)][:8]
        keyword_str = "、".join(keywords)

        if llm is None:
            labels[tid] = keyword_str
            continue

        # one LLM call per topic — cheap (K calls, not N)
        system = (
            "你是一个数据分析师，帮助用户给话题聚类起简短的名字。"
            "只输出JSON：{\"label\": \"<5字以内的话题名>\"}，不要其他内容。"
        )
        user = (
            f"以下是从用户评论中聚类出的一个话题，代表性关键词为：{keyword_str}\n"
            f"代表性原文（前3条）：\n"
        )
        # add up to 3 representative docs
        docs = topic_model.get_representative_docs(tid)
        for doc in (docs or [])[:3]:
            user += f"- {str(doc)[:100]}\n"
        user += '\n请给这个话题起一个简短的中文名（5字以内）。'

        try:
            raw = llm.complete_json(system, user)
            obj = json.loads(raw)
            label = obj.get("label") or keyword_str
        except Exception:
            label = keyword_str

        labels[tid] = str(label)[:20]  # hard cap so column stays clean

    return labels
