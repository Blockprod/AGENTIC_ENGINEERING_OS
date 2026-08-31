# Codex Execution Contract

## Objet et portée

Ce contrat définit la future frontière entre le moteur déterministe certifié et
une exécution Codex dans VS Code. P4.1 est documentaire : il ne fournit ni
invocation Codex, ni transport, ni parser de résultat, ni mécanisme de retry.

Le chemin cible est :

```text
authoritative OS state
→ CodexExecutionRequest
→ selected context
→ compiled Codex mission
→ observed execution
→ structured RoleResult
→ deterministic validation
→ existing workflow and Control Plane
```

La conversation courante n'est jamais une dépendance runtime.

## Frontière d'autorité

`CODEX EXECUTES. CONTROL PLANE DECIDES.`

Codex peut lire le contexte sélectionné, raisonner, modifier les fichiers
autorisés par son rôle et son scope, exécuter les commandes autorisées, puis
retourner des observations et un RoleResult structuré.

Codex ne peut pas autoritativement :

- écrire `ProjectState`, `MissionState` ou `WorktreeRegistry` ;
- choisir ou avancer `workflow_generation` ;
- élargir le scope reçu ou s'attribuer un autre rôle ;
- simuler une Human Approval ou transformer une Human Evidence en approbation
  appliquée ;
- fabriquer une Evidence, un Gate `PASS` ou une Certification ;
- transformer sa déclaration, son prompt ou son RoleResult en preuve.

L'Orchestrator reste déterministe. Phase 4 n'introduit pas de
`CodexOrchestrator`. Les transitions, Gates, merges, certifications, écritures
trusted et transactions de recovery restent sous les autorités P1–P3.

## `CodexExecutionRequest`

`CodexExecutionRequest` est le futur contrat applicatif immuable qui lie une
tentative d'exécution à un contexte autoritatif. Sa représentation machine sera
définie et testée dans une mission ultérieure ; P4.1 fixe seulement sa
sémantique minimale.

### Données minimales

| Donnée | Nécessité | Justification |
|---|---|---|
| `request_id` | Toujours | Identité stable, unique et non réutilisable de la demande. |
| `mission_id` | Toujours | Interdit le replay cross-mission. |
| `workflow_generation` | Toujours | Lie tous les artefacts à la génération autoritative courante. |
| `role` | Toujours | Sélectionne le contrat et le type exact de RoleResult attendu. |
| `subject` | Toujours | Identifie le sujet métier sans le déduire du prompt. |
| `user_story_id` | Si le rôle porte sur une User Story | Interdit les substitutions cross-story. |
| `repository_binding` | Toujours | Identifie le repository autorisé et son état Git de référence. |
| `worktree_binding` | Pour toute exécution assignée à un worktree | Lie chemin, branche, baseline, assignment et result expectations. |
| `observed_commit` | Toujours | Commit réellement observé lors de la compilation de la demande. |
| `operating_step` | Toujours | Lie l'exécution à l'étape courante de l'Operating Loop. |
| `scope` | Toujours | Contient explicitement les chemins autorisés et interdits ainsi que les opérations permises. |
| `role_contract_ref` | Toujours | Référence versionnée du contrat de rôle applicable. |
| `task` | Toujours | Objectif borné à exécuter ; ce texte n'accorde aucune autorité supplémentaire. |
| `authoritative_context_refs` | Toujours | Références et empreintes des sources autoritatives utilisées. |
| `expected_result_contract` | Toujours | Type et version exacts du RoleResult attendu. |
| `verification_requirements` | Toujours | Commandes/contrôles attendus et règles de résultat fail-closed. |

`repository_binding` doit au minimum permettre de vérifier l'identité du
repository, le primary attendu et le commit de référence. `worktree_binding`
est distinct : lorsqu'il est requis, il comprend l'assignment autoritatif, le
chemin canonique, la branche, la baseline, la mission, la User Story et la
génération. Un chemin lisible ne devient jamais un scope modifiable.

Une donnée n'est pas dupliquée dans le request lorsqu'une référence
autoritative versionnée et son empreinte suffisent. Toute donnée nécessaire à
une décision qui est absente, ambiguë ou non résoluble bloque la compilation ou
l'exécution.

### Identité, immutabilité et fraîcheur

Avant démarrage, le futur service reconstruit les références, recalcule les
empreintes et compare Git, ProjectState, MissionState et, si applicable,
WorktreeRegistry. Une demande devient immuable dès qu'une tentative lui est
associée. Toute modification nécessite une nouvelle identité de demande.

Une demande est stale dès qu'un de ses bindings ne correspond plus à l'état
autoritatif, notamment mission, génération, rôle, sujet, assignment, worktree,
branche, baseline ou commit. Une demande terminée ou consommée n'est pas
relancée comme une nouvelle tentative. Les mélanges cross-mission,
cross-story, cross-generation et cross-worktree sont refusés.

Le prompt compilé est une projection du request. Une égalité de texte entre
deux prompts ne prouve ni identité, ni fraîcheur, ni autorité.

## Mémoire cumulative et politique de prompt

La politique est `maximum useful memory, minimum repetition`. Le futur Context
Builder sélectionne le minimum suffisant de sources repository-locales ; le
Prompt Compiler les ordonne sans injecter par défaut toute la conversation ou
toute la documentation.

Une mission compilée contient :

```text
COMPACT CERTIFIED BASELINE
+ CURRENT ROLE / MISSION / SUBJECT
+ RELEVANT ARCHITECTURE
+ RELEVANT INVARIANTS
+ RELEVANT HISTORICAL FINDINGS / ANTI-REGRESSIONS
+ AUTHORIZED CONTEXT
+ SCOPE
+ EXPECTED RESULT CONTRACT
+ VERIFICATION CONTRACT
```

