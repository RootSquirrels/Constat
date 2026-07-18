# ADR-13 — Monetary extraction registry in core

**Status:** accepted (2026-07-18) — closes client-committee findings on amount
extraction and on mixing estimates with accounting drift.

## Context

The POC restitution and the CSV export extracted insight amounts through a
hardcoded two-branch function (chargeback drift, else
`extended_support_monthly_usd`). Two bugs shipped undetected:

1. `ebs_gp2_to_gp3` emits `savings_monthly_usd` — its amounts silently
   dropped out of the restitution and the CSV (committee finding).
2. Worse, found while fixing: the rds_eol tiering refactor had **stopped
   emitting any monthly amount at all** — `HOURS_PER_MONTH` was defined but
   unused, `vcpu` was gated then never consumed. The flagship insight showed
   no dollar figure and no test noticed, because nothing tied "a rule emits
   money" to "the product shows money".

A third, semantic, finding: the restitution total summed catalog estimates
and FOCUS amortized-vs-billed drift into one "Total (known costs)" figure —
indefensible in front of a CFO, since drift is normal reservation accounting,
not an avoidable cost.

## Decision

`constat_core.monetary` is the **single source of truth** for the monetary
semantics of every rule:

- `MONETARY[rule_name] -> (payload_key, value_basis, kind)`;
- `value_basis`: `ESTIMATED` (catalog-priced) vs `ACTUAL` (FOCUS lines);
- `kind`: `AVOIDABLE_SAVING` (stops if the customer acts) vs
  `ACCOUNTING_DELTA` (informational — **never summed into a savings total**);
- `NON_MONETARY_RULES`: the explicit list of rules that legitimately emit no
  amount. A rule in `RUNNERS` that is in neither set **fails CI**
  (`tests/test_monetary_extraction.py`).

Consumers: the API CSV export delegates to the registry; the web mirrors it
in `RULE_MONETary`-table form (`apps/web/lib/api.ts`), pinned by a test that
asserts every rule name, payload key and kind appears in the TS source. The
restitution headline total only sums `AVOIDABLE_SAVING` amounts and says so
in its label.

This module lives in `packages/core` (a change gated by ADR per AGENTS.md)
because monetary meaning is part of the insight contract, not a UI concern:
two consumers already diverged once.

## Consequences

- Adding a money-emitting rule = one registry entry + one TS mirror line,
  both enforced by tests. Forgetting either is a CI failure, not a silent
  omission in a prospect deliverable.
- rds_eol emits `extended_support_monthly_usd` again (same key as
  mysql_eol/aurora_eol), plus `vcpu`, with the arithmetic pinned
  (4 vCPU x $0.20 x 730h = $584.00) in tests.
- Malformed vcpu values now produce `aws.rds.vcpu.malformed` INCONCLUSIVE
  instead of a silent skip (criterion n°15).
- Currency/regional pricing and EUR conversion remain open (committee
  finding, separate work item): everything here is USD and labeled as such.
