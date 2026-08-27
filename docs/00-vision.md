# Vision et contrat d'Agentic Engineering

## Finalité

`AGENTIC_ENGINEERING_OS` est une couche réutilisable destinée à structurer le
développement logiciel réalisé avec VS Code, Codex et un repository Git.

Son objectif est de transformer un workflow de prompting manuel en un processus
d'ingénierie agentique structuré, observable, vérifiable et réutilisable.

## Définition officielle de l'Agentic Engineering

L'Agentic Engineering est une méthode d'ingénierie logicielle guidée par des
objectifs explicites, qui observe l'état réel du repository, planifie et
décompose le travail en tâches, gère leurs dépendances et exécute les actions au
moyen d'outils. Elle progresse par boucles itératives
observation → action → vérification, produit des preuves reproductibles, puis
soumet le résultat à validation et certification. Toute vérification en échec ou
toute incertitude nécessaire à la décision impose un blocage fail-closed.

## Architecture conceptuelle cible

La chaîne conceptuelle future est :

`Human → Orchestrator → Architect → Implementer → Tester → Reviewer → Certifier`

- **Human** : fixe l'intention, les contraintes et l'autorité de décision.
- **Orchestrator** : coordonne le flux de travail et les dépendances.
- **Architect** : définit la solution et ses frontières.
- **Implementer** : réalise les changements autorisés.
- **Tester** : vérifie le comportement attendu.
- **Reviewer** : examine la qualité, la cohérence et les risques.
- **Certifier** : statue à partir des preuves et applique le fail-closed.

Tous ces rôles pourront être opérés via Codex dans VS Code. Ils décrivent ici
des responsabilités conceptuelles futures : aucun de ces agents n'est créé ou
implémenté pendant P0.2.

## Source de vérité

Le repository constitue la mémoire persistante du projet. L'état réel des
fichiers, de Git, des tests et des artefacts prévaut toujours sur la
conversation. Une déclaration d'agent ne constitue jamais, à elle seule, une
preuve suffisante.

## Philosophie de conception

- Simplicité d'usage.
- Progressivité.
- Pas de complexité spéculative.
- Preuves plutôt que déclarations.
- Fail-closed en cas d'échec ou d'incertitude.
- Séparation des responsabilités.
- Automatisation uniquement lorsqu'elle apporte une valeur démontrée.
