# CD automatizado

> [↑ README](../README.md#cicd)

O [caller CD](../.github/workflows/cd.yml) tem os gatilhos `push` na `main` e `workflow_dispatch`, a concorrência por repositório sem cancelar o run em curso e os nove checks locais de CI e segurança. O job `cd` depende de ambos os gates verdes e chama o [reutilizável do Platform](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/3f7cca2eb555a038b0d2780058ef0e692f93ee8a/.github/workflows/cd-servico.yml) pelo SHA integrado `3f7cca2eb555a038b0d2780058ef0e692f93ee8a` (S), com `servico: billing-service` e os quatro inputs de SHA. S fixa a definição do workflow; `sha_platform` seleciona separadamente os scripts e manifests usados no deploy (P). Não há `secrets: inherit`, credencial de provedor, `environment` ou OIDC no caller.

O primeiro CD deste caller na `main` foi aprovado no [run 39355822794](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-billing-service/actions/runs/39355822794), sobre `735a0c23c16f584c02b2f6a77138549eb0a11ac5`: CI, segurança, imagem com SBOM e digest conferido, implantação no kind, smoke, E2E, varredura e `remote-skipped` passaram. O mesmo pin S também foi provado no CD do próprio platform em `3f7cca2` ([run 39227461810](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/actions/runs/39227461810), com `e2e` e `deploy-kind` verdes). No CD do OS Service na `main`, sobre `5e4ed2a` ([run 39233375064](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-os-service/actions/runs/39233375064)), `image`, `deploy-kind` e `remote-skipped` também passaram.

| Job chamado | Papel | Permissões |
|---|---|---|
| `image` | Só publica na `main`; constrói e publica uma imagem no GHCR com tag do commit, SBOM não vazio e proveniência mínima sem payload do evento; confere digest e salva o tar `linux/amd64` | `contents: read`, `packages: write` |
| `deploy-kind` | Carrega essa imagem no kind efêmero do runner, constrói os vizinhos e valida smoke e E2E da saga; varre os relatórios antes do artefato `e2e-kind`, restrito a `reports/e2e/`, por 90 dias | `contents: read`, `actions: read` |
| `deploy-remote` | Opcional e desligado; só na `main`, com `DEPLOY_REMOTO_HABILITADO=true`, implanta o mesmo digest aprovado no kind | `contents: read`, `packages: read` |
| `remote-skipped` | Registra o aviso quando o remoto está desligado | nenhuma |

O caller concede somente a união `contents: read`, `actions: read`, `packages: write`, reduzida pelos jobs chamados. A guarda do reutilizável confere `main` e o par serviço/repositório antes de checkout, login ou build. Os quatro SHAs entram como dados, e o serviço próprio permanece no commit do run; vizinhos usam o último CD verde de push na `main`, com fallback avisado se não houver run verde, sem esconder falhas de API.

O [gancho remoto](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/3f7cca2eb555a038b0d2780058ef0e692f93ee8a/docs/operacao/nuvem-opcional.md) compara P com a `main` do Platform antes do checkout, aceitando somente `identical` ou `behind`. Faz checkout do Platform em `platform/` e do Billing no commit da imagem em `servico/`, sem persistir credenciais; executa `../platform/scripts/ci/deploy-remoto.sh` a partir de `servico/`, com contexto, overlay, `billing-service` e imagem por digest. Acesso ao cluster continua sem configuração. Habilitar o remoto exige alterar deliberadamente o reutilizável, seus testes e o pin S; o job caller com `uses` não admite steps extras.

O [contrato local](../tests/unitarios/test_cd.py) confere caminho literal e SHA completo, gates, inputs (sem `default`), permissões e concorrência, e a imagem do actionlint por digest, com o `lint-workflows` no `make check` e no job `lint`; a [suíte compartilhada](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-platform/blob/3f7cca2eb555a038b0d2780058ef0e692f93ee8a/tests/test_cd_servico_workflow.py) protege uma única vez os passos Bash de imagem, seleção de commits, kind, varredura e remoto. CI, segurança, Dependabot, Dockerfile, manifests e aplicação continuam locais. O Dependabot `github-actions` já cobre a raiz; até a comprovação do primeiro bump reconhecido, a atualização do pin permanece supervisionada.

Para reexecutar, dispare o `workflow_dispatch` sempre com `--ref main`: o reutilizável reprova outra ref antes de publicar. As entradas `sha_*` são opcionais e têm 40 caracteres hexadecimais minúsculos (SHA abreviado, maiúsculo ou nome de ramo reprova o passo antes de qualquer checkout); vazias, seguem a regra do último CD verde descrita acima. O `sha_billing_service` só aceita vazio ou o commit do run, porque este serviço é sempre o commit que iniciou o workflow.

```bash
gh workflow run cd.yml --ref main
gh run watch "$(gh run list --workflow cd.yml --limit 1 --json databaseId --jq '.[0].databaseId')"
```

Antes de reexecutar, três fatos:

1. O artefato `imagem` vive 1 dia. Depois disso o "Re-run failed jobs" reprova no download dele: rode `gh workflow run cd.yml --ref main` no lugar do re-run.
2. A `concurrency` guarda um único run pendente por repositório: o push ou o dispatch seguinte substitui o pendente, e o commit do run substituído fica sem CD. O run que sobra testa o estado mais novo da `main`.
3. Um `sha_*` inválido só reprova no primeiro passo do `deploy-kind`, depois do CI e da imagem. A mensagem nomeia a entrada.

Antes de publicar uma mudança no workflow: `make lint-workflows` (actionlint com shellcheck), `uv run pytest tests/unitarios/test_cd.py`, `make check` e `make smoke`. O `cd.yml` só roda no GitHub, depois do merge ou no dispatch, então esses são os gates que o conferem antes.
