"""Property-based tests for the monetary extraction registry (§IV.1).

The committee-finding bug class: a refactor (rds_eol tiering, ebs
gp2_to_gp3 extraction path) silently dropped the monthly amount;
the example tests in test_monetary_extraction.py pin the
specific cases, but a property test pins the SHAPE: ∀ payload,
extraction either returns a valid float or None, never raises,
and never treats a bool as money.

Hypothesis generates ~100 random payloads per `@given` and
shrinks failing inputs to the minimal counter-example. A bug
in `monthly_cost_and_basis` that only manifests on a specific
value (e.g. NaN, complex, numpy float) is caught here, not by
an example test someone has to remember to add.

The 4 properties, one per invariant:

1. **Total extraction** — no input raises. A future refactor
   that adds a `dict` access on a non-dict payload fails here.
2. **Bool is never money** — `True` must not become $1.00.
   bool is an int in Python (`isinstance(True, int) is True`),
   so this is the easy-to-miss case the committee flagged.
3. **Numeric values extract** — int and float (not bool) become
   a float with the registered value_basis. The basis never
   flips to ACTUAL (V2-only contract).
4. **Registry keys are unique per kind** — two AVOIDABLE_SAVING
   rules cannot share a payload_key (their sums would be
   ambiguous in the restitution's "total savings" column). Two
   ACCOUNTING_DELTA rules can (the column is "drift", not
   "savings"), but the current registry has one of each.
"""

from __future__ import annotations

from constat_core.monetary import (
    MONETARY,
    MonetaryKind,
    ValueBasis,
    monetary_kind,
    monthly_cost_and_basis,
)
from hypothesis import given, settings
from hypothesis import strategies as st

# Strategy: any value a JSON-serialized payload could carry.
# Excludes recursive dicts/lists to keep generation fast;
# the function only inspects top-level keys anyway.
PAYLOAD_VALUE = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-1_000_000, max_value=1_000_000),
    st.floats(
        allow_nan=False,
        allow_infinity=False,
        min_value=-1_000_000.0,
        max_value=1_000_000.0,
    ),
    st.text(max_size=64),
    st.lists(st.integers(), max_size=4),
    st.dictionaries(st.text(max_size=8), st.integers(), max_size=4),
)

# A rule name: any registered rule OR any unregistered string.
RULE_NAME = st.one_of(st.sampled_from(sorted(MONETARY)), st.text(max_size=32))


# ---------------------------------------------------------------------------
# 1. Total extraction — no input raises
# ---------------------------------------------------------------------------


@settings(max_examples=200, deadline=None)
@given(rule_name=RULE_NAME, value=PAYLOAD_VALUE)
def test_monthly_cost_and_basis_never_raises(rule_name, value) -> None:
    """∀ (rule_name, payload) — extraction returns a (cost, basis)
    tuple without raising. A future refactor that adds e.g.
    `payload[entry.payload_key].real` fails here on a string input."""
    payload = {"some_key": value, "another": "x"}
    # If the rule IS registered, place the value at the right key.
    if rule_name in MONETARY:
        payload[MONETARY[rule_name].payload_key] = value
    cost, basis = monthly_cost_and_basis(rule_name, payload)
    assert cost is None or isinstance(cost, float)
    assert basis is None or isinstance(basis, str)


# ---------------------------------------------------------------------------
# 2. Bool is never money
# ---------------------------------------------------------------------------


@settings(max_examples=200, deadline=None)
@given(rule_name=st.sampled_from(sorted(MONETARY)), bool_value=st.booleans())
def test_bool_payload_key_yields_none_cost(rule_name, bool_value) -> None:
    """∀ registered rule, ∀ bool at the payload key — cost is None.
    bool IS an int in Python (`isinstance(True, int) is True`);
    a naive `float(payload[key])` would turn True into $1.00 and
    False into $0.00. The committee finding: this is the easy-to-miss
    case that has bitten V1 once already."""
    entry = MONETARY[rule_name]
    payload = {entry.payload_key: bool_value}
    cost, _ = monthly_cost_and_basis(rule_name, payload)
    assert cost is None, (
        f"{rule_name}: bool at {entry.payload_key!r} extracted as ${cost} "
        f"(must be None — bool is not money)"
    )


