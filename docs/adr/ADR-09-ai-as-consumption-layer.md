# ADR-09 — IA comme couche de consommation, jamais comme source de vérité

**Statut :** accepté

**Source :** extrait verbatim de `docs/design/architecture-cloud-assurance-v2.md` (§9) le 2026-07-19.

---

**Décision :** aucune inférence IA dans la chaîne d'ingestion, de normalisation ou de projection. L'IA est introduite (V2+) comme couche de consommation au-dessus des read models :

- requête en langage naturel traduite vers les filtres existants de l'Inventory ;
- explication d'un insight ou d'un écart, générée uniquement à partir des facts sourcés ;
- résumé de santé des sources et des runs.

Contraintes :

- toute réponse générée cite les facts, sources et timestamps utilisés ;
- aucune valeur générée n'est écrite dans Resource, Fact ou CostFact ;
- fonctionnalité désactivable par tenant (exigence fréquente en environnement régulé) ;
- aucun envoi de données tenant vers un modèle externe sans accord contractuel explicite.

Le modèle facts + provenance rend cette couche fiable à faible coût : le LLM reformule et met en relation des données prouvées, il n'en produit pas.

Cet ADR ne génère aucun composant en Phase 0/1.
