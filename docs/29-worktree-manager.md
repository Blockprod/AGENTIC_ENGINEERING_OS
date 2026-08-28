# Worktree Manager

## API et responsabilités

`WorktreeManager` reçoit explicitement le primary repository root et une
worktree root existante, résolue et disjointe. Il expose uniquement :

- `initialize_registry()` ;
- `plan_assignment(mission, user_story, baseline_commit)` ;
- `activate(assignment_id, current_generation)` ;
- `inspect(...)`, `resume(...)` et `inspect_all(...)` ;
- `complete(...)` et `mark_failed(...)` ;
- `cleanup(...)`.

Le manager ne lance aucun Implementer, ne groupe aucune User Story et ne merge
aucune branche. Les snapshots `WorktreeAssignment`, `WorktreeRegistry`,
`WorktreeInspection` et `WorktreeReconciliation` sont immuables.

## Registre et autorité Git

Le registre versionné est persisté dans
`.agentic-engineering-os/worktrees.json`. Il contient seulement
`schema_version` et les assignments ordonnés par `assignment_id`. Son store
rejette JSON invalide, clés dupliquées, schéma invalide, ordre non canonique,
identités incohérentes et collisions de branche/path. L'initialisation vide est
explicite ; `load()` n'invente jamais de fallback.

Le store n'expose aucune méthode publique `save`. Chaque mutation reçoit une
capability privée liée au store, à l'opération et aux fingerprints exacts
avant/après, puis vérifie aussi la transition du lifecycle. Les écritures sont
atomiques par fichier temporaire, flush, `fsync` et `os.replace`; l'ancien
registre demeure autoritatif si l'écriture échoue.

Le registre décrit l'état attendu. `git worktree list`, les refs, HEAD et
`status` décrivent la réalité physique. `inspect_all()` rapporte toute
divergence, collision ou worktree du namespace `agentic/` sans assignment ; il
ne répare et n'adopte rien.

## Identité et lifecycle

L'identifiant `wa-<24 hex>` est le préfixe SHA-256 de l'encodage canonique
délimité de la mission, User Story, génération et baseline. La branche suit
`agentic/g<generation>/<user-story>-<suffixe>`, et le chemin suit
`<worktree_root>/<assignment_id>`. La baseline est un SHA complet explicitement
résolu comme commit.

Les transitions permises sont :

```text
PLANNED -> ACTIVE
PLANNED -> FAILED
ACTIVE -> COMPLETED
ACTIVE -> FAILED
COMPLETED -> CLEANED
FAILED -> CLEANED
```

P3.7 utilise `ACTIVE -> FAILED`; `PLANNED -> FAILED` reste un invariant de
store pour représenter une création échouée explicitement enregistrée par une
politique future. Aucun état terminal n'est réactivé.

## Création et reprise

`plan_assignment()` valide les identités, résout la baseline et persiste un
record `PLANNED` sans mutation Git. `activate()` applique l'ordre : intention
validée, repository/baseline/primary clean vérifiés, absence de collision,
`git worktree add -b` depuis la baseline explicite, vérification path/branche/
HEAD/propreté, puis seulement persistance `ACTIVE`.

Si Git échoue, le registre reste inchangé. Si la vérification post-création
échoue, un retrait normal est tenté seulement pour la ressource exacte et
propre ; sinon elle reste observable. Si Git réussit mais l'écriture du
registre échoue, l'opération globale échoue et `inspect_all()` détecte la
divergence au redémarrage.

`resume()` exige un assignment `ACTIVE`, la génération courante, le path et la
branche exacts, une baseline ancêtre, un HEAD égal au tip de branche et un
worktree toujours enregistré par Git. Un worktree actif dirty est resumable
mais expose `clean=False`; il n'est jamais nettoyé automatiquement.

## Completion, échec et cleanup

`complete()` observe lui-même le result commit. Le worktree doit être exact et
propre, HEAD doit différer de la baseline, en descendre et être le tip de la
branche. Le manager ne crée aucun commit. Le SHA observé est persisté comme
`result_commit` avec `COMPLETED`.

`mark_failed()` conserve branche, worktree, fichiers dirty et diagnostics ; il
ne déclenche aucun cleanup. `cleanup()` accepte uniquement `COMPLETED` ou
`FAILED`, exige l'absence d'intégration, une confirmation explicite et un
worktree propre. Il utilise `git worktree remove` sans force, vérifie la
disparition, puis persiste `CLEANED`. La branche est toujours conservée.

## Sécurité et limites

L'adaptateur Git utilise `subprocess.run` avec une liste d'arguments,
`shell=False`, capture stdout/stderr et refuse tout exit code inattendu. La
frontière P3.7 ne contient aucun merge ; les primitives de merge ajoutées en
P3.10 sont exposées uniquement au `MergeCoordinator`. L'adaptateur ne contient
ni reset/clean forcé, retrait forcé, suppression de branche, rebase,
cherry-pick, force-push, stash automatique, thread, async ou invocation Codex.

Le registre runtime et ses temporaires sont ignorés par Git. Le contrôle dirty
du primary exclut uniquement le registre canonique attendu ; tout autre
changement bloque les opérations structurantes. Un restart reconstruit tout
depuis le registre et Git, sans dépendre d'objets Python antérieurs.

P3.7 ne fournit ni coordinateur parallèle, décision de compatibilité,
Integration Gate, Merge Coordinator, suppression automatique de branche ou
transaction distribuée.
