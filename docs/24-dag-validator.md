# DAG Validator

## API et projection

`DAGValidator.build(project_state) -> DAGSnapshot` construit une projection
read-only depuis les seules valeurs `UserStory.depends_on`. Les modèles
`DAGNode`, `DAGEdge` et `DAGSnapshot` sont immuables et ne dupliquent que les
données nécessaires au graphe.

Avant la projection, chaque User Story passe par son contrat existant via
`ContractValidator`. Le validateur refuse ensuite les IDs de nœuds dupliqués,
les dépendances absentes, les self-dependencies, les cycles et toute divergence
entre dépendances et edges. Aucune réparation ou dépendance implicite n'est
produite.

## Direction et déterminisme

Une edge `A → B` signifie que `B.depends_on` contient `A`. Les nœuds sont triés
lexicalement par `user_story_id`, leurs dépendances par ID, puis les edges par
`(dependency_id, dependent_id)`. Le ProjectState source et ses User Stories ne
sont jamais réordonnés ou mutés.

La détection de cycle utilise un parcours en profondeur coloré, itératif et
déterministe. La projection et la validation du graphe sont en `O(V + E)`, hors
les tris canoniques.

## Erreurs

`DAGValidationError` expose un `code`, un message et les sujets applicables.
Les principaux codes sont `INVALID_USER_STORY`, `DUPLICATE_NODE`,
`MISSING_DEPENDENCY`, `SELF_DEPENDENCY` et `CYCLE_DETECTED`. Toute erreur bloque
la construction sans modifier la source.

## Schéma et persistance

`schemas/dag-snapshot.schema.json` décrit la structure JSON Draft 2020-12 du
snapshot. Les références globales, cycles, IDs de nœuds uniques, complétude des
edges et ordres canoniques restent des invariants applicatifs vérifiés par
`DAGValidator`.

Le snapshot peut être sérialisé de façon déterministe avec `to_dict`, mais il
n'est jamais persisté. Aucun `dag.json` n'est créé.

## Frontière P3.3

P3.2 valide uniquement la structure logique. Il ne calcule ni dépendances
satisfaites, ni ready set, blocked set, Wave ou ensemble exécutable. P3.3
consommera un `DAGSnapshot` valide et les statuts correspondants pour déterminer
l'éligibilité sans obtenir d'autorité de transition.
