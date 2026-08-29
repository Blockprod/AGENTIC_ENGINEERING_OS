# Deterministic Context Builder

## Portée

P4.2 fournit une projection applicative reconstructible entre l'état
autoritatif et une future compilation de mission Codex. Il ne compile aucun
prompt, n'invoque pas Codex et ne décide d'aucune transition.

```text
CodexExecutionRequest + stores + Git/worktree + sources explicites
→ ContextBuilder
→ ExecutionContext(authoritative, cognitive)
```

`ExecutionContext` est immuable. Il n'est pas persisté : il est recalculé à
partir des stores et observations existants afin d'éviter une seconde source de
vérité et tout problème de migration. Aucun schéma ni dépendance n'est ajouté.

## Bindings fail-closed

Le builder exige une correspondance exacte entre le request, `MissionState`,
`ProjectState`, Git et, pour Implementer, `WorktreeRegistry` et l'inspection
physique. Mission, génération, rôle, sujet/User Story, étape, repository,
commit, scope, contrat de rôle et RoleResult attendu sont comparés. Un état
absent, stale, ambigu, bloqué, dirty ou cross-context est refusé.

Les RoleResults upstream requis sont validés par les contrats déterministes
existants, puis liés à la mission, la génération, le sujet, la User Story et le
commit courants. Les dépendances d'une User Story doivent disposer d'une
Certification unique `CERTIFIED`. Le builder ne crée ni Evidence, Gate,
Certification, Human Approval ou mutation autoritative.

## Politiques fermées par rôle

- `ARCHITECT` : mission, repository, contrats et architecture ; aucune User
  Story, worktree ou écriture autorisée.
- `IMPLEMENTER` : User Story et scope exacts, assignment/worktree actif et
  propre, baseline et Certifications des dépendances.
- `TESTER` : User Story, critères d'acceptation et `ImplementerResult` valide.
- `REVIEWER` : User Story, `ImplementerResult` et `TesterResult` valides.
- `CERTIFIER` : chaîne complète Architect→Reviewer, Evidence, Gates et contexte
  Human exacts du sujet.

Les références communes incluent `AGENTS.md`, les invariants, les frontières
d'autorité, la politique fail-closed et le contrat P4.1. Le contrat opérationnel
et le document d'architecture propres au rôle sont également empreintés.

## Contexte cognitif

Les sources cognitives sont des fichiers Markdown explicitement déclarés sous
`docs/` ou `roles/` (plus `README.md`). Chaque descripteur indique catégorie,
rôles et, si nécessaire, sujets et préfixes de composants. La pertinence est
une intersection exacte de ces métadonnées ; l'ordre et la déduplication sont
canoniques.

Les chemins absolus, traversals, symlinks, fichiers trop grands, encodages non
UTF-8, caches/runtime et noms ressemblant à des secrets sont refusés. Les
documents d'autorité ne peuvent pas être reclassés comme cognitifs. Un contenu
cognitif reste dans la collection `cognitive`, même s'il contient des champs
ressemblant à une autorité : il ne remplace jamais les entrées
`authoritative`, leurs sources ou leurs empreintes.

## Déterminisme et limites

La sélection utilise uniquement égalité, appartenance, préfixes de chemins,
tri canonique et SHA-256. Elle n'utilise ni LLM, embeddings, recherche
sémantique, vector database ou conversation. Les validateurs et services P1–P3
restent les seules autorités de validation et de décision.

P4.2 ne fournit pas le Prompt Compiler P4.3, l'adapter Codex, l'intake de
résultat, l'exécution, le retry ou la recovery.
