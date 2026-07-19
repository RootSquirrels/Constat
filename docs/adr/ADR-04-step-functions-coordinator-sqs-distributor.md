# ADR-04 — Step Functions coordinateur + SQS distributeur

**Statut :** accepté

**Source :** extrait verbatim de `docs/design/architecture-cloud-assurance-v2.md` (§9) le 2026-07-19.

---

**Décision :** Step Functions coordonne un run ; SQS distribue les unités de travail.

Ce découpage évite :

- un workflow par page ;
- des milliers de tâches Fargate d'une minute ;
- une double logique de retry ;
- un couplage entre orchestration et pagination fournisseur.
