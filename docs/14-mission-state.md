# État persistant de mission

## Finalité et emplacement

`MissionState` est la mémoire opérationnelle minimale d'une mission Codex. Il
permet de reprendre un travail en conservant son objectif, son étape courante,
ses blocages et la prochaine action recommandée. Il est persisté en JSON UTF-8
dans un repository explicitement fourni :

```text
.agentic-engineering-os/
├── state.json
└── mission.json
```

`mission.json` et `state.json` sont indépendants. Le premier décrit le travail
en cours ; le second reste l'état autoritatif du projet.

## Contrat

Le document suit `schemas/mission-state.schema.json`, JSON Schema Draft
2020-12. Toutes ses propriétés sont requises et les propriétés inattendues sont
refusées :

- `schema_version` : version du format, actuellement `1.0` ;
- `mission_id` : identifiant non vide de la mission ;
- `status` : `ACTIVE`, `BLOCKED`, `COMPLETED` ou `CANCELLED` ;
- `role` : `ORCHESTRATOR`, `ARCHITECT`, `IMPLEMENTER`, `TESTER`, `REVIEWER` ou
  `CERTIFIER` ;
- `objective` et `subject` : intention et sujet explicites ;
- `operating_step` : étape courante de la boucle opérationnelle ;
- `blockers` : raisons explicites et indépendantes du statut ; un état
  `BLOCKED` en exige au moins une ;
- `next_action` : recommandation opérationnelle sans autorité de contrôle ;
- `observed_commit` : commit Git observé lors de la dernière mise à jour ;
- `updated_at` : horodatage ISO 8601 avec fuseau.

Les neuf étapes canoniques sont `RECONSTRUCT`, `PREFLIGHT`,
`UNDERSTAND_CONTRACT`, `PROVE_READINESS`, `ACT`, `VERIFY`, `RECORD_EVIDENCE`,
`CONTROLLED_TRANSITION` et `REPORT`. Les rôles sont des valeurs de contexte :
P2.2 ne leur associe aucun comportement spécialisé.

## Autorité et reprise

`MissionState` ne peut ni créer une Evidence, ni évaluer un Gate, ni autoriser
une transition, ni certifier un résultat, ni représenter une approbation
humaine. En cas de divergence, les fichiers réels, Git, les tests, les
artefacts et le `ProjectState` prévalent toujours sur `mission.json`.

`observed_commit` est uniquement une observation fournie par l'appelant. Le
store ne découvre pas Git et ne fournit aucun adaptateur Git. À la reprise,
l'appelant compare le HEAD réellement observé à cette valeur. Toute différence
impose de revenir à `RECONSTRUCT` avant de poursuivre ; elle n'est jamais
réparée ou interprétée automatiquement.

## Persistance fail-closed

`MissionStateStore` reçoit une racine de repository explicite et ne fait aucune
découverte. `initialize` exige un `MissionState` complet et refuse d'écraser un
document existant. `load` ne crée rien : absence et corruption sont des erreurs
distinctes. `save` valide avant d'écrire un fichier temporaire dans le même
dossier, force son contenu sur disque puis le remplace atomiquement.

JSON invalide, clé dupliquée, champ absent, valeur canonique inconnue,
horodatage invalide, identité de mission vide, chemin non sûr ou échec
d'écriture produisent une erreur explicite. Aucun état vide de secours n'est
créé et un état antérieur valide est conservé lorsqu'une sauvegarde échoue.
