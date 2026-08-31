# Production Governance & Observability Contract

## Objet et portée

Phase 6 doit rendre le fonctionnement de l'OS explicable, mesurable et
opérable en production sans créer une seconde source d'autorité. P6.1 définit
les frontières conceptuelles ; il ne crée aucun modèle Python, schéma, store,
event bus, métrique, dashboard, daemon, scheduler ou commande.

## Sources de vérité existantes

| Source existante | Responsabilité actuelle |
|---|---|
| Repository, fichiers et Git réels | Réalité matérielle qui prévaut sur les déclarations et registres. |
| `ProjectConfiguration.project_id` | Identité explicite du projet et contraintes repository-locales. |
| `ProjectState` / `ProjectStateStore` | État autoritatif des User Stories, Evidence, Gates, Certifications et AuditEvents. |
| `MissionState` / `MissionStateStore` | Mémoire opérationnelle de mission et génération ; aucune autorité de contrôle. |
| `executions.json` | Ledger des faits d'exécution Codex et de leur validation/recovery. |
| `worktrees.json` et Git | État attendu des affectations et réalité physique des worktrees ; Git prévaut en cas de divergence. |
| `negative-outcomes.json` | Outcomes négatifs bornés, privés et consommables par les workflows autorisés. |
| Services P1–P5 | Seules frontières capables de valider et demander les mutations qu'elles possèdent. |

Les workflows séquentiels P2, parallèles P3 et runtime Codex P4 composent ces
sources. L'adoption, la configuration et les migrations P5 restent inspectées,
planifiées puis appliquées par leurs services existants.

## Séparation des plans

```text
AUTHORITATIVE STATE
        ↓ observations factuelles
OBSERVABILITY PLANE
        ↓ informations non autoritatives
GOVERNANCE PLANE
        ↓ requêtes contrôlées ou blocage futur explicitement autorisé
EXISTING AUTHORITATIVE SERVICES P0–P5
```

- Le **plan autoritatif** conserve les états métier, preuves, décisions et
  mutations contrôlées existants.
- Le **plan d'observabilité** collecte et corrèle des faits sans les convertir
  en décisions métier.
- Le **plan de gouvernance** évalue des politiques opérationnelles à partir de
  faits observés. Il peut recommander ou demander une action contrôlée, jamais
  effectuer une mutation autoritative par raccourci.

### Invariant primaire

`OBSERVATION != AUTHORITY`.

Un log, une métrique, une trace, une alerte, un dashboard, un health signal ou
un incident record ne peut jamais directement :

- changer `UserStory.status` ;
- produire une Evidence ;
- faire passer un Gate ;
- créer une Certification ;
- approuver comme Human ;
- terminer une Mission ;
- autoriser un merge.

Une observation peut déclencher une évaluation ou une requête. Toute action
reste soumise au service P0–P5 propriétaire, à ses préconditions et à son
autorité. Une affirmation d'observabilité n'est pas une preuve certifiante.

## Observability Plane

### Couverture attendue

Le plan devra pouvoir observer, sans les piloter directement :

- missions, générations de workflow, User Stories et handoffs de rôles ;
- exécutions Codex, durées, terminaisons, retries et recovery ;
- worktrees, Integration Gates, merges et outcomes négatifs ;
- Evidence, Gates et Certifications déjà produits par le Control Plane ;
- attentes nécessitant une décision Human ;
- adoption, initialisation et migrations explicites ;
- erreurs de persistance, états bloqués, partiels ou incohérents ;
- disponibilité des stores, de Git et du runtime Codex.

### Primitives conceptuelles minimales

| Primitive | Contrat |
|---|---|
| `EVENT` | Fait opérationnel ponctuel, horodaté, sérialisable et non autoritatif. |
| `METRIC` | Mesure agrégée issue de faits identifiés ; jamais état métier. |
| `HEALTH` | Évaluation bornée de la capacité opérationnelle d'un composant ou périmètre. |
| `DIAGNOSTIC` | Explication factuelle destinée à l'opérateur, avec sources et incertitudes. |
| `TRACE/CORRELATION` | Liens permettant de reconstruire un parcours à partir des identités existantes. |

## OperationalEvent futur

Le futur modèle minimal devra évaluer les champs suivants sans que P6.1 fixe
encore un schéma ou une API :

| Champ conceptuel | Règle |
|---|---|
| identité d'événement | Unique dans son store ; déterministe lorsqu'un fait source possède déjà une identité stable. |
| timestamp UTC | Horodatage explicite ; ne suffit pas à prouver seul un ordre causal. |
| type | Catalogue fermé et versionné par la future mission P6.2. |
| sévérité | Classification opérationnelle fermée ; absence ou ambiguïté reste `UNKNOWN`. |
| composant source | Producteur technique attribuable. |
| corrélations | Références existantes applicables : projet, mission, génération, story, exécution, affectation, Gate, merge ou certification. |
| identité projet/repository | `project_id` et état Git pertinent ; aucun chemin machine ne devient identité projet. |
| payload factuel | Données JSON bornées, explicites et sans verdict implicite. |
| provenance | Source observée, version du producteur et méthode d'observation lorsque nécessaires. |

