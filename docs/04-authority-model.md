# Modèle d'autorité et frontières des rôles

## Cadre conceptuel

Le modèle cible distingue les rôles suivants :

`Human → Orchestrator → Architect → Implementer → Tester → Reviewer → Certifier`

En V1, un même moteur Codex dans VS Code pourra exécuter plusieurs de ces rôles
successivement. Leurs responsabilités et leurs outputs restent néanmoins
conceptuellement séparés. Aucun rôle ne peut contourner les invariants ou la
politique fail-closed.

## Rôles

### Human

Le Human détient l'autorité finale sur :

- les objectifs ;
- les priorités ;
- les contraintes métier ;
- les arbitrages critiques ;
- les approbations humaines explicitement requises ;
- le passage volontaire à une nouvelle phase majeure.

Une décision réservée au Human ne peut pas être remplacée par une décision
implicite ou une auto-approbation de Codex. Le Human peut faire évoluer un
contrat explicitement pour un travail futur, mais une modification rétroactive
ne peut pas transformer un échec observé en succès.

### Orchestrator

L'Orchestrator peut :

- reconstruire l'état du repository ;
- coordonner la mission ;
- demander des analyses ;
- organiser l'ordre du travail ;
- suivre les dépendances ;
- déclencher les rôles appropriés ;
- consolider les résultats.

Il ne peut pas :

- inventer une preuve ;
- modifier silencieusement les critères d'acceptation ;
- déclarer un gate réussi sans vérification ;
- avancer vers une phase suivante sans autorisation.

### Architect

L'Architect peut :

- analyser le besoin ;
- proposer la décomposition ;
- définir les User Stories ;
- définir les dépendances ;
- proposer les critères d'acceptation ;
- définir les frontières architecturales.

Lorsque la séparation des responsabilités est requise, il ne doit pas
implémenter le code métier de la mission qu'il définit.

### Implementer

L'Implementer peut :

- modifier le code et les fichiers autorisés ;
- exécuter les outils nécessaires ;
- créer ou adapter les tests directement liés à l'implémentation lorsque le
  contrat l'autorise ;
- produire les preuves de son travail.

Il ne peut pas :

- réduire les critères d'acceptation ;
- transformer un échec en succès ;
- certifier sa propre implémentation par simple déclaration ;
- modifier hors scope sans justification et autorisation.

### Tester

La mission principale du Tester est de chercher à démontrer que
l'implémentation ne respecte pas son contrat.

Il peut :

- exécuter les tests ;
- inspecter les cas limites ;
- vérifier les erreurs et les comportements négatifs ;
- produire des résultats de vérification.

Il ne doit pas modifier silencieusement l'implémentation afin de faire passer
les tests. Toute correction nécessaire revient dans une boucle de remédiation
explicite.

### Reviewer

Le Reviewer évalue notamment :

- la cohérence architecturale ;
- le scope ;
- la maintenabilité ;
- la duplication ;
- la complexité ;
- la sécurité conceptuelle ;
- le respect des contrats ;
- la dette introduite.

Il peut recommander `PASS`, `FAIL` ou une remédiation. Il ne doit pas masquer
une anomalie importante en la corrigeant silencieusement.

### Certifier

Le Certifier vérifie que les conditions nécessaires à la réussite sont
effectivement prouvées.

Il peut :

- inspecter les critères ;
- inspecter les preuves ;
- vérifier les résultats ;
- produire un verdict de certification.

Il ne peut pas :

- modifier l'implémentation pour obtenir un `PASS` ;
- réduire les critères ;
- fabriquer une preuve ;
- traiter `UNKNOWN` comme `PASS`.

## Séparation des responsabilités

```text
SPECIFY
  Architect

IMPLEMENT
  Implementer

VERIFY BEHAVIOR
  Tester

REVIEW QUALITY
  Reviewer

CERTIFY
  Certifier
```

Un même moteur Codex peut jouer plusieurs rôles successivement dans la V1,
mais :

- les responsabilités ne sont jamais fusionnées conceptuellement ;
- les outputs de chaque rôle restent distinguables ;
- une déclaration produite dans un rôle ne constitue pas automatiquement une
  preuve pour le rôle suivant.

## Principes d'autorité

### Evidence Authority

Les preuves observables ont priorité sur les déclarations des agents.

### Repository Authority

L'état réel du repository reste la source de vérité.

### Contract Authority

Le contrat explicite de la mission prévaut sur toute interprétation
opportuniste d'un agent.

### Certification Authority

Une mission n'est réussie que lorsque les conditions de certification
applicables sont satisfaites.

### Human Authority

Toute décision explicitement réservée à l'opérateur humain ne peut pas être
auto-approuvée par Codex.

Ces autorités sont complémentaires : Human Authority ne remplace ni les
preuves ni les conditions de certification applicables, et aucun rôle agentique
ne peut s'en prévaloir pour contourner le fail-closed.

## Matrice des droits

`Oui` autorise l'action dans le scope du contrat. `Coordonne`, `propose`,
`autorise` et `déclenche` ne confèrent pas le droit d'exécuter l'action ou d'en
déclarer le résultat.

| Action | Human | Orchestrator | Architect | Implementer | Tester | Reviewer | Certifier |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Définir le contrat | Fixe et approuve | Coordonne | Propose et définit | Non | Non | Non | Non |
| Modifier le code | Autorise | Non | Non | Oui | Non | Non | Non |
| Exécuter les tests | Non | Déclenche | Non | Oui si autorisé | Oui | Oui si nécessaire | Oui pour revérifier |
| Reviewer | Peut demander | Déclenche | Non | Non | Non | Oui | Non |
| Certifier | Non | Déclenche | Non | Non | Non | Non | Oui |
| Modifier les critères | Approuve explicitement | Non | Propose | Non | Non | Non | Non |
| Autoriser la phase suivante | Oui | Non | Non | Non | Non | Non | Non |

Toute modification de critères s'applique explicitement et ne peut pas réduire
rétroactivement les critères pour convertir un `FAIL` ou `UNKNOWN` en `PASS`.
L'autorisation humaine d'une phase suivante ne dispense pas de satisfaire les
conditions de certification applicables.
