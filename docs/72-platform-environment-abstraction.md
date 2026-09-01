# Platform & Environment Abstraction

## Contrat V1

La cible de certification V1 est **Windows 11 x64, Windows-first**. Linux et
macOS restent `UNKNOWN / NOT CERTIFIED`. Une simulation de leurs capacités dans
un test ne constitue pas une preuve de support.

`PlatformCapabilities` est une observation read-only et non autoritative. Elle
sépare strictement :

- `PlatformFacts` : famille OS, sémantique de chemins, suffixes exécutables et
  terminaison du processus enfant direct ;
- `MachineFacts` : TEMP, PowerShell, Git, Codex, Python, symlinks, junctions et
  sensibilité à la casse observables sur la machine ;
- `ProjectPlatformBinding` : racine du repository et portée filesystem
  observées pour ce projet ;
- configuration projet et overrides utilisateur : restent dans leurs contrats
  respectifs et ne sont jamais promus par la découverte machine.

Les faits découverts ne sont ni persistés dans l'état projet, ni transformés en
autorité. `require_windows_v1_local_safety()` bloque si la plateforme, la portée
filesystem, le reparse point, TEMP ou Git ne sont pas suffisamment prouvés.

## Inventaire des hypothèses

| Classe | Dépendances observées | Contrat |
|---|---|---|
| A — Windows V1 guarantee | `PATHEXT`, chemins locaux Windows, comparaison casefold, kill du processus enfant direct, shims `.exe` optionnels | Cible Windows 11 x64 uniquement |
| B — Platform-neutral | argv, `shell=False`, cwd explicite, `pathlib`, chemins contractuels POSIX relatifs, containment, UTF-8, fichiers temporaires voisins pour remplacement atomique | Conservé dans le runtime commun |
| C — Machine-specific fact | chemins/version Git, chemin Python courant, chemin Codex injecté, TEMP/TMP, PowerShell disponible, capacités symlink/junction | Observé, jamais persisté comme autorité |
| D — Unknown/unsupported | Linux, macOS, UNC, filesystem réseau/remote, répertoire Windows case-sensitive, sémantique junction non observée, terminaison d'un arbre complet de processus | Bloqué lorsque la sûreté en dépend |
| E — Test-only | drives `D:` de fixtures, faux `codex`, chemins `.exe` synthétiques, PowerShell utilisé par l'opérateur de développement | Aucun contrat produit |

Aucun chemin personnel VS Code, home utilisateur ou drive particulier n'est
une dépendance produit.

## Chemins et filesystem

Les contrats internes déjà définis restent des chemins relatifs POSIX. Leur
conversion vers le filesystem passe par `pathlib` et les contrôles existants de
résolution, containment et traversal. `windows_contract_path_key()` centralise
uniquement la comparaison Windows NFC, séparateurs et casse ; il ne prétend pas
prouver la sensibilité réelle d'un répertoire.

Une racine UNC/réseau est hors garantie. Une racine reparse, ou dont le statut
reparse est inconnu, ne peut pas satisfaire le contrôle local Windows V1. Les
symlinks et junctions restent des faits machine : leur disponibilité n'est
jamais déduite du seul nom de l'OS.

| Sémantique | V1 |
|---|---|
| Racine locale Windows, non-reparse | Supportée selon les contrôles testés |
| Chemins avec espaces et Unicode NFC | Supportés selon les contrôles testés |
| Symlink/junction détecté sur une frontière de sûreté | Refus fail-closed |
| Répertoire Windows case-sensitive | `UNKNOWN / NOT CERTIFIED` |
| UNC ou filesystem réseau/remote | `UNKNOWN / NOT CERTIFIED` |
| Linux/macOS | `UNKNOWN / NOT CERTIFIED` |

## Emplacements temporaires

Les écritures autoritatives utilisent un fichier temporaire voisin de la cible,
condition nécessaire à leur remplacement atomique. Le merge Git isolé utilise
le TEMP choisi par Python. La sonde observe explicitement `TEMP`, puis `TMP`,
vérifie une racine existante, directe et inscriptible, et ne cherche pas un
emplacement alternatif lorsqu'une valeur configurée est invalide. Une
indisponibilité reste `UNKNOWN` et bloque le profil de sûreté ; elle ne justifie
aucun contournement de WDAC/App Control.

## Processus et environnement

Les deux frontières subprocess produit sont `GitAdapter` et
`CodexRuntimeAdapter`. Elles utilisent des argv, `shell=False`, UTF-8 et un
contexte repository/cwd explicite. Git applique une limite de temps configurable
(120 secondes par défaut). Les deux frontières transmettent uniquement un
allowlist d'environnement et ajoutent les protections Git non interactif,
`NO_COLOR` et `PYTHONIOENCODING`; elles ne transmettent pas les variables secrètes non
autorisées.

Le timeout et l'annulation forcent uniquement le processus enfant direct. La
terminaison garantie d'un arbre de processus complet n'est pas revendiquée.
PowerShell est une commodité développeur/opérateur et n'est pas requis par le
runtime cœur ; les commandes projet exécutables par le produit interdisent les
shells.

## Découverte des exécutables

- Git : chemin explicite ou lookup `PATH` borné, version observée avec
  `[git, --version]`, sans shell et avec cwd explicite. `GitAdapter` accepte
  l'exécutable comme dépendance injectée et ne suppose ni `main`, ni drive.
- Codex : chemin explicite ou lookup `PATH` observé. La sonde ne l'exécute pas
  et ne lui accorde aucune confiance. `CodexRuntimeAdapter` conserve l'autorité
  de comparer chemin attendu, SHA-256 et version fraîche avant exécution.
- Python : exécutable et version du processus courant, faits machine.

Un chemin sous une extension VS Code éventuellement observé est seulement un
fait machine. Le produit n'en déduit aucun emplacement Codex canonique et ne
dépend pas de l'UI VS Code.

## Windows Code Integrity

`agentic-os.exe` demeure un shim de commodité susceptible d'être refusé par
Enterprise Code Integrity. L'invocation portable canonique reste :

```text
<environment-python> -m agentic_engineering_os
```

Le produit ne tente ni désactivation, ni bypass, ni changement opportuniste
d'emplacement. La signature des shims n'appartient pas à P7.3.

## Matrice de support V1

| Environnement | Statut |
|---|---|
| Windows 11 x64 | Cible de certification V1 |
| Filesystem local Windows, sémantique NTFS-like testée | Support borné aux garanties observées |
| VS Code + Codex avec exécutable explicitement découvert et lié | Cible, sous contrôles du runtime adapter |
| PowerShell absent | Supporté : aucune dépendance runtime cœur |
| Linux | `UNKNOWN / NOT CERTIFIED` |
| macOS | `UNKNOWN / NOT CERTIFIED` |
| UNC, réseau, remote filesystem | `UNKNOWN / NOT CERTIFIED` |
| Windows case-sensitive directory | `UNKNOWN / NOT CERTIFIED` |
