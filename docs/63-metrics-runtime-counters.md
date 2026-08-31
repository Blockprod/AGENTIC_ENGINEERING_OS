# Metrics & Runtime Counters

## Frontière

`MetricsEngine` dérive des observations P6.2/P6.3 des snapshots factuels,
recalculables et non autoritatifs. Une métrique ne mute aucun état, ne produit
aucune Evidence, ne décide aucun Gate, merge, exécution, approbation Human ou
Certification. La règle reste : `METRIC != AUTHORITY`.

P6.4 n'ajoute ni persistance de métriques, ni collector en arrière-plan, ni
exporter, dashboard, health model ou governance policy.

## API et complétude de la source

- `compute(events, scope, source_complete=...)` accepte seulement un corpus
  borné d'`OperationalEvent`, un `MetricsScope` typé et une déclaration de
  complétude explicite ;
- `compute_from_store(event_store, scope)` lit strictement le store P6.3 et
  vérifie son état de rétention avant tout calcul.

Une source déclarée incomplète ne produit aucune valeur. Une source complète
et vide permet des compteurs exacts à zéro ; les ratios et durées sans faits
restent indisponibles. L'appelant ne peut fournir ni nom de métrique ni label
libre.

## Snapshot

`MetricsSnapshot` est immuable et contient les versions de schéma et de
catalogue, son scope exact, le nombre et les bornes des événements retenus, un
fingerprint SHA-256 déterministe, les `MetricSample` ordonnés et des diagnostics
bornés. Son statut fermé est `COMPLETE`, `INCOMPLETE` ou `UNAVAILABLE`.

Le fingerprint lie le scope et les fingerprints canoniques des événements ;
il sert à expliquer/reproduire le calcul, pas à conférer une autorité. Aucun
`generated_at` n'est ajouté, car le calcul ne dépend pas de l'heure courante.

## Catalogue fermé V1

Le catalogue versionné expose uniquement quatre types : `COUNTER`, `GAUGE`,
`DURATION_SUMMARY` et `DERIVED_METRIC`. Les unités sont `COUNT`, `MICROSECONDS`
et `RATIO`.

Ses 33 noms stables couvrent : missions ; exécutions de rôles et Codex ;
interruptions et timeouts ; remédiations et recovery ; attentes Human ;
worktrees ; résultats de Gates ; merges ; défaillances de persistance ;
adoptions/migrations ; durées de rôle, Codex et attente Human ; taux d'échec de
rôle et Codex. Chaque entrée fixe son type, son unité et sa règle de dérivation.

Les compteurs comptent exclusivement des catégories et opérations explicites
du corpus. Les taux ont besoin d'au moins un terminal observé. Les gauges
`worktrees.active` et `human_waits.active` sont calculées seulement lorsque les
cycles observés ne sont pas ambigus ; elles ne reconstruisent pas un
`ProjectState`.

## Corrélation, scopes et durées

Les dimensions sont fermées et hiérarchiques : projet, mission, génération,
User Story, rôle et exécution. Le projet est obligatoire ; une dimension de
mission exige un `mission_id`, et une exécution exige un rôle. Le mélange de
projets est refusé. Le corpus est limité à 10 000 événements et chaque
dimension corrélée observée à 1 024 valeurs distinctes.

Une durée utilise uniquement une paire corrélée appartenant au même contexte :

- rôle : `STARTED` vers `FINISHED` ou `FAILED` ;
- Codex : `STARTED` vers `FINISHED`, `FAILED` ou `INTERRUPTED` ;
- attente Human : `WAITING_STARTED` vers `WAITING_FINISHED`.

La valeur est un résumé exact en microsecondes (`count`, total, minimum,
maximum). Double start, double terminal, terminal sans start, fin antérieure au
start ou cycle ouvert produisent un diagnostic et rendent la durée concernée
indisponible. Aucune durée n'est extrapolée avec l'heure courante.

## Diagnostics et EventStore

Événement dupliqué, mélange de projets, cardinalité excessive ou corpus
incomplet rendent le snapshot `INCOMPLETE` sans fausses valeurs. Une anomalie
de cycle ou un résultat de Gate non classifiable rend le snapshot incomplet et
les métriques affectées indisponibles.

Corruption, troncature, verrou conflictuel ou indisponibilité du store rendent
le snapshot `UNAVAILABLE`. Une saturation de rétention, persistée par le
marqueur P6.3 `.retention-exhausted`, le rend `INCOMPLETE`. Dans ces cas, aucune
métrique apparemment complète et aucun zéro artificiel ne sont retournés.

## Sécurité

Noms, types, unités et dimensions proviennent des contrats fermés. Les scopes
refusent les identifiants non canoniques, les formes usuelles de secrets et les
valeurs à forte cardinalité. Payloads libres, stdout/stderr et variables
d'environnement ne deviennent jamais des noms ou labels de métriques.
