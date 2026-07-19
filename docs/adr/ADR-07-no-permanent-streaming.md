# ADR-07 — Pas de streaming permanent

**Statut :** accepté

**Source :** extrait verbatim de `docs/design/architecture-cloud-assurance-v2.md` (§9) le 2026-07-19.

---

**Décision :** batch et micro-batch. Des événements peuvent déclencher un rescan ciblé, mais une réconciliation périodique demeure obligatoire.

Streaming si :

- fraîcheur contractuelle inférieure à cinq minutes pour une part significative des données ;
- plus de 10 000 changements par seconde ;
- ou coût du polling supérieur au traitement événementiel.
