# Initialization Planner déterministe

## Objet et frontière

`InitializationPlanner.plan(profile, desired_configuration, ...) ->
InitializationPlan` transforme un `RepositoryProfile` immuable et une
`ProjectConfiguration` explicite en dry-run inspectable. Il ne lit pas le
repository cible, n'écrit aucun fichier, ne modifie pas Git et n'exécute
aucune commande projet.

Le planner décrit des intentions. Il ne les autorise ni ne les applique. Une
observation, une empreinte ou un plan ne remplace jamais une décision du
Control Plane ou une confirmation Human requise.

## Modèle du plan

`InitializationPlan` contient :

- l'identité de la racine Git, HEAD et branche observés ;
- l'empreinte du profil complet et celle de tous les inputs du plan ;
- la classification d'initialisation courante ;
- la configuration désirée immuable, sa version et son empreinte ;
- une suite ordonnée d'opérations structurées ;
- les blockers, warnings et confirmations Human requises ;
- le footprint résultant attendu, avec l'état runtime explicitement différé ;
- un indicateur `ready_for_application` non autoritatif.

Les objets sont des dataclasses gelées. Ils n'embarquent aucun timestamp,
identifiant aléatoire ou état conversationnel.

## Catalogue fermé

Les seuls types d'opérations sont :

- `CREATE_DIRECTORY` ;
- `INITIALIZE_CONFIG` ;
- `CREATE_MANAGED_FILE` ;
- `UPDATE_MANAGED_SECTION` ;
- `ADD_GITIGNORE_SECTION` ;
- `NO_OP` ;
- `BLOCKED_CONFLICT`.

Chaque opération porte un identifiant ordinal déterministe, une cible
repository-relative fermée, l'état courant attendu, l'état désiré, une raison,
une source et le besoin éventuel d'une confirmation Human. Lorsqu'un contenu
est prévu, le contenu canonique exact et son SHA-256 sont présents. Il n'existe
aucune opération de commande arbitraire.

Une opération modifiant un fichier utilisateur porte aussi l'empreinte exacte
des octets attendus de cette cible. Cette empreinte est reprise dans la
confirmation Human et revalidée par l'initializer.

## Liaison aux observations

`profile_fingerprint` est le SHA-256 de la représentation JSON canonique de
toutes les observations sémantiques du `RepositoryProfile`.
`input_fingerprint` lie cette empreinte aux sérialisations canoniques des
configurations désirée et courante.

Le profil contient en complément, pour `AGENTS.md` et `.gitignore`, une
observation bornée de section gérée : fichier absent, section absente,
`CURRENT`, `TAMPERED`, `AMBIGUOUS`, `UNSAFE` ou `UNKNOWN`. Seule l'empreinte du
fichier est conservée, jamais son contenu utilisateur. La configuration valide
porte une empreinte sémantique issue de sa sérialisation P5.2 canonique.

Un appel peut fournir `expected_profile_fingerprint`. Toute divergence produit
`STALE_PROFILE`. Une future application devra refaire la reconnaissance et
comparer l'empreinte au plan ; P5.4 n'effectue pas cette application.

## Validation fail-closed

Le planner bloque si les faits Git ne prouvent pas un repository supporté,
complet et propre pour toute mutation, si racine/HEAD/worktree sont
incohérents, ou si la
configuration courante fournie ne correspond pas à l'empreinte observée. Une
configuration désirée absente, invalide ou forgée n'est jamais remplacée par
une inférence de toolchain ou de commande.

Un repository dirty déjà `INITIALIZED` peut uniquement produire le plan
idempotent `NO_OP` lorsque toutes les cibles sont conformes. Cette exception ne
permet aucune nouvelle écriture.

Les états sont traités ainsi :

- `UNINITIALIZED` : plan du footprint minimal accepté ;
- `INITIALIZED` : `NO_OP` lorsque configuration et sections sont conformes ;
- `PARTIAL_OR_INCONSISTENT` : blockers, aucune réparation planifiée ;
- `UPGRADE_REQUIRED` : blocker, aucune migration planifiée.

## Configuration

Une configuration absente et une configuration désirée P5.2 valide produisent
`CREATE_DIRECTORY` puis `INITIALIZE_CONFIG`. Une configuration courante valide
doit être fournie au planner et correspondre à son empreinte observée. Si elle
est identique à la configuration désirée, l'opération est `NO_OP`. Si elle
diffère, `EXISTING_CONFIG_CONFLICT` bloque sans contenu de remplacement.

Une configuration invalide, illisible, unsafe ou de version inconnue bloque.
Le planner ne répare, ne migre et n'écrase jamais un document existant.

## Sections gérées

Les marqueurs versionnés et leur contenu canonique sont définis dans le modèle
de reconnaissance afin que le dry-run expose exactement l'intention future.
La variante canonique de `.gitignore` est dérivée de la configuration désirée :
elle exclut `mission.json` pour `IGNORED` et conserve la variante historique
sans cette règle pour `TRACKED`. Le planner ne choisit jamais cette politique.

- fichier absent : `CREATE_MANAGED_FILE`, sans écriture pendant P5.4 ;
- fichier utilisateur présent sans section : insertion bornée planifiée avec
  confirmation Human obligatoire ;
- section canonique unique : `NO_OP` ;
- section altérée, dupliquée, ambiguë, unsafe ou inconnue : blocker ;
- version AGENTS ancienne ou future : `UPGRADE_REQUIRED`, sans migration.

Cette politique s'applique séparément à `AGENTS.md` et `.gitignore`. Aucun
contenu situé hors de la section gérée n'est copié dans le plan.

## Idempotence et état runtime

Même profil et mêmes configurations produisent le même plan. Un profil conforme
après future application tend vers trois `NO_OP` : configuration, `AGENTS.md`
et `.gitignore`.

`state.json` apparaît dans le footprint attendu avec
`RUNTIME_INITIALIZATION_DEFERRED`. Aucune opération ne le crée : l'état runtime
reste sous la responsabilité exclusive de ses stores contrôlés.

## Hors scope P5.4

P5.4 ne fournit ni initializer, ni écriture atomique, ni modification
`AGENTS.md`/`.gitignore`, ni runtime state, ni migration, ni CLI, ni exécution
de commandes. `ready_for_application` signifie seulement que le plan ne
contient aucun blocker ou consentement Human non résolu ; ce booléen ne confère
aucune autorité d'exécution.
