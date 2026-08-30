# JSON Schemas

Les six contrats machine-validables de Phase 1 utilisent JSON Schema Draft
2020-12 :

- `user-story.schema.json`
- `evidence.schema.json`
- `gate.schema.json`
- `audit-event.schema.json`
- `certification.schema.json`
- `project-state.schema.json`

Les contrats opérationnels de Phase 2 utilisent le même draft :

- `mission-state.schema.json`
- `architect-result.schema.json`
- `implementer-result.schema.json`
- `tester-result.schema.json`
- `reviewer-result.schema.json`
- `certifier-result.schema.json`

La projection DAG de Phase 3 utilise également ce draft :

- `dag-snapshot.schema.json`
- `readiness-snapshot.schema.json`
- `wave-plan.schema.json`
- `conflict-analysis.schema.json`
- `worktree-assignment.schema.json`
- `worktree-registry.schema.json`
- `parallel-execution-plan.schema.json`
- `integration-gate-result.schema.json`
- `merge-result.schema.json`

Ils valident la structure, les champs requis et les contraintes V1 exprimables
de manière robuste. Les propriétés inattendues sont refusées à la racine et
dans les objets dont le contrat est fermé.

`project-state.schema.json` formalise la mémoire persistante V1. Il contient
uniquement `schema_version`, les User Stories, Evidence, Gates, Certifications
et Audit Events, et référence directement les cinq contrats canoniques.

La validation s'exécute avec :

```text
python -m pytest
```

`ProjectStateStore` complète ce schéma par les contrôles locaux déterministes
d'unicité des IDs et de résolution des références persistées évidentes.
`MissionStateStore` valide séparément `mission.json` ; ce document n'ajoute
aucune autorité au `ProjectState`.

`dag-snapshot.schema.json` contraint les nœuds et edges sérialisés. Les
références globales, cycles, unicité des IDs de nœuds, complétude des edges et
ordres canoniques ne sont pas exprimables de façon robuste par ce schéma ;
`DAGValidator` les vérifie applicativement. Le snapshot n'est pas persisté.

`readiness-snapshot.schema.json` contraint les diagnostics sérialisés et leur
catalogue fermé. La correspondance avec le DAG et le ProjectState, la
satisfaction des dépendances et la politique d'état restent vérifiées par
`ReadinessEngine`. Ce snapshot n'est pas persisté.

`wave-plan.schema.json` contraint les Waves logiques, leurs indices et membres,
ainsi que les nœuds différés et leurs raisons. Le layering topologique, la
cohérence avec la readiness et l'ordre canonique restent applicatifs sous
`WavePlanner`. Le plan n'est pas persisté et n'accorde aucune autorité
d'exécution.

`conflict-analysis.schema.json` contraint les résultats pairwise d'une même
Wave, les classifications, le catalogue de raisons et les chemins de
chevauchement. La cohérence avec le WavePlan et le ProjectState, l'ordre
lexical des IDs, la sémantique de scope et le calcul des chevauchements restent
vérifiés par `ExecutionConflictAnalyzer`. Cette projection n'est pas persistée
et n'accorde aucune autorité d'exécution.

`worktree-assignment.schema.json` ferme le modèle d'identité, de génération,
de baseline, de branche, de path, de lifecycle et de result commit.
`worktree-registry.schema.json` versionne la collection persistante. Le store
et `WorktreeManager` vérifient applicativement l'ordre canonique, les identités
dérivées, l'unicité des ressources actives, les transitions autorisées et la
cohérence avec la réalité Git.

`parallel-execution-plan.schema.json` ferme la projection reconstructible des
groupes d'une Wave courante, liée à une mission, une génération, une baseline
et un fingerprint de contexte. La compatibilité pairwise, le regroupement
greedy, la fraîcheur et la complétude restent contrôlés applicativement par le
`ParallelImplementerCoordinator`. Ce plan n'est pas persisté et n'accorde
aucune autorité Git ou Control Plane.

`integration-gate-result.schema.json` ferme l'artefact P3.9 distinct du modèle
`Gate` du Control Plane. Il contraint le contexte d'intégration, les commits et
fichiers observés par membre, l'ordre déterministe, les findings et le résultat
`PASS`, `FAIL` ou `UNKNOWN`. La réalité Git, les scopes, collisions et
preflights `merge-tree` restent vérifiés applicativement par `IntegrationGate`.

`merge-result.schema.json` ferme le résultat P3.10 avec le contexte de groupe,
l'ordre et les commits membres, le commit d'intégration observé, les HEAD du
primary avant/après, le statut `MERGED`, `FAILED` ou `BLOCKED` et les findings.
La fraîcheur du Gate, la cohérence Git, l'ancestry et la promotion restent des
preuves applicatives du `MergeCoordinator`.

## Limites sémantiques

Les règles suivantes restent obligatoires, mais relèvent de la future logique
métier plutôt que de JSON Schema :

- stabilité et non-réutilisation historique des identifiants à l'échelle du
  projet au-delà de l'état chargé ;
- unicité des IDs de critères d'acceptation lorsque deux objets différents
  portent le même ID ;
- priorité de `forbidden_paths` et contrôle des modifications réelles contre le
  scope ;
- immutabilité contrôlée du contrat et transitions d'état autorisées ;
- champs Evidence requis selon le contexte, provenance, applicabilité et
  staleness réelles ;
- applicabilité métier des références entre Evidence, Gates et certification ;
- politique des Gates requis ou optionnels et autorisation explicite de
  `NOT_APPLICABLE` ;
- Human Authority, provenance réelle et validité d'une approbation humaine ;
- caractère append-oriented de l'Audit Trail et ordre réel des événements ;
- satisfaction complète des critères et Gates avant un verdict `CERTIFIED`,
  ainsi que la validité d'une intégration transitive.

Le contrat structurel `Certification` exige la liste unique
`authorized_not_applicable_gates`. La cohérence de chaque identifiant avec les
Gates requis, leur résultat persistant et la décision `CERTIFIED` reste une
validation sémantique fail-closed effectuée par le runtime.

Ces limites ne réduisent pas les contrats normatifs. Une règle sémantique non
vérifiée reste `UNKNOWN` et doit être traitée selon la politique fail-closed.

Le contrat P5.2 `project-configuration` appartient au produit installé. Son
schéma canonique est embarqué sous
`agentic_engineering_os/resources/schemas/project-configuration.schema.json`
afin que sa validation ne dépende pas du checkout source. Il reste distinct du
`ProjectState` et des observations de reconnaissance.
