# Modèle conceptuel d'audit

## Définition et objectifs

L'Audit Trail est l'historique traçable des événements significatifs du cycle
de vie d'une mission ou d'une User Story.

Il doit permettre de déterminer après coup :

- ce qui s'est produit ;
- l'ordre des événements ;
- l'état du repository concerné ;
- l'acteur et le rôle ayant agi ;
- les vérifications réalisées ;
- la raison pour laquelle la mission a été certifiée ou bloquée.

Ce modèle V1 est documentaire. Il ne définit ni moteur d'audit, ni stockage, ni
base de données, ni chaîne cryptographique.

## Contrat minimal d'un événement

| Champ | Rôle |
| --- | --- |
| `event_id` | Identifiant unique et stable de l'événement. |
| `timestamp` | Date et heure de l'événement avec fuseau explicite. |
| `event_type` | Type canonique de l'événement. |
| `subject` | Mission, User Story, Evidence, Gate ou certification concernée. |
| `actor` | Acteur attribuable à l'origine de l'événement. |
| `role` | Rôle conceptuel exercé par l'acteur. |
| `repository_commit` | Commit du repository auquel l'événement se rapporte. |
| `payload` | Données factuelles minimales nécessaires pour comprendre l'événement. |

Lorsqu'un événement concerne un working tree non propre, `repository_commit`
identifie `HEAD` et `payload` décrit aussi l'état pertinent non commité. Un SHA
seul ne doit pas faire croire qu'il représente des modifications non commitées.

## Types d'événements V1

Le catalogue minimal est :

| `event_type` | Événement significatif |
| --- | --- |
| `USER_STORY_CREATED` | Création d'une User Story. |
| `STATE_CHANGED` | Transition d'un état canonique vers un autre. |
| `EXECUTION_STARTED` | Début d'une exécution autorisée. |
| `EXECUTION_FINISHED` | Fin d'une exécution et résultat observable. |
| `EVIDENCE_PRODUCED` | Production d'une Evidence traçable. |
| `GATE_EVALUATED` | Évaluation d'un Gate et verdict obtenu. |
| `REJECTION_RECORDED` | Rejet et cause prouvée enregistrés. |
| `REMEDIATION_RECORDED` | Cause et remédiation explicite enregistrées. |
| `HUMAN_APPROVAL_RECORDED` | Approbation explicitement produite par le Human. |
| `CERTIFICATION_GRANTED` | Verdict de certification `CERTIFIED`. |
| `CERTIFICATION_REFUSED` | Verdict de certification `REJECTED`. |
| `CERTIFICATION_BLOCKED` | Verdict de certification `BLOCKED` et cause enregistrée. |

Chaque événement contient uniquement les faits connus. Une valeur requise
absente ou ambiguë reste inconnue et ne peut pas être reconstruite par
supposition.

## Ordre et lien avec le repository

`timestamp` permet d'ordonner les événements dans le temps ; `event_id` permet
de les distinguer sans ambiguïté. Lorsqu'un ordre exact est déterminant, il doit
être prouvé par les événements et leur contexte plutôt que déduit d'horodatages
ambigus.

Tout événement dépendant du contenu du repository identifie l'état Git auquel
il s'applique. Un événement associé à un commit ne décrit pas automatiquement
un autre commit.

## Append-oriented

L'Audit Trail est append-oriented : un événement historique n'est jamais
réécrit ou supprimé silencieusement pour faire disparaître un échec, un rejet,
un blocage ou une décision antérieure.

Une correction, une précision ou une remédiation produit un nouvel événement
qui conserve la relation avec l'événement initial dans `payload`. L'historique
original reste visible. Cette règle décrit un contrat d'intégrité conceptuel et
ne prescrit encore aucun mécanisme de stockage.

## Audit et preuves

Un événement d'audit atteste qu'une action ou une décision a été enregistrée ;
il ne remplace pas automatiquement l'Evidence sous-jacente. Les événements
`EVIDENCE_PRODUCED`, `GATE_EVALUATED` et ceux de certification conservent dans
`payload` les références nécessaires pour retrouver les contrats, Evidence et
résultats concernés.

Une déclaration d'acteur non étayée ne devient pas une preuve certifiante par
le seul fait d'être inscrite dans l'Audit Trail.
