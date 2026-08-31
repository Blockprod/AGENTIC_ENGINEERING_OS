# Operational Event Store

## Frontière

`OperationalEventStore` conserve des observations P6.2 dans un journal local,
append-oriented et non autoritatif. Il ne remplace ni `ProjectStateStore`, ni
`MissionStateStore`, ni les AuditEvents, Evidence, Gates ou Certifications.
Une perte ou une corruption du journal réduit l'observabilité ; elle ne permet
jamais de reconstruire ou d'inventer un état métier.

## Emplacement et format

Le store utilise exclusivement le répertoire repository-local :

```text
.agentic-engineering-os/operational-events/
  segment-000001.jsonl
  segment-000002.jsonl
```

Ce répertoire volatile est couvert par le contrat Git-ignore géré par P5. Le
store n'accepte aucun chemin de journal configurable hors repository et refuse
les racines non absolues, symlinks, junctions Windows et entrées inattendues.

JSON Lines a été retenu plutôt qu'une base SQL : chaque ligne constitue une
frontière de record explicite, reste inspectable en UTF-8 et permet un append
simple sans introduire de dépendance. Chaque record contient exactement :

- `record_version`, actuellement `1.0` ;
- le fingerprint SHA-256 canonique ;
- l'`OperationalEvent` canonique complet.

Les clés sont triées, les séparateurs sont stables, Unicode n'est pas échappé
et chaque record se termine par un unique LF. L'ordre de lecture est l'ordre
des segments puis l'ordre d'append des lignes. Il s'agit d'un ordre observé,
pas d'une preuve d'ordre causal.

## API

- `append(event)` revalide un `OperationalEvent`, vérifie le journal existant,
  exclut les doublons et retourne un `OperationalEventAppendReceipt` seulement
  après `flush` et `fsync` réussis ;
- `read()` retourne un tuple immuable d'observations dans l'ordre d'append ;
- `query(OperationalEventQuery(...))` applique uniquement des filtres exacts
  sur type, sévérité, projet, mission ou exécution ;
- `StructuredEventLogger.record(event)` est une façade typée vers `append`.

Il n'existe aucune API `info(message, dict)` ou équivalent libre dans cette
façade. L'appelant construit d'abord un événement factuel valide.

## Validation et durabilité

La frontière d'append sérialise puis reconstruit l'événement et applique le
schéma packagé avec `ContractValidator`. Cette revalidation refuse notamment
un objet forgé après construction, un secret, une valeur surdimensionnée ou
une structure incompatible.

Sous succès, tous les octets du record ont été écrits et le fichier actif a
été synchronisé avec `fsync`. Une erreur avant modification produit
`WRITE_FAILED`. Une taille de fichier modifiée sans confirmation durable
produit `DURABILITY_UNKNOWN`. Aucun de ces cas ne retourne de receipt ni ne
prétend que l'append a réussi.

## Corruption et écritures partielles

La lecture est fail-closed sur :

- JSON ou UTF-8 invalide ;
- clés JSON dupliquées ou constantes non JSON ;
- record vide, tronqué, sans LF ou en CRLF ;
- version inconnue ou champs inconnus/manquants ;
- événement invalide ou fingerprint incohérent ;
- `event_id` dupliqué ;
- segment absent dans la séquence, surdimensionné ou inattendu.

`OperationalEventStoreError` conserve un code, le segment et, lorsque
possible, le numéro de ligne. Aucun record corrompu n'est sauté et aucune
réparation automatique n'est tentée. Un append commence par lire strictement
le journal : une corruption existante bloque donc aussi toute nouvelle ligne.

## Doublons et replay

`event_id` est unique dans tous les segments. Sa réinsertion est refusée, y
compris après redémarrage. Deux IDs différents ne sont pas dédupliqués selon
leur contenu ou une ressemblance métier. Le fingerprint détecte une altération
du contenu ; il ne confère aucune autorité et ne remplace pas l'identité.

## Concurrence V1

V1 est un modèle single-writer coopératif :

- un verrou de thread sérialise les appels d'une instance ;
- `.writer.lock`, créé exclusivement, détecte un autre writer coopératif ;
- lecture et append refusent un verrou concurrent ou stale ;
- aucun support multi-process concurrent n'est revendiqué.

Le verrou stale n'est jamais supprimé automatiquement. Un processus hostile
ignorant le protocole reste hors du modèle de concurrence V1.

## Rétention et rotation

Par défaut, un segment est limité à 1 048 576 octets et le store à quatre
segments. Un record individuel est borné par le contrat OperationalEvent plus
l'enveloppe du store. Lorsque le segment actif ne peut contenir le prochain
record, un segment numéroté suivant est créé. Lorsque la limite est atteinte,
`RETENTION_LIMIT_REACHED` bloque l'append.

Aucun segment, actif ou historique, n'est supprimé, écrasé ou archivé
silencieusement. Une politique future pourra organiser l'archivage, mais elle
n'est pas anticipée ici.

## Séparation d'autorité

Le store et sa façade ne fournissent aucune conversion vers Evidence,
AuditEvent, Gate ou Certification, aucune transition de User Story/Mission et
aucune récupération de `ProjectState`. Une requête retourne uniquement les
observations persistées ; elle ne calcule jamais `PASS`, `CERTIFIED` ou une
décision Human.

## Hors scope P6.3

P6.3 n'ajoute aucune instrumentation générale des composants P0–P5, métrique,
health model, policy, incident management, dashboard ou réparation automatique.
