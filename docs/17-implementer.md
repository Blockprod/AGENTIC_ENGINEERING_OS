# Implementer V1

## Objet

L'Implementer exécute une User Story explicitement assignée dans son scope. Il
peut modifier les fichiers autorisés, ajouter ou adapter les tests associés et
réaliser des vérifications techniques. Il ne modifie ni le contrat, ni les
critères d'acceptation, ni l'état persistant, et ne certifie aucun résultat.

## Entrée autorisée

`ImplementerInput` est construit uniquement depuis un
`RoleHandoff(to_role=IMPLEMENTER, operating_step=ACT)` et une User Story valide
dont l'identifiant correspond au sujet du handoff. Il contient :

- `mission_id`, `workflow_generation`, `user_story`, `observed_commit` et
  `objective` ;
- les `blockers` et `instructions` transmis par l'Orchestrator.

La User Story doit être `IN_PROGRESS`, avoir un `allowed_paths` non vide et ne
présenter aucun blocker actif. Les autres états sont refusés : les passages
vers et depuis `IN_PROGRESS` appartiennent au `StateTransitionService` et non à
l'Implementer. Lorsqu'une décision Human préalable est requise, ses champs
d'approbation doivent être complets ; sinon l'affectation est bloquée.
L'identité d'approbation doit en outre être humaine et attribuable selon la
normalisation canonique existante : aucune variante de casse de l'identité
réservée Codex ne peut satisfaire cette condition.

La construction conserve un snapshot déterministe de l'affectation. Toute
modification ultérieure du statut, du scope, des critères, de l'approbation ou
du contexte invalide la sortie au lieu d'élargir l'autorité de l'Implementer.

## Scope déterministe

Les chemins sont des chemins POSIX relatifs au repository. Les chemins absolus,
segments vides, `.` ou `..`, antislashs, doublons après normalisation Unicode et
de casse, et chemins de dossier déclarés comme fichiers modifiés sont refusés.
Un fichier doit correspondre exactement à un chemin autorisé ou être descendant
d'un scope terminé par `/`. `forbidden_paths` prévaut sur `allowed_paths`.

Les fichiers `.agentic-engineering-os/state.json` et
`.agentic-engineering-os/mission.json` restent interdits même si le scope les
autorise. Chaque test déclaré comme ajouté ou modifié doit aussi figurer dans
`files_changed` et respecter le même scope.

## Sortie structurée

`ImplementerResult` contient :

- `mission_id`, `workflow_generation`, le rôle constant `IMPLEMENTER`,
  `subject`, `user_story_id` et `observed_commit` ;
- `summary`, `files_changed` et `tests_added_or_modified` ;
- `verification_commands` et leurs `verification_results` structurés ;
- `assumptions`, `findings`, `blockers`, `recommended_next_role` et `verdict`.

Les seuls verdicts sont :

- `READY_FOR_TEST`, sans blocker, avec des changements déclarés et une
  recommandation `TESTER` ;
- `BLOCKED`, avec au moins un blocker et une recommandation `ORCHESTRATOR`.

`READY_FOR_TEST` est un rapport de préparation au rôle Tester. Il ne vaut ni
preuve Evidence, ni succès des critères d'acceptation, ni certification, et ne
déclenche automatiquement ni routing ni transition vers `IMPLEMENTED`.

## Vérifications

Chaque commande déclarée correspond à un et un seul résultat : `PASS`, `FAIL`,
`UNKNOWN` ou `NOT_APPLICABLE`, avec son caractère obligatoire, son exit code
quand il existe et un détail observable. Un `PASS` exige un exit code `0` et un
`FAIL` un exit code non nul. `UNKNOWN` et `NOT_APPLICABLE` ne sont jamais
convertis en succès.

En V1, aucune autorité d'inapplicabilité n'est confiée à l'Implementer : toute
vérification obligatoire différente de `PASS`, y compris `NOT_APPLICABLE`,
interdit `READY_FOR_TEST`. Un résultat bloqué conserve le résultat observé sans
le réécrire.

## Frontière déterministe et flux

Le flux est séquentiel :

```text
Orchestrator
  -> RoleHandoff vers Implementer
  -> modifications Codex limitées au scope
  -> ImplementerResult
  -> validation JSON Schema et sémantique déterministe
  -> retour à Orchestrator
```

Le `ContractValidator` applique le JSON Schema Draft 2020-12. Le validateur de
résultat contrôle ensuite la cohérence avec l'entrée, le scope, les commandes
et les résultats obligatoires. Une sortie non validable reste bloquée.

L'Implementer ne produit pas d'Evidence, n'enregistre aucune approbation Human,
ne change pas le statut de la User Story et n'appelle aucun rôle suivant. Ces
responsabilités restent aux services et rôles autorisés par leurs contrats.
