"""Tests for the FX catalog (packages/core/catalog/fx.py).

The whole point of the module: a EUR figure with a dated, citable ECB
rate behind it. These tests pin the rate arithmetic and the audit
fields — if the constant is refreshed, the expected values move and
the diff is the review.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from constat_core.catalog.fx import FX_CATALOG_VERSION, FX_USD_EUR, usd_to_eur

# Hand-computed against the ECB quote for 2026-07-17: 1 EUR = 1.1435 USD
# (https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml).
# 1 USD = 1 / 1.1435 EUR = 0.874508 EUR (6 dp, HALF_UP).
EXPECTED_RATE = Decimal("0.874508")


def test_rate_constant_is_the_published_ecb_quote() -> None:
    """The constant stores the ECB quote verbatim (1 EUR = X USD) —
    not a derived value — so the review can diff it against the source."""
    assert FX_USD_EUR.usd_per_eur == Decimal("1.1435")
    assert FX_USD_EUR.rate_date == date(2026, 7, 17)
    assert FX_USD_EUR.review_date == "2026-07-19"
    assert FX_USD_EUR.source_url.startswith("https://www.ecb.europa.eu/")


def test_usd_to_eur_rate_is_the_inverted_quote() -> None:
    assert FX_USD_EUR.usd_to_eur_rate == EXPECTED_RATE


def test_usd_to_eur_returns_amount_rate_and_date() -> None:
    """The caller must be able to stamp the rate and its date next to
    the EUR figure — an undated conversion is not defensible."""
    eur, rate, rate_date = usd_to_eur(584.0)
    # 584 / 1.1435 = 510.7127... -> 510.71
    assert eur == Decimal("510.71")
    assert rate == EXPECTED_RATE
    assert rate_date == date(2026, 7, 17)


def test_usd_to_eur_rounds_half_up_to_cents() -> None:
    # 42.50 / 1.1435 = 37.16659... -> 37.17
    eur, _, _ = usd_to_eur(42.5)
    assert eur == Decimal("37.17")
    # 100 / 1.1435 = 87.45080... -> 87.45
    eur, _, _ = usd_to_eur(100)
    assert eur == Decimal("87.45")


def test_usd_to_eur_accepts_decimal_and_zero() -> None:
    eur, _, _ = usd_to_eur(Decimal("0"))
    assert eur == Decimal("0.00")


def test_fx_catalog_version_is_a_date_string() -> None:
    """Same convention as EBS_CATALOG_VERSION / CATALOG_VERSION."""
    assert isinstance(FX_CATALOG_VERSION, str)
    assert len(FX_CATALOG_VERSION) == 10
    assert FX_CATALOG_VERSION[4] == "-"
    assert FX_CATALOG_VERSION[7] == "-"
