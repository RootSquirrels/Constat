"""Pin: the FX mirror in apps/web/lib/api.ts cannot drift from the catalog.

packages/core/src/constat_core/catalog/fx.py is the source of truth for the
USD→EUR display conversion (dated ECB reference quote, manually refreshed at
the monthly catalog review). apps/web/lib/api.ts duplicates the rate and its
date as FX_USD_TO_EUR / FX_RATE_DATE for display formatting. Nothing guarded
the pair: a catalog refresh that forgot the TS side would show EUR figures
computed at a stale rate — the first thing a CFO challenges.

Same style as the RULE_MONETARY pin in tests/test_monetary_extraction.py:
parse the TS file as text, assert the values match the Python constants.
"""

from __future__ import annotations

import re
from pathlib import Path

from constat_core.catalog.fx import FX_USD_EUR

REPO_ROOT = Path(__file__).resolve().parent.parent
WEB_API_TS = REPO_ROOT / "apps" / "web" / "lib" / "api.ts"


def _ts_const(source: str, name: str) -> str:
    match = re.search(rf"export const {name} = ([^;]+);", source)
    assert match, f"apps/web/lib/api.ts lost its {name} export"
    return match.group(1).strip().strip('"').strip("'")


def test_ts_fx_rate_matches_catalog() -> None:
    source = WEB_API_TS.read_text(encoding="utf-8")
    expected = str(FX_USD_EUR.usd_to_eur_rate)  # 6dp USD→EUR rate
    assert _ts_const(source, "FX_USD_TO_EUR") == expected, (
        f"TS FX_USD_TO_EUR drifted from catalog fx.py (expected {expected} "
        f"= 1/{FX_USD_EUR.usd_per_eur}) — refresh both at the monthly catalog review"
    )


def test_ts_fx_rate_date_matches_catalog() -> None:
    source = WEB_API_TS.read_text(encoding="utf-8")
    expected = FX_USD_EUR.rate_date.isoformat()
    assert _ts_const(source, "FX_RATE_DATE") == expected, (
        f"TS FX_RATE_DATE drifted from catalog fx.py (expected {expected})"
    )
