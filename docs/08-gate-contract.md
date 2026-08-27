# Contrat canonique des gates

## Définition

Un Gate est une décision de vérification portant sur une condition explicite et
fondée sur des Evidence suffisantes, applicables et traçables.

Ce contrat V1 est documentaire. Il n'implémente ni moteur de gates, ni modèle
Python, ni JSON Schema, ni agent.

## Champs canoniques V1

| Champ | Rôle |
| --- | --- |
| `gate_id` | Identifiant unique du Gate dans son contrat. |
| `subject` | Condition, User Story ou critère évalué. |
| `required` | Indique si le Gate est requis pour la certification. |
| `result` | Verdict canonique de l'évaluation. |
| `evidence_refs` | Références uniques vers les Evidence utilisées. |
| `evaluated_at` | Date et heure d'évaluation avec fuseau explicite. |
| `evaluator` | Évaluateur attribuable et rôle conceptuel exercé. |

Une référence d'Evidence absente, introuvable, ambiguë ou inapplicable ne peut
pas être présumée valide.

## Résultats canoniques

`result` utilise exclusivement les quatre valeurs définies par P0.3 :

- `PASS`
- `FAIL`
- `UNKNOWN`
- `NOT_APPLICABLE`

### `PASS`

La condition du Gate est prouvée satisfaite par des Evidence suffisantes et
applicables.

### `FAIL`

Les Evidence démontrent que la condition n'est pas satisfaite.

### `UNKNOWN`

Les Evidence sont absentes, insuffisantes, ambiguës, invalides, stale ou
impossibles à obtenir. `UNKNOWN` ne satisfait jamais un Gate requis.

### `NOT_APPLICABLE`

Le Gate ne s'applique pas au sujet évalué. Il n'autorise la progression que si
cette non-applicabilité est explicitement permise par le contrat ou la politique
applicable.

## Gates requis et optionnels

Pour un Gate requis :

```text
PASS             → satisfait
FAIL             → bloque
UNKNOWN          → bloque
NOT_APPLICABLE   → bloque sauf autorisation explicite
```

Un Gate optionnel peut fournir une information sans nécessairement bloquer la
certification. Son caractère optionnel doit être défini par le contrat avant
son évaluation. Un Gate ne peut jamais devenir optionnel simplement parce qu'il
échoue ou reste `UNKNOWN`.

Un Gate optionnel ne peut ni remplacer un Gate requis ni neutraliser son
résultat.

## De l'observation au verdict

```text
OBSERVATION
    ↓
EVIDENCE
    ↓
GATE EVALUATION
    ↓
PASS / FAIL / UNKNOWN / NOT_APPLICABLE
```

Un Gate ne doit ni inventer ni présumer une Evidence manquante. Plusieurs
Evidence peuvent alimenter un même Gate. Une même Evidence peut être référencée
par plusieurs Gates lorsqu'elle est réellement pertinente et applicable à leur
sujet et à l'état évalué.

La certification s'appuie sur les résultats des Gates applicables ; une
affirmation d'agent ne peut pas court-circuiter la chaîne
Evidence → Gate → Certification.

## Critères d'acceptation

Chaque critère d'acceptation obligatoire défini par P0.6 doit pouvoir être
associé à une vérification observable et à des Evidence traçables.

Un critère obligatoire non vérifié, ambigu ou reposant uniquement sur une
affirmation non prouvée empêche la certification. Il ne peut pas être considéré
satisfait par défaut.

## Human Authority

Lorsqu'une approbation humaine est requise, le Gate correspondant doit
référencer une Evidence `HUMAN_APPROVAL` valide, produite explicitement par le
Human et associée au sujet évalué.

Codex ne peut jamais fabriquer, déduire ou auto-produire cette Evidence, ni
convertir son absence en `PASS`. Sans approbation traçable, le Gate requis est
`UNKNOWN` et bloque la certification.

## Exemples

### 1. Test réussi

L'Evidence `EV-001` enregistre `pytest -q`, le résultat observable `1 passed`,
le code de sortie `0`, le commit `abc123` et la provenance de l'outil. Le Gate
`GATE-001` référence `EV-001` et peut produire `PASS` si cette Evidence est
suffisante, applicable et non stale.

### 2. Test non exécuté

Aucune Evidence d'exécution n'existe. Le Gate requis `GATE-002` reçoit
`UNKNOWN` et bloque la certification ; une affirmation selon laquelle le test
devrait passer ne change pas ce verdict.

### 3. Approbation humaine absente

Le contrat exige une approbation humaine, mais aucune Evidence
`HUMAN_APPROVAL` attribuable n'existe. Le Gate requis `GATE-003` reçoit
`UNKNOWN` et bloque la certification. Codex ne peut pas produire l'approbation
manquante.
