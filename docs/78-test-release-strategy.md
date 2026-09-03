# Stratégie de test et de release v1

Les catégories officielles sont `unit`, `integration`, `real_git`,
`real_codex`, `clean_room` et `soak`. Les canaries Codex, clean-room et soak
sont isolées dans un workflow protégé. Aucun test Git, subprocess ou
adversarial ne peut être supprimé pour satisfaire un budget de durée.

Le profil initial observé sur la baseline auditée montre un coût dominant dans
les créations de repositories temporaires et les appels Git Windows. La
baseline P6 publiée est de 8 040 s environ ; le budget v1 est donc fixé à
4 020 s au maximum, soit 50 % de cette baseline. La CI exécute les modules de
test indépendants sur huit workers avec `pytest-xdist --dist loadfile`, sans
retirer ni remplacer les tests Git, subprocess ou adversariaux, mesure le temps
mural et refuse tout dépassement. Trois exécutions terminales locales du
2026-09-03 ont produit exactement 2 106 succès et 12 skips attendus en
528,10 s, 526,08 s et 527,12 s. La médiane optimisée est donc 527,12 s, avec
une étendue de 2,02 s (0,38 %), soit une réduction de 93,4 % face à la
baseline P6. Cette médiane est propre à l'hôte local : le premier run
`windows-2025` GitHub a observé 1 083,21 s avant de terminer sur un échec
fonctionnel unique. Jusqu'à l'obtention de trois exécutions terminales vertes
sur cette classe de runner, la CI applique donc le plafond bootstrap de
4 020 s. Elle conserve un rapport JUnit et un enregistrement JSON de durée pour
chaque exécution. Après trois exécutions terminales, le seuil de régression sera
fixé à 115 % de leur médiane, arrondi à la seconde supérieure et plafonné à
4 020 s. Le candidat immuable doit encore reproduire ce gate sur le runner de
certification.

Le soak local du 2026-09-03 a produit un résultat terminal de **26 tests
passants en 2 007,04 s** sur Windows/Python 3.11 : 10 missions mono-story,
5 groupes multi-story, 5 remédiations avec nouvelle génération, 5 reprises à
des frontières durables distinctes et une rotation sur au moins 3 segments.
Cette mesure est une preuve de développement ; elle ne remplace pas le run du
workflow protégé depuis le wheel candidat immuable.

La campagne `real_git` locale du 2026-09-03 a produit 632 succès uniques et
8 skips attendus en deux tranches. La seconde tranche terminale compte 486
succès en 8 597,36 s. Après les derniers correctifs ciblés, la suite rapide a
produit 1 473 succès et 4 skips en 106,60 s. Un wheel installé dans un venv neuf
a passé le parcours clean-room et `pip check`.

Le canary de mission Codex installé est présent et opt-in. Sur l'hôte local, il
s'arrête correctement à l'admission : `REPOSITORY_READ` et `GIT_OBSERVATION`
sont prouvés, tandis que `WORKSPACE_EDIT` et `COMMAND_EXECUTION` sont refusés
par la politique hôte avec `CAPABILITY_BLOCKED_BY_HOST_POLICY`. Ce refus est un
blocker de certification du chemin réel, pas une autorisation de fallback. Le
workflow protégé doit exécuter ce canary sur un runner où ces deux capacités
sont authentiquement prouvées.

La release `v1.0.0` est fail-closed : le workflow exige la version exacte et le
dossier strict `release-certification/v1.0.0.json`, valide ses six familles de
preuves obligatoires et compare le SHA-256 du wheel reconstruit au candidat
certifié. Il construit le wheel reproductible
et une archive source déterministe via `git archive`, vérifie `pip check`,
génère SBOM, checksums, inventaire réel des ressources et provenance, puis atteste
les artefacts avant publication. Tant que le dossier certifié n'existe pas, le
tag ne peut pas produire de GitHub Release.

Le sdist produit directement par setuptools n'est pas un artefact certifié :
ses timestamps de répertoires TAR varient malgré `SOURCE_DATE_EPOCH`. Cette
différence est explicitement exclue ; l'archive Git du commit tagué est la
source immuable publiée.
