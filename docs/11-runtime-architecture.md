# Architecture runtime Phase 1

## Objectif et contraintes

Le runtime V1 transforme les contrats certifiés de Phase 0 en un noyau Python
déterministe et testable. Il cible exclusivement `VS Code + Codex + Git +
Python`. Ses décisions de validation, transition, Gate et certification ne
dépendent d'aucun appel LLM : Codex peut piloter les outils, mais ne fait pas
partie du calcul des règles.

La V1 reste locale au repository. Elle n'introduit ni framework agentique,
serveur, API web, dashboard, broker, infrastructure distribuée, base
vectorielle, abstraction multi-LLM ou système de plugins.

## Structure logique retenue

La séparation suivante est justifiée par la nécessité d'isoler les règles
certifiantes des entrées/sorties :

```text
agentic_engineering_os/
├── domain/
├── application/
└── infrastructure/
```

Cette structure est une cible pour les missions suivantes ; P1.1 ne crée aucun
de ces packages.

### Domain

Contient les objets métier, enums, value objects et règles pures. Il valide les
invariants qui ne nécessitent aucune entrée/sortie : valeurs canoniques,
cohérence interne, transitions, sémantique des Gates et conditions de
certification. Il ne lit ni fichier, ni Git, ni horloge et ne connaît ni Codex
ni `jsonschema`.

### Application

Expose les services de cas d'usage. Il orchestre les règles du domaine, rend
les échecs explicites et utilise une frontière de persistance minimale. Il ne
décide pas à partir d'un texte libre ou d'un jugement LLM et ne dépend d'aucun
format de stockage concret.

### Infrastructure

Contient les adaptateurs aux détails externes : chargement des JSON Schemas,
parsing et sérialisation JSON, accès atomique aux fichiers du repository et,
si nécessaire, observation explicite de Git. Il ne redéfinit aucune règle du
domaine et ne transforme jamais une erreur technique en succès.

### Direction des dépendances

```text
infrastructure ──→ application ──→ domain
       └─────────────────────────→ domain
```

- `domain` dépend uniquement de la bibliothèque standard Python.
- `application` peut dépendre de `domain`.
- `infrastructure` peut dépendre de `application` et de `domain` pour fournir
  leurs adaptateurs.
- Toute composition concrète reste à la périphérie du package.
- Les imports inverses sont interdits : le domaine ne connaît ni stockage, ni
  JSON Schema, ni Git.

Une interface n'est ajoutée que lorsqu'une frontière testable en a besoin. La
V1 prévoit une seule abstraction de persistance, portée par l'application et
réalisée par l'infrastructure ; elle ne crée pas une interface par classe.

## Composants et frontières

| Composant | Responsabilité | Frontière |
| --- | --- | --- |
| Domain models | Représenter les contrats Phase 0 avec des valeurs explicites. | Aucune entrée/sortie ni décision fondée sur Codex. |
| Contract validation | Combiner validation structurelle des schémas et règles sémantiques applicables. | Ne modifie pas les données pour les rendre valides. |
| State transition validation | Autoriser uniquement une transition canonique dont les préconditions sont prouvées. | Ne persiste pas et ne force aucune transition. |
| Evidence recording | Vérifier la forme, la provenance et le contexte avant enregistrement. | Ne produit ni Gate ni certification et ne fabrique aucune preuve. |
| Gate evaluation | Évaluer une condition à partir d'Evidence résolues et applicables. | Ne collecte ni ne réécrit les Evidence. |
| Certification | Agréger critères, Gates, approbations, Evidence et commit pour un verdict. | Ne remédie pas, ne modifie pas le contrat et ne certifie pas une inconnue. |
| Persistent project state | Charger et enregistrer l'état autoritatif versionné dans le repository. | Ne contient aucune règle de transition, Gate ou certification. |

## Futurs objets du domaine

Les objets suivants seront définis sans comportement d'infrastructure :

- `UserStory` : contrat canonique, état courant, dépendances, scope, critères,
  Gates requis, approbation humaine et métadonnées.
- `AcceptanceCriterion` : identifiant, description observable et caractère
  obligatoire.
- `Evidence` : observation, provenance, commande ou artefact éventuel, contexte
  Git, horodatage et producteur.
- `Gate` : condition, caractère requis, résultat et références d'Evidence.
- `Certification` : verdict lié à un commit, résultats d'acceptation et de
  Gates, approbations et Evidence.
