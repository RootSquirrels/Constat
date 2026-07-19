# ADR-10 — Authentification et identités API

**Statut :** accepté

**Source :** extrait verbatim de `docs/design/architecture-cloud-assurance-v2.md` (§9) le 2026-07-19.

---

**Décision :** aucun token anonyme représentant indistinctement « tout un tenant ». Deux populations distinctes dès la V1 :

```text
Utilisateurs UI
  → OIDC (SAML si exigé contractuellement)
  → membership tenant en base
  → rôles Admin / Viewer

Clients machine
  → service accounts nommés, rattachés au tenant
  → API keys hashées, scopes explicites, expiration et rotation
  → rate limits par service account
  → journal d'accès (identité, tenant, scope, horodatage)
```

OAuth2 client credentials remplace les API keys en V2 si les intégrations clients le demandent. Toute clé sans expiration ou sans scope est rejetée à la création.
