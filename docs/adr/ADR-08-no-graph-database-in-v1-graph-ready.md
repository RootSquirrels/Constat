# ADR-08 — Pas de graph database en V1, mais graph-ready

**Statut :** accepté

**Source :** extrait verbatim de `docs/design/architecture-cloud-assurance-v2.md` (§9) le 2026-07-19.

---

**Décision :** les relations courantes sont conservées dans PostgreSQL et leur historique dans Parquet. Neo4j ou un autre moteur de graphe pourra être ajouté comme **projection dérivée**, jamais comme source de vérité.

Seuils d'introduction :

- plus de 50 millions de relations courantes ;
- parcours de plus de cinq sauts dans plus de 20 % des requêtes produit ;
- p95 supérieur à deux secondes après indexation et précalculs ;
- besoin vendu de blast radius, pathfinding, centralité ou dépendances complexes.
