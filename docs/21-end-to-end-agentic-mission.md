# Mission agentique séquentielle V1

## Séquence

`SequentialMissionWorkflow` coordonne la chaîne fermée suivante :

```text
Human objective
  → Orchestrator
  → Architect
  → Implementer
  → Tester
  → Reviewer
  → Certifier
  → Control Plane
  → résultat terminal
```

Chaque étape exige le `RoleHandoff` produit par l'Orchestrator, le statut
persisté attendu et la validation déterministe du RoleResult. Le coordinateur
ne possède aucune table de transitions parallèle : toutes les évolutions de
User Story passent par `ControlLoop` et `StateTransitionService`.

## Frontières et bridges

Les RoleResults restent des rapports sans autorité. L'intégration d'une User
Story candidate passe par validation puis `ProjectStateStore`; les Evidence,
Gates et Certifications passent respectivement par `EvidenceRecorder`,
`GateEvaluator` et `CertificationService` via `ControlLoop`.

Le bridge d'Acceptance exige une Evidence explicite
`ACCEPTANCE_CRITERION_CHECK` dont le payload booléen correspond exactement au
résultat Tester : `PASS → true` et `FAIL → false`. Aucune chaîne ou coercition
n'est admise.

Une décision Human suit exclusivement : décision Human → Evidence persistée →
`HumanApprovalService` via `ControlLoop` → approbation appliquée persistée →
reprise. La présence seule d'une Evidence ne débloque pas la mission.

## Control Plane et remédiations

Le Certifier doit produire `READY_FOR_CONTROL_PLANE` avant soumission. Le
coordinateur reconstruit alors le dossier depuis la User Story, les Evidence,
les Gates, l'approbation appliquée et le commit. Seul `CertificationService`
produit le verdict. Une Certification `CERTIFIED` persistée autorise ensuite la
transition de confiance vers `UserStory.CERTIFIED`; la mission ne devient
`COMPLETED` qu'après relecture de cet état.

Un échec Tester ou Reviewer suit le cycle normatif `REJECTED →
REMEDIATION_REQUIRED → READY → IN_PROGRESS`, retourne à Implementer et impose
un nouveau passage Tester. `BLOCKED` n'avance jamais au rôle suivant. Un Gate
`NOT_APPLICABLE` requis exige la même autorisation explicite dans le dossier du
Certifier et dans le contexte de certification.

## Restart et limites V1

`ProjectState` et `MissionState` contiennent toutes les informations
autoritatives nécessaires au routing et au cycle de vie. Après redémarrage, de
nouvelles instances rechargent ces stores. Les RoleResults ne sont pas
persistés par le workflow V1 et ne constituent pas un historique autoritatif :
l'appelant doit les resoumettre, et chaque artefact est alors revalidé contre
le contexte rechargé avant utilisation.

Les sauvegardes ProjectState/MissionState ne forment pas une transaction
distribuée. Une défaillance entre les deux écritures reste visible comme une
divergence fail-closed à reconstruire ; elle n'autorise jamais une poursuite
silencieuse. V1 n'inclut ni DAG, parallélisme, scheduler, subagents, réseau,
LLM runtime, CLI finale ou orchestration multi-Implementer.
