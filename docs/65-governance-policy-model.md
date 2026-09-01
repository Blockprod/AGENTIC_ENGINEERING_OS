# Governance Policy Model

## Frontière

`GovernancePolicyEvaluator` évalue des contraintes opérationnelles sans les
appliquer et sans muter aucun état.

```text
POLICY DECISION != BUSINESS AUTHORITY
ALLOW != CONTROL PLANE AUTHORIZATION
```

`ALLOW` signifie uniquement qu'aucune contrainte supplémentaire n'est émise
par la gouvernance. `BLOCK` demande à une future intégration gouvernée de
refuser l'opération ; P6.6 n'implémente pas ce hook. Aucune décision ne produit
Evidence, Gate, Certification, approbation Human, merge ou transition d'état.

## API et modèle

`GovernancePolicyEvaluator.evaluate(context) -> GovernanceDecisionSet` reçoit
un contexte immuable contenant le scope, l'opération courante, l'instant UTC,
un `HealthSnapshot`, un éventuel `MetricsSnapshot` lié, des faits opérationnels
fermés et au plus 32 `GovernancePolicy`.

Une policy contient uniquement : identité, version, classe, domaine, activation,
scope, condition fermée, action fermée et rationale fermée. Aucun texte libre,
code dynamique, expression, script, shell, commande ou payload arbitraire
n'est accepté.

P6.6 retient un modèle Python strict en mémoire et n'ajoute pas de format JSON,
loader ou schéma de configuration. Cela évite d'établir prématurément une
surface persistante ou une UX d'édition avant son besoin démontré.

## Taxonomie et domaines

Les classes sont :

- `HARD_SAFETY_POLICY` : activée obligatoirement, action limitée à `BLOCK` ou
  `REQUIRE_OPERATOR` ;
- `OPERATIONAL_POLICY` : peut ajouter une contrainte opérationnelle ;
- `OPERATOR_PREFERENCE` : limitée à `ALLOW` ou `ALLOW_WITH_WARNING` et ne peut
  jamais réduire une contrainte supérieure.

Les domaines fermés sont `EXECUTION_ADMISSION`, `SANDBOX_SAFETY`,
`RECOVERY_REQUIRED`, `MAINTENANCE_MODE`, `OBSERVABILITY_REQUIRED`,
`HEALTH_GATING`, `VERIFICATION_TIER` et `OPERATOR_INTERVENTION`. Les conditions
acceptées sont validées par domaine. Aucun budget ou seuil numérique P6.7
n'est présent.

## Décisions et précédence

Le catalogue de décisions est :

```text
BLOCK > REQUIRE_OPERATOR > ALLOW_WITH_WARNING > ALLOW
```

Le résultat final est le maximum explicite entre le plancher d'entrée et les
policies correspondantes. Deux policies correspondantes de même classe et du
même domaine qui produisent des décisions différentes rendent le policy set
invalide et imposent `BLOCK`. Il n'existe ni last-write-wins, ni score, ni vote.

Au moins une hard safety policy est obligatoire. Elle ne peut être désactivée,
et une policy opérationnelle ou une préférence ne peut jamais réduire son
résultat.

## Health et sources read-only

Le plancher invariant dérivé de Health est :

- `BLOCKED` → `BLOCK` ;
- `UNKNOWN` → `REQUIRE_OPERATOR` ;
- `DEGRADED` → `ALLOW_WITH_WARNING` ;
- `HEALTHY` → `ALLOW` au sens non autoritatif défini ci-dessus.

Un Health stale, futur ou lié à un autre projet, HEAD, mission ou génération
impose `BLOCK`. Tout MetricsSnapshot explicitement fourni est également validé
et doit être complet, frais et exactement scopé ; une projection étrangère ou
incomplète ne peut pas être ignorée. Une policy qui requiert une métrique ou un
fait sandbox/vérification absent impose un blocage fail-closed.

Les conditions peuvent seulement lire Health, Metrics et les faits fermés de
la requête courante. Elles ne reconstruisent jamais ProjectState ou MissionState
depuis des événements et ne transforment jamais `UNKNOWN` en sûr par défaut.

## Scope, déterminisme et résultat

Les scopes réutilisent projet, HEAD Git, mission, génération, rôle, exécution,
worktree et type d'opération fermés. Une policy d'un autre projet ou d'une
ancienne génération est refusée ; une operation non listée est explicitement
hors scope.

Les policies sont évaluées dans un ordre canonique indépendant de l'ordre
d'entrée. `GovernanceDecisionSet` conserve le plancher, la décision agrégée,
chaque résultat de policy, les raisons, les identités de sources et un
fingerprint SHA-256 déterministe. Le modèle rejette un `ALLOW` forgé qui
contredit son plancher ou ses résultats.

## Hors scope P6.6

P6.6 n'ajoute aucun budget de ressources/concurrence, incident, escalade,
enforcement workflow, persistance de policy, édition CLI ou exécution de
maintenance.
