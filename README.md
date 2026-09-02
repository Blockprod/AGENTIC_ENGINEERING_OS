# AGENTIC_ENGINEERING_OS

Couche réutilisable pour structurer le développement logiciel en un processus
d'ingénierie agentique observable, vérifiable et réutilisable.

Environnement cible : VS Code + Codex, au sein d'un repository Git.

Principes fondamentaux : état réel comme source de vérité, responsabilités
séparées, preuves reproductibles et blocage fail-closed.

Statut : Phase 0 — `CERTIFIED / CLOSED` ; Phase 1 — `CERTIFIED / CLOSED` ;
Phase 2 — `CERTIFIED / CLOSED` ; Phase 3 — `CERTIFIED / CLOSED` ;
Phase 4 — `CERTIFIED / CLOSED` ; Phase 5 — `CERTIFIED / CLOSED` ;
Phase 6 — `CERTIFIED / CLOSED` ; Phase 7 — `IN PROGRESS — P7.8`

Roadmap canonique :

- P0 — Foundation
- P1 — Deterministic Control Plane
- P2 — Sequential Agentic Workflow
- P3 — DAG + Waves + Parallel Execution
- P4 — VS Code / Codex Runtime Integration
- P5 — Repository Deployment / Installation Kit
- P6 — Production Governance & Observability
- P7 — Generalization / Final Product Certification — `IN PROGRESS — P7.8`

Documentation :

