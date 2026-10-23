# Troca de senhas e chaves

> [↑ README](../README.md#troca-de-senhas-e-chaves)

A mecânica é a de [Troca de senha do banco](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform#troca-de-senha-do-banco), no README do `platform`; abaixo estão as credenciais do Billing, uma por procedimento. Os comandos usam o contexto do kind (no k3s, troque o `--context`). A senha nova tem só letras e dígitos (a do `openssl rand -hex 24` serve), porque entra crua na `MONGODB_URI`, e não passa por argumento, que ficaria no `ps` e na URL do pedido ao apiserver: o `jq` a recebe por variável de ambiente, e o `mongosh` a lê da variável `SENHA_NOVA` (e a do root, de `SENHA_ROOT`), que o `sh` do contêiner preenche com as duas primeiras linhas da entrada do `kubectl exec -i`. O Secret é gravado antes do banco, para o valor não ficar só numa variável do shell. Os pods leem o Secret só no start: até o restart, conexão nova com a senha antiga é recusada.

## Senhas do MongoDB: `billing`, `exporter` e `root`

Os três usuários seguem os mesmos três passos. Escolha a linha da tabela e defina as variáveis que o passo 1 e o passo 2 usam:

| Credencial | Variáveis | O que reiniciar no passo 3 |
|---|---|---|
| `billing` | `chave=MONGO_BILLING_PASSWORD usuario=billing banco=billing` | os Deployments |
| `exporter` | `chave=MONGO_EXPORTER_PASSWORD usuario=exporter banco=admin` | o StatefulSet do banco, porque o exporter é sidecar dele |
| `root` | `chave=MONGO_INITDB_ROOT_PASSWORD usuario=root banco=admin` | o StatefulSet do banco, que lê a senha do root só no start (até lá, a variável de ambiente do contêiner guarda a antiga); o Job a relê no deploy seguinte |

`banco` é aquele em que o `replica-set.js` criou o usuário (`billing` em `billing`, os outros em `admin`): a troca no banco errado responde `User billing@admin not found`.

1. Leia a senha do root que o banco tem agora, a do Secret, e só então gere a senha nova e grave-a:

   ```bash
   senha_root=$(kubectl --context kind-pytstop-p4 -n pytstop-billing get secret billing-mongo -o json \
     | jq -r '.data.MONGO_INITDB_ROOT_PASSWORD | @base64d')
   senha=$(openssl rand -hex 24)
   kubectl --context kind-pytstop-p4 -n pytstop-billing get secret billing-mongo -o json \
     | VALOR="$senha" jq --arg chave "$chave" '.data[$chave] = (env.VALOR | @base64)' \
     | kubectl --context kind-pytstop-p4 replace -f -
   ```

2. Aplique no banco, como root. O root autentica com `$senha_root`, a do Secret lida no passo 1, e não com a variável de ambiente do contêiner: ela só acompanha o Secret no restart, e se o restart de uma troca anterior do `root` foi pulado, guarda uma senha que o banco já não aceita.

   ```bash
   { printf '%s\n%s\n' "$senha" "$senha_root"; cat <<JS
   db.getSiblingDB("admin").auth(process.env.MONGO_INITDB_ROOT_USERNAME, process.env.SENHA_ROOT)
   db.getSiblingDB("$banco").changeUserPassword("$usuario", process.env.SENHA_NOVA)
   print("senha de $usuario trocada")
   JS
   } | kubectl --context kind-pytstop-p4 -n pytstop-billing exec -i billing-mongo-0 -c mongo -- \
       sh -c 'read -r SENHA_NOVA && read -r SENHA_ROOT && [ -n "$SENHA_NOVA" ] && [ -n "$SENHA_ROOT" ] && export SENHA_NOVA SENHA_ROOT && exec mongosh --quiet --norc --file /dev/stdin'
   ```

   O `mongosh` lê o script como arquivo, e não da entrada interativa, que sai com 0 mesmo quando a autenticação falha. Deu certo quando o comando sai com 0 e imprime `senha de <usuario> trocada`; se a autenticação ou a troca falhar, ele imprime o erro do MongoDB (`MongoServerError: Authentication failed.`, por exemplo) e sai com código diferente de zero. Se o passo falhar, repita-o na mesma sessão, com a mesma `$senha` e a mesma `$senha_root`: o Secret já tem a `$senha`, e reiniciar os pods com o Secret diferente do banco os deixa sem acesso. Na troca do `root`, `$senha_root` é a única cópia da senha anterior depois do passo 1, então não feche a sessão antes de o passo 2 dar certo.
3. Reinicie quem usa a credencial: os Deployments com `kubectl --context kind-pytstop-p4 -n pytstop-billing rollout restart deployment`, e o banco com `kubectl --context kind-pytstop-p4 -n pytstop-billing rollout restart statefulset/billing-mongo`.

## Keyfile

1. Grave um valor novo na chave `MONGO_KEYFILE` do Secret:

   ```bash
   valor=$(openssl rand -base64 756 | tr -d '\n')
   kubectl --context kind-pytstop-p4 -n pytstop-billing get secret billing-mongo -o json \
     | VALOR="$valor" jq --arg chave MONGO_KEYFILE '.data[$chave] = (env.VALOR | @base64)' \
     | kubectl --context kind-pytstop-p4 replace -f -
   ```

2. Reinicie o banco e espere: `kubectl --context kind-pytstop-p4 -n pytstop-billing rollout restart statefulset/billing-mongo` e, em seguida, `rollout status statefulset/billing-mongo`. O replica set tem um membro só, então nenhum outro precisa da chave antiga, mas o banco fica fora durante o restart.

## Chave do link de decisão (`ORCAMENTO_LINK_SECRET`)

1. Apague o Secret: `kubectl --context kind-pytstop-p4 -n pytstop-billing delete secret billing-link`.
2. Rode o `make deploy` do `platform`, que o recria com um valor novo: `make -C ../postech-sw-arch-p4-platform deploy`.
3. Reinicie juntos a API e o consumidor: `kubectl --context kind-pytstop-p4 -n pytstop-billing rollout restart deployment/billing-service-api deployment/billing-service-consumidor`. Os links e checkouts já enviados deixam de valer: o orçamento que aguarda decisão fica sem link até vencer, e o atendente ainda decide por ele. Até o fim do restart, um link pode abrir numa réplica e dar 404 em outra.
