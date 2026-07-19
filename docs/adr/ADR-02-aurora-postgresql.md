# ADR-02 — Aurora PostgreSQL

**Statut :** accepté

**Source :** extrait verbatim de `docs/design/architecture-cloud-assurance-v2.md` (§9) le 2026-07-19.

---

**Décision :** Aurora PostgreSQL pour le transactionnel, l'isolation tenant, les facts courants et les read models.

Configuration initiale :

- writer ;
- reader dans une autre AZ ;
- PITR ;
- RDS Proxy si les profils de connexion le justifient ;
- RLS forcée ;
- rôle API non-owner et sans `BYPASSRLS`.

**Évolution :** cellules de tenants ou cluster dédié si un tenant représente plus de 20 % de la charge, si le cluster dépasse ses SLO après tuning/replicas, ou si une exigence contractuelle impose un silo.

Seuil spécifique `ResourceFactCurrent` : au-delà d'environ 500 millions de lignes courantes, ou si le rafraîchissement des read models sort de son SLO, partitionner par hash de `tenant_id` et servir l'inventaire uniquement depuis `InventoryRowCurrent` (`facts_json`), `ResourceFactCurrent` étant relégué au filtrage et aux projections. Le mécanisme existe déjà dans le modèle ; seul le déclencheur change.
