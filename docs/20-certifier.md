# Certifier V1

## Rôle et autorité

Le Certifier reconstruit le dossier d'une User Story après la review et vérifie
sa complétude et sa cohérence. Son verdict `READY_FOR_CONTROL_PLANE` signifie
seulement que le dossier peut être soumis au workflow contrôlé. Il ne signifie
jamais `CERTIFIED` et le Certifier n'appelle pas automatiquement
`CertificationService`.

## Entrée et dossier

`CertifierInput` provient d'un handoff `ORCHESTRATOR → CERTIFIER` à l'étape
`CONTROLLED_TRANSITION`. La User Story est exactement en état `CERTIFICATION`.
L'entrée contient les résultats Architect, Implementer, Tester et Reviewer, les
Evidence disponibles, les Gates évalués, le commit explicite et les éventuelles
autorisations `NOT_APPLICABLE`. Elle est copiée et protégée par un snapshot.

La chaîne positive exige quatre artefacts présents, valides, associés à la même
mission, à la même User Story et au même commit, avec les verdicts `READY`,
`READY_FOR_TEST`, `READY_FOR_REVIEW` et `READY_FOR_CERTIFICATION`.

## Vérifications et verdicts

Le résultat contient les contrôles d'artefacts, d'Acceptance Criteria, de Gates,
les références Evidence, le contrôle Human, les findings et blockers.

- `READY_FOR_CONTROL_PLANE` exige un dossier complet, tous les critères
  obligatoires à `PASS`, les Gates requis satisfaits, les Evidence résolues,
  l'autorité Human requise et aucun blocker.
- `REMEDIATION_REQUIRED` exige un échec ou une contradiction démontrée.
- `BLOCKED` représente une preuve, une information ou une autorité manquante.

Le runtime conserve `FAIL → REMEDIATION_REQUIRED` et
`UNKNOWN → BLOCKED`; il ne transforme jamais `UNKNOWN` en `PASS`.

## Evidence, Human et NOT_APPLICABLE

Les références ne peuvent désigner que les Evidence réellement fournies. Un
résultat de rôle ou une observation du Certifier ne devient pas une Evidence du
Control Plane. Un résultat d'Acceptance Criterion porté par un rôle utilise
`PASS`, `FAIL` ou `UNKNOWN`, tandis qu'une Evidence Control Plane
`ACCEPTANCE_CRITERION_CHECK` utilise exclusivement le booléen explicite `true`
ou `false`. Aucune chaîne, valeur numérique ou valeur truthy/falsy n'est
convertie implicitement.

Une approbation requise exige une Evidence `HUMAN_APPROVAL`
applicable et une identité humaine attribuable selon les règles canoniques ;
Codex ne peut pas l'apporter.

Un Gate requis `NOT_APPLICABLE` reste bloquant sans identifiant explicitement
autorisé dans l'entrée. Le Certifier vérifie cette autorité, mais ne la crée pas.

## Frontière déterministe et handoff

```text
Orchestrator
  → RoleHandoff(to=CERTIFIER)
  → UserStory + ArchitectResult + ImplementerResult + TesterResult
    + ReviewerResult + Evidence + Gates
  → Codex / Certifier
  → CertifierResult
  → validation déterministe
  → Orchestrator
```

Le futur flux contrôlé pourra ensuite soumettre un résultat prêt à
`CertificationService`, qui seul produit `CERTIFIED`, `REJECTED` ou `BLOCKED`.
P2.8 ne crée aucune Certification, ne mute ni User Story ni ProjectState, ne
persiste rien et ne transforme pas son résultat en Evidence.
