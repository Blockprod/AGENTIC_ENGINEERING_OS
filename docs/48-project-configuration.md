# Contrat de configuration projet

## Objet et frontières

`ProjectConfiguration` est le contrat repository-local, portable et versionné
qui borne l'utilisation future d'`AGENTIC_ENGINEERING_OS` dans un projet cible.
Il est distinct de `ProjectState`, `MissionState`, des Evidence, Gates,
registres de worktrees, observations runtime et paramètres machine.

P5.2 ne découvre aucune valeur, ne crée aucun fichier dans un repository cible
et n'initialise aucun état. La reconnaissance relève de P5.3, le planning de
P5.4 et l'application de P5.5.

## Emplacement et format

Le chemin canonique est :

```text
TARGET/.agentic-engineering-os/config.json
```

Le fichier appartient au repository cible et doit être versionné dans Git. Il
est encodé en UTF-8 et porte `config_version: "1.0"`. Le loader ne cherche
aucun autre nom ou emplacement, ne crée pas le fichier absent et ne migre ni ne
répare un document existant.

Le parsing refuse les clés JSON dupliquées. Le schéma Draft 2020-12 embarqué
dans le package ferme chaque objet avec `additionalProperties: false`. La
sérialisation canonique utilise des clés triées, une indentation de deux
espaces, Unicode non échappé et un saut de ligne final.

## Modèle canonique

```json
{
  "config_version": "1.0",
  "project_id": "example-project",
  "repository_root_policy": "CONFIG_PARENT_GIT_ROOT",
  "toolchains": [
    {"identity": "python", "version_constraint": ">=3.11"}
  ],
  "verification_commands": [
    {
      "command_id": "tests",
      "kind": "TEST",
      "executable": "python",
      "args": ["-m", "pytest", "tests"],
      "cwd": ".",
      "cwd_policy": "REPOSITORY_RELATIVE",
      "required": true
    }
  ],
  "path_policy": {
    "allowed_paths": ["docs", "src", "tests"],
    "protected_paths": ["pyproject.toml"],
    "forbidden_paths": ["src/generated"]
  },
  "context_sources": ["AGENTS.md", "docs/architecture.md", "README.md"],
  "codex_constraints": {
    "maximum_sandbox": "workspace-write",
    "approval_policy": "never",
    "require_clean_git": true,
    "maximum_parallel_executions": 2
  },
  "mission_state_git_policy": "TRACKED"
}
```

Les listes représentant des identités ou chemins doivent être uniques après
normalisation Unicode NFC et `casefold()`, puis triées selon cette clé. Aucun
ordre implicite ou défaut d'autorité n'est ajouté. Une collection vide signifie
explicitement « aucune valeur configurée » ; elle n'autorise ni commande ni
écriture.

Un `project_type` générique n'est pas stocké : aucun besoin runtime démontré ne
justifie cette métadonnée. Les toolchains et commandes structurées portent les
informations opérationnelles utiles sans devenir une configuration fourre-tout.

## Classification des champs

| Champ | Classe | Contrat |
|---|---|---|
| `config_version` | requis structurel | Valeur fermée `1.0`, liée au schéma ; jamais déduite ou migrée. |
| `project_id` | `REQUIRED EXPLICIT` | Identité stable, Unicode NFC, sans séparateur de chemin. |
| `repository_root_policy` | requis structurel | Valeur fermée `CONFIG_PARENT_GIT_ROOT` ; la racine réelle sera un `DISCOVERABLE FACT` P5.3, jamais un chemin absolu persisté. |
| `toolchains` | `DISCOVERABLE FACT` ou déclaration explicite | P5.3 pourra produire des candidats factuels ; P5.2 accepte uniquement la liste enregistrée, sans scanner. Les contraintes de version restent explicites. |
| `verification_commands` | `REQUIRED EXPLICIT` lorsqu'une vérification est requise | Une commande découverte ne devient pas autoritative sans confirmation. Liste vide : toute vérification correspondante reste bloquante. |
| `path_policy` | `REQUIRED EXPLICIT` | Les trois listes sont présentes, même vides ; elles bornent les écritures sans remplacer les scopes User Story. |
| `context_sources` | `OPTIONAL OVERRIDE` explicite | Sources Markdown projet additionnelles ; liste vide autorisée, aucun document n'est deviné. |
| `codex_constraints` | `REQUIRED EXPLICIT` | Plafond de sandbox, politique d'approbation, propreté Git et limite de parallélisme ; ne peut affaiblir P4. |
| `mission_state_git_policy` | `REQUIRED EXPLICIT` | `TRACKED` conserve `mission.json` versionnable. `IGNORED` impose à l'adoption d'installer sa règle exacte dans la section `.gitignore` gérée, sans défaut silencieux. |

