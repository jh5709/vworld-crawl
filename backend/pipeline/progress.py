"""
Pipeline progress parser — extracts per-node stats from duckle's stdout.
"""

import re
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class NodeProgress:
    """Progress for a single pipeline node."""
    name: str           # e.g. "spatial", "dropcol", "geomvalidate"
    status: str = "pending"  # pending | running | done | error
    rows: int = 0
    error: str = ""


@dataclass 
class PipelineProgress:
    """Overall pipeline progress."""
    phase: str = ""             # "valid" | "invalid" | "complete"
    file_index: int = 0
    total_files: int = 0
    file_name: str = ""
    nodes: list[NodeProgress] = field(default_factory=list)
    rows_loaded: int = 0
    rows_rejected: int = 0
    error: str = ""


# Predefined pipeline node sequence (matches the runner's order)
VALID_NODES = [
    "spatial",
    "dropcol",
    "project", 
    "rename",
    "sql",
    "geomvalidate",
    "ducklake",
]


def parse_duckle_output(stdout: str) -> list[NodeProgress]:
    """
    Parse duckle's stdout into per-node progress.

    Example input:
        status   : ok
        duration : 302 ms
          dropcol              ok (3 rows)
          ducklake             ok (2 rows)
          geomvalidate         ok (2 rows)
          ...

    Returns a list of NodeProgress, ordered by appearance.
    """
    nodes: list[NodeProgress] = []
    
    for line in stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("status") or line.startswith("duration") or line.startswith("duckle-runner"):
            continue
        
        # Match: "  dropcol              ok (3 rows)" or "  spatial              error"
        m = re.match(r"(\S+)\s+(ok|error)(?:\s+\((\d+)\s+rows?\))?", line)
        if m:
            name = m.group(1)
            status = m.group(2)
            rows = int(m.group(3)) if m.group(3) else 0
            nodes.append(NodeProgress(
                name=name,
                status="done" if status == "ok" else "error",
                rows=rows,
                error="" if status == "ok" else "error",
            ))
    
    return nodes


def create_initial_progress(
    file_index: int,
    total_files: int,
    file_name: str,
    phase: str,
) -> PipelineProgress:
    """Create a progress object with all nodes in 'pending' state."""
    return PipelineProgress(
        phase=phase,
        file_index=file_index,
        total_files=total_files,
        file_name=file_name,
        nodes=[NodeProgress(name=n, status="pending") for n in VALID_NODES],
    )
