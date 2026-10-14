#!/usr/bin/env bash
# Roda um comando, mostra a saida no log e a registra no summary do job (titulo,
# resultado e a saida num bloco de texto), tambem quando ele reprova: sem isso o
# resultado do pip-audit e do trivy so existe no log do passo. O codigo de saida
# e o do comando, entao o job reprova como antes.
# Uso: scripts/ci/registra-no-summary.sh "<titulo>" <comando> [argumentos...]
set -uo pipefail

if [ "$#" -lt 2 ]; then
  echo "uso: $0 \"<titulo>\" <comando> [argumentos...]" >&2
  exit 64
fi
titulo="$1"
shift
resumo="${GITHUB_STEP_SUMMARY:-/dev/stdout}"

saida=$(mktemp)
trap 'rm -f "$saida"' EXIT

"$@" > "$saida" 2>&1
status=$?

cat "$saida"
{
  if [ "$status" -eq 0 ]; then
    echo "### ${titulo}: sem achado"
  else
    echo "### ${titulo}: reprovou (codigo ${status})"
  fi
  echo '```text'
  cat "$saida"
  echo '```'
} >> "$resumo"
exit "$status"
