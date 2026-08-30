# Codex Runtime Adapter

## Portée

P4.5 implémente uniquement la frontière de transport observée en P4.4 :

```text
CompiledPrompt + CodexExecutionBinding
→ CodexRuntimePort
→ CodexRuntimeAdapter
→ codex exec
→ CodexExecutionObservation
```

`CODEX EXECUTES. CONTROL PLANE DECIDES.` L'observation est un relevé technique
immuable. Elle n'est ni un RoleResult validé, ni une Evidence, ni un Gate, ni
une Certification. Un exit code `0` ne signifie pas réussite métier.

P4.5 n'implémente ni intake structuré P4.6, ni persistance/restart P4.7, retry,
orchestration de rôles, parallélisme ou intégration UI VS Code.

## Contrat applicatif

Le module `application.codex_runtime` expose :

- `CodexRuntimePort.execute(compiled_prompt, binding, cancellation=None)` ;
- `CodexExecutionBinding`, qui lie request, empreinte de contexte, mission,
  génération, rôle, sujet, cwd, commit attendu, sandbox, approbation, timeout et
  éventuel schéma de sortie ;
- `CodexExecutionObservation`, les événements JSONL et les observations Git ;
- les politiques fermées `READ_ONLY | WORKSPACE_WRITE` et `NEVER`.

`CompiledPrompt` transporte les métadonnées déjà validées par P4.2/P4.3 :
mission, génération, sujet, repository/worktree et commit. L'adapter compare
ces valeurs au binding ; il ne parse jamais le texte du prompt pour reconstruire
une autorité.

## Configuration et invocation

`CodexRuntimeConfiguration` exige un executable, son chemin canonique attendu,
sa version exacte et son SHA-256. Ces propriétés sont réobservées avant chaque
exécution. Une absence, substitution, modification ou version divergente bloque
le lancement et reste visible dans `issues`.

L'invocation est une liste d'arguments avec `shell=False` :

```text
<codex> -a never exec --ephemeral --ignore-user-config --json
        --color never --sandbox <mode> -C <cwd>
        [--output-schema <schema>] -
```

Le prompt complet est transmis exclusivement par stdin. Le cwd ne peut pas être
choisi par Codex : son chemin résolu doit correspondre exactement au repository
ou worktree porté par `CompiledPrompt`. Le schéma optionnel doit être un fichier
JSON existant, non symlink et situé sous ce cwd. Il est seulement transporté ;
sa validation métier appartient à P4.6.

`danger-full-access`, auto-approval, `--add-dir`, commande shell concaténée et
prompt en argument sont exclus de la configuration P4.5.

## Environnement enfant

L'adapter ne copie pas `os.environ`. Il reconstruit un environnement à partir
d'une allowlist fermée de variables système nécessaires au runtime et au
toolchain, puis ajoute uniquement `GIT_TERMINAL_PROMPT=0`, `NO_COLOR=1` et
`PYTHONIOENCODING=utf-8`. Les noms ressemblant à une clé API, un token, secret,
password ou credential sont refusés dans l'allowlist. Aucune valeur
d'environnement n'est enregistrée dans l'observation.

L'authentification n'est pas inventée : P4.4 a observé que le runtime installé
utilise son mécanisme externe même avec `--ignore-user-config`. Si cette
authentification n'est pas disponible, le processus l'expose factuellement.

## Préflight et observations Git

Avant le lancement, l'adapter vérifie :

1. cohérence exacte `CompiledPrompt` / `CodexExecutionBinding` ;
2. cwd absolu, existant et égal au binding repository/worktree ;
3. executable, empreinte et version ;
4. HEAD Git égal au commit attendu ;
5. worktree propre lorsque le binding l'exige.

Après toute exécution démarrée, y compris non-zero, timeout ou interruption,
HEAD et propreté sont réobservés. Un échec Git produit `head_commit=None` ou
`clean=None` avec une erreur explicite ; il n'est jamais converti en état
`clean`. Aucun reset, revert, stash, cleanup ou retry n'est effectué.

## Processus, timeout et interruption

stdout et stderr sont drainés séparément, l'exit code et le PID sont conservés,
et les timestamps sont normalisés en UTC. Le parent impose un timeout explicite.
À expiration ou cancellation, il termine le processus direct disponible, attend
sa fin puis réobserve Git. Il ne garantit ni rollback, ni arrêt coopératif des
processus descendants, ni absence d'effets partiels. Toute nouvelle tentative
reste hors scope et devra attendre la reconstruction P4.7.

Les sorties conservées sont bornées par configuration et portent un flag de
troncature. Une troncature est une issue explicite et peut rendre le résultat
final absent ; aucune donnée tronquée n'est reconstituée.

## JSONL et sortie finale

Chaque ligne stdout est traitée dans son ordre original avec un numéro de ligne.
Un objet JSON valide devient `CodexJsonlEvent`, conservant à la fois la ligne
brute et un payload canonique. Les clés dupliquées, valeurs non-object et lignes
malformed deviennent des `InvalidJsonlLine`; aucune ligne non vide n'est perdue
silencieusement.

L'adapter extrait uniquement les faits de transport observés :

- `thread_id` depuis `thread.started` ;
- dernier texte `agent_message` terminé comme `final_output` ;
- statut explicite `failed/error` d'un item comme tool failure observée.

Il ne décode ni ne valide le RoleResult. Une sortie finale manquante, malformed,
tronquée ou accompagnée d'un tool failure reste une observation technique.

## Failure semantics

Les échecs de binding, cwd, executable, digest, version, Git preflight ou spawn
retournent une observation sans PID. Les exécutions démarrées conservent les
flux, événements, exit code et état Git même lorsque :

- stderr existe avec exit `0` ;
- un outil échoue avec exit `0` ;
- JSONL est malformed ou la sortie finale manque ;
- le processus sort non-zero après modification ;
- timeout/interruption laisse le worktree dirty ;
- Git dérive ou devient inobservable après exécution.

Le tuple `issues` décrit ces faits sans produire `PASS`, `READY_FOR_TEST` ou
`CERTIFIED`.

## Stratégie de test

La suite standard utilise `tests/fixtures/fake_codex.py` via un vrai
sous-processus Python. Le fake simule uniquement version, stdin, arguments,
JSONL, stdout/stderr, exit, sleep/timeout et effets Git. Les tests restent
offline et couvrent injection shell, traversal cwd/schema, substitution
d'executable, secret hérité, JSONL hostile, sortie volumineuse, interruption,
side effects et drift Git.

`tests/test_codex_runtime_canary.py` contient un canary réel séparé, désactivé
par défaut. Il exige `AGENTIC_OS_RUN_CODEX_CANARY=1`, une authentification et un
service disponibles ; il ne fait jamais partie de la preuve déterministe
standard.
