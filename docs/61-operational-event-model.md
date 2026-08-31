# OperationalEvent Model

## Frontière

`OperationalEvent` représente un fait d'exploitation observable. Il est
immuable, strict, sérialisable et non autoritatif : il ne peut produire ni
Evidence, ni AuditEvent, ni Gate, ni Certification, ni approbation Human, ni
transition de User Story/Mission, ni autorisation de merge ou d'exécution.

Le module de domaine expose uniquement les modèles fermés, le parsing strict,
la sérialisation canonique et un fingerprint de contenu. Il n'expose aucun
store, handler de log, instrumentation ou conversion vers le Control Plane.

## Modèle canonique

Un événement contient :

- `schema_version`, actuellement `1.0` ;
- un `event_id` UUID canonique fourni par l'appelant ;
- un `event_type` et une `severity` issus de catalogues fermés ;
- `occurred_at`, obligatoirement UTC ;
- un `source_component` attribuable et un `project_id` explicite ;
- une `OperationalCorrelation` réutilisant les identités existantes ;
- un `OperationalEventPayload` fermé et borné ;
- une `OperationalProvenance` descriptive, sans autorité.

Le catalogue d'événements couvre les familles stables suivantes :

- `MISSION_LIFECYCLE` ;
- `ROLE_EXECUTION` ;
- `CODEX_EXECUTION` ;
- `WORKTREE_LIFECYCLE` ;
- `INTEGRATION_GATE` ;
- `MERGE_OPERATION` ;
- `CONTROL_PLANE_DECISION` ;
- `REMEDIATION_RECOVERY` ;
- `HUMAN_WAITING` ;
- `PERSISTENCE_FAILURE` ;
- `ADOPTION_MIGRATION` ;
- `OPERATIONAL_ANOMALY`.

Chaque famille accepte une petite liste d'opérations sémantiques. Il n'existe
pas un type distinct pour chaque ligne de log. Les sévérités sont `INFO`,
`WARNING`, `ERROR` et `CRITICAL`. Elles décrivent un impact observé et ne
déterminent jamais un statut métier, un résultat de Gate, une Certification ou
un futur Health status.

## Corrélation

`project_id` est toujours requis. Les références optionnelles sont
`mission_id`, `workflow_generation`, `user_story_id`, `role`, `execution_id`,
`assignment_id`, `wave_index`, `group_index`, `gate_id`, `certification_id` et
`repository_commit`.

Les invariants sont fail-closed :

- toute référence mission-scoped exige `mission_id` ;
- les familles de mission, rôle, Codex, worktree, intégration, merge,
  contrôle, remédiation et attente Human exigent mission et génération ;
- une exécution exige un rôle ; une exécution Codex exige aussi
  `execution_id` ;
- une affectation worktree exige une User Story ;
- wave et group sont présents ensemble ; Integration Gate et merge les
  exigent ;
- le commit, lorsqu'il existe, est un SHA-1 Git lowercase de 40 caractères.

Aucun workflow ID global parallèle n'est créé. Un futur trace ID ne pourra
être qu'un regroupement technique sans autorité.

## Payload et provenance

Le payload n'est pas un dictionnaire libre. Il contient une `operation`, puis
des champs optionnels fermés (`outcome`, `reason_code`, `duration_ms`,
`attempt`) et au plus 32 `OperationalAttribute` triés et uniques. Une valeur
d'attribut est uniquement un scalaire JSON fini ; objets, listes, bytes,
exceptions et nesting arbitraire sont refusés.

Les provenances sont `DETERMINISTIC_COMPONENT`, `GIT_OBSERVATION`,
`PROCESS_RUNTIME`, `CODEX_RUNTIME` et `OPERATOR_HUMAN`. Un producteur Codex
doit porter un rôle attribuable. Une provenance Human refuse les identités
Codex réservées. Même légitime :

```text
OPERATOR_HUMAN OperationalEvent != HumanApproval Evidence
CODEX_RUNTIME OperationalEvent   != Evidence
```

La provenance décrit l'observateur ; elle ne confère aucun pouvoir.

## AuditEvent séparé

Un service autoritatif peut enregistrer son `AuditEvent` dans `ProjectState`
et un producteur d'observabilité peut décrire séparément le même fait par un
OperationalEvent. Ils peuvent partager les IDs applicables, mais aucun n'est
converti implicitement en l'autre. Le modèle ne fournit aucune fonction
`to_evidence`, `to_audit_event`, `to_gate` ou `to_certification`.

## Identité et déterminisme

`event_id` est un UUID canonique fourni explicitement. Un UUID n'est pas
présenté comme déterministe et ne constitue aucune preuve d'autorité.
`canonical_operational_event_json` produit un JSON UTF-8 logique, Unicode non
échappé, clés triées et séparateurs stables. `operational_event_fingerprint`
calcule le SHA-256 de cette représentation. Ce fingerprint détecte une
différence de contenu ; il n'est ni une signature, ni une autorisation.

## Bornes et secrets

- événement sérialisé : 16 384 octets maximum ;
- attributs : 32 maximum ;
- chaîne scalaire : 2 048 caractères maximum ;
- durée : 604 800 000 ms maximum ;
- valeurs numériques non finies refusées ;
- texte NFC, sans caractères de contrôle.

Les noms sensibles (`environment`, credentials, token, secret, password,
stdout/stderr notamment), marqueurs de clés privées et formats de tokens
connus sont refusés. P6.2 ne prétend pas expurger heuristiquement un secret :
l'événement est rejeté et aucun objet valide ne conserve la matière détectée.

## Schéma et validation

`operational-event.schema.json` utilise JSON Schema Draft 2020-12,
`additionalProperties: false`, des enums fermés et des objets imbriqués dont
tous les champs sont requis, avec `null` explicite lorsqu'une corrélation ne
s'applique pas. `ContractValidator` applique ensuite les invariants de famille,
de corrélation, de canonicalité, de provenance, de taille et de secrets en
reconstruisant strictement le modèle. Aucune coercion ni valeur par défaut
silencieuse n'est appliquée.

## Hors scope P6.2

P6.2 ne persiste, n'émet et ne collecte aucun événement. Il n'ajoute ni Event
Store P6.3, ni logging handler, métrique, health, policy, incident, CLI ou
instrumentation automatique.
