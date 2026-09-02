# Capacités runtime Codex

## Portée et baseline observée

P4.4 décrit le transport Codex réellement disponible sur la baseline
`f4ce75f2b55c7384015844eb0c5c9a013d94e019`. Il ne fournit aucun adapter et ne
change aucune autorité P0–P3 : `CODEX EXECUTES. CONTROL PLANE DECIDES.` Un
`CompiledPrompt`, une sortie Codex ou un code de processus ne constitue ni une
Evidence, ni un Gate, ni une Certification.

L'environnement observé est Windows, Python 3.11.9 dans `.venv`, avec un seul
exécutable `codex` résolu :

- chemin :
  `C:\Users\averr\.vscode\extensions\openai.chatgpt-26.825.41651-win32-x64\bin\windows-x86_64\codex.exe` ;
- CLI : `codex-cli 0.151.0-alpha.7.1` ;
- extension source : `Codex – OpenAI’s coding agent` 26.825.41651 ;
- SHA-256 de l'exécutable :
  `9E2BF0EF4243C335EC60400260D8330DC309352B9CF08FAD758496B47F1C136E`.

Le chemin est fourni par l'installation de l'extension et peut changer lors
d'une mise à jour. Le binaire `codex exec` est néanmoins une interface de
processus indépendante de l'UI VS Code. La documentation OpenAI officielle le
décrit comme le mode stable pour les exécutions scriptées ou CI :
[Developer commands — `codex exec`](https://developers.openai.com/codex/cli/reference#codex-exec).

## Méthode et preuves locales

La découverte a utilisé `Get-Command codex -All`, `where.exe codex`, les
métadonnées de l'extension, `codex --version`, l'aide générale et les aides de
`exec`, `exec resume`, `exec fork`, `sandbox`, `app-server`, `exec-server` et
`doctor`, ainsi que `codex features list`. Aucun fichier de configuration ni
credential n'a été affiché. Le parseur global a également accepté
`-a never` et `-a on-request` devant `exec`, avec un code `0` dans les deux cas.

Un probe unique a lancé `codex exec` par API de processus dans un dépôt Git
temporaire hors du workspace, avec `--ephemeral`, `--ignore-user-config`,
`--ignore-rules`, `--json`, `--sandbox read-only`, `-C <temp>` et un prompt par
stdin. Résultats observés :

- code de sortie `0`, cinq événements JSONL sur stdout et stderr capturé
  séparément ;
- événements `thread.started`, `turn.started`, `item.completed` et
  `turn.completed` ;
- `thread_id` UUID exposé ;
- réponse finale contenant le basename exact du répertoire passé à `-C` ;
- aucun changement Git dans le dépôt temporaire ;
- la commande PowerShell demandée a été bloquée par la policy du sandbox et
  signalée sur stderr, alors que le processus a tout de même terminé avec `0`.

Un second probe, sans prompt complet ni appel modèle, a conservé stdin ouvert
puis interrompu le processus parent. Le processus était actif, a été terminé
par le parent et a exposé le code `-1` ; le dépôt est resté propre. Le dépôt
temporaire a ensuite été supprimé.

## Matrice fermée

`SUPPORTED` signifie observé localement ou décrit par l'aide locale et la
documentation officielle de cette interface. Les limites indiquées font partie
du statut.

| Capacité | Statut | Preuve et limite |
|---|---|---|
| Invocation programmatique | `SUPPORTED` | `codex exec` lancé comme sous-processus lors du probe. |
| Exécution non interactive | `SUPPORTED` | Sous-commande `exec`, probe terminé sans interaction. |
| Prompt par argument | `SUPPORTED` | Argument `PROMPT` documenté par `codex exec --help`. |
| Prompt par stdin | `SUPPORTED` | `PROMPT=-` documenté et observé pendant le probe. |
| Prompt par fichier natif | `UNSUPPORTED` | Aucun flag de fichier de prompt exposé ; un parent peut seulement lire un fichier puis alimenter stdin. |
| Working directory explicite | `SUPPORTED` | `-C/--cd` documenté ; basename ciblé observé dans la réponse du probe. |
| Garantie d'isolation repository/worktree | `UNKNOWN` | `-C` cible un worktree existant mais ne prouve pas une frontière filesystem ni le binding autoritatif P3. |
| Sortie machine-readable | `SUPPORTED` | JSONL observé avec `--json`. |
| Résultat final contraint | `SUPPORTED` | `--output-schema <FILE>` est exposé et documenté ; la validation P4.6 reste obligatoire. |
| Capture stdout | `SUPPORTED` | Flux JSONL capturé séparément. |
| Capture stderr | `SUPPORTED` | Warnings et erreur d'outil capturés séparément. |
| Code de processus | `SUPPORTED` | `0` à completion et `-1` après interruption observés ; ce code n'est pas un verdict métier. |
| Timeout | `SUPPORTED` | Le parent peut borner l'attente et interrompre le sous-processus ; aucun flag timeout natif n'est exposé. |
| Cancellation | `SUPPORTED` | Terminaison parent-process observée ; aucune cancellation gracieuse ou rollback n'est garanti. |
| Identité de session | `SUPPORTED` | UUID `thread_id` observé dans `thread.started`. |
| Interface resume | `SUPPORTED` | `codex exec resume [SESSION_ID]` et `--last` sont exposés et documentés. |
| Reprise fiable après effets partiels | `UNKNOWN` | Aucun probe de crash/reprise ; ni réconciliation Git ni exact-once n'est garanti. |
| Contrôles sandbox | `SUPPORTED` | `read-only`, `workspace-write`, `danger-full-access`; un blocage réel a été observé en `read-only`. |
| Contrôles d'approbation | `SUPPORTED` | `on-request`, `never` et auto-review sont exposés ; leur résultat ne vaut pas Human Authority du Control Plane. |
| Contrôle d'environnement | `SUPPORTED` | Environnement du processus parent, profils, overrides `-c` et `--ignore-user-config`; l'absence de secrets exige une allowlist P4.5. |
| Processus indépendants en parallèle | `UNKNOWN` | Techniquement lançables par l'OS, mais concurrence, quotas et isolation Codex n'ont pas été probés afin d'éviter coût et effets inutiles. |
| VS Code requis pendant `codex exec` | `UNSUPPORTED` | Le mode CLI scripté n'automatise pas l'UI ; seule l'installation observée du binaire provient ici de l'extension. |

`SUPPORTED` dans cette table signifie que l'interface est déclarée par le CLI,
pas que chaque opération est disponible sous toute politique. L'admission
utilise les primitives fermées `REPOSITORY_READ`, `WORKSPACE_EDIT`,
`COMMAND_EXECUTION`, `STRUCTURED_RESULT` et `GIT_OBSERVATION`. La matrice minimale
est :

| Rôle | Capacités opérationnelles requises |
|---|---|
| Architect | `REPOSITORY_READ`, `STRUCTURED_RESULT`, `GIT_OBSERVATION` |
| Implementer | `REPOSITORY_READ`, `WORKSPACE_EDIT`, `COMMAND_EXECUTION`, `STRUCTURED_RESULT`, `GIT_OBSERVATION` |
| Tester | `REPOSITORY_READ`, `WORKSPACE_EDIT`, `COMMAND_EXECUTION`, `STRUCTURED_RESULT`, `GIT_OBSERVATION` |
| Reviewer | `REPOSITORY_READ`, `STRUCTURED_RESULT`, `GIT_OBSERVATION` |
| Certifier | `REPOSITORY_READ`, `STRUCTURED_RESULT`, `GIT_OBSERVATION` |

La lecture est probée par la restitution d'un marqueur borné fourni à Codex via
un `AGENTS.md` dans un repository Git jetable, sans shell. L'édition est probée
séparément par un fichier borné dans ce repository sous `workspace-write`;
l'exécution de commande par une commande déterministe inoffensive; l'observation
Git par le parent sur le repository lié. Le résultat structuré n'est prouvé que
par l'exécution réelle, le transport et l'intake P4.6. Ainsi
`REPOSITORY_READ` n'implique jamais `COMMAND_EXECUTION`, et `WORKSPACE_EDIT` ne
la prouve pas non plus.

Une preuve opérationnelle authentique est liée au chemin, digest et version de
l'exécutable, au sandbox, à la politique d'approbation, à l'environnement borné
et à la primitive. Seule une preuve positive identique peut être mise en cache.
Un refus pré-lancement conserve la primitive, une cause bornée, le sandbox et la
politique sans exposer les valeurs d'environnement.

Le diagnostic R4A sous Windows a observé que `git rev-parse HEAD`, essayé via
PowerShell puis `cmd.exe`, était rejeté comme « blocked by policy » avec exit
Codex `0` et sans événement de commande réussi. Il s'agit d'une limitation de
capacité Codex sous la politique hôte courante, révélant aussi un défaut de
conception du probe R4 qui utilisait l'exécution de commande comme proxy de
lecture. Aucune politique Windows n'a été affaiblie.

Le répertoire jetable doit lui-même être initialisé comme repository Git : sans
cette préparation, le CLI refuse avant le tour avec
`Not inside a trusted directory`. Ce refus est classé séparément comme défaut de
préparation du probe et non comme absence de `REPOSITORY_READ`.

`app-server` et `exec-server` sont présents mais marqués expérimentaux par
l'aide locale. Ils n'ont pas été probés et ne sont pas retenus pour P4.5.

## Entrées, sorties et sémantique de processus

P4.5 peut transmettre `CompiledPrompt.prompt_text` par stdin, fixer le chemin
canonique avec `-C`, demander JSONL avec `--json` et fournir le schéma exact du
RoleResult avec `--output-schema`. Stdin évite les limites et l'exposition du
prompt dans la ligne de commande. Une sortie finale peut aussi être écrite par
`--output-last-message`, mais P4.5 doit privilégier les pipes et conserver les
flux bruts.

Le JSONL fournit des événements et une identité de thread ; il ne transforme
pas leur contenu en résultat autoritatif. Le probe démontre en particulier
qu'un outil bloqué peut coexister avec un exit code `0`. P4.5 doit donc séparer
strictement : état du transport, événements d'outil, message final, RoleResult
décodé, état Git observé et décision du Control Plane.

Le parent peut imposer un délai puis terminer l'arbre de processus. Une telle
interruption ne garantit ni arrêt coopératif, ni absence d'effets déjà produits,
ni rollback. Après timeout ou cancellation, l'état reste incertain jusqu'à la
reconstruction repository/worktree prévue par P4.7.

## Sandbox, approbations et environnement

Codex peut lancer des commandes et, avec une sandbox qui l'autorise, modifier
le filesystem et Git. `workspace-write` est plus large que les chemins fins du
scope Agentic OS ; `danger-full-access` supprime cette frontière. `--add-dir`
peut encore étendre les racines inscriptibles. Aucun de ces mécanismes ne
remplace les bindings mission, génération, repository et worktree.

Le mode `on-request` peut produire une demande d'approbation Codex, mais cette
demande ou son traitement n'est jamais une Evidence Human. Une exécution non
interactive qui nécessite une autorité Human doit être bloquée par le Control
Plane, pas auto-approuvée par le transport.

Un processus peut hériter de variables et donc de secrets. P4.5 devra
construire un environnement minimal explicite, ne jamais journaliser les
valeurs, refuser les extensions de répertoires non autorisées et capturer les
références Git avant et après. La portée de lecture hors `-C` et les garanties
exactes du sandbox Windows restent `UNKNOWN` ; elles ne peuvent pas être
déduites du nom de la policy.

## Frontière VS Code

Le runtime exploitable par P4.5 est le processus `codex exec` : prompt, cwd,
sandbox, événements, session et exit status. L'extension VS Code fournit dans
cet environnement le binaire, sa mise à jour, l'authentification partagée et
l'expérience UI. P4.5 ne doit ni cliquer dans l'UI, ni piloter une conversation
visible, ni dépendre de son état. L'intégration UX explicite reste P4.10.

## Décision de transport P4.5

La stratégie retenue est **A — adapter mono-provider par sous-processus CLI
`codex exec`**. C'est l'interface stable, la plus simple et la seule observée
de bout en bout. L'App Server expérimental n'est pas nécessaire pour satisfaire
le chemin P4.1 :

```text
CompiledPrompt
→ stdin de codex exec dans le repository/worktree lié
→ JSONL + stdout/stderr + exit status + état Git observé
→ future Structured Result Intake
```

P4.5 pourra honnêtement offrir la primitive conceptuelle :

```text
execute(compiled_prompt, execution_binding) -> execution_observation
```

`execution_binding` doit fournir request/attempt, chemin canonique, commit et
worktree attendus, policy sandbox/approbation explicite, environnement autorisé,
schéma de résultat et timeout. `execution_observation` peut contenir :

- horodatages début/fin, identité de tentative, PID et `thread_id` observé ;
- exécutable, version, empreinte, arguments non sensibles et cwd effectifs ;
- état Git/worktree avant et après ;
- exit code, stdout/stderr bruts, événements JSONL et message final brut ;
- flags completion, timeout, interruption et erreur de décodage.

Elle ne peut pas promettre réussite métier, Evidence, Gate, Certification,
rollback, reprise fiable, isolation fine ou absence d'effets après interruption.

## Gaps fail-closed pour la suite

- Isolation worktree non garantie par `-C` : P4.5 doit valider le chemin et les
  bindings avant lancement, interdire `--add-dir` et comparer Git avant/après.
- Lecture hors workspace et garanties Windows `UNKNOWN` : environnement et
  sandbox minimaux, absence de secret dans le contexte, blocage si la mission
  exige une garantie plus forte.
- Exit `0` insuffisant : P4.6 doit refuser toute sortie absente, malformed,
  wrong-role ou contradictoire et revalider tous les bindings.
- Resume fiable `UNKNOWN` : P4.7 ne peut pas annoncer une reprise sûre avant
  persistance d'attempt et réconciliation des effets physiques.
- Cancellation non gracieuse : timeout/interruption impose état incertain et
  reconstruction avant retry.
- Parallélisme non prouvé : P4.9 devra tester limites, isolation et échecs
  partiels ; P4.5 ne garantit aucune capacité concurrente.
- Version/path issus de l'extension : P4.5 doit résoudre, observer et enregistrer
  le binaire à chaque tentative, puis bloquer les versions non acceptées plutôt
  que supposer leur compatibilité.