- [Vision et contrat d'Agentic Engineering](docs/00-vision.md)
- [Périmètre du projet](docs/01-scope.md)
- [Invariants](docs/02-invariants.md)
- [Politique fail-closed](docs/03-fail-closed-policy.md)
- [Modèle d'autorité et frontières des rôles](docs/04-authority-model.md)
- [Machine d'état et cycle de vie](docs/05-state-machine.md)
- [Contrat canonique d'une User Story](docs/06-user-story-contract.md)
- [Contrat des preuves](docs/07-evidence-contract.md)
- [Contrat des gates](docs/08-gate-contract.md)
- [Modèle d'audit](docs/09-audit-model.md)
- [Modèle de certification](docs/10-certification-model.md)
- [Architecture runtime Phase 1](docs/11-runtime-architecture.md)
- [Contrat opérationnel Codex](docs/12-codex-operating-contract.md)
- [Roadmap Phase 2](docs/13-phase-2-roadmap.md)
- [État persistant de mission](docs/14-mission-state.md)
- [Orchestrator V1](docs/15-orchestrator.md)
- [Architect V1](docs/16-architect.md)
- [Implementer V1](docs/17-implementer.md)
- [Tester V1](docs/18-tester.md)
- [Reviewer V1](docs/19-reviewer.md)
- [Certifier V1](docs/20-certifier.md)
- [Mission agentique séquentielle V1](docs/21-end-to-end-agentic-mission.md)
- [Contrat canonique du DAG de User Stories](docs/22-dag-contract.md)
- [Roadmap Phase 3](docs/23-phase-3-roadmap.md)
- [DAG Validator](docs/24-dag-validator.md)
- [Readiness Engine](docs/25-readiness-engine.md)
- [Deterministic Wave Planner](docs/26-wave-planner.md)
- [Execution Conflict Model](docs/27-execution-conflict-model.md)
- [Git Worktree Isolation Contract](docs/28-git-worktree-contract.md)
- [Worktree Manager](docs/29-worktree-manager.md)
- [Parallel Implementer Coordinator](docs/30-parallel-implementer-coordinator.md)
- [Integration Gate](docs/31-integration-gate.md)
- [Merge Coordinator](docs/32-merge-coordinator.md)
- [Mission parallèle end-to-end](docs/33-end-to-end-parallel-mission.md)
- [Remédiation et recovery parallèles](docs/34-parallel-remediation-recovery.md)
- [Certification Phase 3](docs/PHASE-3-CERTIFICATION.md)
- [Contrat d'exécution Codex](docs/35-codex-execution-contract.md)
- [Roadmap Phase 4](docs/36-phase-4-roadmap.md)
- [Deterministic Context Builder](docs/37-context-builder.md)
- [Deterministic Prompt Compiler](docs/38-prompt-compiler.md)
- [Capacités runtime Codex](docs/39-codex-runtime-capabilities.md)
- [Codex Runtime Adapter](docs/40-codex-runtime-adapter.md)
- [Structured Codex Result Intake](docs/41-structured-result-intake.md)
- [Codex Execution State & Restart Recovery](docs/42-codex-execution-recovery.md)
- [Single-Role Codex Execution](docs/43-single-role-codex-execution.md)
- [Parallel Codex Implementers](docs/44-parallel-codex-implementers.md)
- [VS Code / End-to-End Codex Runtime](docs/45-vscode-codex-e2e-runtime.md)
- [Certification Phase 4](docs/PHASE-4-CERTIFICATION.md)
- [Architecture de déploiement](docs/46-deployment-architecture.md)
- [Roadmap Phase 5](docs/47-phase-5-roadmap.md)
- [Contrat de configuration projet](docs/48-project-configuration.md)
- [Reconnaissance déterministe du repository](docs/49-repository-reconnaissance.md)
- [Initialization Planner déterministe](docs/50-initialization-planner.md)
- [Safe Repository Initializer](docs/51-safe-repository-initializer.md)
- [Intégration sûre AGENTS.md](docs/52-agents-integration.md)
- [Runtime State Bootstrap](docs/53-runtime-state-bootstrap.md)
- [Existing Repository Adoption](docs/54-existing-repository-adoption.md)
- [Explicit Upgrade & Migration](docs/55-upgrade-migration.md)
- [Installation Kit & CLI](docs/56-installation-cli.md)
- [Validation de déploiement multi-repository](docs/57-multi-repository-deployment-validation.md)
- [CLI portable compatible avec les politiques d'entreprise](docs/58-policy-compatible-portable-cli.md)
- [Certification Phase 5](docs/PHASE-5-CERTIFICATION.md)
- [Production Governance & Observability](docs/59-production-governance-observability.md)
- [Roadmap Phase 6](docs/60-phase-6-roadmap.md)
- [Modèle OperationalEvent](docs/61-operational-event-model.md)
- [Operational Event Store](docs/62-operational-event-store.md)
- [Metrics & Runtime Counters](docs/63-metrics-runtime-counters.md)
- [Health Evaluation Engine](docs/64-health-evaluation-engine.md)
- [Governance Policy Model](docs/65-governance-policy-model.md)
- [Resource & Concurrency Budgets](docs/66-resource-concurrency-budgets.md)
- [Incident & Escalation Management](docs/67-incident-escalation-management.md)
- [Operator Diagnostics & CLI](docs/68-operator-diagnostics-cli.md)
- [Recovery & Maintenance Governance](docs/69-recovery-maintenance-governance.md)
- [Production E2E & Failure Injection](docs/70-production-e2e-failure-injection.md)
- [Certification Phase 6](docs/PHASE-6-CERTIFICATION.md)
- [Generalization & Final Product Contract](docs/70-generalization-final-product-contract.md)
- [Roadmap Phase 7](docs/71-phase-7-roadmap.md)
- [Platform & Environment Abstraction](docs/72-platform-environment-abstraction.md)
- [Repository Archetype Generalization](docs/73-repository-archetype-generalization.md)
- [Codex Capability Portability](docs/74-codex-capability-portability.md)
- [Configuration and Policy Generalization](docs/75-configuration-policy-generalization.md)
- [Backward Compatibility and Versioning](docs/76-backward-compatibility-versioning.md)
- [Installation and Upgrade Compatibility Matrix](docs/77-installation-upgrade-compatibility-matrix.md)
- [JSON Schemas V1](schemas/README.md)
