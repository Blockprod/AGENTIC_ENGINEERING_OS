# Structured Codex Result Intake

## Portée

P4.6 ajoute uniquement la frontière applicative entre l'observation technique
P4.5 et les cinq validateurs de rôles existants :

```text
CompiledPrompt + CodexExecutionObservation + validation context
→ CodexResultIntake
→ ResultIntakeOutcome
```

`CODEX EXECUTES. CONTROL PLANE DECIDES.` Un résultat accepté signifie seulement
qu'un `RoleResult` canonique a été obtenu et validé pour cette exécution. Il ne
constitue ni Evidence, ni Gate, ni transition, ni Certification.

P4.6 n'ajoute aucune persistance, tentative/restart P4.7, orchestration P4.8,
retry ou mutation d'état autoritatif.

## API et binding

`CodexResultIntake.process(compiled_prompt, observation, validation_context)`
retourne un `ResultIntakeOutcome` immuable. Le `ResultIntakeValidationContext`
contient exactement l'input existant du rôle et le chemin du schéma sélectionné
pour l'exécution. Pour Architect, les identifiants de User Stories déjà connus
peuvent aussi être fournis au validateur existant.

Avant tout parsing métier, l'intake compare :

- request et context fingerprint de l'observation ;
- mission, génération, rôle, sujet et commit du `CompiledPrompt` avec l'input
  de rôle ;
- repository/worktree compilé avec le cwd observé et l'unique `-C` ;
- contrat attendu avec le mapping fermé du rôle ;
- HEAD/propreté pré-exécution avec le commit compilé ;
- canal stdin, JSONL et schéma exacts de l'invocation.

Une valeur du payload Codex ne peut pas remplacer ces bindings.

## Source structurée canonique

La seule source de `RoleResult` admise est le texte d'un unique événement JSONL
`item.completed` dont l'item est `agent_message`, lorsque l'invocation prouve
simultanément un `--output-schema` explicite égal au schéma attendu.

Le dernier message libre n'est donc pas automatiquement un résultat structuré.
L'intake refuse un schéma absent ou multiple, plusieurs messages terminés, une
contradiction avec `final_output`, une ligne JSONL invalide, une troncature ou
un payload absent. Le schéma transporté ne devient pas une autorité : le
payload est revalidé avec le `ContractValidator` repository-local.

## Parsing et cinq rôles

Le JSON est reparsé au boundary applicatif. Les clés dupliquées, racines non
object, champs absents ou supplémentaires, enums et types incorrects sont
refusés sans coercition. En particulier, booléens et entiers restent distincts,
la prose `PASS` n'est pas interprétée et aucun identifiant, commit ou chemin
n'est corrigé.

Le mapping est fermé :

| Rôle | Modèle et validateur réutilisés |
|---|---|
| `ARCHITECT` | `ArchitectResult` / `ArchitectResultValidator` |
| `IMPLEMENTER` | `ImplementerResult` / `ImplementerResultValidator` |
| `TESTER` | `TesterResult` / `TesterResultValidator` |
| `REVIEWER` | `ReviewerResult` / `ReviewerResultValidator` |
| `CERTIFIER` | `CertifierResult` / `CertifierResultValidator` |

Après validation du schéma, l'intake construit le modèle canonique existant et
appelle son validateur avec l'input autoritatif existant. Aucun
`GenericRoleResult` et aucune duplication de règle métier ne sont introduits.

## Processus, transport et diagnostics

Les couches restent distinctes :

```text
PROCESS → TRANSPORT → STRUCTURED PAYLOAD → ROLE VALIDATION
```

Exit non-zero, timeout, interruption, tool failure, flux tronqué, JSONL invalide
ou issue technique non reconnue empêchent l'acceptation, même si le texte
ressemble à un résultat valide. Stderr seul est conservé comme diagnostic et
n'est pas une preuve d'échec métier lorsque le reste du transport est complet.

`ResultIntakeOutcome` distingue acceptation, rôle, résultat validé, raisons de
refus structurées et diagnostics de transport. Il ne contient aucune méthode de
persistance, d'enregistrement d'Evidence, d'évaluation de Gate ou de transition.

## Cohérence Git et effets de bord

Une observation Git complète est obligatoire avant et après l'exécution. Le
commit déclaré doit correspondre au HEAD post-exécution. Architect, Reviewer et
Certifier sont traités comme read-only et toute dérive est refusée.

Pour Implementer et Tester, une déclaration non vide de fichiers modifiés exige
un état post-exécution dirty ; réciproquement, un état dirty sans fichiers
déclarés est contradictoire. La P4.5 n'expose pas la liste Git détaillée : P4.6
peut donc vérifier présence/absence d'effets et commit, mais pas comparer chaque
chemin déclaré au diff physique. Cette limite reste explicite et n'est pas
comblée par une supposition ou un second WorktreeManager.

## Autorité, schéma et persistance

L'intake n'appelle jamais `EvidenceRecorder`, `GateEvaluator`,
`CertificationService`, `HumanApprovalService`, `StateTransitionService`, les
Control Loops ou les stores. Les claims Human restent soumis aux validateurs de
rôle et un Certifier ne peut pas retourner `CERTIFIED`.

Aucun schéma ni dépendance n'est ajouté : les cinq schémas V1 existants sont
réutilisés. Aucun résultat ou outcome n'est persisté ; l'identité d'attempt, le
restart et la réconciliation durable restent exclusivement P4.7.
