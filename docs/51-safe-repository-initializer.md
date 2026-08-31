# Safe Repository Initializer

## Objet et frontière

`RepositoryInitializer.apply(plan, *, human_confirmations=()) ->
InitializationResult` applique uniquement le sous-ensemble P5.5 explicitement
sûr d'un `InitializationPlan` P5.4. Le résultat est immuable et distingue les
opérations `APPLIED`, `NO_OP`, `FAILED` et `NOT_ATTEMPTED`.

L'initializer ne lance aucune commande projet et ne crée, change, commit,
stash ou reset aucune branche Git. Il ne bootstrappe aucun état runtime et ne
fournit aucune CLI.

## Reconstruction de confiance

Un `InitializationPlan` public n'est jamais réputé fiable par sa seule forme.
Immédiatement avant toute mutation, l'initializer :

1. résout la racine canonique sans composant symlink ;
2. reconstruit un nouveau `RepositoryProfile` ;
3. recharge la configuration courante via le loader P5.2 lorsqu'elle est
   valide ;
4. régénère le plan canonique avec `InitializationPlanner` ;
5. exige l'égalité exacte du plan fourni et du plan reconstruit ;
6. valide le catalogue, l'ordre, les cibles et les confirmations.

Le constructeur n'accepte aucun planner, reconnaisseur ou validator injecté
par l'appelant. Il n'existe ni `trusted=True`, ni API d'écriture arbitraire.
Avant chaque opération, racine Git, HEAD, branche, mode detached et worktrees
sont de nouveau comparés au snapshot initial.

## Sous-ensemble d'opérations P5.5

P5.5 autorise uniquement :

- `CREATE_DIRECTORY` pour `.agentic-engineering-os` absent ;
- `INITIALIZE_CONFIG` au chemin canonique absent ;
- `CREATE_MANAGED_FILE` pour `AGENTS.md` ou `.gitignore` absents ;
- `ADD_GITIGNORE_SECTION` pour un fichier existant observé sans section et
  avec confirmation Human exacte ;
- `UPDATE_MANAGED_SECTION` pour `AGENTS.md` uniquement via la frontière bornée
  `AgentsIntegrationService` définie en P5.6 ;
- `NO_OP` après vérification de la configuration ou section courante.

`BLOCKED_CONFLICT` et tout type/cible hors catalogue sont refusés avant la
première écriture. Aucun autre fichier ni contenu libre ne peut emprunter la
voie AGENTS.

## Confirmation Human

`HumanOperationConfirmation` lie exactement :

- `plan.input_fingerprint` ;
- l'identifiant ordinal de l'opération ;
- le chemin cible ;
- l'état courant attendu ;
- l'empreinte exacte du fichier utilisateur attendu ;
- une identité Human attribuable selon la normalisation canonique existante.

Une confirmation manquante, supplémentaire, dupliquée ou liée à un autre plan
est refusée. Les variantes de l'identité réservée Codex ne peuvent jamais
satisfaire cette exigence. Aucun booléen global d'approbation n'existe.

## Écritures et atomicité

Les créations de fichiers utilisent un temporaire dans le même dossier,
`flush` et `fsync`, puis un lien atomique exclusif. Une cible apparue après le
planning ou pendant la création n'est jamais remplacée. Le temporaire est
nettoyé après succès ou échec.

La configuration provient uniquement de la sérialisation canonique P5.2 et est
rechargée puis comparée après création. Les fichiers gérés absents utilisent le
contenu exact et le SHA-256 du plan.

Pour `.gitignore` existant, l'empreinte porte sur les octets bruts observés.
Les octets utilisateur sont conservés comme préfixe inchangé et la section
canonique est ajoutée. Le fichier est relu juste avant un `os.replace` atomique
explicitement confirmé ; tout changement détecté bloque et préserve l'ancien
fichier. Le dossier est fsyncé lorsque la plateforme le permet.

## Ordre, échec partiel et vérification

Les opérations suivent strictement l'ordre P5.4. Chaque précondition est
revalidée juste avant l'opération, puis chaque résultat est relu. Au premier
échec :

- l'exécution s'arrête ;
- l'opération fautive est `FAILED` ;
- les suivantes sont `NOT_ATTEMPTED` ;
- les écritures antérieures restent visibles ;
- aucune suppression ou rollback destructif n'est tenté ;
- une reconnaissance finale expose le footprint partiel.

L'atomicité est celle de chaque fichier, jamais celle du plan complet.

## Idempotence et replay

Après une application réussie, la reconnaissance classe le footprint
`INITIALIZED`. Le working tree est naturellement dirty puisque P5.5 ne commit
pas ; P5.4 autorise alors uniquement un re-plan conforme composé de `NO_OP`.

Un second apply de ce nouveau plan produit `NO_OP`. Le replay de l'ancien plan
de création est refusé comme `UNTRUSTED_OR_STALE_PLAN` et ne réécrit rien.

## Limite du modèle de menace

Les contrôles détectent les changements avant installation et immédiatement
avant remplacement. La bibliothèque standard ne fournit pas un compare-and-swap
portable de fichier existant contre un processus hostile modifiant le même
répertoire dans l'intervalle ultime entre vérification et remplacement. P5.5
ne prétend donc pas résister à un processus hostile contrôlant simultanément
le filesystem et le code du produit.

## Hors scope P5.5

Sont exclus : bootstrap `ProjectState` ou `MissionState`, migrations,
suppression/rename de fichiers utilisateur,
permissions arbitraires, commandes libres et CLI.
