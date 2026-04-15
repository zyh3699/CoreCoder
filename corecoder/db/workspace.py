"""Shared AI-DB workspace.

A single Workspace holds:
  * a DuckDB connection (the analytical engine)
  * a DerivedCache (persistent memo of LLM judgements)
  * a reference to the LLM (so derive_column can call it)
  * a small table registry for schema introspection

Tools (load_table / sql_query / derive_column) reach the workspace via the
module-level `get_workspace()` singleton instead of dependency injection.
This matches the style of the rest of CoreCoder: globals are fine for a
single-process agent.
"""

from __future__ import annotations

import os
from pathlib import Path

import duckdb

from .cache import DerivedCache


def _default_cache_path() -> Path:
    override = os.getenv("CORECODER_CACHE_PATH")
    if override:
        return Path(override).expanduser()
    # project-local by default - keeps caches scoped to the dataset
    return Path.cwd() / ".corecoder" / "cache.db"


class Workspace:
    def __init__(self, cache_path: Path | None = None, llm=None):
        self.conn = duckdb.connect(":memory:")
        self.cache = DerivedCache(cache_path or _default_cache_path())
        self.llm = llm
        self.tables: dict[str, dict] = {}

    def register_table(self, name: str, source: str, n_rows: int, columns: list[str]):
        self.tables[name] = {"source": source, "n_rows": n_rows, "columns": columns}

    def refresh_table(self, name: str):
        """Re-read row count + columns from DuckDB (after derive_column adds a col)."""
        n = self.conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
        cols = [r[0] for r in self.conn.execute(f'DESCRIBE "{name}"').fetchall()]
        src = self.tables.get(name, {}).get("source", "<unknown>")
        self.tables[name] = {"source": src, "n_rows": n, "columns": cols}

    def describe(self) -> str:
        if not self.tables:
            return "(no tables loaded)"
        lines = []
        for name, meta in self.tables.items():
            lines.append(
                f"{name}: {meta['n_rows']} rows, columns={meta['columns']}  [source: {meta['source']}]"
            )
        return "\n".join(lines)

    def close(self) -> None:
        """Close the in-memory DuckDB connection."""
        try:
            self.conn.close()
        except Exception:
            pass


_WORKSPACE: Workspace | None = None


def get_workspace() -> Workspace:
    """Return the process-wide workspace, creating it lazily."""
    global _WORKSPACE
    if _WORKSPACE is None:
        _WORKSPACE = Workspace()
    return _WORKSPACE


def set_workspace(ws: Workspace) -> None:
    global _WORKSPACE
    _WORKSPACE = ws


def reset_workspace() -> None:
    """Used by tests to get a clean slate."""
    global _WORKSPACE
    if _WORKSPACE is not None:
        _WORKSPACE.close()
    _WORKSPACE = None
