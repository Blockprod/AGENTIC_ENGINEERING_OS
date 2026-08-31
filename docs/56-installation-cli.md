# Installation Kit & CLI

## Installation et ressources

Le package expose deux chemins vers la même implémentation `cli.main` :

- invocation portable canonique :
  `<environment-python> -m agentic_engineering_os` ;
- console script de commodité : `agentic-os`, lorsque la politique de la
  plateforme autorise son exécution.

L'interpréteur canonique est toujours celui de l'environnement où le wheel est
installé. Le package embarque les schémas JSON, contrats de rôles et documents
d'autorité nécessaires au runtime P1–P4.
Ces ressources sont résolues par `importlib.resources` et ne dépendent ni du
cwd, ni d'un checkout `AGENTIC_ENGINEERING_OS`, ni de copies dans le repository
cible. `AGENTS.md` et les sources cognitives configurées restent locaux au
projet.

## Surface fermée

```text
<environment-python> -m agentic_engineering_os inspect
<environment-python> -m agentic_engineering_os status
<environment-python> -m agentic_engineering_os plan
<environment-python> -m agentic_engineering_os init [--apply]
<environment-python> -m agentic_engineering_os upgrade [--apply]
```

Chaque commande accepte `--repository` et `--json`. `plan` et `init` acceptent
un fichier `--configuration` explicite lorsque le projet n'a pas encore sa
configuration canonique. La CLI ne lance aucune commande projet ou shell.

- `inspect` délègue à `RepositoryReconnaissance` et ne mute rien ;
- `status` projette uniquement les observations et statuts d'adoption existants ;
- `plan` délègue à `ExistingRepositoryAdoption.prepare_adoption` ;
- `init` prépare toujours avant d'appeler `apply_adoption` avec `--apply` ;
- `upgrade` délègue à `UpgradePlanner`, puis à
  `RepositoryUpgradeService` uniquement avec `--apply`.

Sans `--apply`, `init` et `upgrade` sont des dry-runs sans écriture. Une
installation ou un démarrage de CLI ne migre jamais un projet.

## Human Authority

Une opération réservée au Human exige un `--confirm ID` pour chaque opération
exacte et un `--confirmed-by IDENTITY`. La CLI lie alors la confirmation au
plan, au fingerprint source, à la cible et à l'état courant à travers les
modèles P5 existants. L'ensemble des identifiants doit correspondre exactement
aux exigences du plan. Il n'existe pas de `--yes`; une identité Codex, une
confirmation manquante, inattendue ou stale reste refusée par le service
autoritatif.

## Sortie et codes

La sortie est un objet JSON déterministe contenant `command`, `status` et
`result`. Sans `--json`, il est indenté pour lecture humaine ; avec `--json`, il
est compact et machine-readable. Les services restent la source du statut.

- `0` : observation ou opération réussie ;
- `1` : erreur produit inattendue ;
- `2` : état bloqué, input refusé ou autorité Human manquante.

Les erreurs ne rendent jamais un succès. Les chemins traversants, repositories
ou configurations via symlink, versions inconnues et états partiels sont
traités fail-closed.

## Politique d'exécution de la plateforme

La disponibilité du shim `agentic-os.exe` n'est pas garantie sous toutes les
politiques de contrôle d'applications d'entreprise. L'installation est prête si
au moins un chemin supporté et légitime est compatible avec la politique de
l'hôte. L'invocation portable utilise explicitement le Python autorisé du venv ;
elle ne cherche jamais un autre interpréteur et ne constitue pas un mécanisme de
contournement. Le shim reste packagé pour les plateformes qui l'autorisent.

P5.10 n'ajoute ni validation multi-repository, ni certification Phase 5, ni
commande de mission, scheduler ou exécution shell générique.
