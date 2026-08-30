# Codex Execution State & Restart Recovery

## Portée et autorité

P4.7 ajoute une mémoire opérationnelle repository-locale autour du transport
P4.5 et de l'intake P4.6. Elle n'orchestre aucun rôle et n'autorise aucune
transition de User Story ou MissionState, Evidence, Gate, Certification ou
Human Approval. `CODEX EXECUTES. CONTROL PLANE DECIDES.`

## Identité et modèle persistant

Chaque record lie exactement request, empreinte de contexte, mission,
génération, rôle, sujet, repository/worktree, cwd, commit attendu, empreinte du
prompt compilé, contrat de résultat et identité path/version/SHA-256 de
l'exécutable. Son identifiant déterministe combine l'identité de request et
l'empreinte sémantique ; les collisions de request, d'identifiant ou de
sémantique sont refusées.

Le lifecycle persistant est fermé :

```text
PLANNED → RUNNING → OBSERVED → VALIDATED
                  ↘ FAILED
                  ↘ INTERRUPTED
```

`OBSERVED` signifie seulement qu'une observation de transport durable existe.
`VALIDATED` exige un RoleResult accepté par P4.6 ; un exit code zéro ne suffit
jamais. `FAILED` et `INTERRUPTED` conservent l'observation et leurs raisons sans
fabriquer de succès.

## Store et ordre d'écriture

Le ledger versionné `1.0` est
`.agentic-engineering-os/executions.json`. Il utilise JSON strict, refuse les
clés dupliquées et constantes non JSON, hydrate sans fallback implicite,
valide les identités et écrit atomiquement dans le même répertoire. Les records
sont triés canoniquement. Il n'expose aucun `save(snapshot)` public : chaque
mutation doit correspondre exactement à une capacité interne et à une
transition nommée.

L'ordre durable est : persist `PLANNED`, persist `RUNNING`, lancer le runtime,
persist l'observation, exécuter P4.6, puis persist le résultat. Un échec
d'écriture conserve le dernier état durable. Ainsi un crash après `RUNNING`
reste incertain et un crash après `OBSERVED` permet de rejouer uniquement
l'intake.

La politique par défaut limite chaque flux brut à 1 000 000 caractères et la
représentation complète d'une observation à quatre fois cette limite ; le
ledger est limité à 16 000 000 octets. L'observation P4.5 structurée est
conservée sans réinterprétation. Un RoleResult validé est sérialisé
canoniquement avec SHA-256 puis revalidé par P4.6 après hydration avant d'être
reconnu comme déjà traité.

## Reconstruction et retry

La classification de restart est déterministe :

- `SAFE_NOT_STARTED` : intent `PLANNED` et baseline Git exacte/propre ;
- `INTAKE_REPLAY_AVAILABLE` : observation durable et Git identique au post-état ;
- `VALIDATED_NO_RERUN` : résultat persisté, revalidé et Git cohérent ;
- `NEW_REQUEST_REQUIRED` : tentative infructueuse sans drift prouvé, jamais un
  droit de rejouer la même request ;
- `RECOVERY_REQUIRED` : effet Git possible ou observé, intervention requise ;
- `STALE_OR_INCONSISTENT` : binding, exécutable, génération, prompt, store,
  résultat ou Git non vérifiable.

Le flag de retry aveugle reste toujours faux. Une exécution `RUNNING` perdue,
un timeout, une interruption ou un crash après commit ne sont jamais assimilés
à une absence d'effet. P4.7 ne réalise aucun rollback Git et ne crée aucun
scheduler de retry.

## Réconciliation Git

HEAD, propreté et erreur P4.5 suffisent à la décision conservatrice : tout
HEAD différent, worktree dirty ou état indisponible bloque. L'observer exclut
uniquement son propre ledger opérationnel et son temporaire atomique du calcul
de propreté ; aucune autre modification n'est masquée. Les chemins modifiés ne
sont pas nécessaires car P4.7 n'essaie ni d'attribuer ni de réparer un effet.

## Limites

Le statut d'un processus vivant après perte du processus parent n'est pas
reconstruit : un record `RUNNING` est traité comme outcome incertain. La
reprise de session Codex, l'orchestration mono-rôle P4.8, le parallélisme et les
boucles autonomes restent hors scope.
