# ADR-12 — Insights-first pivot: V1 is sold as "insights", not "inventory"

**Status:** accepted (2026-07-18) — closes audit finding F-07.

**Context.** The architecture doc (`docs/design/architecture-cloud-assurance-v2.md`)
describes a V1 whose centerpiece is a filterable cloud inventory (a `/resources`
endpoint, `aws.tag.*` facts, an inventory web view). The code that actually shipped
does not have any of that: no `/resources` router, no tag collection, no inventory
page. What shipped instead is the narrower scope that `AGENTS.md` had already
redefined in writing: one demoable insight (RDS PostgreSQL Extended Support) plus a
FOCUS-backed chargeback view, both with INCONCLUSIVE semantics and completeness
proofs. The V1 audit flagged the mismatch: selling the pilot as "filterable
inventory" would be selling a capability that does not exist.

**Decision.** The pivot to **insights-first** is confirmed as the product position,
not treated as missing functionality:

- The V1 pilot is sold as *"in 2h of connection, we prove what you don't know about
  your fleet — and what it costs"* (the GTM promise already in `AGENTS.md`).
  Insights + chargeback + INCONCLUSIVE are the deliverable.
- The filterable-inventory capability (`/resources` endpoint, `aws.tag.*` facts,
  inventory web view) is **not** claimed in any demo, sales material, or pilot
  success criterion until it is actually built.
- Building the inventory is a V2 decision, gated on a pilot customer asking for it.
  When it is built, it reuses the existing `resources`/`facts` tables (the schema
  already supports it — this decision closes no doors).

**Consequences.**

- The audit's "NO-GO if sold as inventory" condition is discharged by contract:
  we do not sell it as inventory.
- The GTM doc and any pilot material must use the insights-first framing.
- `AGENTS.md` already reflects this scope; the architecture doc remains the
  long-term target and is not rewritten — V1 is a deliberate subset of it.