Les OperationalEvents sont factuels, append-oriented, déterministes lorsque
le fait le permet, sûrs à sérialiser et secret-aware. Une correction produit un
nouvel événement lié au précédent ; elle ne réécrit pas silencieusement
l'historique. Les doublons, pertes, ordres partiels et redémarrages devront être
traités explicitement par P6.2/P6.3.

### AuditEvent et OperationalEvent

Un `AuditEvent` P1 appartient au `ProjectState`. Il retrace un événement
significatif du cycle de vie autoritatif et référence le commit, l'acteur, le
rôle et les éléments de contrôle concernés. Son enregistrement n'est déjà pas
une Evidence et ne remplace pas la décision sous-jacente.

Un `OperationalEvent` P6 décrira le comportement du runtime, y compris des
tentatives, latences, indisponibilités et incidents qui ne sont pas des
transitions métier. Il appartiendra à une persistance d'observabilité distincte
et non autoritative.

Il n'existe aucune conversion implicite entre eux :

- un AuditEvent peut être observé et référencé par son `event_id` dans un
  OperationalEvent miroir ;
- un OperationalEvent ne crée ni AuditEvent, ni Evidence, ni transition ;
- si une action contrôlée produit ensuite un AuditEvent, ce dernier est créé
  par le service autoritatif concerné, avec sa propre validation.

## Metrics Contract

La surface métrique doit rester petite, bornée et dérivée de sources
attribuables.

| Catégorie | Type principal | Sens |
|---|---|---|
| mission throughput | `COUNTER` / `DERIVED METRIC` | Missions observées par résultat et intervalle. |
| role et Codex execution duration | `HISTOGRAM/DURATION` | Durées bornées par rôle, résultat et classe d'exécution. |
| failure/block rate | `COUNTER` / `DERIVED METRIC` | Échecs et blocages observés, divisés par une population explicite. |
| remediation/recovery | `COUNTER` | Générations, remédiations et recoveries observés. |
| worktree utilization | `GAUGE` / `DERIVED METRIC` | Affectations actives rapportées à une capacité connue. |
| Gate failure rate | `COUNTER` / `DERIVED METRIC` | Résultats de Gates existants, sans les réévaluer. |
| certification latency | `HISTOGRAM/DURATION` | Temps entre bornes corrélées et explicitement définies. |
| Human waiting time | `GAUGE` / `HISTOGRAM/DURATION` | Attentes Human actives et durées closes, sans simuler une approbation. |
| persistence failures | `COUNTER` | Échecs de lecture/écriture classés par store et code. |

Un `COUNTER` est monotone dans un espace de collecte dont les resets sont
visibles. Un `GAUGE` est un instantané et non une vérité historique. Un
`HISTOGRAM/DURATION` utilise des unités et bornes explicites. Une
`DERIVED METRIC` conserve sa formule, sa fenêtre et les sources utilisées.
Aucune métrique dérivée ne devient une source de vérité métier.

Les dimensions à cardinalité non bornée, les payloads libres, chemins absolus,
prompts, stdout/stderr et identités secrètes sont interdits comme labels.

## Health Model

Le futur modèle fermé retient :

- `HEALTHY` : préconditions opérationnelles observées satisfaites ;
- `DEGRADED` : service disponible avec perte ou risque borné et explicite ;
- `BLOCKED` : exécution future sûre impossible selon une politique applicable ;
- `UNKNOWN` : observation obligatoire absente, contradictoire ou non fiable.

L'évaluation doit couvrir séparément : state stores, Git/worktrees, runtime
Codex, execution ledger, remédiations pendantes et cohérence du repository.
L'agrégation future sera déterministe et conservera les causes ; elle ne
transformera jamais `UNKNOWN` en `HEALTHY`.

`HEALTHY != CERTIFIED`. De même, `BLOCKED` dans ce modèle est une condition
opérationnelle et ne mute ni `MissionStatus`, ni `UserStoryStatus`. Health ne
peut autoriser aucune progression.

## Governance Plane

La gouvernance pourra :

- observer et évaluer des politiques versionnées ;
- suivre les budgets de ressources, concurrence, temps et échecs ;
- classifier un incident et recommander une escalade ;
- demander une action contrôlée ou une intervention opérateur ;
- placer les futures exécutions en maintenance/freeze uniquement lorsqu'une
  politique future explicite possède cette autorité bornée.

Elle ne peut ni contourner un service autoritatif, ni affaiblir une règle
P0–P5, ni réparer silencieusement un état. Une décision de gouvernance conserve
la policy, les faits d'entrée, le résultat et la requête produite. La réussite
de la requête reste décidée par le service appelé.

