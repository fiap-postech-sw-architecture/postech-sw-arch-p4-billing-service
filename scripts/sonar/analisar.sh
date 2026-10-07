#!/usr/bin/env bash
# SonarQube Community efemero no CI (ADR-041): o servidor sobe como service
# container do job, este script configura projeto e quality gate a partir de
# .sonar/quality-gate.json, roda o scanner com o coverage.xml do job `test` e
# falha o job se o gate reprovar. Credenciais nascem e morrem no runner.
set -euo pipefail

URL="${SONAR_HOST_URL:-http://localhost:9000}"
PROJETO="${SONAR_PROJECT_KEY:-${GITHUB_REPOSITORY##*/}}"
SCANNER_IMAGE="sonarsource/sonar-scanner-cli:12.2.0.4256_8.1.0"
mkdir -p reports

echo "::group::Esperando o SonarQube subir"
status=""
for _ in $(seq 1 90); do
  status="$(curl -fsS "$URL/api/system/status" 2>/dev/null | jq -r '.status' || true)"
  [ "$status" = "UP" ] && break
  sleep 5
done
echo "status=$status"
echo "::endgroup::"
[ "$status" = "UP" ] || { echo "::error::SonarQube nao ficou UP em 7,5 minutos (status=$status)"; exit 1; }

# A senha padrao admin/admin precisa ser trocada antes de qualquer chamada; a
# nova senha atende a politica de complexidade e so existe neste runner.
SENHA="Pytstop-$(openssl rand -hex 12)-Aa1!"
PADRAO="admin:admin"  # gitleaks:allow - credencial de fabrica do SonarQube efemero, trocada aqui
curl -fsS -u "$PADRAO" -X POST "$URL/api/users/change_password" \
  --data-urlencode "login=admin" --data-urlencode "previousPassword=admin" \
  --data-urlencode "password=$SENHA" >/dev/null
echo "::add-mask::$SENHA"
AUTH="admin:$SENHA"

curl -fsS -u "$AUTH" -X POST "$URL/api/projects/create" \
  --data-urlencode "project=$PROJETO" --data-urlencode "name=$PROJETO" >/dev/null

# Quality gate versionado. Cada condicao lista nomes alternativos de metrica
# (o modo MQR do SonarQube 2025+ renomeou os ratings); vale o primeiro que o
# servidor conhece.
METRICAS="$(curl -fsS -u "$AUTH" "$URL/api/metrics/search?ps=500" | jq -r '.metrics[].key')"
GATE="$(jq -r '.nome' .sonar/quality-gate.json)"
curl -fsS -u "$AUTH" -X POST "$URL/api/qualitygates/create" --data-urlencode "name=$GATE" >/dev/null
jq -c '.condicoes[]' .sonar/quality-gate.json | while read -r cond; do
  metrica=""
  for candidata in $(jq -r '.metricas[]' <<<"$cond"); do
    if grep -qx "$candidata" <<<"$METRICAS"; then metrica="$candidata"; break; fi
  done
  [ -n "$metrica" ] || { echo "::error::nenhuma metrica conhecida em $(jq -c '.metricas' <<<"$cond")"; exit 1; }
  curl -fsS -u "$AUTH" -X POST "$URL/api/qualitygates/create_condition" \
    --data-urlencode "gateName=$GATE" --data-urlencode "metric=$metrica" \
    --data-urlencode "op=$(jq -r '.operador' <<<"$cond")" \
    --data-urlencode "error=$(jq -r '.limite' <<<"$cond")" >/dev/null
  echo "condicao: $metrica $(jq -r '.operador' <<<"$cond") $(jq -r '.limite' <<<"$cond")"
done
curl -fsS -u "$AUTH" -X POST "$URL/api/qualitygates/select" \
  --data-urlencode "gateName=$GATE" --data-urlencode "projectKey=$PROJETO" >/dev/null

TOKEN="$(curl -fsS -u "$AUTH" -X POST "$URL/api/user_tokens/generate" \
  --data-urlencode "name=ci-$GITHUB_RUN_ID" --data-urlencode "type=PROJECT_ANALYSIS_TOKEN" \
  --data-urlencode "projectKey=$PROJETO" | jq -r '.token')"
echo "::add-mask::$TOKEN"

set +e
docker run --rm --network host \
  -e SONAR_HOST_URL="$URL" -e SONAR_TOKEN="$TOKEN" \
  -v "$PWD:/usr/src" "$SCANNER_IMAGE" \
  -Dsonar.projectKey="$PROJETO" \
  -Dsonar.qualitygate.wait=true -Dsonar.qualitygate.timeout=300
rc=$?
set -e

# Medidas e status do gate para o summary e para o artefato do job.
CHAVES="coverage,line_coverage,branch_coverage,ncloc,bugs,vulnerabilities,code_smells,security_hotspots,duplicated_lines_density,reliability_rating,security_rating,sqale_rating,software_quality_reliability_rating,software_quality_security_rating,software_quality_maintainability_rating"
curl -fsS -u "$AUTH" "$URL/api/measures/component?component=$PROJETO&metricKeys=$CHAVES" > reports/sonarqube-medidas.json
curl -fsS -u "$AUTH" "$URL/api/qualitygates/project_status?projectKey=$PROJETO" > reports/sonarqube-gate.json
jq -s '{projeto: $p, medidas: .[0].component.measures, gate: .[1].projectStatus}' --arg p "$PROJETO" \
  reports/sonarqube-medidas.json reports/sonarqube-gate.json > reports/sonarqube.json

if [ "$rc" -ne 0 ]; then
  echo "::error::Quality gate do SonarQube reprovou ou a analise falhou (rc=$rc)"
  exit "$rc"
fi

# Analise vazia passa no gate (sem coverage nem duplicacao para avaliar): um
# sonar.sources errado viraria falso verde. Exige linhas de codigo e cobertura.
ncloc="$(jq -r '[.medidas[]? | select(.metric=="ncloc") | .value][0] // "0"' reports/sonarqube.json)"
cobertura="$(jq -r '[.medidas[]? | select(.metric=="coverage") | .value][0] // ""' reports/sonarqube.json)"
if [ "${ncloc%%.*}" -le 0 ] || [ -z "$cobertura" ]; then
  echo "::error::Analise do SonarQube sem codigo ou sem cobertura (ncloc=$ncloc, coverage=${cobertura:-ausente}); confira sonar.sources e o coverage.xml"
  exit 1
fi
exit 0
