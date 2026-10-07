#!/bin/sh
# Cria ou atualiza o usuario de cada servico no RabbitMQ e aplica as
# permissoes de permissoes.json. Roda no Job rabbitmq-usuarios (Kubernetes) e
# no servico rabbitmq-usuarios (compose), na imagem do proprio RabbitMQ, que
# traz o rabbitmqadmin.
#
# Entrada por ambiente:
#   RABBITMQADMIN_TARGET_HOST, RABBITMQADMIN_TARGET_PORT  API de gerenciamento
#   RABBITMQADMIN_USERNAME, RABBITMQADMIN_PASSWORD        usuario admin
#   RABBITMQ_OS_PASSWORD, RABBITMQ_BILLING_PASSWORD,
#   RABBITMQ_EXECUCAO_PASSWORD                            senhas dos servicos
#   PERMISSOES (opcional)                                 caminho do JSON
#
# Idempotente: declarar usuario que ja existe troca a senha, e a importacao
# sobrescreve as permissoes. Por isso o deploy roda o Job de novo a cada versao.
set -eu

PERMISSOES="${PERMISSOES:-/scripts/permissoes.json}"

# O management sobe alguns segundos depois do AMQP. A ultima resposta vai
# para o log se nunca ficar pronto: um 401 (senha do admin trocada sem
# reiniciar o broker) nao pode parecer "fora do ar".
tentativa=0
until resposta=$(rabbitmqadmin show overview 2>&1); do
  tentativa=$((tentativa + 1))
  if [ "$tentativa" -ge 60 ]; then
    echo "RabbitMQ management API not ready after 60 attempts: $resposta" >&2
    exit 1
  fi
  sleep 2
done

rabbitmqadmin users declare --name os --password "$RABBITMQ_OS_PASSWORD"
rabbitmqadmin users declare --name billing --password "$RABBITMQ_BILLING_PASSWORD"
rabbitmqadmin users declare --name execucao --password "$RABBITMQ_EXECUCAO_PASSWORD"
rabbitmqadmin definitions import --file "$PERMISSOES"
echo "RabbitMQ users os, billing and execucao declared with permissions from $PERMISSOES"
