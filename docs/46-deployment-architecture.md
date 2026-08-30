# Architecture de déploiement

## Objet

La Phase 5 doit permettre à un repository Git existant ou nouveau d'adopter
`AGENTIC_ENGINEERING_OS` sans copier ses composants internes. L'expérience
cible est : installation du produit, positionnement dans le repository cible,
initialisation, reconnaissance sûre, création de la configuration et de l'état
locaux, puis utilisation du runtime Codex certifié en Phase 4.

P5.1 définit les frontières et les contrats de cette adoption. Elle ne crée ni
CLI, ni initialiseur, ni migration.

Les invariants hérités restent applicables : le repository réel prévaut sur les
déclarations, Codex exécute tandis que le Control Plane décide, les stores
restent fail-closed, l'autorité Human est préservée, la réalité Git prévaut sur
les registres et aucune réparation ou migration n'est silencieuse.

## Modèle de propriété

### A — Produit installé

Le produit installé possède :

- le package Python et son futur point d'entrée `agentic-os` ;
- les moteurs de contrôle, de workflow, de parallélisme et d'exécution ;
- l'adaptateur Codex ;
- les schémas, contrats de rôle et modèles génériques nécessaires au runtime.

Ces ressources sont versionnées et distribuées avec le produit. Elles ne sont
normalement pas copiées dans un repository cible. Une future résolution de
ressources du package devra fournir une source unique, immuable et liée à la
version du produit.

Le runtime P4 résout actuellement certains schémas, documents et contrats de
rôle depuis le checkout de `AGENTIC_ENGINEERING_OS`. Cette dépendance est une
contrainte connue : elle devra être supprimée avant de déclarer l'installation
autonome prête. P5.1 ne la corrige pas.

### B — Contrat du repository cible

Le repository utilisateur possède les seuls éléments partageables suivants :

- `.agentic-engineering-os/config.json`, configuration projet minimale et
  versionnée ;
- `AGENTS.md`, si le projet accepte sa création ou une section gérée bornée ;
- les règles et overrides propres au projet explicitement déclarés ;
- les règles `.gitignore` requises pour l'état strictement local, dans une
  section gérée et identifiable.

Les fichiers utilisateur restent sous son autorité. L'OS ne remplace aucun de
ces fichiers et ne traite jamais une section non gérée comme modifiable.

### C — État runtime local au repository

Les stores existants utilisent `.agentic-engineering-os/` :

- `state.json` est le `ProjectState` autoritatif et doit être versionné ;
- `mission.json` est la mémoire de reprise d'une mission. Sa politique de suivi
  Git doit être explicite dans la configuration projet ; elle est versionnée
  par défaut afin que la reprise ne dépende pas d'une machine ;
- `worktrees.json`, `negative-outcomes.json`, `executions.json` et leurs
  fichiers temporaires sont locaux et Git-ignored, conformément à leur
  contenu lié aux chemins, worktrees ou exécutions de la machine.

Tous ces fichiers sont générés et mutés uniquement par leur store contrôlé. Le
fait qu'un fichier soit versionné n'autorise jamais son édition directe. Les
fichiers créés à la demande ne sont pas requis dès la fin de `init`.

### D — Configuration utilisateur ou machine

La découverte de l'exécutable Codex, les chemins de toolchains, les préférences
machine et les caches de capacités appartiennent à une configuration
utilisateur hors du repository. Leur emplacement exact relèvera du contrat
d'installation. Ils ne font pas partie du `ProjectState`, ne portent aucune
autorité de certification et ne sont jamais copiés dans le repository cible.
Les secrets ne doivent figurer dans aucune configuration projet ou état.

## Footprint canonique minimal

Après une initialisation complète, les éléments possibles sont :

```text
TARGET/
├── AGENTS.md                              # absent, créé ou section gérée
├── .gitignore                             # section gérée si nécessaire
├── .agentic-engineering-os/
│   ├── config.json                        # contrat projet versionné
│   ├── state.json                         # état projet versionné
│   ├── mission.json                       # politique Git explicite
│   ├── worktrees.json                     # local, ignoré, créé à la demande
│   ├── negative-outcomes.json             # local, ignoré, créé à la demande
│   └── executions.json                    # local, ignoré, créé à la demande
└── fichiers existants du projet
```

L'initialisation n'ajoute que les fichiers nécessaires et acceptés par son
plan. Elle ne copie ni `src/agentic_engineering_os`, ni tests, documentation
interne complète, historique de développement ou dossiers de certification de
l'OS. Le package installé fournit les ressources génériques.

## Contrat de configuration projet

La configuration doit rester petite et structurée ; son schéma sera défini en
P5.2. Les données sont classées comme suit.

