# Machine d'état et cycle de vie

## Objet

Ce document définit le cycle de vie canonique V1 d'une mission ou d'une User
Story. Il décrit un contrat conceptuel et n'implémente aucune machine d'état.

## États canoniques

### `PROPOSED`

- **Signification** : la demande existe, mais son contrat n'est pas encore
  planifié.
- **Entrée minimale** : objectif ou besoin enregistré et attribuable.
- **Sorties autorisées** : `PLANNED`, `CANCELLED`.
- **Interdit** : implémenter, déclarer la demande prête ou présumer ses critères.

### `PLANNED`

- **Signification** : scope, critères, dépendances et plan sont explicités.
- **Entrée minimale** : contrat analysé et plan documenté par les rôles
  autorisés.
- **Sorties autorisées** : `READY`, `BLOCKED`, `CANCELLED`.
- **Interdit** : commencer l'implémentation ou ignorer une dépendance non
  certifiée.

### `BLOCKED`

- **Signification** : un obstacle identifié empêche la progression autorisée.
- **Entrée minimale** : cause du blocage et condition de déblocage enregistrées.
- **Sorties autorisées** : `READY`, `CANCELLED`.
- **Interdit** : poursuivre le travail comme si l'obstacle était résolu ou
  convertir l'incertitude en succès.

### `READY`

- **Signification** : le contrat est exécutable et tous ses prérequis sont
  prouvés.
- **Entrée minimale** : scope et critères explicites, plan exploitable,
  dépendances obligatoires `CERTIFIED` et aucun blocage actif.
- **Sorties autorisées** : `IN_PROGRESS`, `BLOCKED`, `CANCELLED`.
- **Interdit** : entrer ou rester dans cet état avec une dépendance obligatoire
  non certifiée.

### `IN_PROGRESS`

- **Signification** : l'implémentation autorisée est en cours.
- **Entrée minimale** : état précédent `READY` et début d'exécution traçable.
- **Sorties autorisées** : `IMPLEMENTED`, `BLOCKED`, `CANCELLED`.
- **Interdit** : travailler hors scope, réduire les critères ou déclarer
  l'implémentation terminée sans preuve.

### `IMPLEMENTED`

- **Signification** : les changements prévus sont réalisés et prêts à être
  vérifiés.
- **Entrée minimale** : changements autorisés achevés et preuves
  d'implémentation disponibles.
- **Sorties autorisées** : `TESTING`, `CANCELLED`.
- **Interdit** : sauter les tests, la review ou la certification.

### `TESTING`

- **Signification** : le comportement est vérifié contre le contrat.
- **Entrée minimale** : état précédent `IMPLEMENTED`, environnement et critères
  de test identifiés.
- **Sorties autorisées** : `REVIEW`, `REJECTED`, `CANCELLED`.
- **Interdit** : masquer un échec, modifier silencieusement l'implémentation ou
  traiter un résultat `UNKNOWN` comme réussi.

### `REVIEW`

- **Signification** : la qualité et la conformité de l'implémentation sont
  examinées.
- **Entrée minimale** : vérifications comportementales requises à `PASS` et
  preuves de test disponibles.
- **Sorties autorisées** : `CERTIFICATION`, `REJECTED`, `CANCELLED`.
- **Interdit** : corriger silencieusement une anomalie, réduire les critères ou
  certifier.

### `CERTIFICATION`

- **Signification** : les critères et preuves sont évalués pour produire le
  verdict final.
- **Entrée minimale** : review à `PASS`, critères applicables identifiés et
  dossier de preuves complet.
- **Sorties autorisées** : `CERTIFIED`, `REJECTED`, `CANCELLED`.
- **Interdit** : modifier l'implémentation, fabriquer une preuve, réduire les
  critères ou accepter `FAIL` ou `UNKNOWN`.

### `CERTIFIED`

- **Signification** : toutes les conditions applicables sont prouvées et la
  baseline est identifiable et reproductible.
- **Entrée minimale** : tous les gates obligatoires à `PASS`, certification
  explicite et état Git requis vérifié.
- **Sorties autorisées** : aucune en V1.
- **Interdit** : altérer silencieusement la baseline certifiée ou rouvrir la
  mission sans nouveau contrat explicite.

### `REJECTED`

- **Signification** : une vérification de testing, review ou certification a
  établi que le contrat n'est pas satisfait.
- **Entrée minimale** : échec observable, preuve conservée et origine du rejet
  identifiée.
- **Sorties autorisées** : `REMEDIATION_REQUIRED`, `CANCELLED`.
- **Interdit** : aller directement vers `CERTIFIED` ou `IN_PROGRESS`, masquer
  l'échec ou poursuivre sans remédiation explicite.

### `REMEDIATION_REQUIRED`

- **Signification** : l'échec est analysé et une correction minimale doit être
  préparée avant un nouveau cycle.
- **Entrée minimale** : rejet initial conservé, cause identifiée et périmètre de
  remédiation explicite.
- **Sorties autorisées** : `READY`, `CANCELLED`.
- **Interdit** : effacer la trace de l'échec, élargir silencieusement le scope ou
  éviter une nouvelle vérification des critères concernés.

### `CANCELLED`

- **Signification** : la mission est arrêtée par une décision humaine ou une
  autorité explicitement définie par le contrat.
- **Entrée minimale** : décision et motif d'annulation traçables.
- **Sorties autorisées** : aucune en V1 ; toute réouverture exige une décision
  humaine explicite future et un nouveau contrat applicable.
