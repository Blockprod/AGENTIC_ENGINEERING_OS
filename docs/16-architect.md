# Architect V1

## Mission et entrée

L'Architect est le rôle Codex qui spécifie une solution conceptuelle minimale,
décompose le travail et produit des User Stories candidates. Son entrée
canonique est un `RoleHandoff(to_role=ARCHITECT)` émis par l'Orchestrator.
`ArchitectInput.from_handoff` en extrait `mission_id`, objectif, sujet, commit
observé, blockers et instructions ; les contraintes disponibles sont fournies
explicitement par l'appelant.

L'Architect observe les faits transmis, explicite toute hypothèse et signale
les informations manquantes. Il ne transforme jamais une hypothèse en fait et
retourne `BLOCKED` lorsqu'une décision indispensable ne peut être attribuée.

L'instruction compacte `roles/architect.md` est un artefact Markdown générique
et repository-local destiné à Codex dans VS Code. Elle ne suppose aucune
convention native ou propriétaire d'exécution des rôles.

## Sortie

`ArchitectResult` contient :

- la mission, le rôle fixe `ARCHITECT`, le sujet et le commit observé ;
- un résumé, les hypothèses, décisions, risques et blockers explicites ;
- zéro ou plusieurs User Stories candidates ;
- un rôle suivant recommandé sans autorité de routing ;
- le verdict de rôle `READY` ou `BLOCKED`.

Une décision porte le kind `ARCHITECTURAL` lorsqu'elle appartient au scope du
rôle, ou `HUMAN_REQUIRED` lorsqu'elle est réservée à l'opérateur. `READY`
signifie uniquement que l'output peut être soumis au validateur et au prochain
rôle ; ce verdict n'est ni `CERTIFIED`, ni une autorisation Control Plane.

## User Stories candidates

Toute nouvelle candidate utilise le statut initial `PROPOSED`. Elle respecte
le contrat Phase 0 complet : identifiant, priorité, risque, dépendances, scope,
Acceptance Criteria observables, Gates, Human Approval et métadonnées. Les
dépendances doivent être résolues dans le même résultat ou explicitement
fournies au validateur comme IDs déjà connus. P2.4 ne crée ni DAG ni règle de
readiness.

L'Architect peut déclarer `human_approval.required=true`, mais conserve
obligatoirement `approved=false`, `approved_by=null` et `approved_at=null`. Il
ne fabrique et ne réutilise aucune approbation Human.

## Frontière déterministe

`schemas/architect-result.schema.json` valide la forme, les enums, les règles
`READY/BLOCKED` et les contraintes Human explicites. Ensuite,
`ArchitectResultValidator` soumet chaque User Story à `ContractValidator`,
impose `PROPOSED`, vérifie l'unicité des IDs, résout les dépendances locales et
refuse toute auto-approbation Human. Lorsqu'un `ArchitectInput` est fourni, il
vérifie aussi l'identité de mission, le sujet et le commit observé.

Le validateur accepte ou refuse un contenu produit par Codex. Il ne génère pas
la solution, ne réécrit aucune décision, n'invente ni Acceptance Criterion ni
User Story et ne transforme aucun output invalide en résultat prêt.

## Autorité et handoff

Le flux reste séparé :

```text
Orchestrator
    ↓ RoleHandoff(to=ARCHITECT)
Codex agissant comme Architect
    ↓ ArchitectResult
Validation déterministe
    ↓ résultat validé ou refus
Orchestrator
```

P2.4 ne rappelle pas automatiquement l'Orchestrator. L'Architect ne modifie ni
code métier, ni tests métier, ni `state.json`, ni `mission.json`, ni
Certification, ni Evidence Human. Ses seuls artefacts autorisés sont son
`ArchitectResult`, les User Stories candidates qu'il contient et la
documentation architecturale explicitement demandée. L'Orchestrator conserve
l'autorité de routing ; `ControlLoop` et les services Phase 1 conservent toute
autorité de validation, transition, Gate, Evidence et certification.
