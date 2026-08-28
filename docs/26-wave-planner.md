# Deterministic Wave Planner

## Définition et API

`WavePlanner.plan(dag_snapshot, readiness_snapshot, project_state) -> WavePlan`
construit une projection prospective de couches logiques. Une Wave regroupe
les User Stories pouvant atteindre le même niveau compte tenu exclusivement
des dépendances du DAG.

Les indices commencent à `0`. Les membres d'une même Wave sont indépendants au
regard du DAG uniquement : **same logical wave != safe concurrent execution**.
Une Wave n'est ni une autorisation d'exécution ni un groupe parallèle réputé
sans conflit.

## Readiness actuelle et plan prospectif

La readiness décrit ce qui est éligible maintenant. Le plan commence avec les
nœuds `READY`, puis peut placer un nœud `WAITING_DEPENDENCIES` dans une Wave
future si chaque dépendance non certifiée appartient à une Wave antérieure.
Pour calculer la couche suivante, il simule uniquement l'hypothèse que les
membres de la couche courante deviendront satisfaisants.

Cette hypothèse ne modifie aucun statut, ne crée aucune Certification et ne
promet aucune réussite. Une dépendance déjà `CERTIFIED` est satisfaite avant la
Wave 0 et n'est pas replanifiée. Une dépendance `BLOCKED`, `CANCELLED`,
`REJECTED`, `REMEDIATION_REQUIRED` ou `INELIGIBLE` ne devient jamais
prospectivement satisfaite.

## Layering, priorité et risque

Le layering utilise Kahn par couches en `O(V + E)` hors tris. Les Waves sont
ordonnées par `wave_index`. Dans chaque Wave, les membres sont triés par
priorité numérique croissante puis par ID lexical. La priorité ne change jamais
de couche. Le risque est conservé comme métadonnée et ne crée ni dépendance ni
restriction de parallélisme.

## Deferred

Chaque nœud non planifié reçoit une raison fermée :

- `BLOCKED` pour une readiness bloquée ;
- `INELIGIBLE` pour un travail déjà engagé dans le lifecycle ;
- `TERMINAL_SATISFIED` pour un nœud `CERTIFIED` déjà accompli ;
- `TERMINAL_UNSATISFIED` pour un nœud `CANCELLED` ;
- `UNPLANNABLE_DEPENDENCY` lorsqu'une dépendance ne peut être satisfaite par le
  plan courant.

La déférence se propage sans franchir artificiellement un blocker. Un plan
partiel reste valide lorsque ses branches planifiables sont indépendantes des
branches différées.

## Cohérence, recalcul et autorité

Le planner reconstruit la readiness canonique pour vérifier simultanément le
DAG, le ProjectState, les classifications et leurs détails. Toute divergence
bloque fail-closed. Aucun input n'est muté et aucun WavePlan n'est persisté.

Après un succès ou un échec réel, le plan doit être recalculé depuis le nouvel
état autoritatif. P3.5 analyse désormais séparément les conflits d'exécution
sans transformer une Wave logique en groupes ; le planner ne crée ni conflit,
scheduler, worktree, transition, exécution ou autorité de certification.
