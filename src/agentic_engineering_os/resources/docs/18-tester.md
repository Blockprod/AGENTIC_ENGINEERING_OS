# Tester V1

## Rôle et posture

Le Tester cherche activement à démontrer que l'implémentation ne satisfait pas
la User Story. Il lit le contrat et le résultat de l'Implementer, mais vérifie
indépendamment les comportements observables. Un `ImplementerResult` valide
prouve seulement que les déclarations reçues sont structurées et cohérentes ;
il ne prouve pas que le logiciel fonctionne.

Le Tester prépare un plan explicite couvrant les critères ciblés, les tests
positifs, négatifs, limites et de régression, ainsi que les commandes prévues.
Codex choisit ces vérifications créatives. Le runtime valide uniquement leur
structure et la cohérence de leurs résultats.

## Entrée et éligibilité

`TesterInput` est dérivé d'un handoff
`ORCHESTRATOR → TESTER` pour l'étape `VERIFY`, d'une User Story assignée et d'un
`ImplementerResult` `READY_FOR_TEST`. Mission, génération, sujet, User Story,
commit et résultat d'implémentation doivent être cohérents et sans blocker
actif.

La User Story doit être `TESTING`. Le passage canonique
`IN_PROGRESS → IMPLEMENTED → TESTING` reste exclusivement contrôlé par le
`StateTransitionService`; le Tester ne modifie jamais le statut. Un snapshot
de l'affectation bloque toute altération ultérieure du contrat ou des inputs.

## Sortie et résultats d'acceptation

`TesterResult` contient notamment la génération du handoff, le contexte, le plan, les résultats des critères, les
cas de test, les fichiers de tests modifiés, les commandes et résultats
observés, les findings, blockers, rôle recommandé et verdict.

Chaque résultat de critère référence un identifiant connu et utilise `PASS`,
`FAIL`, `UNKNOWN` ou `NOT_APPLICABLE` :

- `PASS` signifie que la satisfaction est étayée par une observation déclarée ;
- `FAIL` signifie qu'une observation démontre l'échec ;
- `UNKNOWN` signifie que le Tester ne peut pas conclure ;
- `NOT_APPLICABLE` ne satisfait pas un critère obligatoire sans autorisation
  explicite, absente de ce rôle V1.

Une absence de résultat obligatoire est traitée comme une inconnue. Les chaînes
d'observation du résultat ne deviennent pas automatiquement des Evidence du
Control Plane.

## Cas de test et commandes

Les types V1 sont `POSITIVE`, `NEGATIVE`, `EDGE` et `REGRESSION`. Chaque cas
indique objectif, résultat attendu, résultat observé, caractère obligatoire,
exécution et verdict. `PASS` ou `FAIL` exige `executed=true`.

Chaque commande planifiée et déclarée correspond à exactement un résultat
observable. `PASS` exige une exécution déclarée et un exit code `0`; `FAIL`, une
exécution et un exit code non nul. Une commande non exécutée reste `UNKNOWN` et
ne peut pas devenir `PASS`. La véracité de l'exécution reste une obligation
opérationnelle de Codex et une déclaration Tester n'est pas une Evidence.

## Verdicts fail-closed

- `READY_FOR_REVIEW` exige tous les critères obligatoires et tests requis à
  `PASS`, les commandes requises exécutées à `PASS`, la couverture des quatre
  types V1 et aucun blocker. Il recommande seulement `REVIEWER`.
- `REMEDIATION_REQUIRED` exige un échec explicite et au moins un finding. Il
  recommande un retour futur vers `IMPLEMENTER`.
- `BLOCKED` exige un blocker et s'applique lorsqu'une vérification obligatoire
  est inconnue ou qu'une précondition manque.

Ces verdicts n'autorisent aucun routing, aucune transition et aucune
Certification.

## Scope et no silent fix

Le Tester peut ajouter ou adapter uniquement des fichiers situés sous un
dossier `tests` et couverts par `allowed_paths`. Les chemins absolus, traversal,
antislashs, doublons normalisés, chemins hors scope et fichiers interdits sont
refusés ; `forbidden_paths` prévaut. Les fichiers d'état du Control Plane sont
toujours interdits.

Un défaut produit un finding et `REMEDIATION_REQUIRED`. Le Tester ne modifie
pas le code de production pour transformer son propre `FAIL` en `PASS`; toute
correction métier revient à une future intervention de l'Implementer.

## Human Authority, Control Plane et flux

Une approbation Human explicitement requise doit être complète et attribuable.
La normalisation canonique refuse les identités Codex comme producteurs Human.
Une décision absente n'est jamais simulée ni convertie en succès.

```text
Orchestrator
  -> RoleHandoff(to=TESTER)
  -> UserStory + ImplementerResult
  -> Codex / Tester
  -> TesterResult
  -> validation déterministe
  -> Orchestrator
```

Le validateur contrôle le JSON Schema Draft 2020-12, le contexte, les critères
connus, les verdicts, les résultats obligatoires, les blockers, les commandes
et le scope. Il ne choisit pas les tests, n'invente pas les cas limites et ne
décide pas heuristiquement si le code est bon. Aucun rôle suivant n'est appelé
automatiquement et aucune Evidence n'est enregistrée par ce contrat.
