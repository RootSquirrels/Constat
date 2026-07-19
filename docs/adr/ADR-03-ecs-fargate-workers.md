# ADR-03 — ECS Fargate workers

**Statut :** accepté

**Source :** extrait verbatim de `docs/design/architecture-cloud-assurance-v2.md` (§9) le 2026-07-19.

---

**Décision :** workers conteneurisés, autoscalés sur l'âge et la profondeur des queues.

Lambda reste réservé aux webhooks et tâches courtes. EKS n'apporte aucun mécanisme nécessaire en V1.

Glue/Spark n'est introduit que pour les gros traitements FOCUS/replay qui dépassent durablement la mémoire d'un worker ou quatre heures de traitement partitionné.
