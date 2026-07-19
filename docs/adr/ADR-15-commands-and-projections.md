# ADR-15 — Commands and projections: remediation actions go through the queue, reads through projections

**Status:** accepted (2026-07-19) — records review item 6 and the state of play.

**Context.** The architecture review (item 6) asks for a progressive separation
of commands (remediation actions, tickets, synchronizations) from projections
(UI read models): idempotent jobs, transactional outbox, retries, DLQ,
idempotency keys, and UI projections that can be recomputed. The review also
noted the collect queue's partial-publication problem as a blocker for running
remediation actions over it.

**Decision.**

- **The partial-publication problem is closed before any remediation action
  touches the queue.** The collect path now commits the job row *before*
  sending WorkItems, records `enqueue_error` on the job on send failure
  (503 + job_id for reconciliation), surfaces SQS partial sends explicitly,
  and the worker drops orphaned items (`constat_collect_orphan_items_total`).
  This is the outbox discipline V1 needs: the database is the source of
  truth for intent, the queue only carries references to committed intent.
- **Commands** (future remediation actions: acknowledge-on-AWS, ticket
  creation, CMDB sync) will reuse exactly this pattern — a committed job row
  (outbox), queue messages referencing it, idempotent worker processing
  (dedup via unique constraints), bounded retries and a DLQ (already
  terraform'd: `maxReceiveCount=3` → `constat-pilot-collect-dlq` + alarm).
  The `ActionAdapter` contract (ADR-14) is where remediation integrations
  will plug in.
- **Projections** stay recomputed, never authoritative: insights and
  inconclusive are already delete-and-replace per rule run, and
  `insight_events` (appeared/resolved) is derived from that diff — the UI
  reads projections that can always be rebuilt from facts + FOCUS + runs.
  No bespoke projection store in V1; the read models are the tables.

**Consequences.**

- No remediation action ships until it satisfies the same outbox discipline
  (committed intent first, queue second, orphan reconciliation).
- Retries/DLQ/idempotency-key patterns exist and are proven on the collect
  path — adopting them for actions is reuse, not invention.
- When a second cadence appears (actions on top of collect), the queue
  topology is revisited (separate queue per command type, per ADR-04's
  Step Functions threshold); until then one queue + one worker service.
