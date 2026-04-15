"""Smoke tests for the AI-DB three-piece set: load_table + sql_query + derive_column.

The LLM call in derive_column is stubbed with a fake that returns a canned
label, so these tests don't need network or API keys.
"""

import json
import tempfile
from pathlib import Path

import pytest

from corecoder.db.workspace import Workspace, set_workspace, reset_workspace
from corecoder.tools import get_tool
import corecoder.tools.sample_rows as sample_rows_mod
import corecoder.tools.assign_taxonomy as assign_taxonomy_mod


# --- fixtures ---


class FakeLLM:
    """Stand-in for corecoder.llm.LLM in derive_column tests."""

    def __init__(self, label_fn=None, model="fake-model"):
        self.model = model
        self._label_fn = label_fn or (lambda user: "other")
        self.calls = 0

    def complete_json(self, system, user):
        self.calls += 1
        return json.dumps({"value": self._label_fn(user)})


class WorkflowLLM:
    """Fake LLM that can handle taxonomy discovery plus assignment."""

    def __init__(self):
        self.model = "workflow-fake"
        self.calls = 0

    def complete_json(self, system, user):
        self.calls += 1
        if "categories, recommended_column_name" in system:
            if '"taxonomy_shape": "hierarchical"' in user:
                return json.dumps(
                    {
                        "categories": [
                            {
                                "parent_label": "usage_issues",
                                "parent_definition": "Problems with how the product feels or behaves during use",
                                "children": [
                                    {"child_label": "pilling", "child_definition": "Product rolls up or forms residue"},
                                    {"child_label": "sticky_heavy", "child_definition": "Texture feels sticky, greasy, or heavy"},
                                    {"child_label": "other", "child_definition": "Other usage issue"},
                                ],
                            },
                            {
                                "parent_label": "expectation_gap",
                                "parent_definition": "Product does not deliver the promised value or result",
                                "children": [
                                    {"child_label": "not_worth_price", "child_definition": "Too expensive for the result"},
                                    {"child_label": "no_effect", "child_definition": "Little or no visible benefit"},
                                    {"child_label": "other", "child_definition": "Other expectation gap"},
                                ],
                            },
                            {
                                "parent_label": "safety_irritation",
                                "parent_definition": "Signs of irritation or poor skin tolerance",
                                "children": [
                                    {"child_label": "stinging_allergy", "child_definition": "Stinging, itching, redness, or allergic reaction"},
                                    {"child_label": "other", "child_definition": "Other irritation complaint"},
                                ],
                            },
                        ],
                        "recommended_column_name": "complaint_angle",
                        "assignment_prompt": "Assign each post to one parent angle and one child angle.",
                        "notes": "Use parent angles for overview and child angles for drill-down.",
                    }
                )
            return json.dumps(
                {
                    "categories": [
                        {"label": "texture_issue", "definition": "Complaints about sticky, heavy, or pilling texture"},
                        {"label": "irritation", "definition": "Complaints about allergy, stinging, or irritation"},
                        {"label": "value", "definition": "Complaints that the product is not worth the price"},
                        {"label": "other", "definition": "Anything else"},
                    ],
                    "recommended_column_name": "complaint_angle",
                    "assignment_prompt": "Classify each post into exactly one complaint angle.",
                    "notes": "Use this taxonomy for full-set assignment.",
                }
            )
        if "phrases, recommended_column_name" in system:
            return json.dumps(
                {
                    "phrases": [
                        {
                            "canonical_phrase": "pilling",
                            "definition": "Product rolls up, pills, or clashes with layering",
                            "variants": ["causes pilling", "rolls up", "pills on skin"],
                            "example_rids": [1],
                            "parent_angle": "usage_issues",
                        },
                        {
                            "canonical_phrase": "sticky_heavy",
                            "definition": "Texture feels sticky, greasy, or heavy",
                            "variants": ["sticky", "greasy", "heavy"],
                            "example_rids": [4],
                            "parent_angle": "usage_issues",
                        },
                        {
                            "canonical_phrase": "not_worth_price",
                            "definition": "Too expensive for the result",
                            "variants": ["too expensive", "not worth the price"],
                            "example_rids": [2],
                            "parent_angle": "expectation_gap",
                        },
                        {
                            "canonical_phrase": "stinging_allergy",
                            "definition": "Stinging, itching, redness, or allergic reaction",
                            "variants": ["sting", "itch", "allergy"],
                            "example_rids": [3],
                            "parent_angle": "safety_irritation",
                        },
                    ],
                    "recommended_column_name": "issue_phrase",
                    "assignment_prompt": "Assign each post to exactly one canonical issue phrase.",
                    "notes": "Use parent_angle for drill-down from broad complaint classes.",
                }
            )
        row_text = user.split("Row text:\n", 1)[-1]
        lowered = row_text.lower()
        if 'Allowed taxonomy pairs:' in user:
            if '"__skip__"' in user and "great" in lowered:
                return json.dumps({"value": "__skip__"})
            if "itch" in lowered or "sting" in lowered:
                return json.dumps({"value": {"parent": "safety_irritation", "child": "stinging_allergy"}})
            if "sticky" in lowered or "greasy" in lowered or "heavy" in lowered:
                child = "pilling" if "pilling" in lowered else "sticky_heavy"
                return json.dumps({"value": {"parent": "usage_issues", "child": child}})
            if "expensive" in lowered or "worth" in lowered or "price" in lowered:
                return json.dumps({"value": {"parent": "expectation_gap", "child": "not_worth_price"}})
            return json.dumps({"value": {"parent": "usage_issues", "child": "other"}})
        if "Target polarity: negative" in user and "great" in lowered:
            return json.dumps({"value": "__skip__"})
        if "allergy" in lowered or "itch" in lowered or "sting" in lowered:
            label = "irritation"
        elif "sticky" in lowered or "pilling" in lowered or "greasy" in lowered:
            label = "texture_issue"
        elif "expensive" in lowered or "worth" in lowered or "price" in lowered:
            label = "value"
        else:
            label = "other"
        return json.dumps({"value": label})


