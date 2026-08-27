# JSON Schemas V1

Les six contrats machine-validables utilisent JSON Schema Draft 2020-12 :

- `user-story.schema.json`
- `evidence.schema.json`
- `gate.schema.json`
- `audit-event.schema.json`
- `certification.schema.json`
- `project-state.schema.json`

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

## Limites sémantiques

Les règles suivantes restent obligatoires, mais relèvent de la future logique
métier plutôt que de JSON Schema :

- stabilité et non-réutilisation historique des identifiants à l'échelle du
  projet au-delà de l'état chargé ;
- existence des User Stories référencées, absence d'auto-dépendance, absence de
  cycles et état `CERTIFIED` des dépendances ;
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

Ces limites ne réduisent pas les contrats normatifs. Une règle sémantique non
vérifiée reste `UNKNOWN` et doit être traitée selon la politique fail-closed.