# ---------------------------------------------------------------------------
# 3. Numeric values extract + basis is invariant
# ---------------------------------------------------------------------------


@settings(max_examples=200, deadline=None)
@given(
    rule_name=st.sampled_from(sorted(MONETARY)),
    value=st.floats(
        allow_nan=False,
        allow_infinity=False,
        min_value=-1_000_000.0,
        max_value=1_000_000.0,
    ),
)
def test_numeric_payload_extracts_as_float_with_registered_basis(
    rule_name, value
) -> None:
    """∀ registered rule, ∀ finite float at the payload key —
    cost equals float(value) and basis equals the registered
    value_basis (V2 contract: this function never flips to ACTUAL)."""
    entry = MONETARY[rule_name]
    payload = {entry.payload_key: value}
    cost, basis = monthly_cost_and_basis(rule_name, payload)
    assert cost == float(value), (
        f"{rule_name}: {value} should extract as {float(value)}, got {cost}"
    )
    assert basis == entry.value_basis.value, (
        f"{rule_name}: basis flipped from {entry.value_basis.value!r} to {basis!r}"
    )
    # The V1 contract, explicit: the extraction function never returns ACTUAL.
    # ACTUAL is reserved for V2 when a per-charge-type matcher links a rule's
    # amount to a specific FOCUS component.
    assert basis != ValueBasis.ACTUAL.value


# ---------------------------------------------------------------------------
# 4. Shared payload_keys must agree on (basis, kind)
# ---------------------------------------------------------------------------


def test_shared_payload_keys_agree_on_basis_and_kind() -> None:
    """The 3 EOL rules (rds_eol, mysql_eol, aurora_eol) all emit
    `extended_support_monthly_usd` — the dashboard's "total
    Extended Support cost" is a legitimate cross-engine sum. The
    invariant is not "no shared keys" (the product design WANTS
    them shared for cross-engine sums) but "no shared key with
    disagreeing (basis, kind)": two rules that share a key MUST
    also agree on the value_basis and the kind, otherwise the
    sum is ambiguous (e.g. an AVOIDABLE_SAVING $100 + an
    ACCOUNTING_DELTA $100 summed as "total savings" would be
    wrong by half)."""
    by_key: dict[str, list[tuple[str, ValueBasis, MonetaryKind]]] = {}
    for rule_name, entry in MONETARY.items():
        by_key.setdefault(entry.payload_key, []).append(
            (rule_name, entry.value_basis, entry.kind)
        )
    for key, rules in by_key.items():
        if len(rules) == 1:
            continue
        bases = {b for _, b, _ in rules}
        kinds = {k for _, _, k in rules}
        assert len(bases) == 1, (
            f"payload_key {key!r} is shared by rules with different "
            f"value_bases {bases} — their amounts cannot be summed"
        )
        assert len(kinds) == 1, (
            f"payload_key {key!r} is shared by rules with different "
            f"kinds {kinds} — AVOIDABLE_SAVING + ACCOUNTING_DELTA cannot "
            f"be summed as 'total savings'"
        )


# ---------------------------------------------------------------------------
# 5. Every MONETARY rule has a registered value_basis + kind
# ---------------------------------------------------------------------------


@given(rule_name=st.sampled_from(sorted(MONETARY)))
def test_monetary_kind_matches_registry_entry(rule_name) -> None:
    """∀ registered rule — `monetary_kind` returns the same kind as
    the registry. A refactor that re-orders or splits MONETARY
    cannot break the kind surface (the restitution reads `kind` to
    keep savings and accounting-delta columns separate)."""
    assert monetary_kind(rule_name) == MONETARY[rule_name].kind