@pytest.fixture
def workspace(tmp_path):
    ws = Workspace(cache_path=tmp_path / "cache.db")
    set_workspace(ws)
    yield ws
    reset_workspace()


@pytest.fixture
def posts_csv(tmp_path):
    p = tmp_path / "posts.csv"
    p.write_text(
        "id,author,content,likes\n"
        "1,alice,I love this product so much,120\n"
        "2,bob,Terrible experience will not buy again,3\n"
        "3,carol,It is okay I guess,15\n"
        "4,dave,Absolutely amazing service,200\n"
    )
    return p


@pytest.fixture
def complaint_posts_csv(tmp_path):
    p = tmp_path / "complaints.csv"
    p.write_text(
        "id,content,sentiment\n"
        "1,This cream feels sticky and causes pilling,neg\n"
        "2,Way too expensive for the effect,neg\n"
        "3,My skin started to sting and itch,neg\n"
        "4,Texture is greasy and heavy,neg\n"
        "5,Actually hydrates pretty well,pos\n"
    )
    return p


# --- load_table ---


def test_load_table_csv(workspace, posts_csv):
    load = get_tool("load_table")
    r = load.execute(file_path=str(posts_csv), table_name="posts")
    assert "Loaded 'posts'" in r
    assert "4 rows" in r
    assert "posts" in workspace.tables
    assert "_rid" in workspace.tables["posts"]["columns"]


def test_load_table_bad_ident(workspace, posts_csv):
    load = get_tool("load_table")
    r = load.execute(file_path=str(posts_csv), table_name="bad name")
    assert "not a valid identifier" in r


def test_load_table_missing_file(workspace):
    load = get_tool("load_table")
    r = load.execute(file_path="/tmp/corecoder_nope.csv", table_name="x")
    assert "not found" in r


# --- sql_query ---


def test_sql_query_count(workspace, posts_csv):
    get_tool("load_table").execute(file_path=str(posts_csv), table_name="posts")
    sql = get_tool("sql_query")
    r = sql.execute(query="SELECT COUNT(*) AS n FROM posts")
    assert "4" in r


def test_sql_query_groupby(workspace, posts_csv):
    get_tool("load_table").execute(file_path=str(posts_csv), table_name="posts")
    sql = get_tool("sql_query")
    r = sql.execute(query="SELECT author, SUM(likes) AS total FROM posts GROUP BY author ORDER BY total DESC")
    assert "dave" in r
    assert "200" in r


def test_sql_query_error(workspace):
    sql = get_tool("sql_query")
    r = sql.execute(query="SELECT * FROM nonexistent_table")
    assert "SQL error" in r


# --- derive_column ---


def _sentiment_from_user_msg(user: str) -> str:
    u = user.lower()
    if "love" in u or "amazing" in u:
        return "pos"
    if "terrible" in u or "not buy" in u:
        return "neg"
    return "neu"


