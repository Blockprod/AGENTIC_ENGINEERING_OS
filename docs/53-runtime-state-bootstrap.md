# Runtime State Bootstrap

P5.7 initialise l'état runtime minimal d'un repository cible déjà configuré et
structurellement initialisé. Cette opération ne lance pas de mission, ne
certifie rien et n'infère aucune vérité métier.

## API et préconditions

`RuntimeStateBootstrap.bootstrap(repository_root, project_configuration,
expected_profile=...)` exige une `ProjectConfiguration` canonique identique au
fichier du repository et un `RepositoryProfile` courant, rattaché au même
chemin et à la même identité Git. Le repository doit être conforme aux étapes
P5.5/P5.6, sans footprint runtime partiel ni version inconnue. Lorsque la
configuration exige un Git propre, cette condition s'applique à la première
création.

Le profil est reconstruit et comparé exactement avant l'action, puis la
fraîcheur et l'identité Git sont contrôlées une seconde fois juste avant
l'écriture. Il n'existe aucun re-planning implicite.

## Footprint minimal

| Fichier | Politique | Motif |
|---|---|---|
| `.agentic-engineering-os/state.json` | `REQUIRED_AT_BOOTSTRAP` | État autoritatif minimal du Control Plane. |
| `.agentic-engineering-os/mission.json` | `AUTHORIZED_EVENT_ONLY` | Une mission ne peut exister qu'après un événement de mission réel et autorisé. |
| `.agentic-engineering-os/worktrees.json` | `LAZY_INITIALIZED_ON_FIRST_USE` | Aucun registre n'est nécessaire sans worktree planifié. |
| `.agentic-engineering-os/negative-outcomes.json` | `LAZY_INITIALIZED_ON_FIRST_USE` | Aucun ledger n'est nécessaire sans résultat négatif. |
| `.agentic-engineering-os/executions.json` | `LAZY_INITIALIZED_ON_FIRST_USE` | Aucun ledger n'est nécessaire sans requête d'exécution. |

L'absence des stores lazy et de `mission.json` est donc l'état canonique d'un
repository prêt mais idle. Leur présence sans `state.json` constitue un
footprint partiel et bloque le bootstrap.

## Sémantique de l'état

Le bootstrap délègue exclusivement la création à `ProjectStateStore`. L'état
initial est au schéma canonique et contient zéro User Story, Evidence, Gate,
Certification et AuditEvent. Aucune approbation Human n'est créée ou inférée.

Un état existant valide produit `ALREADY_BOOTSTRAPPED` sans réécriture. Un état
corrompu ou dangereux bloque l'opération ; une version inconnue exige une voie
d'upgrade explicite. L'initialisation exclusive du store refuse un fichier qui
apparaît concurremment et ne l'écrase jamais.

## Échec et politique Git

La configuration et `state.json` doivent rester versionnables. Les ledgers
volatils et leurs fichiers temporaires doivent être réellement ignorés par Git,
et la politique de `mission.json` doit correspondre à la configuration. P5.7
ne modifie pas `.gitignore` : une divergence requiert un nouveau plan
structurel.

Il n'y a pas de promesse d'atomicité distribuée. Les stores sont traités dans
un ordre déterministe via leur API autoritative, l'exécution s'arrête au
premier échec, aucun rollback destructif n'est tenté et tout fichier déjà créé
reste observable. Un échec après création est rapporté
`PARTIAL_FAILURE`, jamais `BOOTSTRAPPED`.
