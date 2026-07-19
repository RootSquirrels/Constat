# ADR-01 — S3 + Parquet avant Iceberg

**Statut :** accepté

**Source :** extrait verbatim de `docs/design/architecture-cloud-assurance-v2.md` (§9) le 2026-07-19.

---

**Décision :** S3 pour les batches, manifests et payloads ; Parquet append-only pour l'historique normalisé et FOCUS.

Pas d'Iceberg en V1 car :

- les datasets sont principalement append-only ;
- PostgreSQL sert l'état courant ;
- aucun usage contractuel d'UPDATE/DELETE analytique ou time travel SQL n'existe encore.

**Passage à Iceberg si au moins un besoin durable apparaît :**

- plus de 100 millions de lignes nouvelles par jour ;
- plus de 100 000 fichiers actifs par dataset ;
- writers concurrents sur une même table logique ;
- corrections/suppressions fréquentes ;
- planning de requête supérieur à 10 secondes p95 ;
- maintenance Parquet/manifests supérieure à 0,5 ETP.