def test_derive_column_dry_run(workspace, posts_csv):
    workspace.llm = FakeLLM(label_fn=_sentiment_from_user_msg)
    get_tool("load_table").execute(file_path=str(posts_csv), table_name="posts")

    derive = get_tool("derive_column")
    r = derive.execute(
        table="posts",
        new_column="sentiment",
        source_columns=["content"],
        prompt="Classify the sentiment of this post.",
        output_type="enum",
        enum_values=["pos", "neg", "neu", "other"],
        sample_size=4,
    )
    assert "DRY RUN" in r
    assert "pos=2" in r
    assert "neg=1" in r
    # dry-run must NOT add the column
    cols = workspace.tables["posts"]["columns"]
    assert "sentiment" not in cols


def test_derive_column_materializes_and_is_queryable(workspace, posts_csv):
    workspace.llm = FakeLLM(label_fn=_sentiment_from_user_msg)
    get_tool("load_table").execute(file_path=str(posts_csv), table_name="posts")

    derive = get_tool("derive_column")
    r = derive.execute(
        table="posts",
        new_column="sentiment",
        source_columns=["content"],
        prompt="Classify the sentiment of this post.",
        output_type="enum",
        enum_values=["pos", "neg", "neu", "other"],
    )
    assert "Wrote column" in r
    assert "sentiment" in workspace.tables["posts"]["columns"]

    # Now aggregate via SQL - this is the whole point
    sql = get_tool("sql_query")
    out = sql.execute(
        query="SELECT sentiment, COUNT(*) AS n FROM posts GROUP BY sentiment ORDER BY n DESC"
    )
    assert "pos" in out
    assert "2" in out


def test_derive_column_cache_hit(workspace, posts_csv):
    fake = FakeLLM(label_fn=_sentiment_from_user_msg)
    workspace.llm = fake
    get_tool("load_table").execute(file_path=str(posts_csv), table_name="posts")

    derive = get_tool("derive_column")
    args = dict(
        table="posts", new_column="sentiment", source_columns=["content"],
        prompt="Classify the sentiment of this post.",
        output_type="enum", enum_values=["pos", "neg", "neu", "other"],
    )
    derive.execute(**args)
    first_calls = fake.calls
    assert first_calls == 4

    # second run: every row should hit cache, no new LLM calls
    derive.execute(**args)
    assert fake.calls == first_calls


def test_derive_column_rejects_invalid_enum(workspace, posts_csv):
    # LLM returns a label not in the allowed set -> value becomes None
    workspace.llm = FakeLLM(label_fn=lambda u: "wtf_not_in_enum")
    get_tool("load_table").execute(file_path=str(posts_csv), table_name="posts")

    derive = get_tool("derive_column")
    r = derive.execute(
        table="posts", new_column="sentiment", source_columns=["content"],
        prompt="x", output_type="enum",
        enum_values=["pos", "neg", "neu", "other"],
        sample_size=4,
    )
    assert "NULL=4" in r


def test_derive_column_missing_table(workspace):
    workspace.llm = FakeLLM()
    derive = get_tool("derive_column")
    r = derive.execute(
        table="no_such_table", new_column="x", source_columns=["a"],
        prompt="x", output_type="string",
    )
    assert "not loaded" in r


def test_sample_rows_random(workspace, complaint_posts_csv):
    get_tool("load_table").execute(file_path=str(complaint_posts_csv), table_name="posts")
    sample = get_tool("sample_rows")
    out = sample.execute(
        table="posts",
        sample_size=2,
        where="sentiment = 'neg'",
        method="random",
        columns=["content", "sentiment"],
    )
    assert "Sampled 2 rows" in out
    assert "population=4" in out
    assert "content" in out


def test_discover_taxonomy(workspace, complaint_posts_csv):
    workspace.llm = WorkflowLLM()
    get_tool("load_table").execute(file_path=str(complaint_posts_csv), table_name="posts")
    discover = get_tool("discover_taxonomy")
    out = discover.execute(
        table="posts",
        text_column="content",
        goal="negative_problem_angles",
        where="sentiment = 'neg'",
        sample_size=4,
        max_categories=4,
    )
    assert "Discovered candidate taxonomy" in out
    assert "texture_issue" in out
    assert "complaint_angle" in out


def test_discover_taxonomy_hierarchical(workspace, complaint_posts_csv):
    workspace.llm = WorkflowLLM()
    get_tool("load_table").execute(file_path=str(complaint_posts_csv), table_name="posts")
    discover = get_tool("discover_taxonomy")
    out = discover.execute(
        table="posts",
        text_column="content",
        goal="negative_problem_angles",
        where="sentiment = 'neg'",
        sample_size=4,
        taxonomy_shape="hierarchical",
        granularity_preference="both",
        max_parent_categories=3,
        max_child_categories_per_parent=3,
    )
    assert "usage_issues" in out
    assert "pilling" in out
    assert '"parent": "usage_issues"' in out


