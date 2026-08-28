# Orchestrator V1

## Rôle et frontières

`Orchestrator` coordonne une étape de mission à partir d'une racine de
repository, du commit courant, de `MissionState` et d'un accès contrôlé à
`ProjectState`. Ces entrées sont explicites ; V1 ne découvre pas Git et
n'utilise ni LLM, ni réseau, ni conversation.

L'Orchestrator charge les deux états, vérifie leur cohérence minimale, choisit
le rôle responsable de l'étape et persiste une copie candidate de
`MissionState`. Il ne développe pas de code, n'exécute aucun rôle, ne crée ni
Evidence, ni Gate, ni Certification, ne modifie pas `ProjectState` et
n'autorise aucune transition ou approbation Human. Toute opération de contrôle
reste réservée à `ControlLoop` et aux services déterministes Phase 1.

## Routing V1

La politique est une table fermée et auditable :

| Operating step | Rôle destinataire |
| --- | --- |
| `RECONSTRUCT`, `PREFLIGHT` | `ORCHESTRATOR` |
| `UNDERSTAND_CONTRACT` | `ARCHITECT` |
| `PROVE_READINESS`, `ACT` | `IMPLEMENTER` |
| `VERIFY`, `RECORD_EVIDENCE` | `TESTER` |
| `REPORT` | `REVIEWER` |
| `CONTROLLED_TRANSITION` | `CERTIFIER` |

Un appel route l'étape requise ; il ne prétend pas qu'elle est terminée et ne
l'incrémente pas silencieusement. Une étape absente ou associée à plusieurs
rôles bloque la décision. P2.3 produit uniquement le handoff ; les rôles seront
implémentés séparément.

## Handoff et résultat

`RoleHandoff` contient `from_role`, `to_role`, `mission_id`,
`workflow_generation`, `subject`, `objective`, `observed_commit`,
`operating_step`, `blockers` et des
`instructions` factuelles. Les instructions combinent la règle fixe de l'étape,
le sujet de mission et, lorsqu'elle existe, l'observation correspondante dans
`ProjectState`. Le handoff transmet du contexte et n'accorde jamais d'autorité
Control Plane.
La génération est toujours copiée depuis le `MissionState` chargé ;
l'Orchestrator ne la choisit et ne l'incrémente pas.

`OrchestrationResult` expose le succès, le rôle courant, le rôle suivant, le
handoff éventuel, les blockers, une raison explicite et le `MissionState`
effectivement conservé ou persisté.

## Reprise, blockers et Human Authority

Si le commit courant diffère de `MissionState.observed_commit`, le seul routing
autorisé force `ORCHESTRATOR` et `RECONSTRUCT`. Le commit observé, le rôle,
l'étape, la prochaine action et l'horodatage sont mis à jour dans une copie ;
les blockers sont préservés dans une collection indépendante.

Une mission `BLOCKED` ne produit aucun handoff. Le marker explicite
`HUMAN_REQUIRED` ou `HUMAN_REQUIRED: <raison>` dans les blockers retourne
`HUMAN_REQUIRED`. La même décision est produite lorsqu'une User Story ciblée
exige une approbation Human non accordée dans `ProjectState`. L'Orchestrator ne
crée pas de rôle Human et ne décide jamais à sa place.

## Persistance et fail-closed

La mise à jour passe exclusivement par `MissionStateStore.save`. L'état chargé
n'est jamais muté : une copie candidate est créée avant la sauvegarde. Un échec
de routing ou de persistance ne laisse donc aucune mutation partielle et ne
modifie jamais `ProjectState`.

Mission absente ou invalide, `ProjectState` indisponible, commit ou horodatage
invalide, rôle/statut/étape inconnu, blocker actif, décision Human requise,
sujet ou routing ambigu et échec de sauvegarde produisent un résultat d'échec
explicite sans rôle arbitraire ni fallback.