- `AuditEvent` : événement attribuable, horodaté, lié au repository et
  append-oriented.
- `ProjectState` : agrégat versionné des contrats, Evidence, Gates,
  certifications et événements persistés pour un repository.

Les enums imposés directement par Phase 0 sont :

- `UserStoryStatus` : les 13 états de `PROPOSED` à `CANCELLED` ;
- `RiskLevel` : `LOW`, `MEDIUM`, `HIGH`, `CRITICAL` ;
- `VerificationResult` : `PASS`, `FAIL`, `UNKNOWN`, `NOT_APPLICABLE` ;
- `CertificationResult` : `CERTIFIED`, `REJECTED`, `BLOCKED` ;
- `EvidenceType` : les 7 types définis par le contrat des preuves ;
- `AuditEventType` : les 12 types définis par le modèle d'audit.

Les value objects minimaux candidats sont les identifiants stables, un SHA de
commit validé, un horodatage ISO 8601 avec fuseau et le scope de chemins. Ils ne
seront introduits que s'ils concentrent réellement une validation partagée ;
aucun wrapper décoratif n'est requis.

## Futurs services

### ContractValidator

- **Input** : type de contrat, données parsées et contexte projet requis pour
  les règles sémantiques.
- **Output** : objet de domaine validé, ou `ValidationError` contenant une liste
  structurée d'erreurs ; jamais un objet partiellement valide présenté comme
  réussi.
- **Responsabilité** : appliquer le JSON Schema puis les invariants sémantiques
  applicables, notamment références, unicité et cohérence.
- **Fail-closed** : parsing impossible, schéma inconnu, champ invalide ou règle
  non vérifiable produisent une erreur explicite.
- **Ne doit pas** : corriger les données, changer un état, persister ou assouplir
  un contrat.

### StateTransitionService

- **Input** : User Story courante, état cible, dépendances résolues et preuves
  des préconditions.
- **Output** : décision autorisée avec nouvel état proposé, ou refus structuré
  laissant l'état courant inchangé.
- **Responsabilité** : appliquer exclusivement la table des 28 transitions et
  leurs préconditions Phase 0.
- **Fail-closed** : état inconnu, transition absente, dépendance non certifiée
  ou précondition non prouvée bloquent.
- **Ne doit pas** : persister, exécuter du travail, inventer une preuve ou
  contourner la remédiation.

### EvidenceRecorder

- **Input** : Evidence candidate et contexte exact du repository auquel elle
  s'applique.
- **Output** : Evidence validée et référence persistée, accompagnées de
  l'événement d'audit applicable.
- **Responsabilité** : préserver l'observation, la provenance, l'attribution et
  le lien au commit ou au working tree pertinent.
- **Fail-closed** : provenance, contexte obligatoire ou persistance manquants
  empêchent l'enregistrement réussi.
- **Ne doit pas** : exécuter la commande a posteriori, fabriquer une Evidence,
  décider un Gate ou déclarer la certification.

### GateEvaluator

- **Input** : Gate, contrat applicable, Evidence référencées et contexte
  d'évaluation.
- **Output** : Gate évalué avec `PASS`, `FAIL`, `UNKNOWN` ou
  `NOT_APPLICABLE`, et références conservées.
- **Responsabilité** : appliquer de façon pure la politique du Gate requis ou
  optionnel et vérifier suffisance, applicabilité et staleness des Evidence.
- **Fail-closed** : référence absente, ambiguë, invalide ou stale produit
  `UNKNOWN` ; un `NOT_APPLICABLE` non autorisé reste bloquant.
- **Ne doit pas** : collecter ou modifier les Evidence, rendre un Gate optionnel
  après coup ou certifier.

### CertificationService

- **Input** : User Story, commit ciblé, résultats des critères et Gates,
  Evidence résolues et approbations humaines applicables.
- **Output** : `Certification` avec verdict `CERTIFIED`, `REJECTED` ou
  `BLOCKED`, et justification traçable.
- **Responsabilité** : appliquer les conditions de certification Phase 0 et
  lier la décision à un commit précis.
- **Fail-closed** : un échec prouvé donne `REJECTED` ; une donnée absente,
  ambiguë, stale ou inconnue donne `BLOCKED`, jamais `CERTIFIED`.
- **Ne doit pas** : modifier le code, le contrat, les critères, les Gates ou les
  Evidence, ni fabriquer une approbation humaine.

