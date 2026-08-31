# Invariants

Les invariants suivants sont stables et non négociables.

1. **INV-001 — Repository Truth**
   L'état réel du repository prévaut sur la conversation, les suppositions et
   les déclarations d'un agent.

2. **INV-002 — No Fabrication**
   Aucun agent ne peut inventer un résultat de commande, un résultat de test,
   un état Git, un fichier, une preuve ou une certification.

3. **INV-003 — Mission Scope**
   Une mission ne peut modifier que ce qui est nécessaire à son scope
   explicite.

4. **INV-004 — Explicit Advancement**
   Aucun agent ne peut avancer vers une mission ou une phase suivante sans
   instruction explicite.

5. **INV-005 — Evidence Before Success**
   Une déclaration de réussite n'est valide que si les critères requis sont
   vérifiés par des preuves observables.

6. **INV-006 — Fail-Closed**
   Tout état requis non prouvé doit bloquer la progression.

7. **INV-007 — Unknown Is Not Pass**
   `UNKNOWN` ne peut jamais être interprété comme `PASS`.

8. **INV-008 — Failed Verification Blocks**
   Toute vérification obligatoire échouée empêche la certification.

9. **INV-009 — Independent Review Principle**
   À terme, l'implémentation, la vérification et la certification doivent être
   séparées conceptuellement.

10. **INV-010 — Acceptance Integrity**
    Les critères d'acceptation d'une mission ne peuvent pas être réduits ou
    modifiés pour transformer un échec en succès.

11. **INV-011 — Git Traceability**
    Toute baseline certifiée doit être associée à un état Git identifiable et
    reproductible.

12. **INV-012 — Clean Baseline**
    Une mission certifiée doit se terminer sur un working tree propre, sauf
    exception explicitement documentée et acceptée.

13. **INV-013 — No Silent Recovery**
    Une anomalie importante ne peut pas être masquée, contournée ou corrigée
    silencieusement.

14. **INV-014 — Reproducibility**
    Les vérifications déterminantes doivent pouvoir être reproduites depuis le
    repository et son environnement documenté.

15. **INV-015 — Simplicity First**
    Aucune complexité architecturale ne doit être ajoutée sans besoin démontré.

16. **INV-016 — VS Code + Codex First**
    La V1 cible exclusivement VS Code + Codex.

17. **INV-017 — Persistent Project Memory**
    La mémoire durable du projet doit résider dans le repository, pas
    uniquement dans les conversations.

18. **INV-018 — No Self-Certification by Assertion**
    Un agent ne peut pas certifier une mission uniquement parce qu'il affirme
    qu'elle est correcte.
