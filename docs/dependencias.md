# Dependências automatizadas

> [↑ README](../README.md#cicd)

O Dependabot ([`.github/dependabot.yml`](../.github/dependabot.yml)) abre pull requests para três ecossistemas: `pyproject.toml` e `uv.lock` (uv), as actions dos workflows e as imagens base do `Dockerfile`.

- **Agenda**: uma vez por semana, às segundas às 06:00 (São Paulo), com no máximo 3 PRs abertos por ecossistema.
- **Grupos**: patch e minor de cada ecossistema chegam juntos num PR. Major abre PR próprio, para ser lido com o changelog.
- **Commit e rótulos**: prefixo `chore(deps)`, ou `ci` nas actions (como no OS). O arquivo não declara `labels`: os PRs levam os rótulos padrão do Dependabot (`dependencies` e o do ecossistema), que ele cria no repositório se faltarem. Com `labels` explícito, rótulo que não existe no repositório é ignorado e o Dependabot comenta isso em cada PR.
- **OpenTelemetry**: as bibliotecas `opentelemetry-*` entram no grupo de patch e minor do uv, e não num grupo só delas, que ficaria vazio porque o Dependabot o trata como menos específico que o de patch e minor. O SDK fixa `opentelemetry-api` e `opentelemetry-semantic-conventions` na mesma versão e cada instrumentação fixa a sua, então subir só uma parte não resolve: a família sobe junta.
- **Teto do OpenTelemetry**: `<1.46` no SDK e no exportador e `<0.67` nas instrumentações (`pyproject.toml`) barram também a próxima minor, e não só a major. Pelo grupo só sobe patch (1.45.x e 0.66bN); uma minor nova só entra com os dois tetos subidos à mão, juntos, depois de ler o changelog. O SDK tem teto porque `mensageria/telemetria.py` troca `_status` e `_events` de um span, atributos privados que uma minor nova pode mudar. O teto, porém, não segura o Dependabot: ele o alarga (no OS e na Execução o `sqlalchemy<2.1` virou `<2.2` e a minor nova quebrou o build). Por isso o bloco `uv` do `dependabot.yml` tem um `ignore` de major e minor para `opentelemetry-*`, e o teste do arquivo exige essa entrada.
- **FastAPI**: minor e major ficam de fora (`ignore` no bloco `uv`, que o teste do arquivo também exige) e sobem à mão; o patch sobe pelo grupo. O FastAPI é 0.x e traz mudança incompatível em minor: oito das treze minors entre a 0.144 e a 0.156 têm Breaking Changes nas notas de release (a 0.151, por exemplo, passou a distinguir `null` de campo omitido no corpo JSON). A subida fica segurada até a entrega da fase 4 (17/11/2026), com os três serviços na mesma versão, e é reavaliada depois. O pydantic não entra no `ignore`: a minor dele segue pelo grupo e pelo CI.
- **Docker**: o Dependabot só lê as linhas `FROM` do `Dockerfile`. O uv entra por `FROM ghcr.io/astral-sh/uv:... AS builder`, então sobe pelo bloco do Docker, junto da imagem do Python do runtime. Se o uv passasse a entrar por `COPY --from=<imagem>`, o Dependabot deixaria de vê-lo.
- **Python**: no bloco do Docker, major e minor do Python ficam de fora. A versão sobe à mão, de uma vez, em `.python-version`, `UV_PYTHON` e `python-version` dos workflows, `requires-python` e `python_version` (mypy) do `pyproject.toml`, `sonar.python.version` do `sonar-project.properties` e nas duas imagens do `Dockerfile`. Sem isso, o 3.14 → 3.15 chegaria como "minor" e trocaria só a imagem do runtime. O patch não passa pelo Dependabot: vem do rebuild da tag móvel `python:3.14-slim-trixie`.
- **Checks**: cada PR passa pelos mesmos 9 checks do CI e da varredura de segurança, inclusive o `pip-audit`, o trivy e o gate de cobertura.

Depois do congelamento do código em 06/11/2026:

- **Limite**: o `open-pull-requests-limit` de cada bloco passa de 3 para 0, e o 3 do teste do `dependabot.yml` (`tests/unitarios/test_scripts_ci.py`) muda junto.
- **O que o 0 desliga**: só as atualizações de versão. Os PRs de segurança do Dependabot, habilitados no repositório, não entram no limite e seguem abrindo.

Sobem à mão, porque o Dependabot não os enxerga ou a versão está fixada de propósito:

- `mongo` e `percona/mongodb_exporter`, a cada release do serviço, junto da tabela do `platform` (ver [Implantação](implantacao.md));
- o `mongo` e o RabbitMQ do compose e dos testes de integração;
- o SonarQube do `ci.yml` e o scanner de `scripts/sonar`;
- o trivy, o kubeconform, o gitleaks, o pip-audit e o diff-cover.
