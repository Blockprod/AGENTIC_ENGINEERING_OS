# Parallel Remediation and Recovery

## Taxonomie et API

P3.12 distingue les défauts démontrés des incertitudes techniques.

- Implementer `FAILED` ou `BLOCKED`, Integration Gate `FAIL`, Merge `FAILED`,
  Tester `REMEDIATION_REQUIRED` et Reviewer `REMEDIATION_REQUIRED` ouvrent une
  remédiation explicite.
- Integration Gate `UNKNOWN`, Merge `BLOCKED`, drift ou incohérence de registre
  imposent un recovery explicite sans nouvelle génération automatique.
- Les résultats de rôles `BLOCKED` n'autorisent jamais le rôle suivant.

`ParallelMissionWorkflow` expose `record_blocked_member()`,
`remediate_failed_group()`, `remediate_integration()`, `remediate_dossier()`,
`inspect_recovery()`, `block_for_recovery()` et `resume_recovery()`.
`ParallelRemediationPlan` décrit la cause, les stories affectées, les stories à
réexécuter, la nouvelle baseline et les artefacts préservés ou stale.
`ParallelRecoveryInspection` confronte la génération active, le primary, le
registre et les assignments failed/stale ; il ne répare rien.

## Génération et artefacts stale

La génération reste mission-wide. Un défaut démontré invalide le travail non
certifié appartenant à N et persiste N+1 dans `MissionState`. Les stories
fautives sont attribuées explicitement ; si l'attribution est ambiguë, un choix
group-level explicite est obligatoire. Les autres stories non certifiées qui
possèdent une assignment N sont rebaselinées et réexécutées par prudence, sans
être déclarées fautives. Une story déjà `CERTIFIED` conserve son statut et
continue de satisfaire ses dépendances.

Plans, groupes préparés, assignments, ImplementerResults, Integration Gates,
MergeResults, TesterResults, ReviewerResults et CertifierResults de N restent
historiques mais ne peuvent autoriser N+1. Les commits Git continuent d'exister :
leur existence physique ne leur confère aucune fraîcheur de workflow. Une
nouvelle exécution produit une nouvelle assignment, branche et worktree liés à
N+1 et à la baseline courante. Une assignment N n'est jamais réactivée en N+1.

Cette granularité globale peut invalider davantage de travail non certifié que
strictement nécessaire. P3.12 accepte ce coût conservateur et ne crée pas de
génération par story.

## Worktrees, Gate et Merge

Un groupe contenant des membres `COMPLETED` et `FAILED` reste non intégré. Les
branches, worktrees, commits et fichiers dirty sont conservés pour diagnostic ;
aucun cleanup automatique n'est effectué. Un Gate `FAIL` conserve également
le groupe complet et retourne à Implementer dans N+1. Le Gate doit être
reproductible au moment de la décision de remédiation.

Un Gate `UNKNOWN` bloque la mission dans N. Après résolution technique,
`resume_recovery()` exige un primary et un registre cohérents puis autorise
seulement une nouvelle observation explicite ; il ne retente ni Gate ni Merge.
Un Merge `FAILED` ouvre une remédiation explicite sans essayer un autre ordre.
Un Merge `BLOCKED` ou stale exige reconstruction/replan. Les branches
d'intégration échouées restent observables selon le contrat P3.10.

## Forward remediation post-merge

Après un merge M, un défaut Tester ou Reviewer ne déclenche ni reset, revert,
rebase ni rollback automatique. La correction ouvre N+1 avec M, le HEAD propre
actuel du primary, comme baseline. Un nouveau worktree produit un commit
correctif, puis Integration Gate et Merge produisent M2.

Tester, Reviewer et Certifier sont toujours rejoués après M2. Un résultat
positif de N, une Evidence liée à un ancien commit, un ancien Gate PASS ou un
ancien MergeResult ne peut autoriser N+1. Les règles existantes de fraîcheur
Evidence, Human Approval, `NOT_APPLICABLE` et CertificationIntegrity restent
les seules autorités ; P3.12 ne les duplique pas.

## Restart, Waves et défaillances croisées

Après restart, `MissionState`, `ProjectState`, le registre et Git reconstruisent
la génération active. Les assignments N restent stale/failed et leurs
worktrees restent présents. Le nouveau plan est calculé sur N+1 et la baseline
persistée ; tout ancien plan prospectif devient stale. Une Wave dépendante
reste bloquée jusqu'à la nouvelle Certification de ses dépendances. Une
remédiation n'ajoute aucune edge au DAG d'une composante indépendante.

Les écritures ProjectState et MissionState ne constituent pas une transaction
distribuée. Les transitions ProjectState précèdent l'incrément MissionState :
si elles échouent, N reste inchangée ; si la sauvegarde MissionState échoue, N
reste autoritative et aucune ressource N+1 ne peut être créée. Une assignment
terminale N face à une story rejouable N rend la divergence détectable et
bloquante. Une mutation Git réussie suivie d'un échec de registre conserve la
politique P3.7 : divergence observable, aucun succès global.

## Limites V1

P3.12 ne fournit aucun rollback/revert automatique, reset destructif, rebase,
cleanup daemon, retry Codex, génération par story ou transaction distribuée.
Une remédiation substantielle est toujours explicite et observable. P3.13
reste hors scope.