| Élément | Classe | Règle |
|---|---|---|
| Racine Git canonique, HEAD, branche, propreté | `FACT` auto-détecté | Observé à chaque opération ; aucun chemin absolu machine n'est persisté comme vérité projet. |
| Identité du projet | explicite | Identifiant stable accepté par l'opérateur ; aucun nom risqué n'est deviné. |
| Langages et toolchains | `FACT` ou `INFERENCE` | Les manifestes fournissent des candidats ; les commandes autoritatives restent explicites si ambiguës. |
| Commandes de test | explicite | Une commande découverte est proposée mais doit être confirmée avant exécution contrôlée. |
| Build, lint et typecheck | override optionnel | Enregistrés lorsqu'ils existent et sont confirmés ; absence n'invente aucune commande. |
| Chemins protégés ou interdits | explicite | Requis lorsque la sécurité du projet en dépend ; les politiques certifiées de l'OS restent un plancher. |
| Politique d'exécution Codex | explicite | Bornes d'outils, sandbox, approbation et parallélisme ; elle ne peut élargir les capacités réellement disponibles. |
| Sources de documentation et contexte | explicite, compléments optionnels | Chemins repository-locaux sûrs, existants et autorisés. |
| Politique Git de `mission.json` | explicite avec défaut documenté | Défaut versionné ; tout override reste visible et validé. |
| Préférences et chemins machine | hors configuration projet | Résolus par la couche utilisateur/machine. |

Une observation auto-détectée n'est pas automatiquement une autorisation. Les
valeurs de sécurité ou d'autorité exigent une configuration explicite. Les
overrides ne peuvent assouplir les invariants du Control Plane.

## Sécurité d'un repository existant

Le futur `init` est inspect-first et sans mutation Git cachée. Il ne doit
jamais, sans politique future distincte et explicite :

- écraser ou supprimer un fichier ;
- créer, changer ou supprimer une branche ;
- committer, stasher ou reset ;
- réécrire `AGENTS.md`, remplacer une configuration utilisateur ou modifier
  une section non gérée ;
- interpréter une donnée ambiguë comme sûre.

Pour chaque cible existante, il inspecte, classe, calcule une stratégie de
fusion bornée ou un conflit explicite, puis bloque si l'intention ne peut être
prouvée. Un working tree dirty est un fait rapporté ; toute écriture qui
risquerait de masquer ou mélanger des changements non attribuables bloque.

## Stratégie `AGENTS.md`

- **Absent** : proposer la création d'un fichier court contenant une section
  OS clairement délimitée. Le dry-run montre son contenu exact avant écriture.
- **Existant** : préserver intégralement le contenu utilisateur. Une section
  gérée bornée peut être proposée, sans modification hors marqueurs, et exige
  une acceptation explicite. Si l'insertion sûre n'est pas démontrable, le
  fichier reste intact et la readiness bloque jusqu'à intégration humaine.
- **Déjà initialisé** : reconnaître des marqueurs uniques et un contenu géré
  conforme. Une seconde invocation conforme est un no-op. Des marqueurs
  multiples, édités ou incompatibles produisent `PARTIAL/INCONSISTENT` ou
  `UPGRADE_REQUIRED`, jamais une réécriture.

Aucun fichier compagnon n'est supposé importé automatiquement par Codex : ce
mécanisme n'est pas établi par le runtime P4. Le contrat racine reste donc le
point d'intégration compatible, sous autorité du repository utilisateur.

## Initialisation et idempotence

Le futur `agentic-os init` sépare planification et application :

1. observer sans écrire et classifier chaque résultat ;
2. produire un plan déterministe avec les créations, sections gérées,
   conflits, inconnues et fichiers laissés intacts ;
3. offrir un dry-run sans effet de bord ;
4. appliquer uniquement un plan encore lié aux observations courantes ;
5. relire et valider chaque résultat ;
6. rapporter l'état final sans commit automatique.

Les états conceptuels sont :

- `UNINITIALIZED` : aucun marqueur OS pertinent ;
- `INITIALIZED` : configuration compatible, intégration attendue et état
  présent ou explicitement différé sont cohérents ;
- `PARTIAL/INCONSISTENT` : éléments manquants, contradictoires, corrompus ou
  marqueurs non uniques ;
- `UPGRADE_REQUIRED` : format reconnu mais non compatible avec le produit.

Une seconde invocation sur `INITIALIZED` ne modifie rien. Une initialisation
partielle est détectée et rapportée ; elle n'est ni complétée ni réparée
silencieusement.

## Versions et upgrades

Trois dimensions restent indépendantes :

- la **product version** identifie le package et ses ressources ;
- la **config version** identifie le contrat de configuration du repository ;
- chaque **runtime state schema version** identifie son propre store
  (`ProjectState`, `MissionState`, registre de worktrees, résultats négatifs et
  exécutions).

Mettre à jour le package ne migre jamais automatiquement la configuration ou
l'état. Un format incompatible est refusé. Une future migration devra produire
un plan explicite, identifier les versions source et cible, créer une sauvegarde
récupérable avant mutation, écrire atomiquement, valider le résultat et définir
le rollback. Une réinterprétation, réparation ou suppression silencieuse est
interdite.

## Reconnaissance du repository

Avant toute adoption, P5 observe au minimum : repository et racine Git,
branche/HEAD/propreté, fichiers `AGENTS.md`, configuration et états existants,
manifestes de langage et toolchain, définitions de tests/build, structure et
taille, worktrees Git, zones protégées déclarées et disponibilité/capacités
Codex issues du contrat P4.

Chaque résultat porte une classe :

- `FACT` : résultat directement observé et reproductible ;
- `INFERENCE` : candidat dérivé de faits, jamais autorité d'exécution ;
- `UNKNOWN` : information absente, contradictoire ou non vérifiable.

Une inconnue critique pour l'autorité, la sécurité, une commande ou un chemin
impose une configuration explicite ou un blocage. P5.1 n'autorise aucun guessing
LLM pour transformer une inférence ou une inconnue en fait.
