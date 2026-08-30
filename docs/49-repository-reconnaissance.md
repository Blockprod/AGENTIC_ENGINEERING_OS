# Reconnaissance déterministe du repository

## Objet et frontière

`RepositoryReconnaissance.inspect(repository_root) -> RepositoryProfile`
observe un repository cible sans le modifier. Le profil est immuable,
reconstructible, ordonné canoniquement et non persistant. Il prépare les faits
utiles au futur planner P5.4 mais ne crée ni `ProjectConfiguration`, ni état,
ni autorité d'exécution.

La reconnaissance n'exécute aucune commande projet, ne synthétise aucune
commande shell, ne modifie ni Git, ni `AGENTS.md`, ni `.gitignore`, et ne crée
aucun fichier. Les seules commandes externes sont des primitives Git de lecture
à argv fermé, exécutées avec `shell=False`, `GIT_TERMINAL_PROMPT=0` et
`GIT_OPTIONAL_LOCKS=0`.

## Modèle `RepositoryProfile`

Le profil contient uniquement :

- la racine demandée et le statut `SUPPORTED`, `BLOCKED` ou `UNKNOWN` ;
- les observations Git, branche/HEAD, propreté et worktrees ;
- les entrées racine utiles, manifests reconnus et candidats de contexte ;
- les toolchains inférées et commandes candidates explicitement sourcées ;
- la présence de chemins sensibles et de symlinks, sans leur contenu ;
- l'état observé du footprint Agentic OS ;
- les limites atteintes et erreurs sous forme d'issues ordonnées ;
- une observation Codex séparée, laissée `UNKNOWN` par ce composant
  repository-only.

Le profil ne contient aucun `ProjectState`, `MissionState`, Evidence, Gate,
credential, contenu de secret, commande exécutée ou verdict du Control Plane.

## Sémantique des observations

- `FACT` désigne une observation directement reproductible : présence d'un
  fichier, résultat Git, valeur de version lue ou script déclaré.
- `INFERENCE` désigne une conclusion déterministe mais non autoritative : une
  toolchain candidate issue de marqueurs, ou une invocation candidate issue
  d'un script de package.
- `UNKNOWN` désigne une donnée absente, ambiguë, trop grande, invalide ou non
  vérifiable dans la frontière autorisée.

Une inférence ou une inconnue ne devient jamais une configuration. P5.4 pourra
présenter des propositions à l'opérateur ; P5.3 ne fournit aucune acceptation
implicite.

## Observation Git et worktrees

Le service réutilise `GitAdapter.observe_read_only()` pour observer :

- le top-level Git canonique ;
- la branche ou le mode detached ;
- le SHA complet de HEAD ;
- la propreté via porcelain ;
- tous les worktrees annoncés par Git, triés par chemin canonique.

Un dossier hors Git est `BLOCKED` avec un fait `is_repository = false`. Une
racine demandée située sous un autre top-level est également `BLOCKED`. Une
erreur Git qui ne permet pas de conclure produit `UNKNOWN`. Aucun stderr ou
contenu non borné n'est repris dans le profil.

## Détection des toolchains

La détection est limitée à des marqueurs racine explicites :

- Python : `pyproject.toml`, `setup.cfg`, `tox.ini`, `noxfile.py` et
  `requirements*.txt` ;
- Node : `package.json` ;
- Rust : `Cargo.toml`.

La présence du marqueur est un fait ; la toolchain résultante reste une
`INFERENCE`. Toutes les toolchains sont rapportées dans l'ordre lexical. Aucun
`primary_language` n'est choisi et aucun score opaque, LLM, embedding ou scan
de code source n'est utilisé.

Les manifests JSON/TOML/INI reconnus sont lus seulement sous la limite de
taille, puis parsés strictement. JSON refuse les clés dupliquées et constantes
non standard. Un manifest malformé reste `UNKNOWN`, même si sa présence peut
encore justifier une inférence de toolchain.

## Découverte des commandes

