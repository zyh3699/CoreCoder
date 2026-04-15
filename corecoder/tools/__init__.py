"""Tool registry."""

from .bash import BashTool
from .read import ReadFileTool
from .write import WriteFileTool
from .edit import EditFileTool
from .glob_tool import GlobTool
from .grep import GrepTool
from .agent import AgentTool
from .load_table import LoadTableTool
from .sql_query import SqlQueryTool
from .derive_column import DeriveColumnTool
from .export_table import ExportTableTool
from .classify_column import ClassifyColumnTool
from .discover_topics import DiscoverTopicsTool
from .sample_rows import SampleRowsTool
from .discover_taxonomy import DiscoverTaxonomyTool
from .assign_taxonomy import AssignTaxonomyTool
from .discover_issue_phrases import DiscoverIssuePhrasesTool
from .cache_status import CacheStatusTool
from .invalidate_cache import InvalidateCacheTool

ALL_TOOLS = [
    BashTool(),
    ReadFileTool(),
    WriteFileTool(),
    EditFileTool(),
    GlobTool(),
    GrepTool(),
    AgentTool(),
    # AI-DB tools: load tables, then answer either via SQL or via LLM-derived columns.
    LoadTableTool(),
    SqlQueryTool(),
    DeriveColumnTool(),
    ExportTableTool(),
    ClassifyColumnTool(),
    DiscoverTopicsTool(),
    SampleRowsTool(),
    DiscoverTaxonomyTool(),
    AssignTaxonomyTool(),
    DiscoverIssuePhrasesTool(),
    CacheStatusTool(),
    InvalidateCacheTool(),
]


def get_tool(name: str):
    """Look up a tool by name."""
    for t in ALL_TOOLS:
        if t.name == name:
            return t
    return None
