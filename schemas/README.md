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

## Limites sémantiques

Les règles suivantes restent obligatoires, mais relèvent de la future logique
métier plutôt que de JSON Schema :

- stabilité et non-réutilisation historique des identifiants à l'échelle du
  projet au-delà de l'état chargé ;
- état `CERTIFIED` des dépendances pour la future décision de readiness ;
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
