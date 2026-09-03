# MissionRunner et CLI mission v1

Statut : intégration v1 en cours. Ce document ne constitue ni une
Certification ni une déclaration `PRODUCTION READY`.

La façade applicative `MissionRunner` compose les autorités existantes. Elle
reconstruit les stores à chaque invocation, reprend la prochaine frontière
durable et s'arrête sur toute décision non positive. Les statuts opérateur sont
fermés : `ACTIVE`, `COMPLETED`, `WAITING_FOR_HUMAN`,
`REMEDIATION_REQUIRED`, `RECOVERY_REQUIRED`, `BLOCKED` et `REFUSED`.

Les commandes publiques sont :

```text
python -m agentic_engineering_os mission run --repository PATH (--objective TEXT | --objective-file FILE) [--scope PATH ...] [--verification-command ID ...] [--json]
python -m agentic_engineering_os mission resume --repository PATH --mission-id ID [--human-evidence FILE] [--json]
python -m agentic_engineering_os mission status --repository PATH [--mission-id ID] [--json]
```

`run` et `resume` sont mutantes et prennent un verrou exclusif de repository.
`status` est read-only. Le fichier Human est une Evidence canonique complète ;
il est enregistré par le Control Loop puis appliqué par
`HumanApprovalService`. Il n'existe ni `--yes`, ni identité Codex assimilable à
un Human.

Après l'adoption, ses fichiers doivent être commités, puis la gouvernance de
maintenance doit être initialisée sur ce HEAD propre avec une identité
attribuable :

```text
python -m agentic_engineering_os init --repository PATH --configuration FILE --apply --confirmed-by Human/IDENTITY
```

Le second `init` est un NO-OP d'adoption. L'identité initialise une seule fois
l'état `NORMAL` via le service de
gouvernance. Elle ne constitue ni une Evidence d'approbation de story ni une
autorisation réutilisable. Sans elle, l'adoption reste possible mais le départ
d'une mission est refusé tant que le store de maintenance n'est pas initialisé
sur le HEAD courant.

Le runtime découvre le binaire Codex, lie chemin/version/SHA-256, prouve les
capacités opérationnelles exigées et replie la concurrence à un seul processus
si le parallélisme indépendant n'est pas authentiquement démontré.
