"""Pytest config: ensure local src/ paths are importable without uv sync."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC_PATHS = [
    ROOT / "packages" / "core" / "src",
    ROOT / "packages" / "connectors" / "aws_rds" / "src",
    ROOT / "packages" / "connectors" / "focus" / "src",
    ROOT / "packages" / "insights" / "rds_eol" / "src",
    ROOT / "packages" / "insights" / "chargeback" / "src",
    ROOT / "apps" / "api" / "src",
]
for p in SRC_PATHS:
    p_str = str(p)
    if p_str not in sys.path:
        sys.path.insert(0, p_str)
