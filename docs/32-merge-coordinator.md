# Merge Coordinator

## Rôle et prérequis

`MergeCoordinator.merge(MergeContext) -> MergeResult` est l'unique frontière
Phase 3 autorisée à intégrer un groupe parallèle dans la branche primaire. Il
exige un `IntegrationGateResult` P3.9 exactement lié au groupe et classé
`PASS`. `FAIL`, `UNKNOWN`, contexte incomplet ou Gate forgé interdisent toute
mutation. `MERGED` décrit uniquement une intégration Git : `MERGED ≠ CERTIFIED`.

Avant le staging, le coordinator recalcule le Gate depuis son contexte et exige
une égalité exacte avec le PASS fourni. Il revalide également la mission, la
génération, la Wave, le groupe, la baseline, les assignments `COMPLETED`, les
result commits, les tips de branches, le registre, le HEAD et la propreté du
primary. Une dérive produit un blocage fail-closed, notamment
`STALE_INTEGRATION_GATE`.

## Staging et ordre

L'intégration V1 est transactionnelle au niveau du groupe : elle est construite
hors du primary dans un worktree et une branche temporaires déterministes liés
à la mission, génération, Wave, groupe et baseline. Le chemin reste sous la
racine externe contrôlée par `WorktreeManager`; aucun chemin de plateforme
n'est codé en dur.

Chaque result commit exact est fusionné dans l'ordre
`IntegrationGateResult.integration_order` avec `git merge --no-ff`. Chaque
membre conserve ainsi une frontière de merge explicite, sans squash ni rebase.
Avant chaque merge, le coordinator recontrôle le primary, le staging, le tip du
membre et le registre. La réussite prouve uniquement la séquence appliquée ;
elle ne prétend pas que d'autres ordres seraient commutatifs ni admissibles.

## Promotion du primary

Après la séquence complète, la chaîne de premiers parents doit contenir un
merge explicite par membre, avec le result commit attendu comme second parent,
dans l'ordre certifié. Juste avant promotion, le primary doit encore être
propre et exactement sur la baseline. Une unique opération
revalide d'abord l'ancien HEAD attendu, puis un `git merge --ff-only` vers le
commit d'intégration synchronise sa branche, son index et son worktree. Une
dérive tardive fait échouer le fast-forward. Aucun `reset --hard`, force
checkout, stash, écrasement de ref ou résolution automatique n'est utilisé.

`MERGED` exige ensuite le HEAD exact, un primary propre, des branches membres
inchangées et un registre intact. Les worktrees et branches Implementer restent
`COMPLETED` et ne sont ni nettoyés ni supprimés.

## Échec, restart et idempotence

Un conflit Git pendant le staging produit `FAILED`; `git merge --abort` restaure
l'étape temporaire attendue lorsqu'il est applicable, et le primary reste
inchangé. Un contexte stale, dirty, divergent ou non observable produit
`BLOCKED`. Aucun conflit n'est résolu par `ours` ou `theirs`.

Les ressources temporaires sont conservées. Un restart peut reprendre une
ressource encore propre et exactement à la baseline. Une intégration partielle
ou ambiguë bloque sans cleanup forcé. Après une promotion réussie, un nouvel
appel reconnaît le même tip d'intégration déjà présent sur le primary et
retourne `MERGED` avec `ALREADY_MERGED`, sans créer un second commit.

P3.10 ne lance ni Tester ni Reviewer, ne modifie aucun lifecycle de User Story
ou `ProjectState`, ne certifie rien et ne progresse pas les Waves. P3.11 pourra
orchestrer ces responsabilités sur l'état intégré.
