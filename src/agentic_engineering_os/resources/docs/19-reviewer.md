# Reviewer V1

## Rôle et frontière avec Tester

Le Reviewer évalue indépendamment la qualité d'ingénierie après validation
comportementale par Tester. Il cherche les défauts que des tests verts ne
révèlent pas nécessairement : dette, complexité, duplication, couplage,
frontières d'autorité, changements inutiles et risques de faux positifs.

Il ne refait pas intégralement la campagne Tester. Un `TesterResult
READY_FOR_REVIEW` est une précondition, pas une preuve suffisante de qualité.
Le Reviewer analyse ; le runtime contrôle uniquement la structure et la
cohérence de son analyse.

## Entrée et éligibilité

`ReviewerInput` est construit depuis un handoff
`ORCHESTRATOR → REVIEWER` pour l'étape `REPORT`, une User Story, un
`ImplementerResult READY_FOR_TEST` et un `TesterResult READY_FOR_REVIEW`
cohérents. La User Story doit être `REVIEW` après la transition contrôlée
`TESTING → REVIEW`. Le Reviewer ne change jamais directement son statut.

Les quatre contextes doivent partager mission, génération active, sujet et
commit observé. Un snapshot déterministe bloque toute altération ultérieure de
la User Story ou des résultats précédents. `ReviewerResult` porte cette même
génération.

## Dimensions V1

Le catalogue fermé contient :

- `SCOPE` ;
- `ARCHITECTURE` ;
- `MAINTAINABILITY` ;
- `COMPLEXITY` ;
- `DUPLICATION` ;
- `TEST_QUALITY` ;
- `CONTRACT_COMPLIANCE` ;
- `AUTHORITY_SAFETY`.

Les huit dimensions sont obligatoires pour `READY_FOR_CERTIFICATION`.
`AUTHORITY_SAFETY` recherche notamment les bypass du Control Plane, mutations
autoritatives directes, contournements Human Authority, comportements
fail-open et routes injustifiées vers `PASS` ou `CERTIFIED`.

## Findings et sévérité

Chaque `ReviewFinding` identifie sa dimension, sa sévérité, un résumé, des
observations, les chemins affectés et son caractère bloquant. Les observations
ne deviennent pas automatiquement des Evidence Control Plane.

Les sévérités V1 sont :

- `INFO` : observation utile sans correction obligatoire ; elle ne bloque pas ;
- `MINOR` : défaut limité, bloquant ou non selon le contrat et son impact ;
- `MAJOR` : défaut important, avec décision de blocage explicite ;
- `CRITICAL` : menace grave pour le contrat ou l'autorité, obligatoirement
  bloquante en V1.

Sévérité et blocage restent deux champs distincts. Un `INFO` bloquant ou un
`CRITICAL` non bloquant est incohérent.

Les chemins sont des chemins POSIX relatifs, sans traversal ni ambiguïté. Tout
finding référence un chemin déclaré dans `reviewed_paths`. Un impact hors scope
peut être signalé s'il est explicitement examiné et lié au finding ; cette
capacité n'autorise aucune modification.

## Verdicts

- `READY_FOR_CERTIFICATION` exige les huit dimensions, tous les artefacts
  d'implémentation examinés, aucun finding bloquant et aucun blocker. La
  recommandation du rôle suivant n'est pas une Certification.
- `REMEDIATION_REQUIRED` exige au moins un finding bloquant démontrant une
  correction nécessaire.
- `BLOCKED` représente une impossibilité fiable de conclure et exige un
  blocker explicite. Il ne remplace pas une remédiation clairement démontrée.

## No silent fix et Human Authority

Le résultat Reviewer ne contient aucune liste de fichiers modifiés. Le
Reviewer ne répare ni métier, ni test, ni contrat : il rapporte le finding à
l'Orchestrator, qui pourra coordonner une future remédiation par l'Implementer.

Une décision Human requise doit être complète et attribuable avant la review.
L'identité réservée Codex, quelle que soit sa casse, ne peut pas fournir cette
décision. Le Reviewer peut constater une absence, jamais la combler ou la
convertir en succès.

## Flux et frontière déterministe

```text
Orchestrator
  -> RoleHandoff(to=REVIEWER)
  -> UserStory + ImplementerResult + TesterResult
  -> Codex / Reviewer
  -> ReviewerResult
  -> validation déterministe
  -> Orchestrator
```

Le runtime valide le JSON Schema Draft 2020-12, les contextes, les dimensions,
les paths, la cohérence severity/blocking et les règles verdict/findings. Il
n'invente aucun finding et ne décide pas heuristiquement si l'architecture est
bonne. Aucun appel automatique vers un rôle suivant ou de remédiation n'est
effectué ; aucune Evidence, transition, approbation ou Certification n'est
créée.
