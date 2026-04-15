"""AI-DB layer: DuckDB workspace + derived-column cache.

This turns CoreCoder from a coding agent into an analytics agent.  The model
is: load tables once, then answer questions via two routes -
  * numeric aggregation  -> DuckDB SQL (sql_query tool)
  * semantic judgement   -> LLM per-row extraction materialized as a real
                            column (derive_column tool), cached so re-runs
                            are free

Both tools share a single Workspace so that a column derived in one turn is
queryable in the next.
"""

from .workspace import Workspace, get_workspace, set_workspace
from .cache import DerivedCache

__all__ = ["Workspace", "get_workspace", "set_workspace", "DerivedCache"]
