# Explicit Upgrade & Migration

Une mise à jour du package ne migre jamais un repository. Les loaders,
l'adoption et le bootstrap continuent de retourner ou imposer
`UPGRADE_REQUIRED`. La migration suit exclusivement :

```text
inspect → plan → confirmation Human → backup → apply → validate
→ re-reconnaissance
```

## Inventaire persistant

| Artefact | Version courante | Historique réel | Git | Volatile | Migration P5.9 | Comportement du loader |
|---|---:|---:|---|---|---|---|
| `config.json` | `1.0` | aucun | versionné | non | aucune | version inconnue refusée |
| `state.json` | `1.0` | aucun | versionné | non | aucune | version/forme invalide refusée |
| `mission.json` | `1.0` | aucun | selon `mission_state_git_policy` | non | aucune | version/forme invalide refusée |
| section AGENTS | `2` | `1` | versionnée | non | `1 → 2` | ancienne version classée `UPGRADE_REQUIRED` |
| section Git-ignore | `1` | aucun | versionnée | non | aucune | altération/version incompatible refusée |
| `worktrees.json` | `1.0` | aucun | ignoré | oui | aucune | version incompatible refusée |
| `negative-outcomes.json` | `2.0` | `1.0` | ignoré | oui | `1.0 → 2.0` | exige explicitement une migration `2.0` |
| `executions.json` | `1.1` | `1.0` | ignoré | oui | non supportée | ancienne version refusée |

Les autres contrats JSON sont des entrées/sorties, ressources de validation ou
objets contenus dans `ProjectState`; ils ne constituent pas des stores
supplémentaires du footprint d'adoption.

`executions.json 1.0 → 1.1` n'est pas supporté : `1.1` ajoute les chemins Git
modifiés à chaque observation. Pour une ancienne observation dirty, ces faits
ne peuvent pas être reconstruits sans invention. Une migration vide ou
conditionnelle donnerait une fausse garantie pour l'edge général.

## API et registre fermé

`UpgradePlanner.plan(repository_root) -> UpgradePlan` produit un plan immuable
lié à la racine, HEAD, branche, empreinte du profil, versions sources,
empreintes exactes, versions cibles et ordre des étapes.

`RepositoryUpgradeService.apply(plan, confirmations=()) -> UpgradeResult`
reconstruit d'abord le même plan. Il n'existe ni `migrate(anything)`, ni script
générique, ni migration déclenchée par un loader.

Le registre contient exactement :

- `AGENTS_MANAGED_SECTION: 1 → 2` ;
- `NEGATIVE_OUTCOME_LEDGER: 1.0 → 2.0`.

Aucune chaîne implicite n'existe. Toute autre source, destination ou version
future est `UNSUPPORTED_MIGRATION`.

## Transformations et autorité

La migration AGENTS remplace uniquement la section historique v1 canonique,
en conservant les octets utilisateur et la convention de newline. Elle exige
une confirmation Human liée au plan, à l'étape, à l'artefact, à l'empreinte
source et à la version cible. Une identité Codex est refusée.

La migration negative-outcomes valide strictement chaque outcome v1, conserve
son résultat, son fingerprint et son état `consumed`, passe la version à `2.0`
et ajoute seulement `transactions: []`. Une empreinte sémantique des outcomes
doit être identique avant et après. Aucune User Story, Evidence, Gate,
Certification, approbation Human, génération de workflow ou réussite
d'exécution n'est créée ou promue.

## Backups, Git et écritures

Chaque étape possède un backup déterministe adjacent, incluant versions et
SHA-256 source. Le backup est créé exclusivement avant l'écriture, borné,
fsyncé lorsque possible, vérifié et jamais supprimé automatiquement. Une
collision bloque la migration.

Le candidat est généré de nouveau depuis le registre, prévalidé, écrit dans un
temporaire du même dossier, flushé/fsyncé, puis installé par remplacement
atomique. Le fichier est relu et post-validé. L'identité Git et les chemins
dirty sont contrôlés avant chaque frontière. Seuls les sources versionnées et
backups créés par le plan peuvent apparaître. Il n'y a aucun commit, stash,
reset, rebase ou checkout automatique.

Le repository doit être propre avant une migration réelle. Un repository déjà
current reste `ALREADY_CURRENT` sans écriture, même si l'opérateur n'a pas
encore commité les résultats et backups d'une migration précédente.

## Échec, reprise et replay

Les étapes suivent un ordre fixe. Au premier échec, les suivantes ne sont pas
tentées, les migrations et backups antérieurs restent observables, aucun
rollback destructif n'est réalisé et le résultat n'est jamais `MIGRATED`.
Une nouvelle reconnaissance et un nouveau plan sont obligatoires.

Après succès, un nouveau plan retourne `ALREADY_CURRENT`. Le replay de
l'ancien plan est refusé car sources, profil ou backups ne correspondent plus.

P5.9 ne fournit aucune CLI et n'anticipe pas P5.10.
