# Roadmap Phase 4

## Position dans la Master Roadmap

Phase 4 est `VS Code / Codex Runtime Integration`. Les Phases 0 à 3 restent
`CERTIFIED / CLOSED`. La séquence maîtresse P0→P7 n'est pas modifiée : Phase 5
porte le déploiement dans d'autres repositories, Phase 6 la gouvernance et
l'observabilité de production, et Phase 7 la généralisation et la certification
finale du produit.

| Phase | Périmètre canonique | Position |
|---|---|---|
| P0 | Foundation / Constitution | `CLOSED` |
| P1 | Deterministic Control Plane | `CLOSED` |
| P2 | Sequential Agentic Workflow | `CLOSED` |
| P3 | DAG + Waves + Parallel Execution | `CLOSED` |
| P4 | VS Code / Codex Runtime Integration | `CURRENT` |
| P5 | Repository Deployment / Installation Kit | Future |
| P6 | Production Governance & Observability | Future |
| P7 | Generalization / Final Product Certification | Future |

## Ordre de dépendance

1. **P4.1 — Codex Execution Contract & Phase 4 Roadmap**
   Définir la frontière d'autorité, le request canonique, l'observation honnête,
   les effets de bord et la séquence de livraison. Aucun runtime.

2. **P4.2 — Context Builder & Cumulative Memory**
   Construire une sélection déterministe et attribuable de contexte
   autoritatif/cognitif selon rôle, sujet, dépendances et composants. P4.2 ne
   compile pas encore de prompt et n'invoque pas Codex.

3. **P4.3 — Prompt Compiler**
   Compiler un `CodexExecutionRequest` et le contexte P4.2 en mission compacte,
   déterministe et inspectable, sans accorder d'autorité au texte produit.

4. **P4.4 — Codex Capability Discovery & Adapter Contract**
   Vérifier dans l'environnement réel les capacités disponibles : invocation,
   entrées, sorties, statut, interruption, timeout, identité et observation.
   Fixer ensuite le port infrastructure minimal sans inventer d'API.

5. **P4.5 — Codex Runtime Adapter**
   Implémenter l'adapter mono-provider contre les capacités P4.4 et conserver
   sortie brute et observations. Aucun verdict métier n'est calculé ici.

6. **P4.6 — Structured Result Intake**
   Décoder fail-closed le RoleResult attendu, réutiliser les validateurs P2,
   revalider les bindings P3 et remettre uniquement un résultat validé au
   workflow existant.

7. **P4.7 — Execution State, Timeout & Restart**
   Persister request/attempt state, rendre les effets incertains observables,
   bloquer les retries aveugles et reconstruire après timeout, cancellation,
   crash ou perte de session.

8. **P4.8 — Single-Role Codex Execution**
   Prouver un rôle complet, d'abord dans un worktree contrôlé lorsque le rôle
   mute des fichiers : request, contexte, prompt, exécution, observation,
   RoleResult, validation et workflow.

9. **P4.9 — Parallel Codex Implementers**
   Brancher plusieurs exécutions Codex sur les groupes SAFE et worktrees P3,
   avec isolation, génération, intake individuel, échec partiel et aucune
   autorité de merge implicite.

10. **P4.10 — VS Code Integration & End-to-End Codex Mission**
    Intégrer le transport réel dans VS Code et prouver le chemin nominal sans
    copier-coller manuel : Orchestrator déterministe, rôles, parallélisme,
    Integration Gate, Merge, Evidence, certification et recovery.

11. **P4.11 — Final Adversarial Certification**
    Attaquer identité, replay, stale context, scope, worktree, résultat
    tronqué/forgé, timeout, crash avec effets de bord, Human Authority,
    Control Plane et restart. Aucun finding bloquant n'est toléré.

12. **P4.CLOSE — Phase 4 Closure**
    Enregistrer séparément la baseline auditée et le commit documentaire de
    clôture après recommandation de certification explicite.

P4.2 précède P4.3 afin que le Prompt Compiler consomme un contexte déjà
sélectionné et typé. P4.4 précède l'adapter afin que son contrat repose sur des
capacités Codex observées plutôt que supposées. Chaque étape dépend des
contrats et preuves des étapes précédentes ; aucune mission n'anticipe la
suivante.

## Definition of Done Phase 4

Phase 4 est terminée uniquement lorsqu'une mission existante peut suivre ce
chemin nominal sans copier-coller manuel :

```text
deterministic OS
→ authoritative execution request
→ repository-local context selection
→ compiled Codex mission
→ Codex execution in the bound repository/worktree
→ observed execution and side effects
→ structured RoleResult
→ deterministic role and binding validation
→ existing sequential or parallel workflow
→ existing Control Plane decision
```

La preuve finale exige notamment :

- identité et immutabilité des requests et attempts ;
- binding exact mission/story/génération/repository/worktree/commit ;
- stale et replay refusés ;
- scope d'écriture distinct du contexte visible ;
- sorties malformed, tronquées, contradictoires ou wrong-role refusées ;
- timeout, interruption, crash et effets partiels observables et récupérables ;
- exécution mono-rôle puis Implementers parallèles sur groupes SAFE ;
- Git/registry, Gate, Merge, Evidence, Certification et Human Authority P0–P3
  inchangés ;
- aucune déclaration Codex convertie en preuve ou succès ;
- suite de régression complète et campagne adversariale P4.11 conformes ;
- repository final propre et baseline de certification identifiable.

## Non-objectifs et frontières de phase

- Pas de remplacement de VS Code, Codex, de l'Orchestrator ou du Control Plane.
- Pas de framework agentique ou orchestration multi-provider.
- Pas d'installation générique dans des repositories arbitraires : Phase 5.
- Pas de dashboard, SLO, télémétrie de production ou gouvernance d'exploitation :
  Phase 6.
- Pas d'autonomie générale, marketplace ou certification finale multi-projet :
  Phase 7.
- Pas de transaction distribuée générale ni de Git destructif automatique.
