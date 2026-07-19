tests/golden — FOCUS 1.0 golden exports

The per-provider golden files used by the dialect conformance tests
(roadmap-consolidation §II.2). One file per registered FOCUS provider
in `packages/connectors/focus/src/constat_focus/dialects/`. Each file:

- carries the full FOCUS 1.0 column set (43 columns);
- is small enough to be readable in a code review (a few dozen rows);
- is anonymized (no real customer data, no real account numbers);
- is **versioned in git** — when FOCUS 1.0 ships a new spec revision,
  the corresponding golden is updated in the same commit as the
  loader change so the conformance test pins both halves of the
  contract.

Adding a new provider = one new golden file here + one new entry in
`tests/test_focus_dialect_conformance.py::PROVIDERS` and a new
`Dialect` subclass in `dialects/`. The conformance harness runs
each golden through the same loader and the same aggregator and
asserts the canonical service name is populated end-to-end.

The AWS file (`focus_aws.csv`) is the original "home-grown" fixture
that was kept alive through several audit rounds; it predates the
Azure FOCUS 1.0 conformance work and is the historical reference
for "what AWS FOCUS 1.0 looks like in the wild".