## Policy Model conceptuel

Une future policy devra être identifiable, versionnée, scoped et évaluée sur
des faits attribuables. Son résultat ne vaut que pour le contexte et la fenêtre
observés.

| Classe | Pouvoir maximal |
|---|---|
| `HARD SAFETY POLICY` | Plancher non désactivable : fail-closed, autorité Human, isolation repository, sandbox maximale, intégrité des stores. |
| `OPERATIONAL POLICY` | Borne l'exploitation : concurrence, temps, ressources, seuils d'échec, vérification, maintenance. Elle peut seulement durcir le plancher. |
| `OPERATOR PREFERENCE` | Présentation, seuils informatifs ou choix entre options déjà autorisées ; aucune capacité d'affaiblissement. |

Les domaines initiaux incluent le maximum d'exécutions Codex concurrentes, le
maximum de générations de remédiation, le plafond de timeout, les budgets
disque/worktree, les sandbox autorisées, le niveau de vérification exigé et les
politiques de maintenance/freeze. Une limite P6 ne peut jamais relever une
limite plus stricte de `ProjectConfiguration` ou du runtime P4.

## Incident et escalade

Un incident est un fait opérationnel nécessitant investigation, décision ou
action contrôlée, par exemple : échecs répétés de persistance, transaction
irrécupérable, état incohérent, boucle d'exécution stale, divergence
Git/worktree, échecs Codex répétés, épuisement de ressources ou refus
autoritatif inattendu.

```text
DETECT
  → RECORD
  → CLASSIFY
  → ESCALATE
  → OPERATOR DECISION
  → CONTROLLED REMEDIATION
```

- `DETECT` conserve le signal et ses incertitudes.
- `RECORD` ajoute un incident sans réécrire la cause.
- `CLASSIFY` applique une taxonomie fermée et une sévérité explicite.
- `ESCALATE` désigne le destinataire et l'urgence, sans simuler sa décision.
- `OPERATOR DECISION` est attribuable ; elle ne devient Evidence Human que par
  la frontière Human existante lorsque le contrat métier l'exige.
- `CONTROLLED REMEDIATION` appelle le workflow/service propriétaire et vérifie
  son résultat. Aucune réparation silencieuse n'est permise.

## Corrélation et traçabilité

La chaîne reconstructible est :

```text
project_id / repository Git
  → mission_id
  → workflow_generation
  → UserStory.id
  → role + handoff/result identity
  → execution_id / request_id
  → WorktreeAssignment.assignment_id
  → Integration Gate context/result
  → MergeResult et commits
  → Gate.gate_id
  → Certification.certification_id
```

Chaque événement porte seulement les références applicables. Les identités
existantes et leurs fingerprints/commits sont réutilisés. Un éventuel trace ID
est un regroupement technique, jamais une nouvelle identité de projet,
mission, story, exécution ou décision. Une absence de lien reste explicite ;
elle n'est pas comblée par inférence.

## Persistance, rétention et secrets

Les OperationalEvents nécessaires à la reconstruction d'incidents, les
évaluations de policy et les transitions de health significatives devront être
persistés. Les gauges instantanées, agrégats recalculables et diagnostics
interactifs peuvent rester éphémères tant que leur perte ne masque pas une
décision ou un incident.

La future persistance devra être append-oriented, atomique au niveau défini,
bornée en taille, soumise à rotation/rétention explicites et résistante aux
redémarrages. Une rotation ne doit pas supprimer silencieusement un record sous
rétention. Une corruption ou écriture partielle est signalée ; aucun document
vide de remplacement n'est fabriqué.

Les secrets, credentials, tokens, variables d'environnement, prompts complets,
stdout/stderr bruts et contenus utilisateur sensibles sont exclus par défaut.
La collecte applique minimisation, allowlist, redaction avant persistance et
taille maximale. Une redaction n'invente pas une valeur de substitution ayant
un sens métier.

La perte d'observabilité ne fabrique jamais d'état métier. Une perte critique
peut conduire à `DEGRADED`, `BLOCKED` ou `UNKNOWN` selon une future policy,
mais jamais à une Certification ou à une progression implicite.

## Frontière opérateur

Phase 6 suit `CLI first / structured data first`. Les futures surfaces peuvent
exposer status/health, diagnostics, incidents et résumés métriques en sortie
humaine et machine-readable. VS Code peut présenter ces informations et
transmettre une requête opérateur explicite ; il ne devient pas une nouvelle
autorité.

P6.1 ne sélectionne aucun dashboard web et n'implémente aucune surface. Les
contrats de données stables précèdent toute visualisation.

## Hors scope P6.1

P6.1 ne crée aucun OperationalEvent exécutable, store, instrumentation,
métrique, health engine, policy evaluator, budget, incident manager, CLI,
daemon, scheduler, dashboard ou mécanisme de maintenance. Ces responsabilités
restent ordonnées dans la roadmap Phase 6.
