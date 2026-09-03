# MissionRunner et CLI mission v1

Statut : intégration v1 en cours. Ce document ne constitue ni une
Certification ni une déclaration `PRODUCTION READY`.

`MissionRunner` reprend la prochaine frontière durable, utilise les autorités
existantes et s'arrête sur tout résultat non positif. Les commandes publiques
sont `mission run`, `mission resume` et `mission status`. Les deux premières
sont mutantes et verrouillées ; la dernière est read-only.

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

Une approbation Human doit être fournie comme Evidence canonique complète,
attribuable, liée au sujet et au commit exacts. Elle est enregistrée par le
Control Loop avant application par `HumanApprovalService`. Le parallélisme est
utilisé uniquement lorsqu'il est authentiquement démontré ; sinon l'exécution
est séquentielle et fail-closed.
