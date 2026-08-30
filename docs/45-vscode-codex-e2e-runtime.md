# VS Code / End-to-End Codex Runtime

## Portée

P4.10 remplace le transport manuel entre un `RoleHandoff` autoritatif et un
`RoleResult` validé. `CodexEndToEndRuntime` compose les frontières existantes
sans créer de second workflow :

```text
workflow P2/P3 → P4.8/P4.9 → RoleResult validé → workflow P2/P3
```

La chaîne cognitive couverte est Architect → Implementer → Tester → Reviewer →
Certifier. Le résultat Codex est seulement remis aux méthodes existantes de
`SequentialMissionWorkflow` ou, pour un groupe SAFE, aux méthodes
`submit_member()` et `complete_group()` de `ParallelMissionWorkflow`.

## API runtime

`execute_sequential_role()` exécute un handoff P2 via P4.8. Une exécution
incomplète, en recovery ou refusée n'est jamais transmise au workflow. Le type
du résultat et son rôle doivent correspondre au handoff ; les validateurs P2
restent ensuite responsables des bindings mission, génération, sujet, commit
et artefacts amont.

`execute_parallel_implementers()` accepte uniquement un `ParallelMissionPlan`
et un `PreparedParallelGroup` P3. P4.9 revalide le caractère SAFE, les
assignments et les worktrees. Le bridge exige ensuite l'ensemble exact des
membres avant de les soumettre au coordinator P3. Il ne choisit ni groupe ni
degré de parallélisme.

Après merge, `execute_parallel_dossier_role()` demande au workflow P3 de
dériver le handoff correspondant exactement au stage du dossier. Une factory
construit P4.8 avec une projection read-only de la mission P3 courante ; cette
projection est revalidée contre mission, génération, commit, statut et absence
de blockers, puis n'est jamais persistée. Tester, Reviewer et Certifier sont
ensuite remis respectivement à `accept_tester()`, `accept_reviewer()` et
`submit_certifier()` du workflow P3.

Après cette remise, `IntegrationGate`, `MergeCoordinator` et le workflow P3
normal restent les seules frontières de Gate, merge et traitement post-merge.
`MERGED` ne signifie jamais `CERTIFIED`.

## Autorité et Human flow

Le bridge ne crée ni Evidence, ni Gate, ni approval Human, ni Certification.
Pour Certifier, un `ControlPlaneSubmission` explicite et extérieur au résultat
Codex est obligatoire. Le `CertifierResult` canonique peut seulement être
soumis à `CertificationService` via le workflow P2 ; il ne porte aucun verdict
autoritatif `CERTIFIED`.

Un workflow bloqué sur `HUMAN_REQUIRED` reste bloqué. La reprise continue
d'utiliser la frontière Human existante et une Evidence attribuable appliquée
par le Control Plane. Le runtime Codex ne synthétise jamais de réponse Human.

## Restart et refus

P4.10 conserve les règles P4.7 transitivement : intake-only après observation,
réutilisation d'un résultat déjà `VALIDATED`, absence de rerun aveugle et
`RECOVERY_REQUIRED` lorsque les effets ne sont pas prouvables. Pour un groupe
parallèle incomplet, aucun résultat n'est remis au workflow P3 ; les ledgers
indépendants permettent à P4.9 de réutiliser les membres déjà validés lors de
la reprise.

Les protections P4.6–P4.9 refusent résultat malformed, tool failure même avec
exit code zéro, état d'exécution forgé, génération stale, worktree échangé ou
effets Git incohérents. P4.10 ajoute le refus des types cross-role, des rôles
d'exécution échangés et des ensembles parallèles incomplets, dupliqués ou
swappés. Il n'expose aucune API de Gate, merge direct ou transition d'état.

## VS Code et canary réel

VS Code reste l'environnement opérateur. Depuis son terminal intégré, le
projet appelle la CLI `codex exec` découverte et validée par P4.4 ; aucune
automatisation, extension ou inspection de l'interface VS Code n'est requise.

La suite standard reste offline avec un faux subprocess. Le canary opt-in
`test_real_codex_structured_read_only_canary_uses_only_temporary_repo` lance un
vrai `codex exec` en lecture seule, avec schéma structuré, dans un dépôt Git
temporaire et vérifie HEAD et propreté avant/après :

```powershell
$env:AGENTIC_OS_RUN_CODEX_CANARY = "1"
.venv\Scripts\python.exe -m pytest -q tests/test_codex_e2e_runtime.py `
  -k real_codex_structured_read_only_canary
```

Le parallélisme réel Codex n'est pas exécuté par ce checkpoint :
`real Codex parallelism = UNKNOWN`.

## Limites

P4.10 n'est ni un scheduler autonome, ni une intégration UI VS Code, ni une
abstraction multi-provider. Les boucles de remédiation, Gate/Merge, Human
approval, recovery et certification demeurent celles de P0–P3. P4.11 et la
fermeture de Phase 4 restent hors scope.
