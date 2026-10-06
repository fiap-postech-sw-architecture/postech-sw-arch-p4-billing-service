#!/bin/bash
# Um container, processos diferentes (RFC-004 §10): `api` (padrao) ou `prazos`.
# O relay do outbox e o consumidor de comandos entram com a mensageria.
set -euo pipefail

processo="${1:-api}"
commit="${PYTSTOP_GIT_SHA:-unknown}"
echo ">>> pytstop billing ${processo} | commit ${commit:0:12} | ${PYTSTOP_GIT_DATE:-unknown}"

case "$processo" in
  api)
    if [ "${RUN_SEED_ON_STARTUP:-false}" = "true" ]; then
      # Seed idempotente e best-effort: falha nao impede a API de subir.
      python -m src.seed || echo "Seed de precos nao concluiu; seguindo com a API."
    fi
    # --no-proxy-headers: X-Forwarded-For so com proxy confiavel configurado
    # (mesma postura do p3); a borda e o Kong.
    exec uvicorn src.main:criar_app --factory --host 0.0.0.0 --port 8000 --no-proxy-headers
    ;;
  prazos)
    exec python -m src.prazos
    ;;
  *)
    echo "Processo desconhecido: ${processo} (use api ou prazos)" >&2
    exit 64
    ;;
esac