### Contexte autoritatif et contexte cognitif

Le **contexte autoritatif** est nécessaire pour valider ou refuser l'exécution.
Il provient des sources applicables : `AGENTS.md`, records de certification,
contrats d'architecture et de rôle, ProjectState, MissionState, UserStory, Git,
WorktreeRegistry, Evidence et Gates pertinents. Chaque élément est référencé,
attribuable et lié à une version, une génération ou un commit lorsqu'applicable.

Le **contexte cognitif optionnel** améliore le raisonnement : explications,
extraits documentaires, findings historiques pertinents et exemples. Il peut
être omis sans changer l'autorité et ne peut jamais élargir le request,
contredire une source autoritative ou transformer une mémoire en preuve.

Une contradiction entre les deux bloque ; elle n'est pas résolue en faveur du
contexte cognitif.

## Sélection du contexte par rôle

- **Architect** : objectif, contraintes, architecture et frontières
  d'autorité pertinentes ; pas d'autorité de mutation métier.
- **Implementer** : UserStory, scope, assignment/worktree, baseline,
  dépendances certifiées et architecture affectée.
- **Tester** : critères d'acceptation, ImplementerResult validé, commit et code
  pertinents, exigences positives, négatives, edge et régression.
- **Reviewer** : résultats Implementer et Tester validés, diff/commit,
  dimensions de review et architecture concernée.
- **Certifier** : chaîne de rôles validée, Evidence, Gates, contexte Human et
  `NOT_APPLICABLE`, commit et exigences de certification.

Le contexte est filtré aussi par sujet, dépendances, composants affectés,
frontières d'autorité et anti-régressions applicables. Le contexte visible ou
lisible reste distinct du scope d'écriture.

## Observation de l'exécution

Le futur adapter observe uniquement ce que le transport réel garantit. Selon
les capacités vérifiées, une observation peut inclure : identité de tentative,
début et fin, completion/failure/interruption, statut de transport ou exit
status, sortie brute, et état repository/worktree avant et après.

Une valeur non exposée par le transport reste `UNKNOWN` ou absente ; elle n'est
jamais inventée. L'observation d'un commit ou d'un fichier modifié décrit un
effet physique, pas un succès métier.

```text
Codex execution
!= Codex declaration
!= RoleResult
!= Evidence
```

## Intake des résultats structurés

La sortie attendue est exactement l'un des contrats existants :
`ArchitectResult`, `ImplementerResult`, `TesterResult`, `ReviewerResult` ou
`CertifierResult`. Le rôle du request détermine un seul type admissible.

L'intake futur doit :

1. conserver la sortie brute et les observations de transport ;
2. décoder un résultat explicitement structuré, sans inférer un `PASS` depuis
   de la prose ;
3. refuser une sortie malformed, tronquée, multiple, contradictoire ou de rôle
   incorrect ;
4. réutiliser le validateur déterministe du RoleResult correspondant ;
5. revalider mission, génération, sujet, commit, worktree et scope contre
   l'état réel ;
6. remettre seulement le résultat validé au workflow existant.

Un RoleResult valide reste une proposition de rôle. Il ne devient ni Evidence,
ni Gate, ni Certification, ni autorisation de transition.

## Échecs, interruptions et effets de bord

- **Échec avant modification** : tentative en échec ; aucun succès implicite.
- **Fichiers modifiés puis crash** : exécution incomplète ; effets observés et
  inspection/recovery requis ; aucun statut READY fabriqué.
- **Commit puis crash avant RoleResult** : commit observé, mais absence de
  `ImplementerResult` validé ; aucune progression.
- **RoleResult valide avec Git/worktree divergent** : refus, car la réalité du
  repository prévaut.
- **Timeout, cancellation ou perte de session** : résultat non réussi ; état
  physique à reconstruire avant toute décision ou nouvelle tentative.

Le futur runtime doit persister une identité de tentative et un état
d'exécution suffisant pour distinguer jamais démarré, actif, terminé, échoué,
interrompu et état incertain. Un retry aveugle est interdit lorsque des effets
de bord sont possibles. La recovery doit d'abord réconcilier request, tentative,
Git, worktree et état autoritatif ; P4.1 ne l'implémente pas.

## Frontière Codex et VS Code

La séparation cible est :

```text
APPLICATION CONTRACT
→ CODEX INFRASTRUCTURE ADAPTER
→ VS CODE / ACTUAL TRANSPORT
```

Le domaine et l'application ne dépendent d'aucun détail d'interface VS Code.
L'adapter traduit le contrat applicatif vers des capacités Codex préalablement
vérifiées et rapporte honnêtement leurs limites. L'intégration de transport est
la seule couche qui connaît l'API ou l'interface réellement disponible.

Aucune API Codex, garantie de streaming, statut, cancellation ou reprise n'est
supposé avant capability discovery. Une capacité obligatoire absente produit
un blocage ou une réduction explicitement contractualisée, jamais une
simulation. Phase 4 reste mono-provider : aucun framework ou adapter générique
Claude, OpenHands, LangGraph, AutoGen ou similaire.

## Non-objectifs P4.1

- Invocation Codex ou intégration VS Code exécutable.
- Prompt Compiler, Context Builder, intake ou état d'exécution implémenté.
- Retry, timeout, cancellation ou recovery runtime.
- Modification du Control Plane, des rôles ou des workflows certifiés.
- Installation dans des repositories arbitraires, réservée à Phase 5.
- Gouvernance/observabilité de production, réservée à Phase 6.
- Autonomie multi-projet et généralisation finale, réservées à Phase 7.
