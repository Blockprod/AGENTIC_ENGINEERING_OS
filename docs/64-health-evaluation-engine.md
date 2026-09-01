# Health Evaluation Engine

## Frontière

`HealthEvaluationEngine` décrit la capacité opérationnelle observée d'un
projet ou d'une mission. Il ne mute aucun store et n'autorise aucune action.

```text
HEALTH != AUTHORITY
HEALTHY != CERTIFIED
```

Ainsi, `BLOCKED` health ne change pas une User Story en `BLOCKED`, `DEGRADED`
ne produit pas un Gate `FAIL` et `UNKNOWN` ne produit aucune Certification.
P6.5 n'ajoute ni policy configurable, budget, incident, réparation, action
automatique ou CLI.

## API et contexte factuel

`HealthEvaluationEngine.evaluate(context) -> HealthSnapshot` accepte
uniquement un `HealthEvaluationContext` immuable. Le contexte lie explicitement
le projet, le HEAD Git courant, l'éventuelle mission/génération, l'instant UTC
d'évaluation, les observations read-only et un éventuel `MetricsSnapshot`.

Les sources fermées sont : ProjectStateStore, MissionStateStore,
OperationalEventStore, réconciliation Git/worktrees, runtime Codex, execution
ledger, diagnostic de persistance, store de remédiation, configuration projet
et snapshot métrique. Elles exposent des conditions techniques fermées ; elles
ne fournissent jamais directement un verdict Health à accepter.

Le contexte est construit après inspection des sources propriétaires. Le
moteur observe `ProjectState`/`MissionState`, mais ne les reconstruit jamais à
partir des événements. Une observation autoritative inaccessible ou invalide
prévaut sur une projection métrique apparemment favorable.

## États et dimensions

Les états sont fermés :

- `HEALTHY` : toutes les conditions obligatoires observables sont satisfaites ;
- `DEGRADED` : fonctionnement encore possible avec une anomalie réelle et
  bornée ;
- `BLOCKED` : une condition obligatoire empêche l'action normale sûre ;
- `UNKNOWN` : une information obligatoire est absente, stale, contradictoire
  ou non fiable.

Les huit dimensions fermées sont :

- `AUTHORITATIVE_STATE_ACCESS` ;
- `OBSERVABILITY` ;
- `GIT_WORKTREES` ;
- `CODEX_RUNTIME` ;
- `EXECUTION_RECOVERY` ;
- `PERSISTENCE` ;
- `REMEDIATION_TRANSACTION` ;
- `DEPLOYMENT_CONFIGURATION`.

Chaque résultat de dimension contient son état, son applicabilité, son scope,
sa fraîcheur, des raisons fermées et les références exactes des observations
utilisées.

## Applicabilité et agrégation

Accès autoritatif, observabilité, persistance et configuration sont toujours
`REQUIRED`. Pour une mission active, runtime Codex, execution ledger et
remédiation deviennent également `REQUIRED`. Le runtime Codex est `OPTIONAL`
pour un repository idle. Git/worktrees est `REQUIRED` uniquement pendant une
exécution parallèle. Recovery/remédiation et Git/worktrees sont explicitement
`NOT_APPLICABLE` hors de leurs contextes ; ils n'obtiennent alors aucun faux
état `HEALTHY`.

L'agrégation des dimensions obligatoires est déterministe :

```text
BLOCKED > UNKNOWN > DEGRADED > HEALTHY
```

Il n'existe ni vote majoritaire, ni moyenne, ni score. Une dimension optionnelle
connue comme bloquée ou dégradée dégrade le résultat global ; son inconnue
n'efface pas la santé des dimensions critiques. Toute dimension obligatoire
inconnue interdit `HEALTHY`.

## Fraîcheur

La fenêtre V1 est fixe et non configurable : cinq minutes, avec temps UTC
explicite fourni au contexte. Une observation future ou plus ancienne devient
`UNKNOWN`. Chaque observation est liée au HEAD courant ; un ancien HEAD est
stale. Les sources mission-scoped doivent correspondre exactement à la mission
et à la génération courantes.

Les métriques doivent être liées au même HEAD et à un scope exact : projet
seul pour un repository idle, mission et génération exactes pour une mission
active. Cette règle empêche de réutiliser silencieusement un ancien `HEALTHY`.

## Métriques et pertes d'observabilité

Un snapshot métrique manquant, `INCOMPLETE`, `UNAVAILABLE`, stale ou mal scopé
rend `OBSERVABILITY` inconnue. Un OperationalEventStore corrompu, inaccessible
ou saturé reste également `UNKNOWN`, même face à une projection métrique qui
semble complète.

Pour une mission/génération active exactement liée, tout échec de persistance
observé dégrade `PERSISTENCE`, et tout échec, interruption ou timeout Codex
observé dégrade `CODEX_RUNTIME`. Ces règles fixes décrivent des faits de la
génération courante ; elles ne constituent ni des seuils configurables ni des
policies P6.6. Une source propriétaire `FAILED`, inaccessible ou avec recovery
pending ne peut jamais être améliorée par une métrique.

## Snapshot et explicabilité

`HealthSnapshot` est immuable, recalculable et contient : version, scope,
instant d'évaluation, état global, catalogue complet des dimensions, raison
d'agrégation, identités de sources, diagnostics et fingerprint SHA-256
déterministe. Le modèle rejette un `HEALTHY` forgé qui contredirait ses
dimensions. Aucune API de Gate, Evidence, Certification, approbation, mutation
ou autorisation n'est exposée.
