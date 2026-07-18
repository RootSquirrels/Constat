# SLA — Pilote payant borné (projet)

> **Statut : PROJET à faire relire par un conseil juridique avant toute
> signature.** Ce document n'est pas un avis juridique. Il définit le
> périmètre technique et les niveaux de service d'un pilote volontairement
> borné, conformément à la condition suspensive n°4 du comité d'évaluation
> (« onboarding démontré sur 35 comptes — ou pilote contractuellement
> limité à cinq comptes »).

## 1. Périmètre du pilote

| Élément | Engagement |
|---|---|
| Comptes AWS connectés | **5 maximum** (limite technique documentée du chemin de collecte V1 ; l'extension au-delà fait l'objet d'un avenant après mise en production du traitement asynchrone) |
| Régions par compte | 7 maximum (jeu par défaut, ajustable à l'onboarding) |
| Types de ressources | RDS/Aurora (PostgreSQL, MySQL), volumes EBS |
| Règles actives | `rds_eol`, `mysql_eol`, `aurora_eol`, `ebs_gp2_to_gp3`, `ebs_unattached`, `snapshot_orphan`, `ec2_stopped_with_storage`, vue chargeback FOCUS |
| Accès | Rôle IAM **lecture seule** fourni par template versionné, avec External ID unique par connexion ; aucune access key, aucun agent |
| Durée | 90 jours à compter de la connexion du premier compte |
| Environnement | Instance dédiée au client, région **eu-west-3 (Paris)**, HTTPS obligatoire (le pilote ne démarre pas sans TLS actif — prérequis interne, pas une option) |

## 2. Niveaux de service (pendant le pilote)

| Indicateur | Cible | Mesure |
|---|---|---|
| Onboarding technique | < 2 h pour les 5 comptes, réalisé en séance conjointe | Horodaté de la première `AssumeRole` réussie à la première restitution |
| Fraîcheur des données | Scan quotidien ; toute donnée > 24 h est affichée « inconclusive », jamais comme courante | Métrique `scope_stale` exposée au client |
| Disponibilité de l'interface | 99 % en heures ouvrées France (8 h–20 h, jours ouvrés) | Mesure côté éditeur, rapport mensuel |
| Restitution POC | Remise sous 5 jours ouvrés après le premier scan complet, séparant **économies évitables (estimations, USD, grille et date de tarif citées)**, **coûts observés (FOCUS)** et **écarts comptables** — jamais agrégés ensemble | Document daté, versionné |
| Support | Un canal dédié (email), réponse sous 1 jour ouvré, correction des anomalies bloquantes sous 5 jours ouvrés | Registre partagé |
| Incidents de sécurité | Notification au client sous 72 h de la qualification, conformément au RGPD | — |

Ces cibles sont des engagements de moyens propres au pilote ; elles ne
préjugent pas des SLA de l'offre commerciale ultérieure.

## 3. Données et sécurité

- **Collecte minimale** : métadonnées d'inventaire (identifiants, versions,
  tailles, tags) et données de facturation FOCUS. Jamais de contenu de bases,
  de buckets, de logs applicatifs ni de secrets.
- **Localisation** : toutes les données du pilote résident en région
  eu-west-3 (Paris), chiffrées au repos (KMS) et en transit (TLS).
- **Rétention** : payloads bruts 90 jours, données de facturation 365 jours,
  résultats calculés pendant la durée du pilote + 30 jours, puis suppression.
- **Auditabilité** : journal d'audit des opérations remis au client sur
  demande.
- **Révocation** : le client peut couper l'accès à tout moment en supprimant
  le rôle IAM ; aucune capacité d'écriture n'existe côté éditeur.

## 4. Réversibilité et fin de pilote

À l'issue (ou à la résiliation, possible à tout moment avec préavis de
7 jours) :

1. Export complet remis au client sous 10 jours ouvrés : insights,
   inconclusives, ressources, faits et preuves associées (dump PostgreSQL de
   son instance + exports CSV) — sans plafond de lignes.
2. Suppression de l'ensemble des données du client sous 30 jours, attestation
   écrite à l'appui.
3. Le template IAM et les rapports remis restent la propriété du client.

## 5. Cadrage des résultats (clause importante)

- Les montants qualifiés d'« estimations » sont calculés à partir de grilles
  tarifaires publiques AWS, citées avec leur date et leur région tarifaire.
  Ils ne constituent ni un devis, ni une garantie d'économie.
- Les états « inconclusive » signalent une donnée que la plateforme n'a pas
  pu vérifier. Ils constituent une **information mise à disposition** du
  client ; la décision d'agir ou non sur tout écart ou état signalé, ainsi
  que ses conséquences, relèvent du client. La plateforme fournit un
  mécanisme d'acquittement permettant de tracer ces décisions.
- Le terme « constat » désigne la traçabilité technique (source, horodatage,
  méthode) des informations présentées ; il n'emporte aucune valeur
  probatoire au sens légal ni aucune mission d'audit certifié.

## 6. Critère de succès partagé

Le pilote est réputé concluant si la restitution identifie des économies
évitables annualisées supérieures au prix annuel proposé de l'offre, ou si
le client déclare avoir obtenu une visibilité qu'il ne pouvait pas produire
par ses moyens existants. Dans le cas contraire, le pilote s'arrête sans
frais supplémentaires ni reconduction.

## 7. Ce que ce pilote n'est pas

Pas de remédiation automatique, pas d'écriture dans les comptes du client,
pas de couverture EC2/S3 complète, pas de multi-tenant mutualisé (instance
dédiée), pas d'engagement de disponibilité 24/7. Toute extension fait
l'objet d'un avenant.

---

*Version 0.1 — 2026-07-18. Révision requise : conseil juridique (clauses 5
et 6, responsabilité, droit applicable), DPA à annexer.*
