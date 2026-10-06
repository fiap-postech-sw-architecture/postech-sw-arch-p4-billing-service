# Project Memory -- postech-sw-arch-p4-billing-service

<!-- last-consolidated: 2026-10-06 -->

Add-only log of project-specific learnings. New entries go to the top of each section. Never edit historical entries -- add a contradicting entry above instead.

Updated by AI agents at task end per `postech-ai-helper/ai/canonical/task-end-review.md`. The `last-consolidated` marker above is updated only when `/consolidate-memory` runs, not on every append.

## Recent decisions

- 2026-10-06 - Repo criado na fase 4 com branch protection na `main` desde o commit inicial (PR obrigatorio, admins incluidos, historico linear, conversas resolvidas, squash only). Motivo: a fase 3 perdeu ponto por commits diretos na main (29 no app, 11 na lambda) - spec `postech-sw-arch-p4/docs/superpowers/specs/2026-10-06-fase-4-bootstrap-design.md`

## Discovered conventions

## Gotchas

- 2026-10-06 - PyJWT 2.13.x acumulou 27 advisories em out/2026: comecar em `pyjwt>=2.15.1` e `anyio>=4.15.1`. PyJWT 2.15 exige base64url valido na assinatura mesmo com `verify_signature=False` (JWT falso de teste precisa de segmento valido)

## Tech debt / TODO

## Review lessons
