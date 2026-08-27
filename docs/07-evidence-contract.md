# Contrat canonique des preuves

## Définition

Une Evidence est une preuve observable et traçable utilisée pour démontrer un
fait nécessaire à la vérification ou à la certification.

Une déclaration d'agent telle que `tests passed` ne constitue pas, à elle
seule, une Evidence suffisante. Elle doit être étayée par une observation
attribuable et reproductible.

Ce contrat V1 est documentaire. Il n'implémente ni modèle Python, ni JSON
Schema, ni moteur de preuves, ni agent.

## Champs canoniques V1

| Champ | Rôle |
| --- | --- |
| `evidence_id` | Identifiant unique et stable de la preuve. |
| `evidence_type` | Type conceptuel de preuve V1. |
| `subject` | Fait, User Story, critère, gate, fichier ou état concerné. |
| `result` | Observation produite, sans présumer le verdict d'un Gate. |
| `source` | Outil, système, inspection ou autorité à l'origine de l'observation. |
| `command` | Commande exacte exécutée, lorsqu'elle s'applique. |
| `exit_code` | Code de sortie observé, lorsqu'il s'applique. |
| `artifact` | Sortie, fichier ou référence persistante portant la preuve. |
| `commit` | Commit ou état Git auquel la preuve s'applique. |
| `timestamp` | Date et heure de production avec fuseau explicite. |
| `producer` | Producteur attribuable et, pour Codex, rôle exercé. |

`result` décrit une observation brute, par exemple un nombre de tests et leur
sortie. Il ne remplace pas le résultat d'un Gate.

Selon `evidence_type`, `command`, `exit_code`, `artifact` ou `commit` peuvent
être explicitement non applicables. Une valeur absente ou nulle alors qu'elle
est requise pour établir le fait rend la preuve insuffisante ; elle ne peut
jamais être interprétée comme un succès.

## Types de preuves V1

Le catalogue conceptuel minimal est :

| `evidence_type` | Fait représenté |
| --- | --- |
| `COMMAND_RESULT` | Résultat observable d'une commande. |
| `TEST_RESULT` | Résultat d'une exécution de tests. |
| `GIT_STATE` | Branche, commit, diff ou propreté du repository. |
| `FILE_INSPECTION` | Présence, absence ou contenu observé d'un fichier. |
| `REVIEW_RESULT` | Conclusions traçables d'une review. |
| `HUMAN_APPROVAL` | Approbation explicitement produite par le Human. |
| `ACCEPTANCE_CRITERION_CHECK` | Vérification observable d'un critère d'acceptation. |

Ce catalogue suffit à exprimer les preuves V1 sans définir de mécanisme
d'exécution ou de stockage.

## Lien avec Git

- Toute Evidence dépendant du contenu du repository identifie le commit ou
  l'état Git exact auquel elle s'applique.
- Pour un working tree non propre, le seul SHA de `HEAD` est insuffisant :
  l'état pertinent et les modifications doivent rester identifiables.
- Une Evidence produite pour un commit ne certifie pas automatiquement un autre
  commit.
- Une modification pertinente après la production d'une Evidence peut la
  rendre stale.
- Une Evidence stale nécessaire à la certification doit être revérifiée sur
  l'état applicable.

La détection technique de staleness n'est pas définie dans cette mission.

## Provenance

Une Evidence identifie son origine et son producteur. La provenance distingue
au minimum :

- un outil ou une commande, avec l'invocation et le résultat observé ;
- Codex, avec le rôle conceptuel exercé au moment de la production ;
- un opérateur humain explicitement attribuable.

Une affirmation non traçable n'est pas promue automatiquement en preuve
certifiante. Une production par Codex ne dispense jamais de conserver la source
observable sur laquelle repose son affirmation.

## Approbation humaine

Une approbation humaine requise est une Evidence de type `HUMAN_APPROVAL`. Elle
provient explicitement du Human, désigne le sujet concerné et conserve une
provenance et un horodatage traçables.

Codex ne peut jamais fabriquer, déduire, simuler ou auto-produire cette
Evidence. Son absence rend toute condition d'approbation requise non prouvée.

## Staleness

Une Evidence peut devenir `STALE` lorsque l'état auquel elle s'applique a changé
de manière pertinente. `STALE` décrit l'applicabilité d'une Evidence ; ce n'est
pas un résultat de Gate et cela n'ajoute aucune valeur aux quatre résultats
définis par P0.3.

```text
Evidence tests → commit A
repository → commit B pertinent
        ↓
Evidence potentially STALE
        ↓
Gate cannot rely on it blindly
        ↓
re-verification required
```

Une Evidence potentiellement stale ne peut pas soutenir aveuglément un Gate.
Si sa validité pour l'état courant n'est pas prouvée, le Gate concerné reste
`UNKNOWN` jusqu'à revérification.
