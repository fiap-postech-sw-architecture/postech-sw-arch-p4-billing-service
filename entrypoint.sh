#!/bin/bash
# Um container, processos diferentes (RFC-004 secao 10.2): `api` (padrao),
# `prazos` e `banco` (preparacao idempotente do MongoDB, antes dos outros).
# O relay da outbox e o consumidor dos comandos da saga (ADR-036) nao fazem
# parte desta versao da imagem.
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
    # (mesma postura do p3); a borda e o Kong. --no-server-header: a resposta
    # nao anuncia o servidor (uvicorn) nem a versao.
    exec uvicorn src.main:criar_app --factory --host 0.0.0.0 --port 8000 \
      --no-proxy-headers --no-server-header
    ;;
  prazos)
    exec python -m src.prazos
    ;;
  banco)
    exec python -m src.banco
    ;;
  *)
    echo "Processo desconhecido: ${processo} (use api, prazos ou banco)" >&2
    exit 64
    ;;
esac
