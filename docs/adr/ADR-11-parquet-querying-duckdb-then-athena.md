# ADR-11 — Requêtage Parquet : DuckDB puis Athena

**Statut :** accepté

**Source :** extrait verbatim de `docs/design/architecture-cloud-assurance-v2.md` (§9) le 2026-07-19.

---

**Décision :** les replays, agrégats FOCUS et backtests d'insights lisent le Parquet avec **DuckDB embarqué dans les workers**. Aucun cluster de requête dédié en V1.

Athena prend le relais, par traitement, si :

- le dataset scanné dépasse durablement la mémoire d'un worker Fargate ;
- un traitement partitionné dépasse quatre heures (cohérent avec ADR-03) ;
- un besoin d'analytique ad hoc cross-datasets apparaît côté exploitation.

Le navigateur n'interroge jamais DuckDB ni Athena : leurs résultats redeviennent des read models ou des exports.
