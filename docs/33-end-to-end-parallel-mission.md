# End-to-End Parallel Mission

## Portée

`ParallelMissionWorkflow` compose les composants certifiés des Phases 1 à 3
pour exécuter une mission parallèle déterministe : DAG, readiness, Waves,
analyse des conflits, worktrees Implementer, Integration Gate, merge, puis
Tester, Reviewer, Certifier et Control Plane. Il n'accorde aucune nouvelle
autorité à ces composants et n'exécute aucun LLM automatiquement.

Une intégration Git `MERGED` n'est jamais une certification. Chaque membre
intégré doit fournir un artefact Implementer explicite observé sur le commit
d'intégration, puis traverser Tester, Reviewer et Certifier. Seul le Control
Plane peut persister `CERTIFIED`.

## Progression des Waves et groupes

Le workflow reconstruit le DAG, la readiness, les Waves et les conflits depuis
`ProjectState`, `MissionState`, le registre de worktrees et Git. Il prépare
uniquement le premier front exécutable courant. Les membres sans conflit
peuvent partager un groupe ; un conflit prouvé ou un conflit `UNKNOWN` impose
des groupes distincts.

Après le merge et la certification d'un groupe, le workflow reconstruit un
nouveau plan sur le nouveau HEAD. Cette reconstruction sérialise les groupes
conflictuels sans inventer de dépendance DAG. Une Wave dépendante ne devient
exécutable que lorsque ses dépendances sont réellement `CERTIFIED`. Une
approbation Human requise doit être enregistrée comme Evidence attribuable et
appliquée par le Control Plane avant toute readiness.

## Restart et état autoritatif

Aucun fichier d'état parallèle supplémentaire n'est créé. Après redémarrage,
le workflow recharge les états persistants et confronte le registre aux
ressources Git réelles. Un groupe déjà `ACTIVE` est repris uniquement si ses
assignments correspondent exactement à la mission, la génération, la baseline
et tous les membres attendus. Un merge déjà prouvé est reconnu par
`MergeCoordinator` de manière idempotente.

Les dossiers de rôles restent des artefacts explicites fournis par l'appelant ;
ils ne constituent pas une nouvelle mémoire autoritative. Toute divergence de
mission, génération, commit, story, fichiers gated, branche, worktree ou
résultat de rôle bloque la progression.

## Fail-closed

`FAIL`, `UNKNOWN`, membre échoué, plan périmé ou forgé, worktree incohérent,
diff hors scope, Gate non `PASS`, merge non prouvé, résultat Tester/Reviewer/
Certifier invalide ou Evidence insuffisante interdisent l'étape suivante. Le
workflow ne modifie pas Git directement : l'isolation, le Gate et le merge
restent sous l'autorité de leurs services dédiés.

La remédiation parallèle et la récupération avancée restent hors scope de
P3.11 et sont réservées à P3.12.
