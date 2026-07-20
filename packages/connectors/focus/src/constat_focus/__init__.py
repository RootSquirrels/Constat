"""FOCUS 1.0 loader — CSV + Parquet (V1), with provider dialects."""

from constat_focus.aggregator import AggregatedFocusCharge, aggregate_for_storage
from constat_focus.dialects import REGISTRY, Dialect, auto_detect, get_dialect
from constat_focus.loader import (
    FOCUS_OPTIONAL_COLUMNS,
    FOCUS_REQUIRED_COLUMNS,
    FocusCharge,
    load_focus,
    load_focus_csv,
    load_focus_parquet,
)
from constat_focus.service_catalog import ServiceCatalog, get_catalog

__all__ = [
    "FOCUS_OPTIONAL_COLUMNS",
    "FOCUS_REQUIRED_COLUMNS",
    "REGISTRY",
    "AggregatedFocusCharge",
    "Dialect",
    "FocusCharge",
    "ServiceCatalog",
    "aggregate_for_storage",
    "auto_detect",
    "get_catalog",
    "get_dialect",
    "load_focus",
    "load_focus_csv",
    "load_focus_parquet",
]
