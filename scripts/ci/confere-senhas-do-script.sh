#!/usr/bin/env bash
# A excecao do trivy (KSV-0109, k8s/trivy-ignore.rego) vale para o ConfigMap do
# replica-set.js inteiro, e o gitleaks nao enxerga um pwd: com literal. Este
# script estreita a excecao ao que ela precisa: todo campo de senha do script
# (pwd, password, secret, token) recebe a chamada segredo(...), que le o
# ambiente do initContainer; valor literal reprova. Roda no make manifests, na
# raiz do repositorio.
set -euo pipefail

arquivo="${1:-k8s/base/replica-set.js}"
campos='(^|[^A-Za-z0-9_])(pwd|password|secret|token)[[:space:]]*:'
literais=$(grep -niE "$campos" "$arquivo" | grep -vE ':[[:space:]]*segredo\([A-Za-z_.]+\),?[[:space:]]*$' || true)
if [ -n "$literais" ]; then
  echo "$arquivo: campo de senha sem a chamada segredo(...):" >&2
  echo "$literais" >&2
  exit 1
fi
# Sem nenhum campo de senha a conferencia nao prova nada (o script mudou de forma).
if ! grep -qiE "$campos" "$arquivo"; then
  echo "$arquivo: nenhum campo de senha encontrado" >&2
  exit 1
fi
echo "$arquivo: todo campo de senha vem de segredo(...)"
