# ADR-05 — REST/FastAPI + Next.js, API comme surface produit

**Statut :** accepté

**Source :** extrait verbatim de `docs/design/architecture-cloud-assurance-v2.md` (§9) le 2026-07-19.

---

**Décision :** API REST versionnée, curseurs de pagination, opérations longues asynchrones, frontend Next.js/React derrière CloudFront.

Le navigateur n'interroge jamais directement S3, Athena ou une source externe.

L'API n'est pas seulement le backend du frontend : c'est une surface produit dès la V1 :

- service accounts avec scopes read-only, quotas et rate limits (ADR-10) ;
- spécification OpenAPI publiée ;
- endpoints inventory, facts, insights et source health identiques à ceux consommés par l'UI ;
- exports asynchrones livrés par URL présignée courte ;
- webhooks (run terminé, nouvel insight) prévus en V2.

Aucun endpoint « privé UI » ne contourne ce contrat : ce que l'interface affiche, un client peut l'extraire par API.
