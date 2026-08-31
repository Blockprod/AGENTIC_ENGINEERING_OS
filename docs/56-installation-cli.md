# Installation Kit & CLI

## Installation et ressources

Le package expose l'entrypoint installé `agentic-os`. Il embarque les schémas
JSON, contrats de rôles et documents d'autorité nécessaires au runtime P1–P4.
Ces ressources sont résolues par `importlib.resources` et ne dépendent ni du
cwd, ni d'un checkout `AGENTIC_ENGINEERING_OS`, ni de copies dans le repository
cible. `AGENTS.md` et les sources cognitives configurées restent locaux au
projet.

## Surface fermée

```text
agentic-os inspect
agentic-os status
agentic-os plan
agentic-os init [--apply]
agentic-os upgrade [--apply]
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

P5.10 n'ajoute ni validation multi-repository, ni certification Phase 5, ni
commande de mission, scheduler ou exécution shell générique.
