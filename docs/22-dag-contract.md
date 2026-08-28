# Contrat canonique du DAG de User Stories

## Définition et source de vérité

Le DAG est la projection logique, validée et reconstruisible des dépendances
entre les User Stories d'un `ProjectState`. Il ne constitue pas une seconde
source de vérité : chaque dépendance provient exclusivement de
`UserStory.depends_on`.

Le DAG représente uniquement les dépendances logiques. Il ne décide ni de
l'exécution, du parallélisme, du scheduling, des Waves, des worktrees, des
conflits de fichiers ou de ressources, de l'intégration, ni du merge.

## Nœuds

Chaque nœud représente exactement une User Story existante. Le contrat logique
minimal `DAGNode` expose seulement :

- `user_story_id` : l'identifiant stable de la User Story ;
- `status` : son état canonique ;
- `priority` : sa priorité déclarée ;
- `risk` : son niveau de risque déclaré ;
- `depends_on` : les identifiants de ses dépendances logiques.

Une User Story apparaît une seule fois. Le nœud ne duplique pas le titre, le
scope, les critères d'acceptation, les Gates ou les autres données du contrat
User Story.

`priority` et `risk` sont des métadonnées disponibles pour de futurs
composants. Ils ne changent jamais la relation de dépendance. Une priorité
élevée ou un risque `CRITICAL` ne crée aucune edge implicite et P3.1 ne définit
aucun algorithme de scheduling.

## Edges et direction

La direction canonique est unique :

```text
A → B
```

signifie exactement : **B dépend logiquement de A**. Cette edge existe si et
seulement si `B.depends_on` contient `A`. Son origine est le prérequis et sa
destination est la User Story dépendante.

Le DAG n'invente, ne supprime et n'inverse aucune edge : toute dépendance
déclarée apparaît, et aucune edge absente de `depends_on` n'apparaît.

## Satisfaction des dépendances

Une dépendance est satisfaite uniquement lorsque la User Story prérequise est
`CERTIFIED`. Aucun autre état ne suffit, notamment `IMPLEMENTED`, `TESTING`,
`REVIEW`, `CERTIFICATION`, `REJECTED`, `BLOCKED` ou `CANCELLED`.

Le DAG décrit cette relation mais ne calcule pas encore les nœuds ready,
bloqués ou exécutables. Il ne réalise aucune transition et aucune remédiation.

## Invariants

Pour tout DAG construit depuis un `ProjectState` concret :

1. **Unique node** : chaque ID de User Story correspond à un seul nœud.
2. **Existing dependency** : chaque ID de `depends_on` correspond à une User
   Story existante du même `ProjectState`.
3. **No self-dependency** : aucun nœud ne dépend de lui-même.
4. **No cycles** : le graphe est acyclique.
5. **Deterministic projection** : un même `ProjectState` produit le même DAG
   logique.
6. **No implicit dependencies** : aucune edge n'est inventée.
7. **No missing declared dependencies** : toute dépendance déclarée produit
   exactement l'edge correspondante.

Une structure dupliquée, absente, ambiguë ou contradictoire invalide le DAG.
Aucune réparation silencieuse n'est autorisée.

## Cycles et politique fail-closed

Tout cycle est invalide, y compris :

```text
A → A
A → B → A
A → B → C → A
```

Un cycle n'est jamais cassé, réordonné ou ignoré automatiquement : il produit
un DAG invalide et bloque fail-closed. Une référence de dépendance absente, une
self-dependency, un nœud dupliqué ou une structure ambiguë ont la même
sémantique d'échec.

## États terminaux et états non satisfaisants

- `CERTIFIED` est terminal et peut satisfaire les dépendants.
- `CANCELLED` est terminal mais ne satisfait aucune dépendance. Ses dépendants
  restent bloqués jusqu'à une intervention explicite future ; leurs
  dépendances ne sont jamais réécrites automatiquement.
- `REJECTED` et `BLOCKED` ne satisfont jamais une dépendance. Le DAG reste une
  structure logique et n'exécute aucune remédiation.

## Dépendance logique et conflit d'exécution

Une **logical dependency** signifie que A doit être `CERTIFIED` avant B et se
représente par l'edge `A → B`.

Un **execution conflict** signifie que deux User Stories logiquement
indépendantes pourraient ne pas pouvoir s'exécuter simultanément, par exemple
si elles touchent les mêmes fichiers, migrations, schémas ou une même ressource
exclusive. Un conflit d'exécution n'est pas une dépendance logique et ne crée
aucune edge DAG. Son contrat est réservé à P3.5.

## Projection, ordre canonique et persistance

En V1, le DAG est une projection déterministe du `ProjectState`, pas un
artefact autoritatif persistant. Aucun `dag.json` n'est créé.

La représentation canonique ordonne les nœuds lexicalement par
`user_story_id`, les valeurs de `depends_on` lexicalement, puis les edges par
couple `(prerequisite_id, dependent_id)`. Elle ne dépend ni de l'ordre du
filesystem, ni d'un LLM, ni du réseau. Cet ordre assure une représentation
stable mais ne constitue pas un ordre de scheduling.

P3.1 n'introduisait ni modèle Python ni JSON Schema décoratif. P3.2 fournit
désormais le modèle concret, son schéma machine-validable et le DAG Validator
exécutable décrits dans `docs/24-dag-validator.md`.

## Relations avec les autorités existantes

- **Architect** : continue à produire `UserStory.depends_on`. Il ne produit pas
  de DAG autoritatif séparé.
- **Orchestrator** : pourra consulter la projection, mais ne pourra inventer,
  supprimer ou inverser une edge. Toute modification revient au contrat User
  Story et à son autorité applicable.
- **Control Plane** : le DAG n'est pas une autorité de transition. Les statuts
  restent sous `StateTransitionService` et `ControlLoop`. Le Readiness Engine
  consulte le DAG sans muter directement une User Story.

## Limites P3.1

P3.1 ne calcule aucun ready node, blocked node, ensemble exécutable, numéro de
Wave ou batch parallèle. Il n'implémente aucun moteur DAG, validateur,
scheduler, Wave Planner, worktree, exécution parallèle, intégration ou merge.
