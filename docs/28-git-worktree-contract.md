# Git Worktree Isolation Contract

## Rôle et frontière d'autorité

Le **primary worktree** est le checkout principal et le point d'intégration
contrôlé du repository. Sa branche et sa baseline sont toujours fournies
explicitement ; ce contrat ne suppose pas que la branche principale s'appelle
`main`. Un Implementer parallèle ne travaille jamais directement dans le
primary worktree.

Chaque Implementer parallèle travaille dans un worktree Git dédié, sur une
branche dédiée et depuis un commit explicite. Cette isolation sépare les
répertoires de travail ; elle n'élargit pas `UserStory.scope`, ne crée aucune
autorité métier et ne vaut ni permission d'exécution, ni Evidence, ni
certification, ni approbation d'intégration.

P3.6 ne crée aucun worktree et n'exécute aucune commande Git. Le futur
Worktree Manager de P3.7 sera l'unique frontière opérationnelle des opérations
worktree/branch du workflow parallèle. Les rôles cognitifs ne pourront pas le
contourner par des commandes Git arbitraires.

## WorktreeAssignment conceptuel

Une affectation contient uniquement :

- `assignment_id` ;
- `mission_id` ;
- `user_story_id` ;
- `workflow_generation` ;
- `baseline_commit` ;
- `branch_name` ;
- `worktree_path` ;
- `status` ;
- `result_commit`, absent jusqu'à ce qu'un résultat commité existe.

Une affectation appartient exactement à une mission, une User Story, une
génération, une baseline, une branche et un chemin. `baseline_commit` et
`result_commit` sont des SHA Git complets de 40 caractères hexadécimaux ; ils
ne désignent pas un HEAD implicite.

