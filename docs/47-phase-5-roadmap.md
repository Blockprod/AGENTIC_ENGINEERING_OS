# Roadmap Phase 5 — Repository Deployment / Installation Kit

## Position dans la roadmap

```text
P0  Foundation / Constitution                 CERTIFIED / CLOSED
P1  Deterministic Control Plane               CERTIFIED / CLOSED
P2  Sequential Agentic Workflow               CERTIFIED / CLOSED
P3  DAG + Waves + Parallel Execution          CERTIFIED / CLOSED
P4  VS Code / Codex Runtime Integration       CERTIFIED / CLOSED
P5  Repository Deployment / Installation Kit  CURRENT
P6  Production Governance & Observability     OUT OF SCOPE
P7  Generalization / Final Certification      OUT OF SCOPE
```

## Séquence P5

| Mission | Résultat attendu | Dépendances / critère de sortie |
|---|---|---|
| **P5.1 — Deployment Architecture & Installation Contract** | Frontières de propriété, footprint, sécurité d'adoption, idempotence, versions et reconnaissance documentés. | Baseline P4 certifiée ; documentation cohérente uniquement. |
| **P5.2 — Project Configuration & Product Resource Contract** | Schéma minimal de configuration et résolution versionnée des schémas, rôles, contrats et modèles depuis le produit installé. | P5.1 ; supprime contractuellement le couplage aux ressources du checkout avant implémentation autonome. |
| **P5.3 — Repository Reconnaissance** | Observations déterministes classées `FACT`, `INFERENCE`, `UNKNOWN`, sans écriture. | P5.2 ; inconnues critiques bloquantes et tests adversariaux. |
| **P5.4 — Initialization Planner / Dry Run** | Plan stable, lisible et lié au snapshot observé, sans effet de bord. | P5.3 ; conflits et état d'initialisation explicités. |
| **P5.5 — Safe Repository Initializer** | Application atomique et minimale d'un plan encore valide, sans mutation Git cachée. | P5.4 ; second appel conforme sans modification. |
| **P5.6 — `AGENTS.md` Integration** | Création ou section gérée bornée, coexistence sûre et détection des altérations. | P5.5 ; aucun écrasement ni dépendance à un import Codex non prouvé. |
| **P5.7 — Runtime State Bootstrap** | Création contrôlée de la configuration et des états requis via leurs stores. | P5.5–P5.6 ; politiques Git/locales validées, aucune édition directe. |
| **P5.8 — Existing Repository Adoption** | Workflow intégré pour repository existant, dirty state et initialisation partielle. | P5.3–P5.7 ; conflits fail-closed et fichiers utilisateur préservés. |
| **P5.9 — Upgrade / Migration** | Détection de compatibilité, plan explicite, sauvegarde, validation et rollback. | P5.2, P5.7–P5.8 ; aucune migration déclenchée par une simple mise à jour du package. |
| **P5.10 — Installation Kit / CLI Entry Point** | Installation reproductible et commandes d'inspection, dry-run/init exposées sans contourner les services. | P5.2–P5.9 ; ressources du produit indépendantes du checkout. |
| **P5.11 — Multi-Repository Deployment Validation** | Validation sur repositories neufs et existants, langages/configurations variés et machines distinctes. | P5.10 ; footprint minimal, reprise et idempotence prouvés. |
| **P5.12 — Final Adversarial Certification** | Audit des bypass, écrasements, migrations, états partiels, chemins et mutations Git. | P5.11 ; zéro finding bloquant pour recommander la certification. |
| **P5.CLOSE — Phase 5 Closure** | Enregistrement documentaire de la baseline auditée et fermeture de phase. | P5.12 `PASS` et working tree propre. |

Chaque mission conserve le scope qui lui est propre. Cette roadmap n'autorise
aucune implémentation anticipée d'une mission suivante.

## Definition of Done

> Sur une installation propre d'`AGENTIC_ENGINEERING_OS`, un repository cible
> existant peut être inspecté, initialisé, configuré et rendu prêt pour le
> runtime P4 sans copier le code interne de l'OS, sans écraser les fichiers
> utilisateur et avec reprise, idempotence et upgrade fail-closed.

P6 et P7 ne commencent qu'après instruction explicite et fermeture certifiée de
la Phase 5.
