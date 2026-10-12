#!/usr/bin/env bash
# Teste negativo das regras do .gitleaks.toml (job gitleaks do CI; roda tambem
# na maquina local, com o gitleaks do PATH): a URI do MongoDB e a URL do AMQP
# com a senha que o make deploy gera reprovam num manifesto, a forma dos
# manifests ($(MONGO_BILLING_PASSWORD)) passa, a senha de demonstracao so passa
# onde a allowlist diz (ou com o marcador gitleaks:allow na linha), e senha
# apagada no commit seguinte ainda reprova no historico. Sem ele, uma regra que
# nao casa mais nada passaria verde.
set -euo pipefail

raiz=$(cd "$(dirname "$0")/../.." && pwd)
dir=$(mktemp -d "${TMPDIR:-/tmp}/testa-gitleaks.XXXXXX")
trap 'rm -rf "$dir"' EXIT
mkdir -p "$dir/k8s"

# mongo <senha> e amqp <senha>: as linhas que os manifests montam.
mongo() { printf 'value: mongodb://billing:%s@billing-mongo-0.billing-mongo.pytstop-billing.svc.cluster.local:27017/billing\n' "$1"; }
amqp() { printf 'RABBITMQ_URL: "amqp://billing:%s@rabbitmq.pytstop-plataforma.svc.cluster.local:5672/%%2F"\n' "$1"; }
mongo "$(openssl rand -hex 24)" > "$dir/k8s/mongo-senha-gerada.yaml"
amqp "$(openssl rand -hex 24)" > "$dir/k8s/amqp-senha-gerada.yaml"
amqp pytstop-billing-demo-2026 > "$dir/k8s/amqp-senha-de-demonstracao.yaml"
# shellcheck disable=SC2016 # o $(...) e literal: a expansao e do kubelet
mongo '$(MONGO_BILLING_PASSWORD)' > "$dir/k8s/mongo-expansao-do-pod.yaml"
amqp pytstop-billing-demo-2026 > "$dir/Makefile"
amqp pytstop-billing-demo-2026 > "$dir/docker-compose.yml"
{ amqp pytstop-billing-demo-2026 | sed 's/$/  # gitleaks:allow/'; } > "$dir/compose-com-marcador.yml"

(cd "$dir" && gitleaks dir . --config "$raiz/.gitleaks.toml" --no-banner --redact --exit-code 0 \
  --report-format json --report-path "$dir/achados.json")
achados=$(jq -r '[.[] | .RuleID + " " + .File] | sort | join(", ")' "$dir/achados.json")
esperado="pytstop-url-amqp-com-senha docker-compose.yml, pytstop-url-amqp-com-senha k8s/amqp-senha-de-demonstracao.yaml, pytstop-url-amqp-com-senha k8s/amqp-senha-gerada.yaml, pytstop-url-mongodb-com-senha k8s/mongo-senha-gerada.yaml"
if [ "$achados" != "$esperado" ]; then
  echo "gitleaks found [$achados], expected [$esperado]" >&2
  exit 1
fi
# Historico: senha commitada e apagada no commit seguinte continua reprovando
# com o mesmo escopo do CI (--log-opts="HEAD").
repo="$dir/historico"
git init -q "$repo"
git -C "$repo" -c user.name=teste -c user.email=teste@example.invalid commit -q --allow-empty -m inicio
mkdir -p "$repo/k8s"
mongo "$(openssl rand -hex 24)" > "$repo/k8s/vazou.yaml"
git -C "$repo" add k8s/vazou.yaml
git -C "$repo" -c user.name=teste -c user.email=teste@example.invalid commit -q -m vaza
git -C "$repo" rm -q k8s/vazou.yaml
git -C "$repo" -c user.name=teste -c user.email=teste@example.invalid commit -q -m apaga
if gitleaks git "$repo" --log-opts="HEAD" --config "$raiz/.gitleaks.toml" --no-banner --redact >/dev/null 2>&1; then
  echo "gitleaks passed a password committed and deleted in the next commit" >&2
  exit 1
fi
echo "gitleaks rules hold: generated passwords flagged in k8s/, the pod expansion allowed, demo password allowed only in the Makefile and with the inline marker, deleted password still flagged in history"
