# Operator Diagnostics & CLI

## Frontière

P6.9 étend l'unique CLI `agentic-os`, également disponible par
`python -m agentic_engineering_os`, avec quatre commandes strictement read-only :

```text
health
metrics
incidents
diagnose
```

`CLI DIAGNOSTIC != AUTHORITY`. Ces commandes ne mutent aucun store, ne créent
ni Evidence, Gate, Certification ou approbation Human, ne déclenchent aucun
merge, acknowledgement, résolution, remediation ou maintenance.

## Sources et moteurs

La CLI valide une racine Git canonique sans symlink, charge la
`ProjectConfiguration` repository-locale, observe le HEAD courant et lit les
stores existants. `health` construit un `HealthEvaluationContext` factuel puis
appelle `HealthEvaluationEngine`. `metrics` lit `OperationalEventStore` puis
appelle `MetricsEngine`. `incidents` reconstruit les dernières révisions via
`IncidentEventJournal` et appelle `IncidentManager` sans append. `diagnose`
agrège ces résultats avec les évaluations read-only de
`GovernancePolicyEvaluator` et `ResourceBudgetEvaluator`.

Les scopes métriques sont fermés : projet, mission, génération, User Story,
rôle et exécution. Mission et génération sont indissociables ; une exécution
exige un rôle. Un `project_id`, une mission, une génération ou un HEAD
étranger est refusé ou rendu explicitement non fiable. Les sources stale,
corrompues, indisponibles ou incomplètes ne sont jamais promues en succès.

## Présentation

`--json` produit un JSON compact avec clés triées ; sans cette option, la même
structure est indentée. Les datetimes utilisent UTC ISO 8601. Les modèles
fermés conservent scopes, fraîcheur, raisons, diagnostics, décisions, limites,
usages et corrélations. La sortie est bornée à 1 000 000 octets et à 256
incidents. Aucun environnement, stdout/stderr brut, secret ou query language
arbitraire n'est exposé.

Une décision governance `ALLOW` est présentée comme
`NO_ADDITIONAL_GOVERNANCE_BLOCK`, jamais comme une autorisation. Le budget
Codex affiché est un probe diagnostique d'une exécution supplémentaire fondé
sur la limite projet et l'execution ledger ; il ne réserve rien et n'autorise
rien. Aucun score synthétique n'est calculé.

## Codes de sortie

- `0` : commande évaluée et état acceptable (`HEALTHY`, métriques `COMPLETE`,
  aucun incident actif et aucune contrainte diagnostique) ;
- `2` : état opérationnel dégradé, bloqué, inconnu, incomplet ou nécessitant
  l'attention de l'opérateur ;
- `1` : erreur technique ou produit inattendue.

Ces codes décrivent uniquement l'exécution diagnostique. Ils ne constituent
jamais un `GateResult` ou un `CertificationResult`.
