# Validation de déploiement multi-repository

P5.11 valide le produit packagé, installé dans un environnement virtuel propre,
depuis un répertoire extérieur au checkout source. La campagne utilise
l'invocation portable canonique `<environment-python> -m agentic_engineering_os`,
uniquement des repositories Git temporaires, et n'exécute aucune commande
découverte dans un projet cible.

## Matrice et résultats attendus

| Cas | Forme du repository | Résultat attendu |
| --- | --- | --- |
| A | Python minimal | `UNINITIALIZED`, puis `ADOPTED` après configuration et apply explicites |
| B | Python avec `pyproject.toml` et tests | reconnaissance Python, puis adoption explicite |
| C | Node minimal | reconnaissance Node, puis adoption explicite sans exécuter les scripts projet |
| D | Python + Node | reconnaissance des deux toolchains, puis adoption explicite |
| E | `AGENTS.md` utilisateur | confirmation Human exacte requise, octets utilisateur préservés |
| F | déjà adopté | `ADOPTED` et seconde initialisation idempotente |
| G | initialisation partielle | `PARTIAL_OR_INCONSISTENT`, sans réparation silencieuse |
| H | working tree dirty | opération bloquée, sans mutation |
| I | HEAD détachée | état observé explicitement ; préparation permise par le contrat existant |
| J | plusieurs worktrees | état observé explicitement ; préparation permise par le contrat existant |
| K | configuration/version incompatible | `PARTIAL_OR_INCONSISTENT` ou `UPGRADE_REQUIRED` selon le cas |
| L | fichiers sensibles présents | contenu secret absent des sorties CLI |
| M | Rust minimal, déjà supporté | reconnaissance Rust, puis adoption explicite |

## Frontières de la campagne

La campagne couvre le chemin `inspect → plan → init → status`, le dry-run,
l'adoption sûre d'un `AGENTS.md` utilisateur, les migrations supportées et
refusées, les incohérences persistantes, les plans périmés et les chemins non
sûrs. Elle prouve l'origine du package, des schémas et des contrats de rôles dans
l'environnement installé et recherche tout chemin absolu vers le checkout dans
le wheel et l'installation.

Cette validation est un checkpoint ciblé : elle ne remplace pas la certification
globale et n'autorise pas l'exécution de la suite pytest complète.