### ProjectStateStore

- **Input** : chemin racine autorisé et `ProjectState` validé pour une écriture,
  ou demande de chargement.
- **Output** : état chargé ou confirmation explicite d'une écriture atomique.
- **Responsabilité** : fournir la frontière de persistance, contrôler la
  version du format et préserver l'historique append-oriented.
- **Fail-closed** : fichier absent lorsqu'il est requis, JSON invalide, version
  inconnue, écriture partielle ou erreur disque sont des échecs explicites.
- **Ne doit pas** : décider une transition, évaluer un Gate, certifier, réparer
  silencieusement ou dépendre de Codex.

## Stratégie d'erreurs

Les refus métier attendus sont des résultats structurés : transition refusée,
Gate `FAIL` ou `UNKNOWN`, certification `REJECTED` ou `BLOCKED`. Ils ne sont ni
des exceptions génériques ni des succès.

Les échecs techniques utilisent seulement trois catégories explicites :

- `ParseError` pour une entrée illisible ou un JSON mal formé ;
- `ValidationError` pour un contrat, état ou invariant invalide ;
- `PersistenceError` pour un chargement, une écriture ou une intégrité de
  stockage impossible.

Chaque erreur conserve son type, son sujet et des détails structurés. Aucune
exception n'est absorbée pour produire une valeur par défaut. Une Evidence
manquante devient explicitement `UNKNOWN`, un état inconnu invalide la demande,
et une panne de stockage empêche de déclarer l'opération réussie.

## Persistance V1

Le format recommandé est un document JSON versionné situé dans le repository :

```text
.agentic-engineering-os/
└── state.json
```

`state.json` porte un `schema_version` et sérialise le `ProjectState`. JSON est
retenu parce que les contrats Phase 0 disposent déjà de JSON Schemas, que son
parsing est non ambigu et que sa sérialisation canonique est testable. YAML et
SQLite n'apportent pas de valeur démontrée à ce stade.

L'écriture devra être atomique, la lecture strictement validée et l'ordre des
événements d'audit préservé sans suppression ou réécriture silencieuse. Le
fichier est versionné par Git : le repository reste la mémoire persistante et
la source autoritative. Le chemin et le format ne sont pas créés pendant P1.1 ;
leur contrat précis relève de P1.8.

## Déterminisme

Les comportements suivants doivent être des fonctions ou opérations testables
sans VS Code, Codex, réseau ou LLM :

- validation structurelle et sémantique des contrats ;
- autorisation ou refus des transitions et vérification des dépendances ;
- sémantique `PASS / FAIL / UNKNOWN / NOT_APPLICABLE` des Gates ;
- règles `CERTIFIED / REJECTED / BLOCKED` de certification ;
- parsing, sérialisation, écriture atomique et intégrité du `ProjectState`.

Les mêmes entrées, politiques et états persistés doivent produire le même
résultat. Horloge, identifiants, état Git et système de fichiers sont fournis
explicitement aux frontières lorsqu'ils influencent le résultat.

## Stratégie minimale de tests Phase 1

- **Unit** : objets, enums, invariants, table de transitions et décisions pures
  des Gates et certifications.
- **Contract/schema consistency** : parité entre modèles Python, valeurs
  canoniques et cinq JSON Schemas, avec fixtures valides et invalides.
- **Negative/fail-closed** : parsing invalide, état ou transition inconnus,
  dépendance non certifiée, Evidence absente ou stale, approbation manquante et
  panne de stockage.
- **Intégration minimale** : charger un état JSON temporaire, valider les
  contrats, enregistrer une Evidence, évaluer un Gate, produire une décision et
  relire exactement l'état persisté.

P1.1 n'ajoute aucun test : son seul artefact exécutable reste la suite existante
qui protège les schémas certifiés et l'import du package.

## Séquence Phase 1 proposée

1. P1.2 Domain Models
2. P1.3 Contract Validator
3. P1.4 State Transition Engine
4. P1.5 Evidence Recorder
5. P1.6 Gate Evaluator
6. P1.7 Certification Engine
7. P1.8 Persistent Project State
8. P1.9 Integrated Control Loop
9. P1.10 Final Certification

La séquence est compatible avec les dépendances architecturales : les objets et
validations précèdent les décisions, puis la persistance et l'intégration. Aucun
ajustement de Phase 0 ni aucune implémentation anticipée n'est requis.
