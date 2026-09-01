# Repository Archetype Generalization

## Contrat

P7.4 distingue strictement la reconnaissance d'un repository de sa capacité
d'exécution. La règle d'autorité est :

```text
DISCOVERY MAY SUGGEST. CONFIGURATION DECIDES.
```

Un manifest, un lockfile ou un script découvert ne devient jamais une commande
autoritative. Seules les commandes canoniques déjà présentes dans
`ProjectConfiguration.verification_commands` peuvent contribuer à
`EXECUTION_READY`. L'évaluation n'exécute aucune commande projet.

## Matrice BEFORE

| Archetype | Discovered | Configurable | Adoptable | Executable | Verifiable | Certified |
|---|---|---|---|---|---|---|
| Python | Marqueurs racine inférés | Oui, contrat générique | Oui | Non établi | Non établi | Non |
| Node | `package.json`, lockfile et scripts candidats | Oui, contrat générique | Oui | Non établi | Non établi | Non |
| Rust | `Cargo.toml` inféré | Oui, contrat générique | Oui | Non établi | Non établi | Non |
| Python + Node | Toolchains multiples observées sans composants/scopes | Partiel | Oui | Non établi | Non établi | Non |
| Autre | Non reconnu | Structure générique possible | Configuration requise | Non | Non | Non |

La reconnaissance P5 reste read-only. Ses `candidate_commands` sont des
inférences et ne sont pas consommées comme autorité par P7.4.

## Modèle fermé

`RepositoryArchetype` contient uniquement `PYTHON`, `NODE`, `RUST` et
`UNKNOWN`. Un `RepositoryArchetypeProfile` est lié à une racine absolue, à un
`project_id` et à l'empreinte canonique complète de sa configuration, et
contient :

- des `ArchetypeComponent` avec racine repository-relative, manifests,
  lockfiles, package manager, statut workspace, toolchains détectées, scopes
  source/test/build et capacités requises ;
- des `VerificationCommandContract` issus exclusivement de la configuration,
  avec argv, cwd, type, caractère required/optional et ownership ;
- les ambiguïtés et blockers déterministes.

Il n'existe ni plugin, ni priorité de langage, ni « primary language ».

## Niveaux de support

| Niveau | Signification |
|---|---|
| `UNSUPPORTED` | Aucun archetype fermé n'est détecté. |
| `RECOGNIZED` | Un archetype est détecté, sans commande autoritative. |
| `ADOPTABLE` | La configuration est explicite mais une capacité, un binding ou une preuve manque. |
| `EXECUTION_READY` | Toutes les commandes required sont liées, strictes et couvertes par des faits machine frais. |
| `AMBIGUOUS` | Manifest, package manager, scope ou ownership contradictoire. |

`EXECUTION_READY` n'est ni une exécution, ni une vérification réussie, ni une
certification. Aucun niveau `CERTIFIED` n'est produit par P7.4.

## Archetypes

Python reconnaît `pyproject.toml`, `setup.cfg` ou `setup.py`, ainsi que les
lockfiles connus lorsqu'ils existent. Une commande Python reste obligatoire et
explicite ; `pytest` n'est jamais supposé.

Node distingue `package.json`, scripts déclarés, lockfiles et package manager.
Plusieurs lockfiles rendent le profil ambigu. Une commande `npm`, `pnpm` ou
`yarn` exige le lockfile correspondant et un script `run` doit être déclaré.
Aucune installation de dépendance n'est lancée.

Rust distingue `Cargo.toml`, `Cargo.lock` et la présence d'une section
workspace. Aucune invocation `cargo` n'est déduite du manifest.

Les repositories mixtes conservent tous leurs composants. Des scopes disjoints
peuvent recevoir des commandes distinctes ; des scopes de toolchains différentes
qui se recouvrent sont `AMBIGUOUS`. Une configuration peut sélectionner un seul
composant sans rendre les autres exécutables.

## Faits machine et readiness

`RepositoryToolchainProbe` réutilise la découverte bornée P7.3. Il observe, sans
persistance : exécutable demandé, chemin résolu, provenance, version, taille,
mtime et SHA-256. Les statuts fermés sont `AVAILABLE`, `UNAVAILABLE` et
`UNKNOWN`.

`RepositoryArchetypeEvaluator.evaluate(profile, configuration,
platform_capabilities, machine_facts)` vérifie la racine projet P7.3, l'identité
de configuration, les commandes, l'ownership, la disponibilité et la fraîcheur
des exécutables. Un fait absent, dupliqué, substitué ou stale bloque. Une
contrainte de version non évaluée bloque également ; elle n'est jamais déclarée
satisfaite par approximation. Les faits doivent être réobservés avant toute
future exécution autorisée.

## Contrat des commandes

Les types fermés restent `TEST`, `BUILD`, `LINT`, `TYPECHECK` et `OTHER`.
L'exécutable est un nom nu, les arguments sont un argv, le cwd est
repository-relatif et le statut required/optional est explicite. Les chemins
absolus, traversals, shells, métacaractères de contrôle et valeurs ressemblant à
des secrets sont refusés. P7.4 n'ajoute aucun exécuteur de commandes.

## Support observé sur la machine de certification

| Toolchain | Statut observé P7.4 | Portée |
|---|---|---|
| Python 3.11.9 | `AVAILABLE` | Sonde réelle requise et exécutée |
| Node/npm | `UNAVAILABLE` | Aucun support d'exécution certifié par simulation |
| Cargo/rustc | `UNAVAILABLE` | Aucun support d'exécution certifié par simulation |

La cible plateforme reste Windows 11 x64 selon P7.3. Linux, macOS, remote,
containers, installation de dépendances et portabilité Codex P7.5 restent hors
scope.