L'identité V1 est déterministe : `assignment_id` suit
`wa-<24 caractères hexadécimaux minuscules>`, dérivés du préfixe SHA-256 de
l'encodage UTF-8, Unicode NFC, délimité par longueur du tuple `(mission_id,
user_story_id, workflow_generation décimal, baseline_commit minuscule)`. Le
nom ne révèle donc pas le texte de la mission. Une collision avec un tuple
différent bloque fail-closed ; elle n'est jamais résolue par écrasement. Le
même tuple identifie la même affectation à reprendre, pas une seconde
ressource. Les identifiants contractuels utilisés par cette dérivation ne
doivent eux-mêmes contenir aucun secret ou libellé sensible.

## Lifecycle

Le catalogue fermé est :

- `PLANNED` : identité réservée, aucune ressource Git réputée créée ;
- `ACTIVE` : branche et worktree existent, correspondent au record et sont
  affectés à l'Implementer ;
- `COMPLETED` : résultat entièrement commité et worktree propre, sans décision
  d'intégration implicite ;
- `FAILED` : création ou exécution échouée, ressources et artefacts conservés
  pour diagnostic ;
- `CLEANED` : worktree physique retiré après contrôles ; la branche et
  l'historique du record restent des objets distincts.

Les seules transitions conceptuelles sont :

```text
PLANNED -> ACTIVE
PLANNED -> FAILED
ACTIVE  -> COMPLETED
ACTIVE  -> FAILED
COMPLETED -> CLEANED
FAILED    -> CLEANED
```

`FAILED` n'est pas réactivé et `CLEANED` ne redevient jamais `ACTIVE`. Une
remediation crée une nouvelle affectation, normalement dans la génération
suivante, afin de préserver l'historique.

## Invariants d'identité et d'exclusivité

- `assignment_id` est unique dans le registre du repository.
- Une affectation ne référence qu'une User Story et qu'une génération.
- Toute affectation possède une baseline explicite.
- Parmi toutes les affectations non `CLEANED`, `branch_name` et
  `worktree_path` sont chacun uniques. Cela interdit notamment deux
  affectations actives sur la même branche ou le même répertoire physique.
- Une User Story ne partage jamais un worktree actif avec une autre User
  Story.
- La relation active est strictement `one assignment -> one branch -> one
  worktree`.
- Une affectation terminale n'est pas réutilisée comme `ACTIVE`.
- Une génération stale ne peut pas être reprise.
- `COMPLETED` exige `result_commit`; les fichiers seulement dirty ne
  suffisent pas.
- `CLEANED` est irréversible.

Une violation ou une information manquante produit `BLOCKED`, sans réparation
silencieuse.

## Baseline et cohérence d'une exécution parallèle

Le futur coordinateur choisit la baseline avant toute affectation. Le futur
manager devra vérifier que le SHA existe et résout exactement un objet commit,
par une primitive équivalente à `rev-parse --verify <sha>^{commit}`, avant de
créer la branche ou le worktree. L'Implementer ne choisit ni ne remplace cette
baseline.

Toutes les affectations d'une même exécution parallèle/Wave autorisée partent
du même `baseline_commit`. Toute divergence reste visible dans les records et
bloque le lancement groupé ; elle n'est ni rebased ni corrigée implicitement.
Après un changement autoritatif de baseline, l'ancienne affectation est stale.

## Conventions de branche et de chemin

La branche V1 suit :

```text
agentic/g<generation>/<user-story-id-minuscule>-<suffixe assignment_id>
```

Par exemple : `agentic/g3/us-0042-0123456789abcdef01234567`. Les composants
sont ASCII, sans espace, secret ou texte de mission. Le futur manager doit
encore appliquer la validation Git native de type `check-ref-format`, une
limite V1 de 120 octets UTF-8 et une comparaison anti-collision avec
`casefold()` avant création. Une branche existante est refusée, sauf reprise
exacte et validée de la même affectation.

Le futur manager reçoit explicitement une racine absolue dédiée au repository,
située hors de la racine versionnée du primary worktree. Le chemin déterministe
est :

```text
<worktree_root>/<assignment_id>
```

Aucun chemin n'est hardcodé à `D:\DEV`. La racine et le chemin cible absolu
sont résolus avant usage et le chemin résolu est enregistré ; la cible ne peut
être ni dans le primary worktree, ni alias d'un autre worktree. Les composants
générés restent portables. Les
comparaisons tiennent compte de la normalisation Unicode, des séparateurs
natifs et des collisions insensibles à la casse de Windows, sans rendre le
contrat Windows-only. Un chemin dépassant la limite sûre configurée pour la
plateforme est refusé, sans raccourcissement ambigu.

Avant création, le futur manager devra vérifier cumulativement : repository
Git valide et primary worktree reconnu, baseline résolue comme commit, branche
d'affectation absente ou conforme à une reprise exacte, chemin cible absent ou
déjà enregistré comme ce même worktree, unicité dans Git et le registre, et
état du primary compatible avec l'opération. Une condition inconnue bloque.

## Génération, conflit et scope

`workflow_generation` est obligatoire et non négatif. Une affectation de
génération N devient stale dès que la mission autoritative passe à N+1 ; son
worktree peut rester physiquement présent mais ne peut pas autoriser un resume
ou une nouvelle exécution. Il n'est jamais supprimé automatiquement.

P3.6 ne décide pas quelles User Stories s'exécutent ensemble. Une future
politique P3.8 doit avoir validé la compatibilité avant affectation parallèle :
`CONFLICT` interdit l'exécution simultanée standard et `UNKNOWN` la bloque
fail-closed. `SAFE` ne crée jamais automatiquement un worktree.

Dans chaque worktree, l'Implementer reste limité aux `allowed_paths`,
`forbidden_paths` et autres contrôles de son `UserStory.scope`. Isolation Git
ne signifie jamais autorisation de modifier tout le repository.

## État dirty et commit de résultat

Avant une opération Git structurante, le futur manager vérifie le repository,
le commit, les refs, la liste réelle des worktrees, le chemin cible et l'état
du primary worktree. Un primary worktree comportant des changements inattendus
ou non autorisés bloque l'opération. Aucun `git stash` automatique n'est
permis.

Un worktree `ACTIVE` peut être dirty pendant l'implémentation, mais cet état
doit être observé et rapporté. Il ne peut pas devenir `COMPLETED` avant que
tous les changements de résultat soient préservés dans un commit explicite et
que le worktree soit propre. `result_commit` doit être le tip attendu de la
branche dédiée, être descendant de `baseline_commit` et rester attribuable à
l'affectation, la User Story et la génération.

L'Implementer ne merge pas dans le primary, ne rebase pas arbitrairement sa
branche, ne force-push pas et ne supprime ni branche ni worktree. `COMPLETED`
signifie seulement que le travail isolé est commité ; il ne signifie jamais
`approved_for_merge`.

## Resume, stale et divergence

À la reprise, une affectation `ACTIVE` n'est reconnue que si le record, la
branche, le chemin, la baseline, la mission, la User Story et la génération
correspondent exactement à Git et à l'état de mission courant. Le worktree doit
être listé par Git au chemin attendu avec la branche attendue. Son état dirty
est observé, pas effacé. Le manager réutilise alors ce worktree au lieu d'en
créer un second.

Une branche ou un chemin divergent, une ref absente, une baseline différente,
une génération stale ou un registre ambigu bloque fail-closed. Une ressource
physique orpheline n'est ni adoptée ni supprimée automatiquement.

## Cleanup et opérations destructrices

Le cleanup exige cumulativement : affectation `COMPLETED` ou `FAILED`, aucun
changement non préservé, `result_commit` connu pour `COMPLETED`, aucune
intégration en cours et confirmation explicite que le worktree n'est plus
nécessaire. Un worktree `FAILED` est conservé tant que ses artefacts peuvent
servir au diagnostic.

Retirer un worktree et supprimer sa branche sont deux opérations séparées. Une
branche portant un résultat non intégré n'est jamais supprimée silencieusement.
Par défaut, le workflow interdit l'automatisation de `git reset --hard`,
`git clean -fd`, checkout forcé, retrait forcé de worktree, suppression forcée
de branche et force-push. Toute future exception devra être explicitement
contractualisée ; lorsqu'elle risque de détruire du travail non intégré,
l'autorisation Human applicable doit être réelle et ne peut pas être inventée
par Codex.

## Git et registre persistant

Une ressource externe doit survivre aux redémarrages. P3.7 devra donc fournir
un registre persistant minimal, avec
`.agentic-engineering-os/worktrees.json` comme emplacement conceptuel possible.
P3.6 ne crée ni ce fichier, ni son schéma, ni son store ; P3.7 fixera le format
et l'emplacement exacts.

Le registre représente l'état opérationnel attendu. La sortie physique de Git
— refs et liste des worktrees — représente la réalité du repository. Ni l'un
ni l'autre ne suffit seul : toute opération compare les deux, et toute
divergence bloque fail-closed. Le registre ne remplace pas Git et Git ne crée
pas implicitement une ownership métier absente du registre.

Les primitives futures minimales sont `worktree list`, `worktree add`, création
et inspection de branche, `rev-parse`, `status` et `worktree remove`. P3.6 ne
définit ni wrapper Git générique, ni subprocess, ni mutation filesystem.

## Relations avec les étapes futures

- **Implementer** : reçoit une affectation prévalidée et travaille uniquement
  dans son worktree et son scope ; `ImplementerInput` reste inchangé en P3.6.
- **P3.8 Parallel Implementer Coordinator** : sélectionnera uniquement des
  affectations isolées et compatibles selon la politique de conflits.
- **P3.9 Integration Gate** : inspectera baseline, result commit, fichiers
  modifiés, tests et compatibilité entre résultats.
- **P3.10 Merge Coordinator** : détiendra la responsabilité d'intégrer dans le
  primary worktree. Aucun Implementer ne merge directement.

P3.6 ne réalise aucune de ces opérations et ne crée ni manager, worktree,
scheduler, lock, groupe parallèle, gate d'intégration ou moteur de merge.
