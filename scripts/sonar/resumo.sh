#!/usr/bin/env bash
# Escreve em Markdown o resultado da analise do SonarQube (status do gate e
# medidas principais) a partir de reports/sonarqube.json gerado por analisar.sh.
set -euo pipefail
ARQ="reports/sonarqube.json"
if [ ! -s "$ARQ" ]; then
  echo "### SonarQube"
  echo
  echo "Sem relatorio: a analise nao chegou a rodar (veja o log do step anterior)."
  exit 0
fi
status="$(jq -r '.gate.status // "DESCONHECIDO"' "$ARQ")"
echo "### SonarQube: quality gate \`$status\`"
echo
echo "| Medida | Valor |"
echo "|---|---|"
jq -r '.medidas | sort_by(.metric)[] | "| \(.metric) | \(.value) |"' "$ARQ"
echo
echo "| Condicao do gate | Comparacao | Limite | Valor | Status |"
echo "|---|---|---|---|---|"
jq -r '.gate.conditions[]? | "| \(.metricKey) | \(.comparator) | \(.errorThreshold) | \(.actualValue) | \(.status) |"' "$ARQ"
