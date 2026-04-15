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
