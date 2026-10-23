# Cobertura

> [↑ README](../README.md#cobertura)

O gate é de 95% no total, com linhas e ramos somados (`fail_under` no `.coveragerc`), e o código novo precisa de 90% (`make diff-cover`), como pede o ADR-041. Ficam fora da medição os `__init__.py` (todos vazios), o `if __name__ == "__main__"` de cada um dos cinco processos (`# pragma: no cover`) e os `Protocol` puros dos repositórios e da unidade de trabalho (`omit` do `.coveragerc`).

As tabelas da [seção Cobertura do README](../README.md#cobertura), por contexto e camada, não são digitadas: o [`scripts/cobertura_resumo.py`](../scripts/cobertura_resumo.py) as escreve entre os marcadores a partir do `coverage.xml` do `make test`, com o link do run do CI que tem o mesmo `src/`. O `make test` reprova o trecho editado à mão, com as contas erradas, com a taxa fora do formato, com um contexto que não existe ou com o link de outro repositório ([`test_documentacao.py`](../tests/unitarios/test_documentacao.py)). O CI repete a medida, por pacote, no summary do job `test` de cada run ([Actions → CI](https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-billing-service/actions/workflows/ci.yml)), com o `diff-cover` do pull request logo abaixo.

## Para refazer as tabelas

Para refazer as tabelas depois de mudar `src/`, com o número do run do CI em `RUN`:

```bash
make test
uv run python scripts/cobertura_resumo.py coverage.xml --readme README.md --run "https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-billing-service/actions/runs/$RUN"
```

O `make cobertura-readme RUN=<id>` faz o mesmo com o `coverage.xml` do artefato `relatorios-de-teste` desse run, que o `gh` baixa em `reports/`. Sem o `coverage.xml`, ou com os marcadores fora do lugar, o script sai com 1 e uma linha de erro, sem tocar no README.

O `make diff-cover` lê o `coverage.xml` do `make test` e exige que ele exista e seja mais novo que o código de `src/`: com o XML ausente ou velho recusa e manda rodar o `make test` antes, em vez de passar sem medir nada. Mede também o arquivo novo que ainda não foi para o git (`--include-untracked`). O `make check` já roda os dois na ordem certa.

## Oscilação

Com o mesmo `src/`, o `make test` no macOS e o CI medem o mesmo valor. Conferido em 23/10/2026: o macOS mediu 4.551 linhas e 624 ramos, todos cobertos (100,00%), os mesmos números do run 39935825688 que a tabela do README cita. A tabela vale para o `src/` desse run; se uma medida local diferir, compare primeiro o `src/` e o `.coveragerc` e veja o `diff-cover` do pull request, no summary do job `test`.
