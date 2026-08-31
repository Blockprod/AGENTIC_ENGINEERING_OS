# Intégration sûre AGENTS.md

## Objet et API

`AgentsIntegrationService` est l'unique frontière d'inspection et d'écriture
de la section Agentic OS dans `AGENTS.md`. Son API est volontairement fermée :

- `inspect(content)` classe des octets bornés sans écrire ;
- `create_from_plan(root, *, planned_content)` crée uniquement le fichier
  canonique absent ;
- `integrate_from_plan(root, *, expected_fingerprint, planned_content)` ajoute
  uniquement la section canonique au fichier racine existant.

La cible est toujours `AGENTS.md`. Le contenu fourni doit être strictement
identique à la ressource canonique du produit ; il n'existe ni paramètre de
cible ni API de texte arbitraire. L'application normale suit exclusivement :

```text
RepositoryReconnaissance
→ InitializationPlanner
→ confirmation Human si requise
→ RepositoryInitializer
→ AgentsIntegrationService
→ vérification et nouvelle reconnaissance
```

## Format et version

La version courante du contrat géré est `2`. Ses marqueurs Markdown exacts
sont :

```text
<!-- BEGIN AGENTIC_ENGINEERING_OS MANAGED SECTION v2 -->
<!-- END AGENTIC_ENGINEERING_OS MANAGED SECTION v2 -->
```

La section contient uniquement les invariants repository-locaux nécessaires :
le rôle control/runtime de l'OS, la priorité de la vérité Git/repository, le
scope explicite, Human Authority, les mutations autoritatives contrôlées,
l'interdiction d'éditer directement l'état runtime et l'usage des contrats de
rôle, handoffs et `RoleResult`. Elle ne duplique ni prompts P4, ni roadmap, ni
historique ou certification interne.

## Classification fail-closed

Les quatre états d'usage sont :

- fichier absent : création canonique contrôlée ;
- fichier utilisateur sans marqueur réservé : ajout proposé avec confirmation
  Human exacte ;
- section v2 unique et canonique : `CURRENT`, donc `NO_OP` ;
- section modifiée, partielle, dupliquée, imbriquée ou marqueur ambigu : blocage.

Une section v2 au contenu différent est `TAMPERED`. Une paire exacte portant
une version ancienne ou future est `UPGRADE_REQUIRED`. Aucune réparation,
migration ou recherche floue n'est effectuée.

## Préservation et confirmation

Lors d'un ajout, les octets utilisateur existants restent un préfixe strictement
inchangé. La convention de newline observée en premier est utilisée pour la
nouvelle section ; LF, CRLF, Unicode et absence de newline finale sont pris en
charge sans reformater le contenu existant.

La confirmation Human lie exactement le fingerprint du plan, l'identifiant de
l'opération, `AGENTS.md`, l'état attendu et le SHA-256 des octets actuels. Toute
modification du fichier rend plan et confirmation obsolètes. Une identité
Codex, quelle que soit sa casse ou sa normalisation pertinente, est refusée.

## Écriture et vérification

Création et ajout utilisent un temporaire dans le même dossier, flush, fsync
et installation atomique. Une création est exclusive. Un ajout relit et
compare le fingerprint juste avant `os.replace`; un échec antérieur au
remplacement conserve les anciens octets. Après écriture, les octets et la
classification `CURRENT` sont vérifiés. Aucune mutation Git n'est réalisée.

## Hors scope P5.6

P5.6 ne migre aucune ancienne version, ne remplace aucune zone utilisateur, ne
bootstrappe aucun état runtime et ne fournit ni CLI ni fonctionnalité P5.7.
