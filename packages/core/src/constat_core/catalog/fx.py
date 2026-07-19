"""USD → EUR conversion catalog.

Last reviewed: 2026-07-19. Update at the monthly catalog review, like
every other catalog entry.

Source: ECB euro foreign exchange reference rates —
https://www.ecb.europa.eu/stats/policy_and_exchange_rates/euro_reference_exchange_rates/html/eurofxref-graph-usd.en.html
(daily XML feed: https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml)

The rate is refreshed MANUALLY: there is no live fetch at runtime, so
the conversion is deterministic, offline-safe, and every EUR figure can
be traced to a dated ECB quote — the property a CFO challenges first.
The ECB publishes reference rates around 16:00 CET each TARGET business
day as "1 EUR = X USD"; we store the published quote verbatim and
invert it for USD → EUR.

All AWS pricing in the catalogs is USD, so the EUR figure is always a
display/export convenience derived from the USD source of truth — never
the other way around.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

# Catalog version stamp, same convention as catalog/aws.py and
# catalog/ebs.py. Bumped at every review.
FX_CATALOG_VERSION = "2026-07-19"


@dataclass(frozen=True)
class FxRate:
    """One dated ECB reference quote.

    `usd_per_eur` is the published ECB quote (1 EUR = this many USD).
    `rate_date` is the ECB reference date of the quote (the day the
    rate is FOR, not the day we copied it). `review_date` is when this
    constant was last cross-checked against the source.
    """

    usd_per_eur: Decimal
    rate_date: date
    source_url: str
    review_date: str  # ISO date string

    @property
    def usd_to_eur_rate(self) -> Decimal:
        """1 USD = this many EUR, 6 decimal places (display + audit)."""
        return (Decimal(1) / self.usd_per_eur).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)


# ECB reference rate for 2026-07-17: 1 EUR = 1.1435 USD
# (eurofxref-daily.xml, copied 2026-07-19 — the most recent published
# rate at review time; 2026-07-18/19 fall on a weekend).
FX_USD_EUR = FxRate(
    usd_per_eur=Decimal("1.1435"),
    rate_date=date(2026, 7, 17),
    source_url="https://www.ecb.europa.eu/stats/policy_and_exchange_rates/euro_reference_exchange_rates/html/eurofxref-graph-usd.en.html",
    review_date="2026-07-19",
)


def usd_to_eur(amount: float | Decimal) -> tuple[Decimal, Decimal, date]:
    """Convert a USD amount to EUR at the catalogued ECB reference rate.

    Returns (eur_amount, rate, rate_date):
    - eur_amount: the EUR amount rounded to cents (ROUND_HALF_UP),
    - rate: the USD→EUR rate used (1 USD = X EUR, 6 dp),
    - rate_date: the ECB reference date of that rate.

    The caller should stamp the rate and its date next to the EUR
    figure — an undated conversion is not defensible.
    """
    usd = Decimal(str(amount))
    eur = (usd / FX_USD_EUR.usd_per_eur).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return eur, FX_USD_EUR.usd_to_eur_rate, FX_USD_EUR.rate_date
