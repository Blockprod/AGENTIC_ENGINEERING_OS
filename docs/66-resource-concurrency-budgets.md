# Resource & Concurrency Budgets

## Frontière

`ResourceBudgetEvaluator.evaluate(context) -> ResourceBudgetDecisionSet`
évalue une admissibilité opérationnelle de manière pure, déterministe et
read-only. Il ne réserve aucune ressource et n'applique pas sa décision.

```text
RESOURCE BUDGET DECISION != BUSINESS AUTHORITY
WITHIN_BUDGET != CONTROL PLANE AUTHORIZATION
```

Une décision ne peut produire ni Evidence, ni Gate, ni Certification, ni
approbation Human, ni merge, ni mutation de User Story, `MissionState` ou
`workflow_generation`. P6.7 n'ajoute aucun scheduler, queue, daemon, cleanup,
throttling automatique ou hook dans les workflows P2–P4.

## API et catalogue fermé

Le contexte immuable lie le `project_id`, le HEAD Git, la racine du repository,
la mission/génération éventuelle, l'opération et l'instant UTC d'évaluation. Il
contient au plus 32 budgets et une observation courante par domaine parmi :

- `CODEX_CONCURRENCY` en `EXECUTIONS` ;
- `WORKTREE_CONCURRENCY` en `WORKTREES` ;
- `EXECUTION_TIME` en `SECONDS` ;
- `REMEDIATION_GENERATIONS` en `GENERATIONS` ;
- `RUNTIME_STORAGE` en `BYTES` ;
- `OBSERVABILITY_STORAGE` en `BYTES`.

Chaque `ResourceBudget` possède une identité/version, un domaine, un scope, une
limite entière, une unité imposée par le domaine, une classe de policy P6.6,
une source attribuable, une applicabilité explicite et une rationale fermée.
Les limites négatives, booléennes, supérieures à un entier signé 64 bits,
flottantes, NaN/Infinity, expressions et unités incohérentes sont refusées.
Aucun pourcentage configurable ou script n'est interprété.

## Current usage factuel

`ResourceUsageObservation` distingue `COMPLETE`, `UNKNOWN` et `UNAVAILABLE`.
Une observation complète porte une valeur courante factuelle ; les deux autres
n'ont pas le droit de revendiquer une valeur, notamment zéro. Les sources
fermées correspondent aux lecteurs existants :

- execution-state store pour les exécutions Codex actives ;
- `WorktreeRegistry` ou un `MetricsSnapshot` complet pour les worktrees actifs ;
- runtime Codex/execution-state pour timeout demandé ou durée écoulée observée ;
- `MissionState` pour la génération de remédiation courante ;
- filesystem repository-local pour les stockages runtime ;
- filesystem et état de rétention de l'`OperationalEventStore` pour le stockage
  d'observabilité.

Le producteur de contexte reste responsable de lire la source propriétaire et
de déclarer `UNKNOWN`/`UNAVAILABLE` lorsqu'elle est incomplète. L'évaluateur
refuse une source incompatible, étrangère, stale, future ou liée à un autre
HEAD/projet/mission/génération. La fenêtre de fraîcheur V1 est cinq minutes.

## Décisions et calcul

Le résultat par domaine contient la limite effective, la valeur courante, la
demande, la valeur future, les identités de budgets/sources et des raisons
fermées. Le catalogue est :

- `WITHIN_BUDGET` ;
- `NEAR_LIMIT` ;
- `LIMIT_REACHED` ;
- `LIMIT_EXCEEDED` ;
- `UNKNOWN`.

La valeur future est strictement `current + requested`, avec détection de
dépassement numérique. L'admission de concurrence n'est donc possible que si
`current + requested <= limit`. `LIMIT_REACHED` reste distinct d'un
dépassement. `NEAR_LIMIT` signifie que la valeur future est inférieure à la
limite mais atteint au moins quatre cinquièmes de cette limite effective ; la
base est ainsi explicite et non ambiguë. `UNKNOWN` n'est jamais converti en
`WITHIN_BUDGET`.

Pour Codex et les worktrees, une demande doit être strictement positive. Le
nombre et l'unicité des identités actives doivent correspondre à la valeur
courante, et chaque racine associée doit être le repository courant. Aucun
oversubscription optimiste, ensemble stale, doublon ou compte cross-repository
n'est admis.

Pour le temps, la demande est un plafond/timeout positif explicitement fourni
ou une extension factuelle ajoutée à un elapsed observé ; aucun temps restant
n'est extrapolé. Pour la remédiation, la demande est exactement `N + 1`. Le
calcul ne modifie jamais `workflow_generation`.

## Précédence des limites

La limite effective est le minimum déterministe de toutes les limites
applicables. Il n'existe aucun last-write-wins. Plusieurs limites hard safety
produisent donc le plafond le plus restrictif. Une policy opérationnelle ou
une préférence opérateur peut demander plus bas ; une préférence supérieure au
hard ceiling est explicitement ignorée et ne relève jamais ce plafond. Une
préférence sans plafond non-preference produit `UNKNOWN`.

Cette relation reste conceptuelle avec `GovernanceDecision` :
`LIMIT_REACHED`, `LIMIT_EXCEEDED` ou `UNKNOWN` pourront alimenter une future
policy, mais P6.7 ne traduit ni n'applique automatiquement ces résultats.

## Mesure de stockage

`BoundedStorageUsageObserver` mesure seulement les octets de fichiers réguliers
d'une zone explicitement fournie. Le chemin doit être absolu, sans traversal,
et contenu lexicalement puis réellement dans la racine du repository. Toute
symlink, junction/reparse point, indisponibilité, sortie de racine, erreur de
lecture, dépassement numérique ou dépassement de la borne de 10 000 entrées
produit une observation `UNKNOWN`.

La mesure ne suit aucun lien, ne supprime aucun fichier et ne tente aucune
libération d'espace. Le fingerprint du résultat explique et reproduit le
calcul ; il ne constitue ni une signature ni une autorisation.
