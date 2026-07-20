"""Tests for the bench_runner regression detection (Chantier V).

The V chantier: "le bench e2e quotidien devient une série
temporelle avec alerte de régression >20%". The detection
logic lives in `scripts/bench_runner.py::_check_regression`
(tested here in isolation; the script's CLI behavior is
exercised by `python scripts/bench_runner.py --check` in CI).

The contract pinned by these tests:
1. First run (no history) returns 0 — no baseline to compare to.
2. Second run (1 historical row) returns 0 — too few rows to
   compute a stable median.
3. With >= 2 historical rows, the median is the baseline. A
   latest value > 20% above the median = exit 1.
4. Improvements (latest < baseline) are reported but never alert.
5. NaN / 0 baselines do not divide-by-zero.

The detection uses the MEDIAN (not the mean) so a single
cold-start outlier in the baseline does not silently hide a
regression. The window is the last 5 rows; a stale baseline
slowly rolls forward.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

# Load the bench_runner module under its real name (the script's
# `if __name__ == "__main__"` block would otherwise run on
# import). We bypass that by loading the file as a module WITHOUT
# executing the guard.
_BENCH_PATH = Path(__file__).resolve().parent.parent / "scripts" / "bench_runner.py"
_spec = importlib.util.spec_from_file_location("bench_runner", _BENCH_PATH)
assert _spec is not None and _spec.loader is not None
bench = importlib.util.module_from_spec(_spec)
sys.modules["bench_runner"] = bench
_spec.loader.exec_module(bench)


def _row(wall_s: float, peak_mib: float = 50.0) -> dict:
    """Build a synthetic history row with the same shape as the
    one `_append_history` writes. Only the fields the detector
    reads (wall_s, peak_mib) matter; the rest are placeholders."""
    return {
        "timestamp": "2026-07-20T00:00:00+00:00",
        "resources": 1000,
        "wall_s": wall_s,
        "peak_mib": peak_mib,
        # rate is unused by the detector; we keep it 0 when
        # wall_s is 0 to avoid a ZeroDivisionError in the helper
        # itself (the detector guards against zero baselines).
        "rate": 0.0 if wall_s == 0 else 1000 / wall_s,
        "insights_emitted": 0,
    }


# ---------------------------------------------------------------------------
# 1. Empty / single-row history: no alert (no baseline yet)
# ---------------------------------------------------------------------------


def test_no_history_returns_0_no_baseline() -> None:
    exit_code, message = bench._check_regression(
        _row(0.5), history=[]
    )
    assert exit_code == 0
    assert "first run" in message.lower()


def test_single_row_history_returns_0_not_enough_data() -> None:
    exit_code, message = bench._check_regression(
        _row(0.5), history=[_row(0.5)]
    )
    assert exit_code == 0
    assert "only 1" in message or "at least 2" in message


# ---------------------------------------------------------------------------
# 2. No regression: stable history, latest within ±20% of median
# ---------------------------------------------------------------------------


def test_stable_history_returns_0_no_regression() -> None:
    history = [_row(0.50), _row(0.50), _row(0.50), _row(0.50), _row(0.50)]
    exit_code, message = bench._check_regression(_row(0.51), history=history)
    assert exit_code == 0
    assert "OK" in message


def test_improvement_returns_0_never_alert_on_faster() -> None:
    """A 50% speedup is reported as OK — the alert is one-way
    (regression only). Improvements don't need operator
    attention; the dashboard is for pain, not celebration."""
    history = [_row(1.00), _row(1.00), _row(1.00), _row(1.00), _row(1.00)]
    exit_code, message = bench._check_regression(_row(0.50), history=history)
    assert exit_code == 0
    assert "OK" in message


def test_within_threshold_returns_0() -> None:
    """A 15% slowdown (below the 20% threshold) is OK — the
    threshold gives a buffer for noise."""
    history = [_row(1.00), _row(1.00), _row(1.00), _row(1.00), _row(1.00)]
    # 1.18 = 18% slower
    exit_code, _ = bench._check_regression(_row(1.18), history=history)
    assert exit_code == 0


# ---------------------------------------------------------------------------
# 3. Regression: latest > baseline_median * 1.20 → exit 1
# ---------------------------------------------------------------------------


def test_25_percent_wall_time_regression_alerts() -> None:
    history = [_row(1.00)] * 5
    # 1.25 = 25% slower
    exit_code, message = bench._check_regression(_row(1.25), history=history)
    assert exit_code == 1
    assert "regressed" in message.lower()
    assert "wall_time_s" in message


def test_exactly_at_threshold_returns_0_strict_greater_than() -> None:
    """The threshold is `> threshold_pct`, not `>=`. A slowdown
    that lands EXACTLY at the threshold is OK (the next run
    that pushes it over is the one that fires the alert). This
    is the same operator-strictness choice as the streak check
    in tests/test_health_checks.py (cf. memory doctrine
    'Streak check — verify math + match operator strictness')."""
    history = [_row(1.00)] * 5
    # 1.20 = exactly 20% slower → not > 20%, so OK
    exit_code, _ = bench._check_regression(_row(1.20), history=history)
    assert exit_code == 0


def test_peak_mib_regression_alerts() -> None:
    """Memory regression is its own dimension. A 50% wall_time
    regression AND a 50% mem regression both fire."""
    history = [_row(1.00, 50.0)] * 5
    exit_code, message = bench._check_regression(_row(1.50, 75.0), history=history)
    assert exit_code == 1
    assert "wall_time_s" in message
    assert "peak_mib" in message


def test_only_peak_mib_regression_alerts() -> None:
    """Asymmetric regression: time stable, memory regresses. The
    alert is per-metric, so this should still fire."""
    history = [_row(1.00, 50.0)] * 5
    exit_code, message = bench._check_regression(_row(1.00, 70.0), history=history)
    assert exit_code == 1
    assert "peak_mib" in message


# ---------------------------------------------------------------------------
# 4. Median robustness: a single outlier in the baseline does not
#    hide a regression (this is why we use median, not mean).
# ---------------------------------------------------------------------------


def test_median_robust_to_one_outlier_in_baseline() -> None:
    """4 rows at 1.00s + 1 outlier at 100.00s. Mean would be
    ~20.8s, hiding any reasonable regression. Median is 1.00s,
    so a real 1.25s regression still fires. This is the math
    property the median choice buys us."""
    history = [_row(1.00), _row(1.00), _row(1.00), _row(1.00), _row(100.00)]
    exit_code, _ = bench._check_regression(_row(1.25), history=history)
    assert exit_code == 1, "median failed to absorb the outlier"


def test_median_window_is_last_5_rows() -> None:
    """Rows outside the window are ignored. With 10 historical
    rows (5 fast, 5 slow), the window of the last 5 is the
    slow set, so a fast latest triggers an alert."""
    fast = [_row(0.5)] * 5
    slow = [_row(1.5)] * 5
    # The latest is fast (0.5) but the recent 5 baseline is
    # slow (1.5). The "regression" is -67% (an improvement),
    # which never alerts.
    exit_code, _ = bench._check_regression(_row(0.5), history=fast + slow)
    assert exit_code == 0
    # Reverse: latest is slow, recent 5 are fast. The
    # "regression" is +200%, which alerts.
    exit_code, _ = bench._check_regression(_row(1.5), history=slow + fast)
    assert exit_code == 1


# ---------------------------------------------------------------------------
# 5. Numerical safety: zero baselines (e.g., synthetic data) must
#    not divide by zero.
# ---------------------------------------------------------------------------


def test_zero_baseline_does_not_divide_by_zero() -> None:
    """A baseline of 0 (degenerate, but possible in synthetic
    data) must not raise. We treat the division as N/A → no
    regression (the alert would be meaningless at zero)."""
    history = [_row(0.0)] * 5
    exit_code, _ = bench._check_regression(_row(1.0), history=history)
    assert exit_code == 0


def test_two_rows_are_enough_for_a_baseline() -> None:
    """The minimum is 2 rows (median over a 2-element list is the
    average). Less than 2 is 'not enough data'."""
    history = [_row(1.0), _row(1.0)]
    exit_code, _ = bench._check_regression(_row(1.5), history=history)
    # 1.5 vs median([1.0, 1.0]) = 1.0 = +50% → alerts
    assert exit_code == 1
