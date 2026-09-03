# Dossier de certification de release

Ce répertoire ne contient volontairement aucun verdict tant que les workflows
protégés et l'audit adversarial du candidat immuable n'ont pas produit leurs
preuves. Un fichier `v1.0.0.json` n'est ajouté qu'après ces résultats ; sa seule
présence ne suffit pas, car `scripts/validate_release_certification.py` applique
le contrat fermé ci-dessous et compare le SHA-256 du wheel reconstruit.

Le candidat est construit après le passage à `1.0.0`, avec un
`source_date_epoch` fixé. Les validations protégées portent sur ce wheel. Le
commit de clôture peut ensuite ajouter uniquement le dossier de certification,
qui n'entre pas dans le wheel ; le workflow de release reconstruit avec le même
epoch et refuse tout digest différent.

```json
{
  "schema_version": "1.0",
  "release": "v1.0.0",
  "verdict": "CERTIFIED",
  "candidate": {
    "package_version": "1.0.0",
    "source_date_epoch": 1788451200,
    "wheel_sha256": "<64 caractères hexadécimaux minuscules>"
  },
  "environment": {
    "os": "Windows 11",
    "architecture": "x64",
    "python": "CPython 3.11",
    "git": "2.55",
    "codex": "<version et identité observées>"
  },
  "evidence": [
    {"id": "adversarial_audit", "result": "PASS", "reference": "<preuve>"},
    {"id": "clean_room", "result": "PASS", "reference": "<preuve>"},
    {"id": "license_review", "result": "PASS", "reference": "<preuve>"},
    {"id": "real_codex_sequential", "result": "PASS", "reference": "<preuve>"},
    {"id": "soak", "result": "PASS", "reference": "<preuve>"},
    {"id": "windows_ci", "result": "PASS", "reference": "<preuve>"}
  ]
}
```

Une référence doit désigner une preuve attribuable et conservée. Ce modèle est
informatif et ne doit jamais être copié comme si ses placeholders étaient des
preuves.
