# ADR-16 — Ack carry-over across delete-and-replace by gap identity

**Status:** accepted (2026-07-19) — closes the daily ack loss flagged while
preparing the post-pilot retrospective.

## Context

The runner's delete-and-replace (audit F-03) wipes the `insights` table
on every run, which also wipes the operator's `ack_status` / `ack_at` /
`ack_by`. The lifecycle log (`insight_events`) preserves appeared/resolved
history via fingerprint (sha256 of `rule_name|resource_id|title`), but
the operator's *decision* on the current gap was lost on every re-run.

The fingerprint is the wrong key for ack carry-over: it hashes the
title, and the EOL rules' title embeds `days_to_eol` (and the
phase-transition branch's title string). On a daily re-run, the title
changes, the fingerprint changes, the ack is reset to NULL.

## Decision

`insights_repo.stable_id_of(rule_name, resource_id, account_id, payload)`
is the **gap identity** — distinct from the lifecycle fingerprint:

- Resource rules: `resource:{rule_name}:{resource_id}`.
- Chargeback: `chargeback:{account_id}:{service}:{period_label}:{tag_key}:{tag_value}`
  (read from the payload; the drift amount in the title is the value being
  measured, not the identity).

The runner:

1. Snapshots every acked row by `stable_id` before the per-rule delete
   (`insights_repo.snapshot_acks`).
2. Inserts the fresh insights (F-03 path, unchanged).
3. Applies the snapshotted acks to the fresh rows by `stable_id`
   (`insights_repo.apply_acks_to_rule`) — only rows with
   `ack_status IS NULL` are touched, and only matched rows get the
   carried values.

A snapshot that doesn't match any current row (the gap genuinely
closed: resource retired, EOL fixed, chargeback bucket emptied) is
silently ignored. The lifecycle log records the closure as a
`resolved` event with the last known amount — the CFO-facing "money
recovered" stays accurate.

The fingerprint (lifecycle key) is unchanged. The title instability
remains a problem for the lifecycle log (daily appeared/resolved churn
on EOL rules); that's a separate, larger fix and is not in scope here.

## Consequences

- An operator's ack on a gap survives every re-run, even when the
  title, the amount, and the phase all change.
- When the gap genuinely closes, the ack is correctly NOT carried
  over (no fresh row to carry to) — the operator's decision is moot
  on a closed gap.
- Multi-resource rules: each resource has its own stable_id; the
  ack on one resource does not bleed to a sibling.
- Chargeback: the stable_id includes `tag_value`, so an ack on the
  `[Application=web]` bucket does not bleed to `[Application=api]`.
- No schema change: the carry-over uses the existing `ack_*` columns
  on the existing `insights` table.
