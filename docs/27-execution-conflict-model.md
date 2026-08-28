# Execution Conflict Model

## Dépendance logique et conflit d'exécution

Une dépendance logique impose qu'une User Story soit `CERTIFIED` avant une
autre et appartient au DAG. Un conflit d'exécution indique seulement que deux
User Stories d'une même Wave logique ne sont pas démontrées compatibles pour
une exécution simultanée. Il ne crée, ne supprime et ne réordonne aucune edge
du DAG.

`ExecutionConflictAnalyzer.analyze(wave_plan, project_state)` reconstruit le
DAG, la readiness et le WavePlan canoniques avant toute analyse. Un plan forgé
ou divergent est refusé fail-closed. La source de vérité reste le
`ProjectState`, en particulier `UserStory.scope`; aucun résultat n'est déduit
du filesystem, d'un LLM ou d'une ressource implicite.

## Classifications et raisons

Chaque paire distincte d'une même Wave reçoit exactement une classification :

- `SAFE` : aucune incompatibilité P3.5 applicable n'est connue et l'isolation
  de chemins est démontrée ;
- `CONFLICT` : un chevauchement de chemins est explicitement démontré ;
- `UNKNOWN` : le scope est absent ou ambigu et l'isolation ne peut pas être
  prouvée.

Les raisons fermées sont `PATH_OVERLAP`, `SCOPE_UNSPECIFIED` et
`SCOPE_AMBIGUOUS`. `UNKNOWN` doit être traité comme non sûr par toute future
décision de parallélisme. `SAFE` ne constitue jamais une autorisation
d'exécution, de scheduling, de worktree, de lock ou de transition Control
Plane.

## Sémantique des chemins

L'analyse réutilise la normalisation Implementer/Tester. Les chemins sont
repository-relative, en syntaxe POSIX, normalisés Unicode NFC puis comparés
avec `casefold()`, indépendamment du filesystem local. Les chemins absolus,
les séparateurs `\`, les segments vides, `.` ou `..`, les espaces périphériques
et les chemins vides sont invalides.

La barre oblique finale est la convention explicite de répertoire ou
sous-arbre : `src/auth/` couvre ses descendants. Sans barre finale, un chemin
désigne exactement un fichier. Ainsi `src/auth/` chevauche
`src/auth/models.py`, tandis que le fichier `src/auth` et le répertoire
`src/auth/` sont distincts. Deux fichiers identiques, deux sous-arbres
imbriqués, ou un fichier descendant d'un sous-arbre se chevauchent.

`forbidden_paths` est uniquement une contrainte négative et ne crée jamais un
conflit à lui seul. Il retire les régions interdites des chevauchements
possibles. Un scope autorisé entièrement neutralisé par son propre scope
interdit, ou des chemins déclarés distincts qui deviennent identiques après
normalisation, est ambigu et ne peut pas produire `SAFE` sans preuve restante.

## Analyse pairwise et déterminisme

Seules les paires de membres d'une même Wave sont pertinentes. Les Waves sont
traitées par index, les IDs de chaque paire sont ordonnés lexicalement avec
`left_user_story_id < right_user_story_id`, puis raisons et chemins de
chevauchement sont triés. Une Wave de N membres coûte O(N²). Les paires de
Waves différentes ne reçoivent aucune classification pour le plan courant.

`ConflictAnalysis` est une projection immuable et non persistée. Ses vues
`safe_pairs`, `conflicting_pairs` et `unknown_pairs` sont dérivées de la liste
canonique de paires. L'analyse ne modifie ni le ProjectState, ni les scopes, ni
le WavePlan, ni les projections DAG/readiness, y compris lors d'un refus. Tout
changement autoritatif de scope impose simplement un nouveau calcul.

## Limites V1

P3.5 V1 détecte uniquement les conflits prouvables depuis les métadonnées
actuelles. Le risque et la priorité ne sont pas des conflits. Aucune ressource
partagée hors chemins, inférence d'imports, migration, base de données ou lock
n'est modélisé sans contrat explicite. P3.5 ne construit aucun groupe sûr, ne
fait aucun graph coloring et ne lance aucune exécution.
