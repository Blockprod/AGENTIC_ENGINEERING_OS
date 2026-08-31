# Roadmap Phase 6 — Production Governance & Observability

## Position dans la Master Roadmap

```text
P0  Foundation / Constitution                  CERTIFIED / CLOSED
P1  Deterministic Control Plane                CERTIFIED / CLOSED
P2  Sequential Agentic Workflow                CERTIFIED / CLOSED
P3  DAG + Waves + Parallel Execution           CERTIFIED / CLOSED
P4  VS Code / Codex Runtime Integration        CERTIFIED / CLOSED
P5  Repository Deployment / Installation Kit   CERTIFIED / CLOSED
P6  Production Governance & Observability      CURRENT
P7  Generalization / Final Product Certification  OUT OF SCOPE
```

Phase 6 ajoute une couche d'observation et de gouvernance au-dessus des
frontières certifiées. Elle ne remplace aucun store, service ou modèle
d'autorité P0–P5. P7 reste hors scope jusqu'à instruction explicite et clôture
certifiée de Phase 6.

## Principes d'ordonnancement

1. Définir les faits avant de les stocker ou agréger.
2. Persister les événements avant de calculer des métriques durables.
3. Évaluer health à partir de faits et métriques explicitement définis.
4. Définir les policies avant les budgets qui les appliquent.
5. Classifier les incidents avant d'exposer les actions opérateur.
6. N'autoriser maintenance et recovery qu'après les contrats de policy,
   incident et diagnostics.
7. Tester les défaillances de bout en bout avant la certification.

## Séquence P6

| Mission | Résultat attendu | Dépendances / critère de sortie |
|---|---|---|
| **P6.1 — Governance & Observability Contract** | Séparation des plans, primitives, frontières d'autorité et roadmap. | P0–P5 certifiées ; documentation uniquement. |
| **P6.2 — Operational Event Model** | Catalogue fermé, modèle et validation des événements/corrélations non autoritatifs. | P6.1 ; distinction AuditEvent/OperationalEvent conservée. |
| **P6.3 — Event Store / Structured Logging** | Persistance append-oriented, bornée, crash-safe, secret-aware et inspectable. | P6.2 ; aucune mutation métier depuis le store. |
| **P6.4 — Metrics & Runtime Counters** | Compteurs, gauges, durées et métriques dérivées à cardinalité bornée. | P6.2–P6.3 ; formules et resets explicites. |
| **P6.5 — Health Evaluation Engine** | Health fermé par composant et agrégation déterministe avec causes. | P6.2–P6.4 ; `HEALTHY != CERTIFIED`, `UNKNOWN` préservé. |
| **P6.6 — Governance Policy Model** | Policies versionnées, évaluations traçables et taxonomie safety/operational/preference. | P6.1, P6.5 ; aucune policy ne réduit un invariant P0–P5. |
| **P6.7 — Resource / Concurrency Budgets** | Budgets de concurrence, temps, disque/worktrees, remédiation et échecs. | P6.4–P6.6 ; minimum des limites produit, projet et machine. |
| **P6.8 — Incident & Escalation Management** | Détection, record, classification, escalade et décision opérateur sans réparation silencieuse. | P6.3, P6.5–P6.7 ; incidents corrélés et persistés. |
| **P6.9 — Operator Diagnostics / CLI** | Status/health, diagnostics, incidents et métriques en sorties CLI structurées. | P6.3–P6.8 ; lecture par défaut, actions séparées et explicites. |
| **P6.10 — Recovery & Maintenance Governance** | Freeze/maintenance bornés et requêtes de recovery vers les services propriétaires. | P6.6–P6.9 ; aucune autorité parallèle ni auto-réparation. |
| **P6.11 — Production E2E / Failure Injection** | Golden paths et défaillances de stores, Git, runtime, ressources, restart et observabilité. | P6.2–P6.10 intégrées ; effets isolés et preuves reproductibles. |
| **P6.12 — Final Adversarial Certification** | Audit des bypass d'autorité, pertes, corruption, secrets, cardinalité, replay et maintenance. | P6.11 ; suite complète et zéro finding bloquant. |
| **P6.CLOSE — Phase 6 Closure** | Record de certification et fermeture documentaire de la phase. | P6.12 `PASS`, recommandation explicite et tree propre. |

## Gates transverses

Chaque mission doit préserver :

- `OBSERVATION != AUTHORITY` et `HEALTHY != CERTIFIED` ;
- les identités, stores et services autoritatifs P0–P5 ;
- Human Authority et les refus fail-closed ;
- l'absence de secrets dans événements, labels, diagnostics et incidents ;
- une cardinalité, taille, rétention et consommation de ressources bornées ;
- la reconstruction après restart sans fabrication de succès ;
- le scope propre de la mission suivante.

## Definition of Done Phase 6

> L'OS peut expliquer son état opérationnel, mesurer son fonctionnement,
> détecter et classifier les dégradations, appliquer des politiques
> opérationnelles bornées et guider l'opérateur sans créer une seconde source
> d'autorité ni affaiblir P0→P5.

La Definition of Done exige également une campagne de production/failure
injection reproductible, un audit adversarial final, zéro finding bloquant et
un record de clôture distinct de la baseline auditée.

## Hors scope de la roadmap Phase 6

Phase 6 ne définit ni produit multi-tenant, ni plateforme SaaS, ni dashboard
web obligatoire, ni orchestration multi-provider, ni généralisation finale.
Ces sujets ne sont pas implicitement autorisés et P7 demeure hors scope.
