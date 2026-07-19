# ADR-06 — Facts namespacés avant modèle universel

**Statut :** accepté

**Source :** extrait verbatim de `docs/design/architecture-cloud-assurance-v2.md` (§9) le 2026-07-19.

---

**Décision :** chaque connecteur publie des facts namespacés. Aucun `owner`, `security_status` ou `compliance_score` universel n'est imposé en V1.

Une projection sémantique commune n'est ajoutée que lorsqu'un cas utilisateur nécessite réellement de résoudre plusieurs sources.
