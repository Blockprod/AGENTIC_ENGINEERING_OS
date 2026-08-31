# CLI portable compatible avec les politiques d'entreprise

## Contrat d'invocation

L'invocation canonique portable est :

```text
<environment-python> -m agentic_engineering_os <command>
```

`<environment-python>` désigne exactement l'interpréteur de l'environnement où
le wheel est installé. Le module package et le console script optionnel
`agentic-os` délèguent tous deux directement à
`agentic_engineering_os.cli:main`. Ils n'ont ni logique ni autorité distincte.

## Observation Windows Enterprise

Lors de P5.12-R1, Windows Code Integrity a refusé les nouveaux shims non signés
`pip.exe` et `agentic-os.exe` avec `WinError 4551`. Les événements 3033 et 3077,
associés à la policy ID `{0283ac0f-fff1-49ae-ada1-8a933130cad6}`, indiquaient
que les fichiers ne satisfaisaient pas le niveau de signature Enterprise. Le
même comportement a été observé sous `%TEMP%` et `D:\DEV`.

Le shim `agentic-os.exe` reste un lanceur de commodité et peut être indisponible
selon la politique de l'hôte. L'emploi du Python autorisé du venv pour charger
le package installé est le chemin portable supporté ; il ne désactive, ne
modifie et ne contourne aucun contrôle de sécurité Windows.
