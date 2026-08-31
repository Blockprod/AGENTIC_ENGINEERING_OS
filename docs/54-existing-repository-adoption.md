# Existing Repository Adoption

P5.8 compose les frontières P5.2 à P5.7 pour rendre un repository existant
structurellement prêt à utiliser `AGENTIC_ENGINEERING_OS`. `ADOPTED` signifie
uniquement que la configuration, l'intégration structurelle et l'état runtime
minimal sont prêts. Ce statut ne crée ni mission, ni User Story, ni Evidence,
ni Certification.

## API et séparation des phases

`ExistingRepositoryAdoption` expose uniquement :

- `prepare_adoption(repository_root, project_configuration=None)` :
  reconnaissance, résolution explicite de configuration et planification sans
  mutation ;
- `apply_adoption(preparation, human_confirmations=())` : application de la
  préparation exacte, puis bootstrap runtime et reconnaissance finale.

La préparation retourne le profil courant, les décisions de configuration
encore requises, le plan, les confirmations Human nécessaires et les blockers.
L'application retourne les opérations réellement appliquées, le résultat du
bootstrap, le profil final et les anomalies observées.

## États fermés

- `NEEDS_CONFIGURATION` : aucune configuration autoritative n'est disponible ;
- `NEEDS_HUMAN_CONFIRMATION` : le plan est valide mais une mutation de fichier
  utilisateur attend une confirmation exactement liée ;
- `READY_TO_APPLY` : le plan peut être soumis explicitement à l'initializer ;
- `PARTIAL_OR_INCONSISTENT` : un footprint partiel existe ou une application
  s'est interrompue après mutation ;
- `UPGRADE_REQUIRED` : une version existante n'est pas supportée ;
- `ADOPTED` : la reconnaissance finale prouve la readiness structurelle et
  runtime minimale ;
- `BLOCKED` : une autre précondition obligatoire n'est pas satisfaite.

## Configuration et Human Authority

Une configuration explicitement fournie est validée par P5.2. À défaut, seul
un `config.json` existant, valide et rechargé peut servir d'autorité. Les
toolchains et commandes classées `INFERENCE` par la reconnaissance ne sont
jamais promues en configuration. Une configuration absente retourne
`NEEDS_CONFIGURATION`.

Les confirmations sont les `HumanOperationConfirmation` P5.5 liées au plan, à
l'opération, à la cible et à son empreinte. Le coordinateur ne les crée pas.
Une identité Codex, une confirmation périmée ou un repository modifié est
refusé par la reconstruction de confiance de l'initializer.

## Chaîne d'application

Le coordinateur n'écrit aucun fichier. La chaîne fermée est :

```text
InitializationPlan
→ RepositoryInitializer
→ AgentsIntegrationService
→ RuntimeStateBootstrap
→ RepositoryReconnaissance
```

Le passage P5.5 → P5.7 autorise le working tree dirty produit par
l'initialisation uniquement lorsque le `InitializationResult` est `APPLIED`,
son profil final et son identité Git correspondent exactement au profil
courant, et que les chemins Git modifiés sont exactement les sorties
structurelles appliquées parmi `config.json`, `AGENTS.md` et `.gitignore`.
Tout chemin supplémentaire bloque le bootstrap. Aucun commit, changement de
branche, stash ou reset n'est réalisé.

## Readiness finale et reprise

Après application, une nouvelle reconnaissance doit prouver : repository
supporté, scan complet, configuration valide et identique, sections AGENTS et
Git-ignore courantes, `state.json` de version compatible, et identité Git
inchangée. La politique Git réelle des stores est contrôlée par P5.7.
`mission.json` n'est pas nécessaire pour un repository idle et P5.8 ne le crée
jamais.

Au premier échec, les composants s'arrêtent sans rollback destructif. Les
fichiers déjà créés restent observables. Un nouvel appel doit recommencer par
`prepare_adoption`; aucun ancien plan ou handoff n'est rejoué. Un repository
structurellement complet dont le bootstrap n'a pas commencé peut exiger que
l'opérateur établisse d'abord une nouvelle baseline Git propre, car P5.8 ne
fabrique pas rétroactivement une autorité d'initialisation.

P5.8 ne migre aucune version, n'exécute aucune mission et ne fournit ni CLI ni
découverte LLM.