HEAD, branche, dirty state, taille du repository, worktrees, versions observées
et disponibilité de Codex sont des faits runtime. Ils ne sont jamais persistés
dans cette configuration. Le chemin de l'exécutable Codex, les chemins absolus
de toolchains, préférences machine, variables d'environnement, caches et
credentials en sont également exclus.

## Contrat des commandes

Chaque `VerificationCommand` contient :

- un `command_id` unique et un type fermé `TEST`, `BUILD`, `LINT`, `TYPECHECK`
  ou `OTHER` ;
- un `executable` réduit à un nom de programme, jamais un chemin ou un shell ;
- une liste `args` conservant exactement les frontières argv ;
- un `cwd` repository-relatif et la politique fermée
  `REPOSITORY_RELATIVE` ;
- le booléen explicite `required`.

Le futur exécuteur devra utiliser ces éléments sans `shell=True`. Les shells,
opérateurs de contrôle, caractères NUL/nouvelle ligne, traversals et arguments
portant un chemin absolu sont refusés. Aucune expansion de variable, glob ou
substitution de commande ne constitue une autorité.

## Contrat des chemins et scopes

Les chemins utilisent une syntaxe POSIX repository-relative canonique. Les
chemins absolus Windows/POSIX, antislashs, segments vides, `.`, `..`, traversal
et formes Unicode non NFC sont refusés. `.` seul est permis comme `cwd` de
commande, jamais comme autorisation globale de scope.

`forbidden_paths` prévaut sur `protected_paths`, qui prévaut sur
`allowed_paths`. Un interdit descendant d'un chemin autorisé est valide et
réduit le scope. Une même cible normalisée dans plusieurs catégories, ou un
interdit contenant entièrement une cible autorisée/protégée, est contradictoire
et refusé.

`.git`, `.agentic-engineering-os` et les zones ressemblant à des secrets ne
peuvent recevoir d'autorisation ou de statut protégé. Ils restent interdits par
le plancher de sécurité du produit, même s'ils ne sont pas répétés dans la
configuration. Les sources contextuelles doivent être des fichiers Markdown
sûrs et repository-locaux.

## Contraintes Codex repository-locales

`maximum_sandbox` borne l'exécution à `read-only` ou `workspace-write`.
`approval_policy` reste fermée à `never`, et `require_clean_git` à `true`,
conformément au runtime P4. `maximum_parallel_executions` est un plafond projet
entre 1 et 64 ; la capacité machine réellement observée peut le réduire mais
jamais l'augmenter. Aucun de ces champs ne confère une autorité du Control Plane.

## Validation fail-closed

`ProjectConfigurationLoader` et `ProjectConfigurationValidator` refusent
explicitement : fichier absent ou illisible, version inconnue, JSON corrompu,
clé dupliquée, champ absent ou inattendu, type/enum invalide, ordre non
canonique, identité/path dupliqué après normalisation, chemin absolu ou
traversant, contradiction de scope, commande ambiguë, dépendance absolue à un
checkout et valeur ressemblant à un secret.

Le loader contrôle aussi que `.agentic-engineering-os` et `config.json` ne sont
pas des symlinks. Il n'existe aucun fallback, merge, découverte, écriture ou
migration dans P5.2.

## Audit des ressources P1 à P4

| Ressource observée | Classe cible | État P5.2 |
|---|---|---|
| Schéma `project-configuration` | A — package resource | Embarqué dans `agentic_engineering_os.resources` et résolu par `importlib.resources`, indépendamment du cwd/checkout. |
| Schémas P1–P3 chargés par `ContractValidator` | A — package resource | Résolu en P5.10 via les ressources installées. |
| Schémas de résultats utilisés par `SingleRoleCodexExecutor` | A — package resource | Résolu en P5.10 ; aucun dossier `TARGET/schemas/` requis. |
| `roles/*.md` et contrats génériques `docs/02`, `03`, `04`, `12`, `16`–`20`, `35` utilisés par `ContextBuilder` | A — package resource | Résolu en P5.10 ; `AGENTS.md` seul reste une autorité du repository cible. |
| `AGENTS.md` et `context_sources` configurées | B — target repository resource | Restent repository-locales et sous autorité utilisateur. |
| `state.json`, `mission.json`, `worktrees.json`, `negative-outcomes.json`, `executions.json` | C — runtime state | Exclus de `ProjectConfiguration` ; leurs stores et autorités restent inchangés. |
| tests, fixtures, historique documentaire et certifications de phase | D — development-only | Non requis par l'installation ; l'unique référence de certification nécessaire au runtime est embarquée comme contrat produit. |

`PromptCompiler` conserve des identités logiques `roles/...` mais ne résout pas
lui-même les fichiers. Depuis P5.10, les ressources produit nécessaires à P1–P4
sont résolues depuis le package installé, sans dépendance à un checkout source.
