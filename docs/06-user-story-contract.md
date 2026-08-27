# Contrat canonique d'une User Story

## Définition

Une User Story est l'unité canonique de travail planifiable, dépendante,
exécutable, vérifiable et certifiable de `AGENTIC_ENGINEERING_OS`.

Le contrat V1 est documentaire. Il ne constitue ni un JSON Schema, ni un modèle
Python, ni un DAG, ni une implémentation agentique.

## Champs canoniques V1

Chaque User Story possède au minimum les champs suivants :

| Champ | Rôle |
| --- | --- |
| `schema_version` | Version du contrat utilisé. |
| `id` | Identité stable de la User Story. |
| `title` | Intitulé court et non vide. |
| `description` | Besoin et résultat attendu. |
| `status` | État canonique du cycle de vie. |
| `priority` | Ordre relatif de traitement. |
| `risk` | Niveau de risque V1. |
| `depends_on` | IDs des User Stories dont elle dépend. |
| `scope` | Chemins autorisés et interdits. |
| `acceptance_criteria` | Conditions observables de réussite. |
| `required_gates` | Vérifications requises pour la certification. |
| `human_approval` | Exigence et preuve d'approbation humaine. |
| `metadata` | Données minimales de traçabilité. |

Tous les champs sont explicites, y compris les listes vides et les valeurs
nulles autorisées. Un champ obligatoire absent ou ambigu bloque la progression.

## Identité et version du contrat

`id` suit le format :

```text
US-XXXX
```

où `XXXX` représente exactement quatre chiffres. L'identifiant est unique dans
le projet, stable pendant tout le cycle de vie et non réutilisable pour une
autre User Story.

`schema_version` identifie la version du contrat afin de permettre son évolution
future. En V1, sa valeur est une chaîne explicite telle que `"1.0"`. Elle ne
permet pas de modifier silencieusement le sens d'une User Story existante.

## Status

`status` utilise obligatoirement et exclusivement l'un des 13 états définis
dans `docs/05-state-machine.md` :

- `PROPOSED`
- `PLANNED`
- `BLOCKED`
- `READY`
- `IN_PROGRESS`
- `IMPLEMENTED`
- `TESTING`
- `REVIEW`
- `CERTIFICATION`
- `CERTIFIED`
- `REJECTED`
- `REMEDIATION_REQUIRED`
- `CANCELLED`

Le contrat User Story ne définit aucun état supplémentaire. Les transitions et
leurs conditions restent celles de la machine d'état canonique.

## Priority et risk

`priority` est un entier supérieur ou égal à `1`. Une valeur plus petite indique
une priorité plus élevée : `1` précède `2`. Une égalité ne définit pas, à elle
seule, l'ordre d'exécution.

`risk` utilise exclusivement l'une des valeurs V1 suivantes :

- `LOW`
- `MEDIUM`
- `HIGH`
- `CRITICAL`

Le risque pourra ultérieurement influencer les gates ou les approbations. Cette
logique n'est pas définie ni implémentée dans cette mission.

## Dependencies

`depends_on` est une liste explicite contenant uniquement des IDs de User
Stories.

- Une User Story ne peut pas dépendre d'elle-même.
- Une référence ne peut pas apparaître plusieurs fois.
- Chaque User Story référencée doit exister dans le projet.
- Une dépendance est satisfaite uniquement si son état est `CERTIFIED`.
- Les cycles de dépendances seront interdits par le futur DAG Engine.

Cette mission ne définit ni n'implémente le DAG Engine. Une référence absente,
ambiguë, cyclique ou non certifiée empêche la User Story dépendante de devenir
`READY`.

## Scope

La structure minimale est :

```yaml
scope:
  allowed_paths: []
  forbidden_paths: []
```

Les chemins sont relatifs à la racine du repository. Le scope doit être
explicite. `forbidden_paths` prévaut toujours en cas de conflit avec
`allowed_paths`.

Toute modification hors scope est une anomalie qui exige un traitement
explicite. Le scope ne peut pas être élargi silencieusement pendant
l'implémentation.

## Acceptance criteria

`acceptance_criteria` est une liste non vide. Chaque critère possède au minimum :

```yaml
- id: AC-001
  description: ...
  mandatory: true
```

- L'ID est unique dans la User Story.
- La description exprime une condition observable et vérifiable.
- Le booléen `mandatory` identifie explicitement les critères obligatoires.
- Tout critère obligatoire non prouvé empêche la certification.
- Aucun critère ne peut être réduit ou supprimé pour transformer un échec en
  succès.

Une formulation vague comme `works correctly`, sans condition observable,
n'est pas un critère valide.

## Required gates

`required_gates` contient les IDs uniques des vérifications nécessaires à la
certification. Le catalogue complet des gates n'est pas défini dans cette
mission.

- Aucun gate obligatoire ne peut être ignoré silencieusement.
- `FAIL` bloque la certification.
- `UNKNOWN` bloque la certification.
- `NOT_APPLICABLE` suit strictement la politique P0.3 : il bloque sauf
  autorisation explicite par le contrat ou la politique applicable.
- Tous les gates requis doivent être résolus avant la certification.

Un ID de gate introuvable ou ambigu reste `UNKNOWN` et bloque la progression.

## Human approval

La structure minimale est :

```yaml
human_approval:
  required: false
  approved: false
  approved_by: null
  approved_at: null
```

Si `required` vaut `true`, la certification exige `approved: true`, un
`approved_by` attribuable à l'opérateur humain autorisé et un `approved_at`
traçable. Une approbation requise mais absente ou ambiguë bloque la
certification.

Codex ne peut jamais auto-produire, auto-attribuer ou auto-approuver une
approbation réservée au Human.

## Metadata

Les métadonnées V1 sont limitées à :

- `created_at` : date et heure de création ;
- `created_by` : auteur ou acteur attribuable de la création ;
- `updated_at` : date et heure de dernière modification.

Les dates utilisent une représentation ISO 8601 avec fuseau explicite. Aucune
métadonnée spéculative n'est requise.

## Contrôle des modifications du contrat

`id` est immuable dès son attribution. Dès que la User Story quitte `PROPOSED`,
les éléments suivants deviennent contrôlés :

- l'identité ;
- les dépendances ;
- le scope ;
- les critères d'acceptation ;
- les gates requis.

Toute modification ultérieure d'un élément contrôlé doit être explicite,
traçable, respecter Human Authority lorsqu'elle est requise et mettre à jour
`metadata.updated_at`. Elle ne peut jamais masquer un échec existant, réduire
rétroactivement le contrat pour obtenir un succès ou effacer les preuves déjà
produites.

Ce contrôle ne définit encore aucun mécanisme technique de versioning.

## Exemple canonique minimal

```yaml
schema_version: "1.0"
id: US-0001
title: Ajouter une note de documentation
description: Ajouter un document contenant le titre observable défini par le contrat.
status: PROPOSED
priority: 1
risk: LOW
depends_on: []
scope:
  allowed_paths:
    - docs/example.md
  forbidden_paths:
    - src/
    - tests/
acceptance_criteria:
  - id: AC-001
    description: Le fichier docs/example.md existe et contient le titre "# Exemple".
    mandatory: true
required_gates:
  - GATE-001
human_approval:
  required: false
  approved: false
  approved_by: null
  approved_at: null
metadata:
  created_at: "2026-08-27T10:00:00+02:00"
  created_by: human-operator
  updated_at: "2026-08-27T10:00:00+02:00"
```
