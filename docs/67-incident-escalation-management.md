# Incident & Escalation Management

## Frontière

`IncidentManager.evaluate(context) -> IncidentAssessment` détecte, classe et
corrèle des conditions opérationnelles à partir de sources read-only.

```text
INCIDENT != AUTHORITY
ESCALATION != REMEDIATION
```

Un incident ne mute aucun `ProjectState`, `MissionState`, User Story ou
`workflow_generation`. Il ne crée ni Evidence, Gate, Certification ou Human
Approval, ne déclenche aucun merge, scheduler, maintenance, recovery, cleanup
ou suppression de fichier/worktree.

## Modèle fermé

Les états sont `OPEN`, `ACKNOWLEDGEMENT_REQUIRED`, `ESCALATED` et `RESOLVED`.
La résolution n'est jamais déduite de la seule disparition d'un signal. Un
incident non résolu absent de l'évaluation courante reste inchangé.

Les classifications sont :

- `PERSISTENCE_FAILURE` ;
- `RECOVERY_STUCK` ;
- `REMEDIATION_LOOP` ;
- `GIT_WORKTREE_DIVERGENCE` ;
- `CODEX_RUNTIME_FAILURE` ;
- `RESOURCE_EXHAUSTION` ;
- `OBSERVABILITY_LOSS` ;
- `POLICY_BLOCK` ;
- `UNKNOWN_CRITICAL_STATE`.

La sévérité est déterminée par la classification et non acceptée comme une
affirmation libre. Elle reste strictement opérationnelle et ne vaut jamais
résultat de Health, Gate ou Certification.

## Sources et fraîcheur

Le contexte immuable lie projet, HEAD Git, mission/génération et instant UTC.
Il peut lire `HealthSnapshot`, `GovernanceDecisionSet`,
`ResourceBudgetDecisionSet`, `MetricsSnapshot`, `OperationalEvent` et des
`IncidentDiagnostic` factuels. Health, Governance et Budgets sont critiques :
leur absence, ancienneté, scope étranger ou incohérence produit un
`UNKNOWN_CRITICAL_STATE` fail-closed.

La fenêtre de fraîcheur V1 est cinq minutes. Les corpus d'événements et de
diagnostics portent une complétude explicite. Une source incomplète n'est pas
assimilée à une absence de problème, un événement stale n'est pas courant et
les logs ne reconstruisent jamais `ProjectState`.

## Identité, corrélation et anti-flood

La corrélation réutilise les identités applicables : projet, mission,
génération, User Story, rôle, exécution, worktree assignment, opération et
domaine de budget. L'identité logique est le SHA-256 tronqué du tuple canonique
`project + classification + correlation`, préfixé par `inc-`. Elle ne devient
pas une identité métier parallèle.

La déduplication est exacte : même projet, même classification et même
corrélation produisent le même `incident_id`. Une condition répétée et encore
non résolue réutilise le dernier record sans nouvel append. Aucun throttling
temporel ou rapprochement heuristique n'est appliqué. Les signaux distincts ne
sont pas supprimés pour réduire le volume.

## Escalade et Human Authority

Les recommandations fermées sont `OBSERVE_ONLY`, `OPERATOR_ACK_REQUIRED`,
`OPERATOR_ACTION_REQUIRED` et `EMERGENCY_BLOCK_RECOMMENDED`. Une recommandation
d'urgence ne bloque pas elle-même un workflow.

`acknowledge(record, acknowledgement)` exige l'identité exacte de l'incident,
le scope exact, un timestamp non antérieur et une identité Human attribuable
selon le contrôle canonique. Toutes les variantes réservées Codex sont refusées.
L'acknowledgement d'incident est un fait opérateur non autoritatif ; il ne vaut
pas HumanApproval métier.

## Résolution et reopen

`resolve(record, context, resolution)` exige simultanément :

- l'identité et le scope exacts de l'incident courant ;
- un instant identique à l'évaluation courante ;
- l'absence de la condition active dans toutes les sources courantes ;
- exactement un diagnostic `NORMALIZED` correspondant à la même
  classification/corrélation et à la source référencée ;
- une identité opérateur Human attribuable.

Une résolution ajoute une révision chaînée au fingerprint précédent. Si le
même problème réapparaît, `evaluate` conserve le même `incident_id`, ajoute une
révision, incrémente `occurrence_count`/`reopen_count` et restaure l'escalade
initiale. Aucun incident n'est fermé automatiquement.

## Persistance

P6.8 ne crée pas de second EventStore. `IncidentEventJournal` encode chaque
snapshot strict sous forme d'un `OperationalEvent`
`OPERATIONAL_ANOMALY/DETECTED` contenant le JSON canonique borné de
`IncidentRecord`, puis utilise explicitement l'`OperationalEventStore` P6.3.

L'identité d'événement est déterministe à partir du fingerprint de la révision :
réappender le même snapshot est donc refusé comme doublon. La lecture valide
l'enveloppe, le record, les fingerprints, la séquence de révisions et la chaîne
`previous_fingerprint`. Corruption, trou ou contradiction bloquent ; aucune
réparation silencieuse n'est effectuée. L'append reste une action explicite et
séparée de `IncidentManager.evaluate`.