def test_assign_taxonomy_materializes_and_is_queryable(workspace, complaint_posts_csv):
    workspace.llm = WorkflowLLM()
    get_tool("load_table").execute(file_path=str(complaint_posts_csv), table_name="posts")
    assign = get_tool("assign_taxonomy")
    out = assign.execute(
        table="posts",
        text_column="content",
        new_column="complaint_angle",
        goal="negative_problem_angles",
        taxonomy=["texture_issue", "irritation", "value", "other"],
        category_definitions={
            "texture_issue": "Sticky, greasy, heavy, or pilling texture complaints",
            "irritation": "Stinging, allergy, redness, or irritation complaints",
            "value": "Too expensive or not worth the price",
            "other": "Anything else",
        },
        where="sentiment = 'neg'",
    )
    assert 'Wrote column "complaint_angle"' in out
    sql = get_tool("sql_query")
    agg = sql.execute(
        query=(
            "SELECT complaint_angle, COUNT(*) AS n "
            "FROM posts WHERE sentiment = 'neg' GROUP BY complaint_angle ORDER BY n DESC"
        )
    )
    assert "texture_issue" in agg
    assert "irritation" in agg
    assert "value" in agg


def test_assign_taxonomy_hierarchical_materializes_and_is_queryable(workspace, complaint_posts_csv):
    workspace.llm = WorkflowLLM()
    get_tool("load_table").execute(file_path=str(complaint_posts_csv), table_name="posts")
    assign = get_tool("assign_taxonomy")
    out = assign.execute(
        table="posts",
        text_column="content",
        new_column="unused_flat_column",
        new_column_parent="complaint_angle_l1",
        new_column_child="complaint_angle_l2",
        goal="negative_problem_angles",
        taxonomy_shape="hierarchical",
        taxonomy=[
            {"parent": "usage_issues", "child": "pilling", "definition": "Product rolls up or forms residue"},
            {"parent": "usage_issues", "child": "sticky_heavy", "definition": "Texture feels sticky, greasy, or heavy"},
            {"parent": "expectation_gap", "child": "not_worth_price", "definition": "Too expensive for the result"},
            {"parent": "safety_irritation", "child": "stinging_allergy", "definition": "Stinging, itching, redness, or allergy"},
            {"parent": "usage_issues", "child": "other", "definition": "Other usage issue"},
        ],
        where="sentiment = 'neg'",
    )
    assert 'Wrote hierarchical columns "complaint_angle_l1" and "complaint_angle_l2"' in out
    sql = get_tool("sql_query")
    agg = sql.execute(
        query=(
            "SELECT complaint_angle_l1, complaint_angle_l2, COUNT(*) AS n "
            "FROM posts WHERE sentiment = 'neg' GROUP BY 1, 2 ORDER BY n DESC"
        )
    )
    assert "usage_issues" in agg
    assert "pilling" in agg
    assert "sticky_heavy" in agg
    assert "not_worth_price" in agg
    assert "stinging_allergy" in agg


def test_discover_issue_phrases(workspace, complaint_posts_csv):
    workspace.llm = WorkflowLLM()
    get_tool("load_table").execute(file_path=str(complaint_posts_csv), table_name="posts")
    phrase_tool = get_tool("discover_issue_phrases")
    out = phrase_tool.execute(
        table="posts",
        text_column="content",
        goal="negative_issue_phrases",
        where="sentiment = 'neg'",
        parent_angle_column="sentiment",
        parent_angle_value="neg",
        sample_size=4,
        max_phrases=4,
        phrase_style="canonical_issue",
    )
    assert "Discovered candidate issue phrases" in out
    assert "pilling" in out
    assert "sticky_heavy" in out
    assert '"label": "stinging_allergy"' in out