- **Interdit** : reprendre automatiquement le travail, certifier ou réactiver la
  mission sur décision implicite d'un agent.

## Parcours canonique

```text
PROPOSED
    ↓
PLANNED
    ↓
READY
    ↓
IN_PROGRESS
    ↓
IMPLEMENTED
    ↓
TESTING
    ↓
REVIEW
    ↓
CERTIFICATION
    ↓
CERTIFIED
```

## Règles de dépendances

- Une User Story avec une dépendance non satisfaite ne peut pas être `READY`.
- Une dépendance est satisfaite uniquement lorsqu'elle atteint `CERTIFIED`.
- Les états `IMPLEMENTED`, `TESTING`, `REVIEW` et `CERTIFICATION` ne suffisent
  pas à débloquer une dépendance.
- Une dépendance `REJECTED`, `BLOCKED` ou `CANCELLED` empêche la progression de
  ses dépendants, sauf politique explicite future.

## Règles fail-closed sur les transitions

Une transition est bloquée si :

- sa condition d'entrée n'est pas prouvée ;
- l'état courant est ambigu ;
- une dépendance obligatoire n'est pas `CERTIFIED` ;
- une preuve obligatoire manque ;
- un gate requis vaut `FAIL` ou `UNKNOWN`.

Le blocage d'une transition ne change pas implicitement l'état courant. Seule
une transition autorisée et prouvée peut le modifier. Aucun rôle agentique ne
peut forcer une transition interdite.

## Remédiation

```text
REJECTED
    ↓
REMEDIATION_REQUIRED
    ↓
READY
```

La remédiation conserve la trace de l'échec initial, identifie sa cause, limite
les changements au strict nécessaire et impose une nouvelle vérification
complète des critères concernés.

Les transitions `REJECTED → CERTIFIED` et `REJECTED → IN_PROGRESS` sont
interdites. Le passage explicite par `REMEDIATION_REQUIRED`, puis `READY`, est
obligatoire.

## Transitions humaines

Le Human peut notamment annuler une mission, approuver explicitement une
décision qui lui est réservée et autoriser un changement de scope ou de contrat.
Toute modification d'un contrat déjà engagé est traçable et ne peut jamais
servir à masquer un échec ou à convertir rétroactivement `FAIL` ou `UNKNOWN` en
`PASS`.

## Table des transitions V1 autorisées

| From | To | Allowed | Condition |
| ---- | -- | ------- | --------- |
| `PROPOSED` | `PLANNED` | Oui | Contrat et plan explicités. |
| `PROPOSED` | `CANCELLED` | Oui | Annulation autorisée et tracée. |
| `PLANNED` | `READY` | Oui | Prérequis prouvés et dépendances `CERTIFIED`. |
| `PLANNED` | `BLOCKED` | Oui | Obstacle et condition de déblocage identifiés. |
| `PLANNED` | `CANCELLED` | Oui | Annulation autorisée et tracée. |
| `BLOCKED` | `READY` | Oui | Cause résolue et conditions de `READY` prouvées. |
| `BLOCKED` | `CANCELLED` | Oui | Annulation autorisée et tracée. |
| `READY` | `IN_PROGRESS` | Oui | Début d'implémentation autorisé et tracé. |
| `READY` | `BLOCKED` | Oui | Un prérequis nécessaire n'est plus satisfait. |
| `READY` | `CANCELLED` | Oui | Annulation autorisée et tracée. |
| `IN_PROGRESS` | `IMPLEMENTED` | Oui | Implémentation achevée et preuves disponibles. |
| `IN_PROGRESS` | `BLOCKED` | Oui | Obstacle empêchant l'implémentation identifié. |
| `IN_PROGRESS` | `CANCELLED` | Oui | Annulation autorisée et tracée. |
| `IMPLEMENTED` | `TESTING` | Oui | Implémentation et critères de test disponibles. |
| `IMPLEMENTED` | `CANCELLED` | Oui | Annulation autorisée et tracée. |
| `TESTING` | `REVIEW` | Oui | Toutes les vérifications requises sont à `PASS`. |
| `TESTING` | `REJECTED` | Oui | Un échec de comportement est prouvé. |
| `TESTING` | `CANCELLED` | Oui | Annulation autorisée et tracée. |
| `REVIEW` | `CERTIFICATION` | Oui | Review à `PASS` et preuves complètes. |
| `REVIEW` | `REJECTED` | Oui | Une non-conformité de review est prouvée. |
| `REVIEW` | `CANCELLED` | Oui | Annulation autorisée et tracée. |
| `CERTIFICATION` | `CERTIFIED` | Oui | Tous les critères et gates applicables sont à `PASS`. |
| `CERTIFICATION` | `REJECTED` | Oui | Une condition de certification est à `FAIL`. |
| `CERTIFICATION` | `CANCELLED` | Oui | Annulation humaine autorisée et tracée. |
| `REJECTED` | `REMEDIATION_REQUIRED` | Oui | Cause et scope minimal de remédiation identifiés. |
| `REJECTED` | `CANCELLED` | Oui | Annulation autorisée et tracée. |
| `REMEDIATION_REQUIRED` | `READY` | Oui | Plan de remédiation prêt et conditions de `READY` prouvées. |
| `REMEDIATION_REQUIRED` | `CANCELLED` | Oui | Annulation autorisée et tracée. |

Toute transition absente de cette table est interdite par défaut. Cette règle
est fail-closed. `CERTIFIED` et `CANCELLED` sont terminaux en V1.
