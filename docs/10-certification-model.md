# Modèle conceptuel de certification

## Définition

La certification est le verdict formel indiquant que les conditions
obligatoires d'un contrat ont été prouvées satisfaites pour un état identifiable
du repository.

Ce modèle V1 est documentaire. Il ne définit ni moteur de certification, ni
JSON Schema, ni modèle Python.

## Contrat minimal

| Champ | Rôle |
| --- | --- |
| `certification_id` | Identifiant unique et stable de la décision. |
| `subject` | Mission ou User Story évaluée. |
| `result` | Verdict canonique de certification V1. |
| `commit` | Commit Git exact auquel le verdict s'applique. |
| `acceptance_results` | Résultats et preuves des critères d'acceptation. |
| `gate_results` | Résultats des Gates applicables. |
| `human_approval` | État et Evidence des approbations humaines requises. |
| `evidence_refs` | Références uniques vers les Evidence utilisées. |
| `certified_at` | Date et heure de la décision avec fuseau explicite. |
| `certifier` | Certifier attribuable et rôle exercé. |

Chaque référence nécessaire doit être résolue et applicable au sujet et au
commit évalués. Une donnée obligatoire absente ou ambiguë ne peut pas être
présumée satisfaite.

## Verdicts V1

`result` utilise exclusivement :

- `CERTIFIED`
- `REJECTED`
- `BLOCKED`

### `CERTIFIED`

Ce verdict est autorisé uniquement si :

- tous les critères d'acceptation obligatoires sont prouvés satisfaits ;
- tous les Gates requis sont satisfaits selon leur politique ;
- toutes les approbations humaines requises sont présentes et traçables ;
- toutes les Evidence nécessaires sont applicables et non stale ;
- le commit certifié est identifiable ;
- aucune condition obligatoire ne reste `UNKNOWN`.

### `REJECTED`

Ce verdict est utilisé lorsqu'une Evidence applicable démontre qu'une condition
obligatoire échoue.

### `BLOCKED`

Ce verdict est utilisé lorsque la certification ne peut pas être déterminée
correctement, notamment à cause d'une Evidence absente, insuffisante, ambiguë,
stale ou d'un état requis inconnu. `BLOCKED` ne peut jamais être interprété
comme une certification partielle ou implicite.

## Alignement avec la machine d'état

- Le verdict `CERTIFIED` permet la transition canonique vers l'état User Story
  `CERTIFIED`.
- Un échec prouvé et le verdict `REJECTED` peuvent conduire à l'état canonique
  `REJECTED`.
- Le verdict `BLOCKED` bloque la progression mais ne crée pas une transition
  `CERTIFICATION → BLOCKED`, absente de P0.5. L'état courant ne change pas
  implicitement.
- Aucune décision de certification ne contourne la boucle
  `REJECTED → REMEDIATION_REQUIRED → READY`.

Aucun nouvel état de User Story n'est défini par ce modèle.

## Certification liée au commit

```text
Certification
      ↓
specific repository state
      ↓
specific Git commit
```

Une certification obtenue pour le commit A ne certifie pas automatiquement le
commit B. Une modification pertinente après certification impose une nouvelle
vérification avant de considérer le nouvel état comme certifié.

Si le contenu évalué n'est pas représenté par un commit identifiable, la
condition de certification reste non prouvée et le verdict est `BLOCKED`.

## Certification par preuves

```text
USER STORY CONTRACT
        ↓
ACCEPTANCE CRITERIA
        +
REQUIRED GATES
        +
HUMAN APPROVAL
        ↓
EVIDENCE
        ↓
CERTIFICATION DECISION
```

Le Certifier ne peut pas remplacer une Evidence manquante par son jugement. Une
recommandation de Reviewer et une déclaration d'Implementer ne valent pas
automatiquement certification.

## Human Authority

```text
approval absent
      ↓
certification impossible
      ↓
BLOCKED
```

Lorsqu'une approbation humaine est requise, seule une Evidence
`HUMAN_APPROVAL` explicitement applicable peut la prouver. Codex ne peut pas la
fabriquer ni la déduire du silence ou d'une action antérieure non explicitement
applicable.

## Auditabilité de la certification

Toute décision de certification permet de retrouver :

- le contrat évalué ;
- le commit concerné ;
- les critères d'acceptation et leurs résultats ;
- les Gates et leurs résultats ;
- les Evidence utilisées ;
- les approbations humaines éventuelles ;
- le Certifier ;
- le verdict ;
- la date de décision.

La décision produit l'événement d'audit correspondant sans réécrire les
événements historiques.

## Frontière V1 de certification transitive

La certification individuelle de plusieurs User Stories ne prouve pas
automatiquement que leur intégration combinée est correcte.

La future architecture devra prévoir une vérification d'intégration ou
transitive avant certaines promotions ou releases. Ce mécanisme n'est pas
spécifié dans cette mission.

## Exemples

### 1. Certification réussie

Tous les critères obligatoires et Gates requis sont satisfaits, les Evidence
sont applicables au commit `abc123` et l'approbation requise est présente : le
verdict peut être `CERTIFIED` pour ce commit.

### 2. Condition obligatoire en échec

Une Evidence applicable établit qu'un Gate requis vaut `FAIL` : le verdict est
`REJECTED` et la boucle de remédiation reste obligatoire.

### 3. Evidence obligatoire absente

Une Evidence obligatoire manque ou la condition reste `UNKNOWN` : le verdict
est `BLOCKED`, jamais `CERTIFIED`.