def test_assign_taxonomy_skips_off_polarity_rows(workspace, tmp_path):
    p = tmp_path / "mixed.csv"
    p.write_text(
        "id,content,sentiment\n"
        "1,This cream is great and absorbs well,neg\n"
        "2,Way too expensive for the effect,neg\n"
        "3,My skin started to sting and itch,neg\n"
    )
    workspace.llm = WorkflowLLM()
    get_tool("load_table").execute(file_path=str(p), table_name="posts")
    out = get_tool("assign_taxonomy").execute(
        table="posts",
        text_column="content",
        new_column="issue_phrase",
        goal="negative_issue_phrases",
        taxonomy=["texture_issue", "irritation", "value", "other"],
        category_definitions={
            "texture_issue": "Sticky, greasy, heavy, or pilling texture complaints",
            "irritation": "Stinging, allergy, redness, or irritation complaints",
            "value": "Too expensive or not worth the price",
            "other": "Anything else",
        },
        where="sentiment = 'neg'",
        rerun_mode="refresh",
    )
    assert 'errors (NULL):' in out
    sql = get_tool("sql_query")
    agg = sql.execute(
        query="SELECT issue_phrase, COUNT(*) AS n FROM posts GROUP BY 1 ORDER BY n DESC"
    )
    assert "value" in agg
    assert "irritation" in agg
    assert "NULL" in agg


def test_cache_status_and_invalidate_cache(workspace, complaint_posts_csv):
    workspace.llm = WorkflowLLM()
    get_tool("load_table").execute(file_path=str(complaint_posts_csv), table_name="posts")
    assign = get_tool("assign_taxonomy")
    assign.execute(
        table="posts",
        text_column="content",
        new_column="issue_phrase",
        goal="negative_issue_phrases",
        taxonomy=["texture_issue", "irritation", "value", "other"],
        category_definitions={
            "texture_issue": "Sticky, greasy, heavy, or pilling texture complaints",
            "irritation": "Stinging, allergy, redness, or irritation complaints",
            "value": "Too expensive or not worth the price",
            "other": "Anything else",
        },
        where="sentiment = 'neg'",
    )
    status = get_tool("cache_status")
    out = status.execute(table="posts", column="issue_phrase")
    assert "issue_phrase" in out
    invalid = get_tool("invalidate_cache")
    cleared = invalid.execute(table="posts", column="issue_phrase")
    assert "Deleted cache rows:" in cleared
    out2 = status.execute(table="posts", column="issue_phrase")
    assert "No matching cache entries" in out2


def test_materialize_subset(workspace, complaint_posts_csv):
    get_tool("load_table").execute(file_path=str(complaint_posts_csv), table_name="posts")
    tool = get_tool("materialize_subset")
    out = tool.execute(
        source_table="posts",
        new_table="neg_posts",
        where="sentiment = 'neg'",
    )
    assert "Materialized subset 'neg_posts'" in out
    assert "4 rows" in out
    assert "neg_posts" in workspace.tables


def test_sample_rows_diverse(monkeypatch, workspace, complaint_posts_csv):
    get_tool("load_table").execute(file_path=str(complaint_posts_csv), table_name="posts")

    def fake_encode(texts, model_name=None):
        return [[float(i), 0.0] for i, _ in enumerate(texts)]

    monkeypatch.setattr(sample_rows_mod, "encode_texts", fake_encode)
    out = get_tool("sample_rows").execute(
        table="posts",
        sample_size=2,
        where="sentiment = 'neg'",
        method="diverse",
        text_column="content",
        columns=["content"],
    )
    assert "method=diverse" in out
    assert "Diverse sampling via embeddings" in out


def test_assign_taxonomy_embed_then_llm(monkeypatch, workspace, complaint_posts_csv):
    workspace.llm = WorkflowLLM()
    get_tool("load_table").execute(file_path=str(complaint_posts_csv), table_name="posts")

    def fake_encode(texts, model_name=None):
        vecs = []
        for text in texts:
            lower = text.lower()
            if "sticky" in lower or "pilling" in lower or "greasy" in lower or "heavy" in lower:
                vecs.append([1.0, 0.0, 0.0])
            elif "itch" in lower or "sting" in lower:
                vecs.append([0.0, 1.0, 0.0])
            else:
                vecs.append([0.0, 0.0, 1.0])
        return vecs

    monkeypatch.setattr(assign_taxonomy_mod, "encode_texts", fake_encode)
    out = get_tool("assign_taxonomy").execute(
        table="posts",
        text_column="content",
        new_column="issue_phrase",
        goal="negative_issue_phrases",
        taxonomy=["texture_issue", "irritation", "value"],
        category_definitions={
            "texture_issue": "sticky greasy heavy pilling texture",
            "irritation": "sting itch allergy irritation",
            "value": "expensive not worth the price",
        },
        where="sentiment = 'neg'",
        routing_mode="embed_then_llm",
        confidence_threshold=0.8,
    )
    assert "embed assigns:" in out
    sql = get_tool("sql_query")
    agg = sql.execute(
        query="SELECT issue_phrase, COUNT(*) AS n FROM posts WHERE sentiment = 'neg' GROUP BY 1 ORDER BY n DESC"
    )
    assert "texture_issue" in agg
