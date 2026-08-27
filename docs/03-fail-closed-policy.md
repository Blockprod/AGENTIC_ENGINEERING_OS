# Politique fail-closed

## États de vérification

Toute vérification reçoit conceptuellement l'un des états suivants :

- `PASS`
- `FAIL`
- `UNKNOWN`
- `NOT_APPLICABLE`

## Sémantique

- `PASS` : la progression est possible.
- `FAIL` : la progression est bloquée.
- `UNKNOWN` : la progression est bloquée.
- `NOT_APPLICABLE` : l'état est accepté uniquement lorsqu'il est explicitement
  autorisé par le contrat ou la politique applicable.

## Règle centrale

Pour toute condition obligatoire :

```text
PASS             → ACCEPT
FAIL             → BLOCK
UNKNOWN          → BLOCK
NOT_APPLICABLE   → BLOCK sauf autorisation explicite
```

Ni `FAIL` ni `UNKNOWN` ne peuvent être interprétés comme un succès. Une absence
de preuve ne constitue pas une preuve de réussite.

## Exemples

| Situation | État | Décision |
| --- | --- | --- |
| Un test obligatoire n'a pas été exécuté. | `UNKNOWN` | Bloquer. |
| Une commande requise est indisponible. | `UNKNOWN` | Bloquer. |
| L'état Git est ambigu. | `UNKNOWN` | Bloquer. |
| Un fichier attendu est absent. | `FAIL` | Bloquer. |
| Le résultat d'un contrôle de sécurité est inconnu. | `UNKNOWN` | Bloquer. |
| Le commit exigé est impossible. | `FAIL` | Bloquer. |
| Une vérification est explicitement déclarée non applicable par le contrat. | `NOT_APPLICABLE` | Continuer. |
| Une vérification est déclarée non applicable sans autorisation explicite. | `NOT_APPLICABLE` | Bloquer. |

## Principe de remédiation

Un échec ne conduit pas à recommencer arbitrairement une phase entière. Le
système privilégie la séquence suivante :

```text
FAIL/BLOCKED
    ↓
CAUSE IDENTIFIED
    ↓
MINIMAL REMEDIATION
    ↓
RE-VERIFY
    ↓
PASS ou BLOCKED
```

La remédiation doit rester limitée à la cause identifiée et au scope autorisé.
La progression ne reprend qu'après une nouvelle vérification concluante.