P5.3 supporte uniquement les scripts `test`, `build`, `lint`, `typecheck` et
`type-check` explicitement présents dans un `package.json` valide. Le contenu
du script n'est jamais copié dans le profil. Une lockfile unique prouve le
runner `npm`, `pnpm` ou `yarn`, puis une invocation structurée
`runner + ("run", script_name)` est produite comme `INFERENCE`.

Sans lockfile, le package manager reste `UNKNOWN`. Plusieurs lockfiles sont
ambiguës et ne produisent aucune commande. Un script vide, non textuel ou
ressemblant à un secret est refusé/redacted. Les commandes ne sont jamais
exécutées et le README n'est jamais interprété comme une autorité exécutable.

## Footprint Agentic OS existant

La reconnaissance observe séparément :

- la validité/version de `.agentic-engineering-os/config.json` via le loader
  strict P5.2 ;
- la présence et version déclarée de `state.json`, `mission.json`,
  `worktrees.json`, `negative-outcomes.json` et `executions.json` ;
- la présence littérale d'une référence Agentic OS dans `AGENTS.md`, sans
  en faire une autorité ;
- le statut borné des sections gérées versionnées dans `AGENTS.md` et
  `.gitignore`, ainsi que l'empreinte SHA-256 de leurs octets exacts sans
  conserver le contenu utilisateur ;
- les seules règles `.gitignore` Agentic OS connues.

La classification dérivée signifie :

- `UNINITIALIZED` : aucun footprint observé ;
- `INITIALIZED` : configuration P5.2 valide, référence Agentic OS dans
  `AGENTS.md`, sections gérées canoniques, règles d'ignore runtime minimales et
  aucune contradiction de version observée ; ce terme n'implique pas la
  readiness P4 ;
- `PARTIAL_OR_INCONSISTENT` : footprint partiel, config invalide, document
  runtime illisible/unsafe ou contradiction structurelle observable ;
- `UPGRADE_REQUIRED` : version de config ou de runtime non supportée.

Les documents runtime compatibles sont seulement classés
`VERSION_OBSERVED` : P5.3 ne remplace pas la validation complète de leurs
stores et ne les répare jamais.

## Politique filesystem et secrets

Le scan reste sous la racine fournie. La racine elle-même ne peut pas être un
symlink. Les symlinks rencontrés ne sont jamais suivis ; leur cible est classée
`INSIDE_REPOSITORY`, `OUTSIDE_REPOSITORY` ou `UNKNOWN`. Les composants symlink
d'un chemin de configuration sont refusés.

`.git` n'est jamais parcouru par le scanner ; seules les primitives Git dédiées
l'observent. `.venv`, `node_modules`, caches, `target`, `vendor` et fichiers
runtime temporaires ne sont pas parcourus. `.env`, clés privées et chemins
ressemblant à des secrets sont seulement signalés par leur chemin. Leur contenu
n'est ni lu volontairement, ni conservé dans le profil.

## Déterminisme et limites

Les collections sont triées avec normalisation NFC et `casefold()`. Le profil
n'inclut ni horloge, ni ordre filesystem natif, ni état conversationnel.

Limites par défaut :

- 256 000 octets par manifest/configuration/document candidat ;
- 512 entrées au premier niveau ;
- 512 entrées examinées sous `docs/` ;
- 128 sources de contexte ;
- profondeur maximale 2 sous `docs/`.

Un dépassement crée une issue `UNKNOWN` et positionne `scan_complete = false`
lorsque l'inventaire borné ne peut être complet. Une arborescence ignorée très
volumineuse ne consomme pas ce budget interne, car elle n'est jamais parcourue.

## Hors scope P5.3

La disponibilité de l'exécutable Codex reste une observation machine séparée
et `UNKNOWN` dans ce profil. La génération d'une configuration, le dry-run,
l'initialisation, l'intégration `AGENTS.md`, les migrations et toute écriture
appartiennent aux missions P5 suivantes et ne sont pas anticipés ici.
