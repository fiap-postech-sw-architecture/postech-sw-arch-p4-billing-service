"""Documentos do servico: README, docs/arquitetura.md e docs/api.

- Os comandos dos blocos ``bash`` do README rodam colados no terminal.
- ``docs/api`` (OpenAPI e colecao Postman) acompanha o app (``make docs-api``), e
  a colecao cumpre o que o README promete dela.
- Toda rota do OpenAPI tem um ``curl`` na secao Exemplos da API do README, e cada
  ``curl`` do Billing casa com uma rota e traz a query, o corpo no schema e os
  cabecalhos que a operacao pede (``Authorization: Bearer``, ``Content-Type``).
- ``docs/arquitetura.md`` segue o codigo: os nomes dos campos e a coluna
  Validador, os indices, os estados dos agregados e, nas tabelas de comandos e
  eventos, quais existem, a routing key, quem publica, quem consome e a fila. Ficam
  de fora o tipo dos campos, as marcas PK e UK do ER e as colunas Resposta (so se
  confere que o evento existe), Etapa esperada (do OS Service) e Quando sai.
- Os links relativos do README e da arquitetura caem em arquivo e secao reais.
- A secao Cobertura do README sai de ``scripts/cobertura_resumo.py``.
"""

from __future__ import annotations

import copy
import importlib
import json
import os
import re
import shlex
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, NamedTuple
from urllib.parse import parse_qsl

import pytest
import yaml
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from opentelemetry.sdk.trace import TracerProvider
from pydantic import ValidationError

from src.banco import preparar_banco
from src.compartilhado.infraestrutura.mensageria import contratos
from src.compartilhado.infraestrutura.mensageria.consumidor import (
    PRODUTOR_DOS_COMANDOS,
)
from src.configuracao import Configuracao
from src.consumidor import FILA
from src.main import criar_app
from src.orcamento.dominio import orcamento as dominio_do_orcamento
from src.orcamento.dominio.orcamento import CanalDecisao, Orcamento, StatusOrcamento
from src.orcamento.infraestrutura.repository import MongoOrcamentoRepository
from src.pagamento.dominio.estados import (
    TRANSICOES,
    StatusNoProvedor,
    StatusPagamento,
)
from src.pagamento.dominio.pagamento import Pagamento
from src.pagamento.infraestrutura.repository import MongoPagamentoRepository
from src.pagamento.interfaces.dependencies import obter_simular_resultado
from src.precos.dominio.preco import PrecoPeca, PrecoServico
from src.precos.infraestrutura.repository import (
    MongoPrecoPecaRepository,
    MongoPrecoServicoRepository,
)
from tests.conftest import carregar_script
from tests.factories import (
    AGORA,
    ATENDENTE_SUB,
    dinheiro,
    orcamento,
    pagamento,
    situacao,
)
from tests.unitarios.test_cobertura_resumo import XML_DOS_CONTEXTOS

if TYPE_CHECKING:
    from collections.abc import Iterator
    from types import ModuleType
    from uuid import UUID

    from fastapi import FastAPI

RAIZ = Path(__file__).resolve().parents[2]
README = RAIZ / "README.md"
ARQUITETURA = RAIZ / "docs" / "arquitetura.md"
OPENAPI = RAIZ / "docs" / "api" / "openapi.json"
COLECAO = RAIZ / "docs" / "api" / "billing.postman_collection.json"
# A cerca pode vir indentada: os passos numerados do README levam o bloco recuado.
_BLOCO = re.compile(
    r"^[ \t]*```(?:bash|sh)\n(.*?)^[ \t]*```[ \t]*$", re.DOTALL | re.MULTILINE
)
_COMENTARIO = re.compile(r"(^|\s)#(\s|$)")
_CERCA = re.compile(r"^[ \t]*```.*?^[ \t]*```[ \t]*$", re.DOTALL | re.MULTILINE)


def _linhas_com_comentario(texto: str) -> list[str]:
    # No zsh interativo (o padrao do macOS) o # nao e comentario: o texto depois
    # dele vira argumento do comando. O que o comando faz fica na prosa acima.
    return [
        linha
        for bloco in _BLOCO.findall(texto)
        for linha in bloco.splitlines()
        if _COMENTARIO.search(linha)
    ]


def test_blocos_de_comando_nao_levam_comentario() -> None:
    texto = README.read_text(encoding="utf-8")

    assert len(_BLOCO.findall(texto)) > 10
    assert _linhas_com_comentario(texto) == []


def test_bloco_indentado_de_passo_numerado_tambem_e_conferido() -> None:
    texto = (
        "1. Passo:\n\n"
        "   ```bash\n"
        "   kubectl get pods  # espera\n"
        "   ```\n\n"
        "```sh\n"
        "make test\n"
        "```\n"
    )

    assert _linhas_com_comentario(texto) == ["   kubectl get pods  # espera"]


@pytest.fixture(scope="module")
def openapi() -> dict[str, Any]:
    config = Configuracao.do_ambiente({"ENVIRONMENT": "test", "MP_MODE": "simulado"})
    app: FastAPI = criar_app(config, provedor=TracerProvider(shutdown_on_exit=False))
    # Ida e volta pelo JSON: e assim que o documento chega ao arquivo.
    documento: dict[str, Any] = json.loads(json.dumps(app.openapi()))
    return documento


@pytest.fixture(scope="module")
def gerador() -> Iterator[ModuleType]:
    with pytest.MonkeyPatch.context() as monkeypatch:
        yield carregar_script("gerar_docs_api", monkeypatch)


def _pares(openapi: dict[str, Any]) -> list[tuple[str, str]]:
    return sorted(
        (metodo, rota) for rota, ops in openapi["paths"].items() for metodo in ops
    )


def _fora_do_schema(
    openapi: dict[str, Any], declaracao: dict[str, Any], valor: object, onde: str
) -> list[str]:
    """Erros de ``valor`` contra o schema JSON de um requestBody ou de uma resposta."""
    schema = declaracao.get("content", {}).get("application/json", {}).get("schema")
    if schema is None:
        return [f"{onde}: a rota nao declara corpo JSON"]
    validador = Draft202012Validator(
        {**schema, "components": openapi["components"]},
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    )
    return [f"{onde}: {erro.message}" for erro in validador.iter_errors(valor)]


# O JSON Schema que o Pydantic gera para o Decimal tem o pattern sem ancora no
# fim ("abc" passa), entao o corpo tambem passa pelo modelo que a rota usa.
_MODULOS_DOS_SCHEMAS = (
    "src.precos.interfaces.schemas",
    "src.orcamento.interfaces.schemas",
    "src.pagamento.interfaces.schemas",
)


def _recusado_pelo_modelo(
    declaracao: dict[str, Any], valor: object, onde: str
) -> list[str]:
    """Erros do modelo Pydantic do ``requestBody`` (o mesmo que a API aplica)."""
    ref = declaracao["content"]["application/json"]["schema"]["$ref"]
    nome = ref.rsplit("/", 1)[1]
    [modelo] = [
        getattr(modulo, nome)
        for modulo in map(importlib.import_module, _MODULOS_DOS_SCHEMAS)
        if hasattr(modulo, nome)
    ]
    try:
        modelo.model_validate(valor)
    except ValidationError as erro:
        return [
            f"{onde}: o modelo {nome} recusa {'.'.join(map(str, e['loc']))}: {e['msg']}"
            for e in erro.errors()
        ]
    return []


# docs/api: o OpenAPI e a colecao Postman.


def test_openapi_versionado_e_o_do_app(openapi: dict[str, Any]) -> None:
    versionado = json.loads(OPENAPI.read_text(encoding="utf-8"))

    assert versionado == openapi, (
        "docs/api/openapi.json atras do app: rode make docs-api"
    )


def test_openapi_tem_as_rotas_do_simulador_e_nenhuma_de_metricas(
    openapi: dict[str, Any],
) -> None:
    rotas = {rota for _, rota in _pares(openapi)}

    assert "/simulador/checkout/{pagamento_id}" in rotas
    assert "/metrics" not in rotas


def _requisicoes(colecao: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for pasta in colecao["item"] for item in pasta["item"]]


def _par(pedido: dict[str, Any]) -> tuple[str, str]:
    """(metodo, rota) da requisicao; toda variavel do caminho vira ``{}``."""
    caminho = "/" + "/".join(pedido["url"]["path"])
    return pedido["method"].lower(), re.sub(r"\{\{\w+\}\}", "{}", caminho)


def _par_do_openapi(metodo: str, rota: str) -> tuple[str, str]:
    return metodo, re.sub(r"\{\w+\}", "{}", rota)


def test_colecao_postman_tem_uma_requisicao_por_rota_do_openapi(
    openapi: dict[str, Any],
) -> None:
    colecao = json.loads(COLECAO.read_text(encoding="utf-8"))

    pares = sorted(_par(item["request"]) for item in _requisicoes(colecao))

    assert pares == sorted(_par_do_openapi(m, r) for m, r in _pares(openapi))


def test_colecao_postman_e_a_que_o_script_gera(
    openapi: dict[str, Any], gerador: ModuleType
) -> None:
    colecao = json.loads(COLECAO.read_text(encoding="utf-8"))

    assert colecao == gerador.colecao(openapi), "colecao atras do script: make docs-api"


_TOKEN_DA_COLECAO = {
    "type": "bearer",
    "bearer": [{"key": "token", "value": "{{token}}", "type": "string"}],
}
_CORPO_JSON = {"key": "Content-Type", "value": "application/json"}
# O que a API do Billing nao devolve em resposta nenhuma: a descricao da colecao
# diz de onde cada um vem (README, Exemplos da API).
_DE_FORA = ("token", "ordem_id", "pagamento_id", "token_do_link", "token_do_checkout")
_GUARDA = re.compile(
    r"if \(pm\.response\.code === (\d+)( && pm\.response\.json\(\)\.length)?\) "
    r'pm\.collectionVariables\.set\("(\w+)", '
    r"pm\.response\.json\(\)((?:\[\d+\]|\.\w+)+)\);"
)


def _acessa(valor: Any, acesso: str) -> bool:
    """O ``valor`` tem o campo que o acesso (``[0].id``, ``.codigo``) nomeia?"""
    for trecho in re.findall(r"\[\d+\]|\.\w+", acesso):
        try:
            valor = valor[int(trecho[1:-1])] if trecho[0] == "[" else valor[trecho[1:]]
        except (IndexError, KeyError, TypeError):
            return False
    return True


def _guardadas(colecao: dict[str, Any]) -> tuple[set[str], list[str]]:
    """Variaveis que um script de teste grava e os erros dos scripts."""
    guardadas: set[str] = set()
    erros: list[str] = []
    for item in _requisicoes(colecao):
        metodo, rota = _par(item["request"])
        onde = f"{metodo.upper()} {rota}"
        resposta = item["response"][0] if item["response"] else {"code": 0, "body": ""}
        for evento in item.get("event", []):
            for linha in evento["script"]["exec"]:
                achado = _GUARDA.fullmatch(linha)
                if achado is None:
                    erros.append(f"{onde}: script fora do molde de uma linha")
                    continue
                status, lista_conferida, variavel, acesso = achado.groups()
                guardadas.add(variavel)
                if int(status) != resposta["code"]:
                    erros.append(f"{onde}: o script espera {status}")
                if "[" in acesso and not lista_conferida:
                    # A lista vazia e resposta valida, e [0].id nela e TypeError.
                    erros.append(f"{onde}: {acesso} sem conferir que a lista tem item")
                corpo = str(resposta["body"])
                if not corpo or not _acessa(json.loads(corpo), acesso):
                    erros.append(f"{onde}: {acesso} nao esta na resposta de exemplo")
    return guardadas, erros


def _promessas_quebradas(openapi: dict[str, Any], colecao: dict[str, Any]) -> list[str]:
    """O que o README promete da colecao e ela nao cumpre.

    O token da colecao como bearer, herdado pelas rotas com JWT, e ``noauth`` nas
    outras (sondas, link publico, webhook e simulador); ``Content-Type`` em todo
    corpo; a URL a partir de ``{{baseUrl}}``; uma resposta de exemplo por
    requisicao, com corpo (menos a do 204); e todo id que as rotas usam gravado
    por um script de teste, ou declarado na descricao como vindo de fora.
    """
    erros = []
    if colecao.get("auth") != _TOKEN_DA_COLECAO:
        erros.append("colecao sem o token {{token}} como bearer")
    for item in _requisicoes(colecao):
        pedido = item["request"]
        metodo, rota = _par(pedido)
        onde = f"{metodo.upper()} {rota}"
        operacao = next(
            ops[m]
            for r, ops in openapi["paths"].items()
            for m in ops
            if _par_do_openapi(m, r) == (metodo, rota)
        )
        protegida = "security" in operacao
        if pedido.get("auth") != (None if protegida else {"type": "noauth"}):
            erros.append(f"{onde}: auth fora do padrao (token so nas rotas com JWT)")
        if "body" in pedido and _CORPO_JSON not in pedido["header"]:
            erros.append(f"{onde}: corpo sem Content-Type: application/json")
        url = pedido["url"]
        if url["host"] != ["{{baseUrl}}"] or not url["raw"].startswith("{{baseUrl}}/"):
            erros.append(f"{onde}: URL sem a variavel baseUrl")
        if not item["response"]:
            erros.append(f"{onde}: sem resposta de exemplo")
        elif item["response"][0]["code"] != 204 and not item["response"][0]["body"]:
            erros.append(f"{onde}: resposta de exemplo sem corpo")
    guardadas, erros_dos_scripts = _guardadas(colecao)
    usadas = set(re.findall(r"\{\{(\w+)\}\}", json.dumps(colecao["item"])))
    sem_dono = usadas - {"baseUrl", *_DE_FORA} - guardadas
    erros += [f"variavel {nome} sem script que a grave" for nome in sorted(sem_dono)]
    erros += [
        f"variavel {nome} de fora sem dizer de onde vem na descricao"
        for nome in _DE_FORA
        if nome not in colecao["info"]["description"]
    ]
    return erros + erros_dos_scripts


def test_colecao_versionada_e_a_do_script_cumprem_o_que_o_readme_promete(
    openapi: dict[str, Any], gerador: ModuleType
) -> None:
    assert _promessas_quebradas(openapi, json.loads(COLECAO.read_text("utf-8"))) == []
    assert _promessas_quebradas(openapi, gerador.colecao(openapi)) == []


def test_colecao_que_quebra_cada_promessa_e_apontada(openapi: dict[str, Any]) -> None:
    colecao = json.loads(COLECAO.read_text(encoding="utf-8"))
    itens = {_par(item["request"]): item for item in _requisicoes(colecao)}
    sondas = ("get", "/api/v1/saude")
    del colecao["auth"]
    del itens[sondas]["request"]["auth"]
    itens["get", "/api/v1/precos/servicos"]["request"]["auth"] = {"type": "noauth"}
    itens["post", "/api/v1/precos/servicos"]["request"]["header"] = []
    itens["get", "/api/v1/precos/pecas"]["request"]["url"]["raw"] = (
        "http://127.0.0.1:8002/api/v1/precos/pecas?limit=20"
    )
    itens["get", "/api/v1/precos/pecas/{}"]["response"] = []
    itens["get", "/simulador/checkout/{}"]["response"][0]["body"] = ""
    del itens["post", "/api/v1/precos/pecas"]["event"]
    itens["get", "/api/v1/orcamentos"]["event"][0]["script"]["exec"] = [
        "pm.collectionVariables.set('orcamento_id', 1);"
    ]
    colecao["info"]["description"] = colecao["info"]["description"].replace(
        "token_do_checkout", "x"
    )

    assert sorted(_promessas_quebradas(openapi, colecao)) == [
        "GET /api/v1/orcamentos: script fora do molde de uma linha",
        "GET /api/v1/precos/pecas/{}: sem resposta de exemplo",
        "GET /api/v1/precos/pecas: URL sem a variavel baseUrl",
        "GET /api/v1/precos/servicos: auth fora do padrao (token so nas rotas com JWT)",
        "GET /api/v1/saude: auth fora do padrao (token so nas rotas com JWT)",
        "GET /simulador/checkout/{}: resposta de exemplo sem corpo",
        "POST /api/v1/precos/servicos: corpo sem Content-Type: application/json",
        "colecao sem o token {{token}} como bearer",
        "variavel orcamento_id sem script que a grave",
        "variavel sku sem script que a grave",
        "variavel token_do_checkout de fora sem dizer de onde vem na descricao",
    ]


def test_script_de_teste_com_status_ou_campo_que_a_resposta_nao_tem_e_apontado() -> (
    None
):
    colecao = json.loads(COLECAO.read_text(encoding="utf-8"))
    itens = {_par(item["request"]): item for item in _requisicoes(colecao)}
    itens["post", "/api/v1/precos/servicos"]["response"][0]["code"] = 200
    resposta = itens["get", "/api/v1/orcamentos"]["response"][0]
    resposta["body"] = "[]"
    itens["post", "/api/v1/precos/pecas"]["response"][0]["body"] = ""

    guardadas, erros = _guardadas(colecao)

    assert guardadas == {"codigo", "sku", "orcamento_id"}
    assert sorted(erros) == [
        "GET /api/v1/orcamentos: [0].id nao esta na resposta de exemplo",
        "POST /api/v1/precos/pecas: .sku nao esta na resposta de exemplo",
        "POST /api/v1/precos/servicos: o script espera 201",
    ]


def test_script_que_le_a_lista_sem_conferir_que_tem_item_e_apontado() -> None:
    colecao = json.loads(COLECAO.read_text(encoding="utf-8"))
    itens = {_par(item["request"]): item for item in _requisicoes(colecao)}
    itens["get", "/api/v1/orcamentos"]["event"][0]["script"]["exec"] = [
        'if (pm.response.code === 200) pm.collectionVariables.set("orcamento_id", '
        "pm.response.json()[0].id);"
    ]

    guardadas, erros = _guardadas(colecao)

    assert guardadas == {"codigo", "sku", "orcamento_id"}
    assert erros == ["GET /api/v1/orcamentos: [0].id sem conferir que a lista tem item"]


def _erros_do_exemplo(openapi: dict[str, Any], item: dict[str, Any]) -> list[str]:
    """O corpo e as respostas de exemplo de uma requisicao contra os schemas."""
    metodo, rota = _par(item["request"])
    operacao = next(
        ops[m]
        for r, ops in openapi["paths"].items()
        for m in ops
        if _par_do_openapi(m, r) == (metodo, rota)
    )
    onde = f"{metodo.upper()} {rota}"
    erros: list[str] = []
    if "requestBody" in operacao:
        corpo = json.loads(item["request"].get("body", {}).get("raw", "null"))
        erros += _fora_do_schema(openapi, operacao["requestBody"], corpo, onde)
        erros += _recusado_pelo_modelo(operacao["requestBody"], corpo, onde)
    elif "body" in item["request"]:
        erros.append(f"{onde}: corpo numa rota sem requestBody")
    for resposta in item["response"]:
        declarada = operacao["responses"][str(resposta["code"])]
        if not resposta["body"]:
            continue
        # Sem Content-Type na resposta de exemplo, o corpo e JSON.
        tipo = {h["key"].lower(): h["value"] for h in resposta["header"]}.get(
            "content-type", "application/json"
        )
        if tipo.startswith("text/html"):
            if "text/html" not in declarada.get("content", {}):
                erros.append(f"{onde} resposta: a rota nao declara text/html")
        else:
            valor = json.loads(resposta["body"])
            erros += _fora_do_schema(openapi, declarada, valor, f"{onde} resposta")
    return erros


def test_exemplos_da_colecao_seguem_o_schema_da_rota(openapi: dict[str, Any]) -> None:
    colecao = json.loads(COLECAO.read_text(encoding="utf-8"))

    erros = [
        erro
        for item in _requisicoes(colecao)
        for erro in _erros_do_exemplo(openapi, item)
    ]

    assert erros == []


def test_exemplo_com_corpo_a_mais_ou_fora_do_schema_ou_do_formato_reprova(
    openapi: dict[str, Any],
) -> None:
    colecao = json.loads(COLECAO.read_text(encoding="utf-8"))
    itens = {_par(item["request"]): item for item in _requisicoes(colecao)}
    itens["get", "/api/v1/saude"]["request"]["body"] = {"mode": "raw", "raw": "{}"}
    itens["delete", "/api/v1/precos/pecas/{}"]["response"][0]["body"] = "{}"
    peca = itens["post", "/api/v1/precos/pecas"]
    peca["request"]["body"]["raw"] = json.dumps({"sku": "pec-minuscula", "nome": "x"})
    decisao = itens["post", "/api/v1/orcamentos/{}/decisao"]
    corpo = json.loads(decisao["response"][0]["body"])
    corpo["id"] = "nao-e-uuid"
    corpo["criado_em"] = "ontem de manha"
    decisao["response"][0]["body"] = json.dumps(corpo)

    assert sorted(_erros_do_exemplo(openapi, itens["get", "/api/v1/saude"])) == [
        "GET /api/v1/saude: corpo numa rota sem requestBody"
    ]
    assert _erros_do_exemplo(openapi, itens["delete", "/api/v1/precos/pecas/{}"]) == [
        "DELETE /api/v1/precos/pecas/{} resposta: a rota nao declara corpo JSON"
    ]
    assert sorted(_erros_do_exemplo(openapi, peca)) == [
        "POST /api/v1/precos/pecas: 'pec-minuscula' does not match "
        "'^[A-Z0-9]+(?:-[A-Z0-9]+)*$'",
        "POST /api/v1/precos/pecas: 'preco' is a required property",
        "POST /api/v1/precos/pecas: o modelo CadastrarPecaRequest recusa preco: "
        "Field required",
        "POST /api/v1/precos/pecas: o modelo CadastrarPecaRequest recusa sku: "
        "String should match pattern '^[A-Z0-9]+(?:-[A-Z0-9]+)*$'",
    ]
    assert sorted(_erros_do_exemplo(openapi, decisao)) == [
        "POST /api/v1/orcamentos/{}/decisao resposta: 'nao-e-uuid' is not a 'uuid'",
        "POST /api/v1/orcamentos/{}/decisao resposta: "
        "'ontem de manha' is not a 'date-time'",
    ]


def test_exemplo_html_em_rota_que_so_declara_json_reprova(
    openapi: dict[str, Any],
) -> None:
    colecao = json.loads(COLECAO.read_text(encoding="utf-8"))
    itens = {_par(item["request"]): item for item in _requisicoes(colecao)}
    resposta = itens["get", "/api/v1/saude"]["response"][0]
    resposta["header"] = [{"key": "Content-Type", "value": "text/html; charset=utf-8"}]
    resposta["body"] = "<p>ola</p>"

    assert _erros_do_exemplo(openapi, itens["get", "/api/v1/saude"]) == [
        "GET /api/v1/saude resposta: a rota nao declara text/html"
    ]


class _CheckoutFalso:
    """O caso de uso da pagina de checkout, sem banco: o pagamento da colecao."""

    def consultar(self, pagamento_id: UUID, token: str | None) -> SimpleNamespace:
        return SimpleNamespace(
            id=pagamento_id, moeda="BRL", valor=Decimal("570.00"), status="SOLICITADO"
        )


def test_exemplo_html_do_checkout_e_a_pagina_que_a_rota_devolve(
    gerador: ModuleType,
) -> None:
    variaveis = gerador.VARIAVEIS
    config = Configuracao.do_ambiente(
        {
            "ENVIRONMENT": "test",
            "MP_MODE": "simulado",
            "BILLING_PUBLIC_URL": variaveis["baseUrl"],
        }
    )
    app = criar_app(config, provedor=TracerProvider(shutdown_on_exit=False))
    app.dependency_overrides[obter_simular_resultado] = _CheckoutFalso

    resposta = TestClient(app).get(
        f"/simulador/checkout/{variaveis['pagamento_id']}",
        params={"token": variaveis["token_do_checkout"]},
    )

    colecao = json.loads(COLECAO.read_text(encoding="utf-8"))
    [item] = [
        item
        for item in _requisicoes(colecao)
        if _par(item["request"]) == ("get", "/simulador/checkout/{}")
    ]
    exemplo = item["response"][0]
    assert resposta.status_code == exemplo["code"] == 200
    assert {"key": "Content-Type", "value": resposta.headers["content-type"]} in (
        exemplo["header"]
    )
    assert resposta.text == exemplo["body"], "pagina de checkout mudou: make docs-api"


def test_rota_nova_sem_exemplo_reprova_a_geracao(
    openapi: dict[str, Any], gerador: ModuleType
) -> None:
    com_rota_nova = copy.deepcopy(openapi)
    com_rota_nova["paths"]["/api/v1/nova"] = {"get": {"summary": "x", "tags": ["X"]}}

    with pytest.raises(ValueError, match="faltam \\[\\('get', '/api/v1/nova'\\)\\]"):
        gerador.colecao(com_rota_nova)


def test_exemplo_de_rota_que_o_openapi_nao_tem_reprova_a_geracao(
    openapi: dict[str, Any], gerador: ModuleType
) -> None:
    sem_a_rota = copy.deepcopy(openapi)
    del sem_a_rota["paths"]["/api/v1/saude"]

    with pytest.raises(ValueError, match="sobram \\[\\('get', '/api/v1/saude'\\)\\]"):
        gerador.colecao(sem_a_rota)


@pytest.fixture
def docs_api_temporario(
    gerador: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Path:
    """O ``main()`` do gerador escrevendo num ``docs/api`` de ``tmp_path``."""
    destino = tmp_path / "docs" / "api"
    monkeypatch.setattr(gerador, "RAIZ", tmp_path)
    monkeypatch.setattr(gerador, "DOCS_API", destino)
    monkeypatch.setattr(gerador, "OPENAPI", destino / OPENAPI.name)
    monkeypatch.setattr(gerador, "COLECAO", destino / COLECAO.name)
    return destino


def test_main_grava_os_arquivos_versionados_byte_a_byte_e_de_novo_igual(
    gerador: ModuleType,
    docs_api_temporario: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    versionados = {
        OPENAPI.name: OPENAPI.read_bytes(),
        COLECAO.name: COLECAO.read_bytes(),
    }

    assert gerador.main() == 0
    primeira = {p.name: p.read_bytes() for p in docs_api_temporario.iterdir()}
    assert gerador.main() == 0

    assert primeira == versionados, "docs/api atras do codigo: rode make docs-api"
    assert {p.name: p.read_bytes() for p in docs_api_temporario.iterdir()} == primeira
    assert capsys.readouterr().out.splitlines()[0] == (
        f"atualizados: docs/api/{OPENAPI.name} e docs/api/{COLECAO.name}"
    )


def test_main_com_rota_sem_exemplo_reprova_sem_gravar(
    gerador: ModuleType,
    docs_api_temporario: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sem_a_sonda = dict(gerador.EXEMPLOS)
    del sem_a_sonda["get", "/api/v1/saude"]
    monkeypatch.setattr(gerador, "EXEMPLOS", sem_a_sonda)

    assert gerador.main() == 1
    assert not docs_api_temporario.exists()
    assert "faltam [('get', '/api/v1/saude')]" in capsys.readouterr().err


# README: um curl por rota do OpenAPI.

# Opcoes do curl que levam valor; as demais (-s, -k, -f...) sao flags.
_DADOS = frozenset({"-d", "--data", "--data-raw", "--data-binary", "--json"})
_OPCOES_COM_VALOR = _DADOS | frozenset(
    {"-X", "--request", "-H", "--header", "-o", "--output", "-w", "--write-out"}
    | {"-m", "--max-time", "-u", "--user"}
)
_VARIAVEL = re.compile(r"\$\{?\w+\}?")


class Curl(NamedTuple):
    metodo: str
    caminho: str
    corpo: str | None
    # Os nomes dos cabecalhos, em minusculas.
    cabecalhos: frozenset[str] = frozenset()
    # O que vem depois do ``?`` e o valor do cabecalho Authorization.
    query: str = ""
    autorizacao: str = ""


def _curl(trecho: str) -> Curl | None:
    """O ``curl`` do comeco de ``trecho``, ate o pipe, o ``)`` ou o ``;``."""
    lexer = shlex.shlex(trecho, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    metodo, url, corpo, autorizacao = "", "", None, ""
    cabecalhos: set[str] = set()
    palavras = iter(lexer)
    for palavra in palavras:
        if palavra[:1] in {"|", ")", ";", "&", "<", ">"}:
            break
        if palavra in _OPCOES_COM_VALOR:
            valor = next(palavras)
            metodo = valor.upper() if palavra in {"-X", "--request"} else metodo
            corpo = valor if palavra in _DADOS else corpo
            if palavra in {"-H", "--header"}:
                nome, _, conteudo = valor.partition(":")
                cabecalhos.add(nome.strip().lower())
                if nome.strip().lower() == "authorization":
                    autorizacao = conteudo.strip()
            elif palavra == "--json":  # o curl ja manda o Content-Type
                cabecalhos.add("content-type")
        elif not palavra.startswith("-") and not url:
            url = palavra
    caminho = re.match(r"(?:https?://)?[^/]+(/[^?#]*)(?:\?([^#]*))?", url)
    if caminho is None:
        return None
    metodo = metodo or ("POST" if corpo else "GET")
    return Curl(
        metodo, caminho[1], corpo, frozenset(cabecalhos), caminho[2] or "", autorizacao
    )


def _curls(texto: str) -> list[Curl]:
    chamadas = []
    for bloco in _BLOCO.findall(texto):
        for linha in bloco.replace("\\\n", " ").splitlines():
            for trecho in re.split(r"(?<![\w-])curl\s", linha)[1:]:
                chamada = _curl(trecho)
                if chamada is not None:
                    chamadas.append(chamada)
    return chamadas


def _casa(caminho: str, rota: str) -> bool:
    # O parametro da rota casa com uma variavel do shell ($ORCAMENTO_ID), nunca com
    # um texto fixo: `/precos/servicos/SRV-X` nao vale por `/{codigo}`.
    partes, modelo = caminho.split("/"), rota.split("/")
    return len(partes) == len(modelo) and all(
        parte == trecho
        or (trecho.startswith("{") and _VARIAVEL.fullmatch(parte) is not None)
        for parte, trecho in zip(partes, modelo, strict=True)
    )


def _rota(openapi: dict[str, Any], curl: Curl) -> tuple[str, str] | None:
    for metodo, rota in _pares(openapi):
        if metodo.upper() == curl.metodo and _casa(curl.caminho, rota):
            return metodo, rota
    return None


def test_leitura_dos_curl_do_readme() -> None:
    texto = (
        "```bash\n"
        "TOKEN=$(curl -s -X POST localhost:8000/api/v1/autenticacao/login \\\n"
        "  -H 'Content-Type: application/json' \\\n"
        '  -d \'{"email": "a@b.dev"}\' | jq -r .access_token)\n'
        'curl -s "localhost:8000/api/v1/x?limit=5" -H "Authorization: x"\n'
        "curl -s -X DELETE localhost:8000/api/v1/x/$ID; echo\n"
        "curl -sk https://localhost/billing/api/v1/saude\n"
        'curl -sk "$BASE_URL/api/v1/w?data.id=$REF&type=payment" '
        '-H "Authorization: Bearer $TOKEN"\n'
        'curl -s localhost:8000/x -d \'{"r": "\'"$REFRESH"\'"}\'\n'
        "curl -s --data-binary '{\"a\": 1}' localhost:8000/y\n"
        "curl -s --json '{\"b\": 2}' localhost:8000/z\n"
        "curl --version\n"
        "```\n"
    )

    assert _curls(texto) == [
        Curl(
            "POST",
            "/api/v1/autenticacao/login",
            '{"email": "a@b.dev"}',
            frozenset({"content-type"}),
        ),
        Curl(
            "GET",
            "/api/v1/x",
            None,
            frozenset({"authorization"}),
            "limit=5",
            "x",
        ),
        Curl("DELETE", "/api/v1/x/$ID", None),
        Curl("GET", "/billing/api/v1/saude", None),
        Curl(
            "GET",
            "/api/v1/w",
            None,
            frozenset({"authorization"}),
            "data.id=$REF&type=payment",
            "Bearer $TOKEN",
        ),
        Curl("POST", "/x", '{"r": "$REFRESH"}'),
        Curl("POST", "/y", '{"a": 1}'),
        Curl("POST", "/z", '{"b": 2}', frozenset({"content-type"})),
    ]


def test_parametro_da_rota_so_casa_com_variavel_do_shell() -> None:
    assert _casa("/api/v1/pagamentos/$PAGAMENTO_ID", "/api/v1/pagamentos/{id}")
    assert _casa("/api/v1/pagamentos/${PAGAMENTO_ID}", "/api/v1/pagamentos/{id}")
    assert not _casa(
        "/api/v1/precos/servicos/SRV-X", "/api/v1/precos/servicos/{codigo}"
    )
    assert not _casa("/api/v1/pagamentos/$ID/x", "/api/v1/pagamentos/{id}")


def _secao(texto: str, titulo: str) -> str:
    """De ``titulo`` (com os ``#``) ate o proximo titulo do mesmo nivel ou acima."""
    nivel = len(titulo.split(" ", 1)[0])
    resto = texto.split(f"\n{titulo}\n", 1)[1]
    return re.split(rf"\n#{{1,{nivel}}} ", resto, maxsplit=1)[0]


def test_toda_rota_do_openapi_tem_um_curl_na_secao_exemplos_da_api(
    openapi: dict[str, Any],
) -> None:
    exemplos = _secao(README.read_text(encoding="utf-8"), "## Exemplos da API")
    cobertas = {_rota(openapi, curl) for curl in _curls(exemplos)}

    faltam = [par for par in _pares(openapi) if par not in cobertas]

    assert faltam == [], "rota do OpenAPI sem curl no README (secao Exemplos da API)"


def test_curl_fora_da_secao_exemplos_da_api_nao_conta(openapi: dict[str, Any]) -> None:
    texto = (
        "## Como rodar\n\n```bash\ncurl localhost:8002/api/v1/saude\n```\n\n"
        "## Exemplos da API\n\nsem curl\n\n## Outra\n"
    )

    exemplos = _secao(texto, "## Exemplos da API")

    assert _curls(exemplos) == []
    assert _curls(texto) == [Curl("GET", "/api/v1/saude", None)]


# O curl cujo caminho comeca assim e do Billing (o do OS Service passa por /os/ e o
# da borda do kind por /billing/): tem de casar com uma rota do OpenAPI.
_CAMINHOS_DO_BILLING = ("/api/v1/", "/simulador/")
# O webhook le o ``type`` de ``request.query_params``, e o OpenAPI so declara o
# ``data.id`` (src/ nao muda neste PR).
_QUERY_FORA_DO_OPENAPI = {("post", "/api/v1/webhooks/mercadopago"): {"type"}}


def _erros_de_acesso(
    openapi: dict[str, Any], par: tuple[str, str], curl: Curl, onde: str
) -> list[str]:
    """Token com o esquema da operacao, e a query que a operacao declara."""
    operacao = openapi["paths"][par[1]][par[0]]
    erros: list[str] = []
    if "security" in operacao:
        esquemas = openapi["components"]["securitySchemes"]
        aceitos = {
            esquemas[nome]["scheme"].capitalize()
            for requisito in operacao["security"]
            for nome in requisito
        }
        if "authorization" not in curl.cabecalhos:
            erros.append(f"{onde}: rota protegida sem Authorization")
        elif not any(re.fullmatch(rf"{e} \S+", curl.autorizacao) for e in aceitos):
            erros.append(
                f"{onde}: Authorization {curl.autorizacao!r} "
                f"fora do esquema {sorted(aceitos)}"
            )
    parametros = [p for p in operacao.get("parameters", []) if p["in"] == "query"]
    declarados = {p["name"] for p in parametros}
    obrigatorios = {p["name"] for p in parametros if p.get("required")}
    usados = {nome for nome, _ in parse_qsl(curl.query, keep_blank_values=True)}
    fora = _QUERY_FORA_DO_OPENAPI.get(par, set())
    erros += [
        f"{onde}: query {nome} que a rota nao declara"
        for nome in sorted(usados - declarados - fora)
    ]
    erros += [
        f"{onde}: sem a query obrigatoria {nome}"
        for nome in sorted(obrigatorios - usados)
    ]
    return erros


def _erros_do_curl(openapi: dict[str, Any], curl: Curl) -> list[str]:
    """O que a operacao pede e o curl nao traz, ou o curl que nenhuma rota tem.

    Rota, query e esquema do token; corpo e Content-Type.
    """
    onde = f"{curl.metodo} {curl.caminho}"
    par = _rota(openapi, curl)
    if par is None:
        if curl.caminho.startswith(_CAMINHOS_DO_BILLING):
            return [f"{onde}: nenhuma rota do OpenAPI casa com o curl"]
        return []
    operacao = openapi["paths"][par[1]][par[0]]
    erros = _erros_de_acesso(openapi, par, curl, onde)
    if "requestBody" not in operacao:
        erros += [f"{onde}: corpo numa rota sem requestBody"] if curl.corpo else []
    elif curl.corpo is None:
        erros.append(f"{onde}: rota com requestBody e curl sem corpo")
    else:
        if "content-type" not in curl.cabecalhos:
            erros.append(f"{onde}: corpo sem Content-Type (a API responderia 422)")
        corpo = json.loads(curl.corpo)
        erros += _fora_do_schema(openapi, operacao["requestBody"], corpo, onde)
        erros += _recusado_pelo_modelo(operacao["requestBody"], corpo, onde)
    return erros


def test_curl_do_readme_tem_o_token_o_corpo_e_o_content_type_que_a_rota_pede(
    openapi: dict[str, Any],
) -> None:
    curls = _curls(README.read_text(encoding="utf-8"))

    erros = [erro for curl in curls for erro in _erros_do_curl(openapi, curl)]

    assert len(curls) > 25
    assert erros == []


def test_curl_sem_token_sem_content_type_ou_com_corpo_errado_reprova(
    openapi: dict[str, Any],
) -> None:
    servicos = "/api/v1/precos/servicos"
    corpo = '{"codigo": "srv-x", "nome": "x", "descricao": "y", "preco": "1.00"}'
    com_token = frozenset({"authorization"})
    bearer = "Bearer $TOKEN"

    assert _erros_do_curl(openapi, Curl("POST", servicos, None)) == [
        f"POST {servicos}: rota protegida sem Authorization",
        f"POST {servicos}: rota com requestBody e curl sem corpo",
    ]
    assert _erros_do_curl(
        openapi, Curl("POST", servicos, corpo, com_token, "", bearer)
    ) == [
        f"POST {servicos}: corpo sem Content-Type (a API responderia 422)",
        f"POST {servicos}: 'srv-x' does not match '^[A-Z0-9]+(?:-[A-Z0-9]+)*$'",
        f"POST {servicos}: o modelo CadastrarServicoRequest recusa codigo: "
        "String should match pattern '^[A-Z0-9]+(?:-[A-Z0-9]+)*$'",
    ]
    # O pattern que o Pydantic gera para o Decimal deixa "abc" passar no JSON
    # Schema; so o modelo da rota o recusa.
    assert _erros_do_curl(
        openapi,
        Curl(
            "POST",
            servicos,
            '{"codigo": "SRV-X", "nome": "x", "descricao": "y", "preco": "abc"}',
            com_token | {"content-type"},
            "",
            bearer,
        ),
    ) == [
        f"POST {servicos}: o modelo CadastrarServicoRequest recusa preco: "
        "Input should be a valid decimal"
    ]
    assert _erros_do_curl(openapi, Curl("GET", "/api/v1/saude", "{}")) == [
        "GET /api/v1/saude: corpo numa rota sem requestBody"
    ]


_COM_TOKEN = frozenset({"authorization"})
_BEARER = "Bearer $TOKEN"


@pytest.mark.parametrize(
    ("curl", "esperado"),
    [
        pytest.param(
            Curl("GET", "/api/v1/orcamentos", None, _COM_TOKEN, "ordem=$O", _BEARER),
            [
                "GET /api/v1/orcamentos: query ordem que a rota nao declara",
                "GET /api/v1/orcamentos: sem a query obrigatoria ordem_id",
            ],
            id="nome-da-query-trocado",
        ),
        pytest.param(
            Curl("GET", "/api/v1/orcamentos", None, _COM_TOKEN, "", _BEARER),
            ["GET /api/v1/orcamentos: sem a query obrigatoria ordem_id"],
            id="query-obrigatoria-ausente",
        ),
        pytest.param(
            Curl("GET", "/simulador/checkout/$P", None, frozenset(), "tok=$T"),
            ["GET /simulador/checkout/$P: query tok que a rota nao declara"],
            id="query-do-checkout-trocada",
        ),
        pytest.param(
            Curl(
                "POST",
                "/api/v1/webhooks/mercadopago",
                "{}",
                frozenset({"content-type"}),
                "tipo=x",
            ),
            [
                "POST /api/v1/webhooks/mercadopago: query tipo que a rota nao declara",
            ],
            id="query-do-webhook-que-nao-existe",
        ),
        pytest.param(
            Curl("GET", "/api/v1/pagamentos/$P", None, _COM_TOKEN, "", "Basic $TOKEN"),
            [
                "GET /api/v1/pagamentos/$P: Authorization 'Basic $TOKEN' "
                "fora do esquema ['Bearer']"
            ],
            id="esquema-basic",
        ),
        pytest.param(
            Curl("GET", "/api/v1/pagamentos/$P", None, _COM_TOKEN, "", "Bearer"),
            [
                "GET /api/v1/pagamentos/$P: Authorization 'Bearer' "
                "fora do esquema ['Bearer']"
            ],
            id="bearer-sem-credencial",
        ),
        pytest.param(
            Curl("GET", "/api/v1/pagamento/$P", None, _COM_TOKEN, "", _BEARER),
            ["GET /api/v1/pagamento/$P: nenhuma rota do OpenAPI casa com o curl"],
            id="caminho-sem-rota",
        ),
        pytest.param(
            Curl("PATCH", "/api/v1/precos/pecas/$S", "{}", _COM_TOKEN, "", _BEARER),
            ["PATCH /api/v1/precos/pecas/$S: nenhuma rota do OpenAPI casa com o curl"],
            id="metodo-sem-rota",
        ),
        pytest.param(
            Curl("POST", "/os/api/v1/autenticacao/login", "{}"),
            [],
            id="curl-de-outro-servico-fica-de-fora",
        ),
    ],
)
def test_curl_com_query_esquema_ou_rota_errados_reprova(
    openapi: dict[str, Any], curl: Curl, esperado: list[str]
) -> None:
    assert _erros_do_curl(openapi, curl) == esperado


def test_curl_com_o_type_do_webhook_e_o_esquema_bearer_passa(
    openapi: dict[str, Any],
) -> None:
    webhook = Curl(
        "POST",
        "/api/v1/webhooks/mercadopago",
        '{"type": "payment"}',
        frozenset({"content-type", "x-signature"}),
        "data.id=$REFERENCIA&type=payment",
    )
    atendente = Curl(
        "GET", "/api/v1/orcamentos", None, _COM_TOKEN, "ordem_id=$O", _BEARER
    )

    assert _erros_do_curl(openapi, webhook) == []
    assert _erros_do_curl(openapi, atendente) == []


# docs/arquitetura.md e as referencias do README.


def test_readme_aponta_a_arquitetura_e_os_documentos_da_api() -> None:
    texto = README.read_text(encoding="utf-8")

    assert ARQUITETURA.is_file()
    assert "](docs/arquitetura.md)" in texto
    assert "](docs/api/)" in texto


def _ancoras(texto: str) -> set[str]:
    """As ancoras que o GitHub gera para os titulos do Markdown.

    Minusculas, sem pontuacao, espaco vira hifen, e o titulo repetido ganha
    ``-1``, ``-2``... Os blocos de codigo ficam de fora (``#`` neles nao e titulo).
    """
    ancoras: set[str] = set()
    vistas: dict[str, int] = {}
    for titulo in re.findall(r"^#{1,6} (.+)$", _CERCA.sub("", texto), re.MULTILINE):
        base = re.sub(r"[^\w\- ]", "", titulo.strip().lower()).replace(" ", "-")
        repeticao = vistas.get(base, 0)
        vistas[base] = repeticao + 1
        ancoras.add(f"{base}-{repeticao}" if repeticao else base)
    return ancoras


def test_ancora_gerada_como_a_do_github() -> None:
    texto = "# Ação (LGPD)\n```bash\n# nao e titulo\n```\n## Testes\n### Testes\n"

    assert _ancoras(texto) == {"ação-lgpd", "testes", "testes-1"}


def _links_relativos(origem: Path) -> list[tuple[str, Path, str]]:
    """(alvo, arquivo de destino, ancora) dos links de ``origem`` para o repositorio."""
    links = []
    for alvo in re.findall(
        r"\]\(([^)\s]+)\)", _CERCA.sub("", origem.read_text(encoding="utf-8"))
    ):
        caminho, _, ancora = alvo.partition("#")
        # http:, https:, mailto: saem do repositorio.
        if re.match(r"[a-z]+:", caminho) is None:
            links.append((alvo, origem.parent / caminho if caminho else origem, ancora))
    return links


def _quebrados(links: list[tuple[str, Path, str]]) -> list[str]:
    return [
        alvo
        for alvo, destino, ancora in links
        if not destino.exists()
        or (
            ancora != ""
            and destino.suffix == ".md"
            and ancora not in _ancoras(destino.read_text(encoding="utf-8"))
        )
    ]


@pytest.mark.parametrize(
    "documento", [README, ARQUITETURA], ids=["README", "arquitetura"]
)
def test_links_relativos_apontam_arquivo_e_secao_que_existem(documento: Path) -> None:
    links = _links_relativos(documento)

    assert len(links) > 15
    assert _quebrados(links) == [], "link para arquivo ou secao que nao existe"


def test_link_para_arquivo_ou_secao_que_nao_existe_reprova(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_text("# Seção A\n", encoding="utf-8")
    origem = tmp_path / "README.md"
    origem.write_text(
        "# Topo\n[a](docs/a.md#seção-a) [b](#topo) [c](docs/) [d](https://x.dev/#y)\n"
        "[e](docs/b.md) [f](docs/a.md#outra) [g](#fim) [h](docs/a.md)\n"
        "```bash\n[nao e link](nada.md)\n```\n",
        encoding="utf-8",
    )

    assert _quebrados(_links_relativos(origem)) == [
        "docs/b.md",
        "docs/a.md#outra",
        "#fim",
    ]


# docs/arquitetura.md: o modelo de dados, os estados dos agregados e as mensagens,
# lidos do codigo.


def _mermaid(texto: str, tipo: str) -> list[str]:
    return re.findall(rf"```mermaid\n{tipo}\n(.*?)```", texto, re.DOTALL)


class _ColecaoFalsa:
    def __init__(self, nome: str, banco: _BancoFalso) -> None:
        self._nome = nome
        self._banco = banco

    def create_index(self, chaves: Any, **opcoes: Any) -> None:
        self._banco.indices.append((self._nome, chaves, opcoes))

    def replace_one(self, _filtro: object, documento: dict[str, Any], **_: Any) -> None:
        self._banco.documentos[self._nome] = documento

    def update_one(
        self, filtro: dict[str, Any], mudanca: dict[str, Any], **_: Any
    ) -> None:
        self._banco.atualizacoes[self._nome] = (filtro, mudanca)


class _BancoFalso:
    """O que o ``python -m src.banco`` e os repositorios pedem ao MongoDB, anotado."""

    name = "billing"

    def __init__(self) -> None:
        self.indices: list[tuple[str, Any, dict[str, Any]]] = []
        self.validadores: dict[str, dict[str, Any]] = {}
        self.documentos: dict[str, dict[str, Any]] = {}
        self.atualizacoes: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}

    def __getitem__(self, nome: str) -> _ColecaoFalsa:
        return _ColecaoFalsa(nome, self)

    def create_collection(self, nome: str, **opcoes: Any) -> None:
        self.validadores[nome] = opcoes["validator"]["$jsonSchema"]


class _UowEspia:
    """A unidade de trabalho dos repositorios: guarda o documento de cada agregado."""

    sessao = None

    def __init__(self, banco: _BancoFalso) -> None:
        self.banco = banco
        self.gravados: dict[str, dict[str, Any]] = {}

    def registrar(self, agregado: object, colecao: str | None = None) -> None: ...

    def gravar(
        self, colecao: str, _agregado: object, documento: dict[str, Any]
    ) -> None:
        self.gravados[colecao] = documento


def _caminhos(documento: dict[str, Any], prefixo: str = "") -> set[str]:
    """Todo campo do documento: o do objeto ou da lista e o de dentro dele.

    ``linhas[].codigo`` e o campo ``codigo`` de cada item da lista ``linhas``.
    """
    caminhos: set[str] = set()
    for chave, valor in documento.items():
        caminho = f"{prefixo}{chave}"
        caminhos.add(caminho)
        if isinstance(valor, dict):
            caminhos |= _caminhos(valor, f"{caminho}.")
        elif isinstance(valor, list) and valor:
            caminhos |= _caminhos(valor[0], f"{caminho}[].")
    return caminhos


# Gravado uma vez, na criacao durante um comando (MongoUnitOfWork.gravar, com o
# $setOnInsert; tests/integracao/test_outbox.py confere o conteudo).
_ABERTO_POR = {
    "aberto_por",
    "aberto_por.mensagem_id",
    "aberto_por.traceparent",
    "aberto_por.tracestate",
}


def _banco_preparado() -> _BancoFalso:
    banco = _BancoFalso()
    como_o_pymongo: Any = banco
    preparar_banco(como_o_pymongo)
    return banco


def _campos_do_codigo() -> dict[str, set[str]]:
    """Os campos de cada colecao, como o codigo os grava."""
    banco = _banco_preparado()
    uow: Any = _UowEspia(banco)
    MongoPrecoServicoRepository(uow).salvar(
        PrecoServico.cadastrar(
            codigo="SRV-X", nome="n", descricao="d", preco=dinheiro("1.00")
        )
    )
    MongoPrecoPecaRepository(uow).salvar(
        PrecoPeca.cadastrar(sku="PEC-X", nome="n", preco=dinheiro("1.00"))
    )
    aprovado = orcamento()
    aprovado.aprovar(
        canal=CanalDecisao.ATENDENTE, agora=AGORA, decidido_por=ATENDENTE_SUB
    )
    MongoOrcamentoRepository(uow).salvar(aprovado)
    cobrado = pagamento()
    for referencia, status in (
        ("1", StatusNoProvedor.RECUSADO),
        ("2", StatusNoProvedor.APROVADO),
    ):
        cobrado.aplicar_notificacao(
            situacao(cobrado, referencia=referencia, status=status),
            agora=AGORA,
            max_recusas=3,
        )
    cobrado.registrar_estorno_automatico("3", agora=AGORA)
    MongoPagamentoRepository(uow).salvar(cobrado)
    marca_filtro, marca = banco.atualizacoes["versao_do_banco"]
    return {
        "precos_servicos": _caminhos(banco.documentos["precos_servicos"]),
        "precos_pecas": _caminhos(banco.documentos["precos_pecas"]),
        "orcamentos": _caminhos(uow.gravados["orcamentos"]) | _ABERTO_POR,
        "pagamentos": _caminhos(uow.gravados["pagamentos"]) | _ABERTO_POR,
        "outbox": {"_id", *banco.validadores["outbox"]["properties"]},
        "mensagens_processadas": {
            "_id",
            *banco.validadores["mensagens_processadas"]["properties"],
        },
        "versao_do_banco": {*marca_filtro, *marca["$max"]},
    }


def _colunas_do_er(texto: str) -> dict[str, set[str]]:
    """Colecao -> campos do ``erDiagram`` (o tipo e o nome vem antes do resto)."""
    (er,) = _mermaid(texto, "erDiagram")
    return {
        colecao: {linha.split()[1] for linha in corpo.splitlines()}
        for colecao, corpo in re.findall(
            r"^ {4}(\w+) \{\n(.*?)^ {4}\}", er, re.M | re.S
        )
    }


def _tabelas_de_campos(texto: str) -> dict[str, dict[str, str]]:
    """Colecao -> {campo: coluna Validador} das tabelas da secao 3.

    Cada tabela vem depois do titulo ``#### `colecao```; a primeira celula de cada
    linha traz o campo entre crases.
    """
    campos: dict[str, dict[str, str]] = {}
    atual = ""
    for linha in _CERCA.sub("", _secao(texto, "## 3. Modelo de dados")).splitlines():
        if linha.startswith("#"):
            atual = ""
        if titulo := re.fullmatch(r"#### `(\w+)`", linha):
            atual = titulo[1]
            campos[atual] = {}
        elif atual and (
            linha_de_campo := re.match(r"\| `([^`]+)` \| [^|]+ \| ([^|]+) \|", linha)
        ):
            campo, validador = linha_de_campo[1], linha_de_campo[2].strip()
            assert campo not in campos[atual], f"campo repetido: {atual}.{campo}"
            campos[atual][campo] = validador
    return campos


def _de_primeiro_nivel(campos: set[str]) -> set[str]:
    return {campo for campo in campos if not re.search(r"[.\[]", campo)}


def test_er_da_arquitetura_tem_os_campos_de_primeiro_nivel_de_cada_colecao() -> None:
    texto = ARQUITETURA.read_text(encoding="utf-8")
    esperado = {
        colecao: _de_primeiro_nivel(campos)
        for colecao, campos in _campos_do_codigo().items()
    }

    assert _colunas_do_er(texto) == esperado, "campo novo: o ER de docs/arquitetura.md"


def test_tabelas_da_arquitetura_tem_os_campos_de_cada_colecao() -> None:
    texto = ARQUITETURA.read_text(encoding="utf-8")
    no_codigo = _campos_do_codigo()

    no_documento = {c: set(campos) for c, campos in _tabelas_de_campos(texto).items()}

    assert no_documento == no_codigo, "campo novo: a tabela dele em docs/arquitetura.md"


def test_toda_colecao_que_o_init_cria_tem_tabela_na_arquitetura() -> None:
    # Colecao nova com validador e sem indice nao aparece em nenhuma outra
    # comparacao: _campos_do_codigo e a lista escrita a mao.
    banco = _banco_preparado()
    criadas = set(banco.validadores) | {colecao for colecao, *_ in banco.indices}

    no_documento = set(_tabelas_de_campos(ARQUITETURA.read_text(encoding="utf-8")))

    assert len(criadas) == 6
    assert criadas <= no_documento, "colecao nova: a tabela dela em docs/arquitetura.md"
    assert criadas <= set(_campos_do_codigo()), "colecao nova: _campos_do_codigo"


def test_coluna_validador_da_arquitetura_e_a_do_json_schema_do_banco() -> None:
    texto = ARQUITETURA.read_text(encoding="utf-8")
    esquemas = _banco_preparado().validadores

    for colecao, campos in _tabelas_de_campos(texto).items():
        esquema = esquemas.get(colecao, {"required": [], "properties": {}})
        obrigatorios, declarados = set(esquema["required"]), set(esquema["properties"])
        esperado = {
            campo: (
                "obrigatório"
                if campo in obrigatorios
                else "opcional"
                if campo in declarados
                else "não declarado"
            )
            for campo in _de_primeiro_nivel(set(campos))
        }
        no_documento = {c: campos[c] for c in esperado}
        assert no_documento == esperado, colecao


def _indices_do_codigo() -> list[tuple[str, tuple[str, ...], frozenset[str]]]:
    indices = []
    for colecao, chaves, opcoes in _banco_preparado().indices:
        campos = (chaves,) if isinstance(chaves, str) else tuple(c for c, _ in chaves)
        flags = set()
        if opcoes.get("unique"):
            flags.add("único")
        if "partialFilterExpression" in opcoes:
            flags.add("parcial")
        if "expireAfterSeconds" in opcoes:
            flags.add(f"TTL {opcoes['expireAfterSeconds'] // 86400} dias")
        indices.append((colecao, campos, frozenset(flags)))
    return sorted(indices, key=lambda indice: (indice[0], indice[1]))


def _indices_do_documento(
    texto: str,
) -> list[tuple[str, tuple[str, ...], frozenset[str]]]:
    indices = []
    for linha in _secao(texto, "### 3.5 Índices").splitlines():
        if linha.startswith("| `"):
            _, colecao, chaves, opcoes, *_ = (c.strip() for c in linha.split("|"))
            flags = frozenset(o.strip() for o in opcoes.split(",") if o.strip())
            indices.append(
                (colecao.strip("`"), tuple(re.findall(r"`(\w+)`", chaves)), flags)
            )
    return sorted(indices, key=lambda indice: (indice[0], indice[1]))


def test_indices_da_arquitetura_sao_os_que_o_banco_cria() -> None:
    texto = ARQUITETURA.read_text(encoding="utf-8")

    assert _indices_do_documento(texto) == _indices_do_codigo(), (
        "indice novo ou trocado: a tabela 3.5 de docs/arquitetura.md"
    )


def _setas(bloco: str) -> set[tuple[str, str]]:
    return set(re.findall(r"^ +(\S+) --> ([^\s:]+)", bloco, re.M))


def _diagrama_com(texto: str, estado: str) -> str:
    (bloco,) = [
        b for b in _mermaid(texto, "stateDiagram-v2") if re.search(rf"\b{estado}\b", b)
    ]
    return bloco


def _estados_do_codigo(
    transicoes: Any,
    iniciais: set[str],
) -> set[tuple[str, str]]:
    """As setas do diagrama de um agregado, a partir das transicoes do dominio."""
    setas = {
        (de.value, para.value) for de, alvos in transicoes.items() for para in alvos
    }
    setas |= {("[*]", inicial) for inicial in iniciais}
    finais = {e.value for e in type(next(iter(transicoes))) if e not in transicoes}
    return setas | {(final, "[*]") for final in finais}


def test_estados_do_orcamento_na_arquitetura_sao_os_das_transicoes() -> None:
    bloco = _diagrama_com(ARQUITETURA.read_text(encoding="utf-8"), "PENDENTE")
    gerado = orcamento()
    lapide = Orcamento.lapide(
        id=gerado.id,
        ordem_id=gerado.ordem_id,
        cancelado_em=AGORA,
        motivo="cancelamento",
    )

    assert _setas(bloco) == _estados_do_codigo(
        dominio_do_orcamento._TRANSICOES,
        {gerado.status.value, lapide.status.value},
    )
    assert {s for seta in _setas(bloco) for s in seta} - {"[*]"} == {
        status.value for status in StatusOrcamento
    }


def test_estados_do_pagamento_na_arquitetura_sao_os_das_transicoes() -> None:
    bloco = _diagrama_com(ARQUITETURA.read_text(encoding="utf-8"), "SOLICITADO")
    cobrado = pagamento()
    lapide = Pagamento.lapide(
        id=cobrado.id,
        ordem_id=cobrado.ordem_id,
        cancelado_em=AGORA,
        motivo="cancelamento",
    )

    assert _setas(bloco) == _estados_do_codigo(
        TRANSICOES, {cobrado.status.value, lapide.status.value}
    )
    assert {s for seta in _setas(bloco) for s in seta} - {"[*]"} == {
        status.value for status in StatusPagamento
    }


def test_estados_do_preco_na_arquitetura_sao_os_que_o_dominio_permite() -> None:
    bloco = _diagrama_com(ARQUITETURA.read_text(encoding="utf-8"), "ativo")
    setas = {("[*]", "ativo")}
    for preco in (
        PrecoServico.cadastrar(
            codigo="SRV-X", nome="n", descricao="d", preco=dinheiro("1.00")
        ),
        PrecoPeca.cadastrar(sku="PEC-X", nome="n", preco=dinheiro("1.00")),
    ):
        assert preco.ativo
        preco.desativar()
        setas.add(("ativo", "inativo" if not preco.ativo else "ativo"))
        if isinstance(preco, PrecoServico):
            preco.atualizar(nome="n", descricao="d", preco=dinheiro("1.00"), ativo=True)
        else:
            preco.atualizar(nome="n", preco=dinheiro("1.00"), ativo=True)
        setas.add(("inativo", "ativo" if preco.ativo else "inativo"))

    assert _setas(bloco) == setas


def _celulas(trecho: str) -> dict[str, list[str]]:
    """Primeira celula (sem crase) de cada linha de dados das tabelas -> as demais."""
    linhas = [
        [celula.strip() for celula in linha.strip("|").split("|")]
        for linha in trecho.splitlines()
        if linha.startswith("| `")
    ]
    tabela = {primeira.strip("`"): resto for primeira, *resto in linhas}
    assert len(tabela) == len(linhas), "linha repetida na tabela"
    return tabela


def _asyncapi() -> dict[str, Any]:
    texto = (contratos.diretorio() / "asyncapi.yaml").read_text(encoding="utf-8")
    documento: dict[str, Any] = yaml.safe_load(texto)
    return documento


def _routing_key(asyncapi: dict[str, Any], tipo: str) -> str:
    [chave] = [
        chave
        for chave, canal in asyncapi["channels"].items()
        if re.match(r"(comando|evento)\.", canal["address"])
        and tipo in canal["messages"]
    ]
    return str(chave)


def _produtor(asyncapi: dict[str, Any], tipo: str) -> str:
    [produtor] = [
        op["bindings"]["amqp"]["userId"]
        for op in asyncapi["operations"].values()
        if op["action"] == "send"
        and op["channel"]["$ref"].endswith(f"/{_routing_key(asyncapi, tipo)}")
    ]
    return str(produtor)


def _consumidor(asyncapi: dict[str, Any], tipo: str) -> str:
    [servico] = [
        op["tags"][0]["name"].removesuffix("-service")
        for op in asyncapi["operations"].values()
        if op["action"] == "receive"
        and tipo
        in asyncapi["channels"][op["channel"]["$ref"].rsplit("/", 1)[1]]["messages"]
    ]
    return str(servico)


def test_comandos_consumidos_na_arquitetura_sao_os_do_catalogo() -> None:
    asyncapi = _asyncapi()
    secao = _secao(ARQUITETURA.read_text(encoding="utf-8"), "### Comandos consumidos")

    celulas = _celulas(secao)

    assert {
        nome: [c.strip("`") for c in resto[:4]] for nome, resto in celulas.items()
    } == {
        comando: [
            _routing_key(asyncapi, comando),
            _produtor(asyncapi, comando),
            _consumidor(asyncapi, comando),
            FILA,
        ]
        for comando in contratos.COMANDOS
    }
    assert {_produtor(asyncapi, c) for c in contratos.COMANDOS} == {
        PRODUTOR_DOS_COMANDOS
    }
    respostas = {
        r for resto in celulas.values() for r in re.findall(r"`(\w+)`", resto[4])
    }
    assert respostas <= contratos.eventos_publicados()


def test_eventos_publicados_na_arquitetura_sao_os_do_catalogo() -> None:
    asyncapi = _asyncapi()
    secao = _secao(ARQUITETURA.read_text(encoding="utf-8"), "### Eventos publicados")

    celulas = _celulas(secao)

    assert {
        nome: [c.strip("`") for c in resto[:3]] for nome, resto in celulas.items()
    } == {
        evento: [
            contratos.routing_key_do_evento(evento),
            _produtor(asyncapi, evento),
            _consumidor(asyncapi, evento),
        ]
        for evento in contratos.eventos_publicados()
    }
    assert all(
        contratos.routing_key_do_evento(e) == _routing_key(asyncapi, e)
        for e in contratos.eventos_publicados()
    )


def test_mensagem_nova_sem_linha_na_tabela_da_arquitetura_reprova() -> None:
    secao = _secao(ARQUITETURA.read_text(encoding="utf-8"), "### Eventos publicados")
    sem_a_linha = "\n".join(
        linha
        for linha in secao.splitlines()
        if not linha.startswith("| `PagamentoCancelado`")
    )

    assert set(_celulas(sem_a_linha)) == contratos.eventos_publicados() - {
        "PagamentoCancelado"
    }
    with pytest.raises(AssertionError, match="linha repetida"):
        _celulas(secao + secao.splitlines()[-1] + "\n")


# A secao Cobertura do README: gerada pelo script, nunca digitada.

_LINHA_DA_TABELA = re.compile(
    r"^\| (?P<rotulo>[^|]+?) \| (?P<total>[\d.]+) \| (?P<cobertas>[\d.]+)"
    r" \| [\d,]+% \|$",
    re.MULTILINE,
)
_RAMOS = re.compile(r"e (?P<total>[\d.]+) ramos, (?P<cobertos>[\d.]+) cobertos")
_RUN = re.compile(
    r"\]\((?P<url>https://github\.com/fiap-postech-sw-architecture/"
    r"postech-sw-arch-p4-billing-service/actions/runs/\d+)\)"
)


def _numero(texto: str) -> int:
    return int(texto.replace(".", ""))


def _tabela(texto: str) -> dict[str, tuple[int, int]]:
    return {
        linha["rotulo"]: (_numero(linha["total"]), _numero(linha["cobertas"]))
        for linha in _LINHA_DA_TABELA.finditer(texto)
        if linha["rotulo"] != "**total**"
    }


def _secao_de_cobertura(
    cobertura: ModuleType, texto: str
) -> tuple[str, Any, re.Match[str]]:
    """A secao entre os marcadores, a ``Medida`` que ela mostra e o link do run."""
    assert texto.count(cobertura.MARCADOR_INICIO) == 1
    assert texto.count(cobertura.MARCADOR_FIM) == 1
    secao = texto.split(cobertura.MARCADOR_INICIO)[1].split(cobertura.MARCADOR_FIM)[0]
    contextos, camadas = secao.split("| Camada |")
    ramos, run = _RAMOS.search(secao), _RUN.search(secao)
    assert ramos is not None, "secao Cobertura sem a contagem dos ramos"
    assert run is not None, "secao Cobertura sem o link do run do CI"
    medida = cobertura.Medida(
        contextos=_tabela(contextos),
        camadas=_tabela("| Camada |" + camadas),
        ramos=(_numero(ramos["total"]), _numero(ramos["cobertos"])),
    )
    return secao, medida, run


def test_secao_de_cobertura_do_readme_e_a_que_o_script_gera(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cobertura = carregar_script("cobertura_resumo", monkeypatch)
    texto = README.read_text(encoding="utf-8")

    secao, medida, run = _secao_de_cobertura(cobertura, texto)

    # Ponto fixo: os numeros que a secao mostra, passados de novo pelo script,
    # devolvem a secao inteira. Uma linha editada a mao (percentual, total,
    # formato) ou o link de outro repositorio nao passam.
    assert f"\n{cobertura.secao_do_readme(medida, run['url'])}\n" == secao, (
        "secao Cobertura editada a mao: rode o comando de Para refazer, no README"
    )


def test_cobertura_do_readme_so_tem_contextos_e_camadas_que_existem(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cobertura = carregar_script("cobertura_resumo", monkeypatch)

    _, medida, _ = _secao_de_cobertura(cobertura, README.read_text(encoding="utf-8"))

    contextos = [r.strip("`") for r in medida.contextos if r != cobertura.PROCESSOS]
    assert contextos
    assert [c for c in contextos if not (RAIZ / c).is_dir()] == []
    assert cobertura.PROCESSOS in medida.contextos
    assert set(medida.camadas) <= {f"`{c}`" for c in cobertura.CAMADAS}
    assert all(cobertas <= total for total, cobertas in medida.contextos.values())
    assert medida.ramos[1] <= medida.ramos[0]


def test_percentual_editado_a_mao_na_secao_de_cobertura_nao_e_o_ponto_fixo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cobertura = carregar_script("cobertura_resumo", monkeypatch)
    texto = README.read_text(encoding="utf-8")
    secao, _, _ = _secao_de_cobertura(cobertura, texto)
    editada = secao.replace("100,0% |", "99,9% |", 1)

    _, medida, run = _secao_de_cobertura(cobertura, texto.replace(secao, editada))

    assert f"\n{cobertura.secao_do_readme(medida, run['url'])}\n" != editada


def test_link_de_outro_repositorio_na_secao_de_cobertura_e_apontado(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cobertura = carregar_script("cobertura_resumo", monkeypatch)
    texto = README.read_text(encoding="utf-8")
    secao, _, _ = _secao_de_cobertura(cobertura, texto)
    de_outro = secao.replace(
        "postech-sw-arch-p4-billing-service/actions", "outro/actions"
    )

    with pytest.raises(AssertionError, match="sem o link do run do CI"):
        _secao_de_cobertura(cobertura, texto.replace(secao, de_outro))


@pytest.mark.parametrize(
    "run",
    ["", "abc", "../1", "12 34", "1;ls"],
    ids=["vazio", "texto", "caminho", "espaco", "comando"],
)
def test_make_cobertura_readme_so_aceita_o_id_numerico_do_run(run: str) -> None:
    resultado = subprocess.run(  # noqa: S603 - o Makefile do proprio repositorio
        ["make", "--no-print-directory", "cobertura-readme", f"RUN={run}"],  # noqa: S607
        cwd=RAIZ,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )

    assert resultado.returncode == 2
    assert "RUN=<CI run id, digits only> is required" in resultado.stderr
    assert not (RAIZ / "reports" / f"ci-{run}").exists()


@pytest.mark.parametrize(
    "molde",
    [
        '1"; touch {marca}; echo "',
        "1'; touch {marca}; echo '",
        "`touch {marca}`",
        "$$(touch {marca})",
        "$(shell touch {marca})",
        "1\ntouch {marca}",
    ],
    ids=[
        "aspas",
        "aspa-simples",
        "crase",
        "substituicao",
        "funcao-do-make",
        "quebra-de-linha",
    ],
)
def test_make_cobertura_readme_recusa_o_run_sem_executar_o_que_vem_nele(
    molde: str, tmp_path: Path
) -> None:
    # O valor chega ao shell como texto do ambiente: o que vem dentro das aspas,
    # da crase ou de uma funcao do make nao roda nem antes de o alvo recusar o RUN.
    marca = tmp_path / "executou"
    resultado = subprocess.run(  # noqa: S603 - o Makefile do proprio repositorio
        [  # noqa: S607
            "make",
            "--no-print-directory",
            "cobertura-readme",
            f"RUN={molde.format(marca=marca)}",
        ],
        cwd=RAIZ,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )

    assert resultado.returncode == 2
    assert "RUN=<CI run id, digits only> is required" in resultado.stderr
    assert not marca.exists()


def test_make_cobertura_readme_baixa_o_artefato_do_run_e_reescreve_a_secao(
    tmp_path: Path,
) -> None:
    for caminho in ("Makefile", "scripts/cobertura_resumo.py"):
        (tmp_path / caminho).parent.mkdir(exist_ok=True)
        (tmp_path / caminho).write_bytes((RAIZ / caminho).read_bytes())
    (tmp_path / "README.md").write_text(
        "# Servico\n<!-- cobertura:inicio -->\nvelha\n<!-- cobertura:fim -->\n",
        encoding="utf-8",
    )
    # O gh de mentira grava o coverage.xml no destino do -D e anota os argumentos.
    (tmp_path / "bin").mkdir()
    gh = tmp_path / "bin" / "gh"
    gh.write_text(
        '#!/bin/sh\necho "$@" > gh-args\nmkdir -p "$7"\n'
        + "cat > \"$7/coverage.xml\" <<'XML'\n"
        + XML_DOS_CONTEXTOS
        + "XML\n",
        encoding="utf-8",
    )
    gh.chmod(0o755)
    (tmp_path / "bin" / "python").symlink_to(sys.executable)

    comando = ["make", "--no-print-directory", "cobertura-readme", "RUN=123"]
    resultado = subprocess.run(  # noqa: S603 - o Makefile do proprio repositorio
        [*comando, "PY=env"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
        env={**os.environ, "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}"},
    )

    assert resultado.returncode == 0, resultado.stderr
    assert (tmp_path / "gh-args").read_text().split() == [
        "run",
        "download",
        "123",
        "-n",
        "relatorios-de-teste",
        "-D",
        "reports/ci-123",
    ]
    readme = (tmp_path / "README.md").read_text(encoding="utf-8")
    assert "velha" not in readme
    assert (
        "[run 123 do CI](https://github.com/fiap-postech-sw-architecture/"
        "postech-sw-arch-p4-billing-service/actions/runs/123)"
    ) in readme


def test_gate_do_readme_e_o_do_coveragerc_e_o_do_makefile() -> None:
    cobertura = _secao(README.read_text(encoding="utf-8"), "## Cobertura")
    gate = re.search(r"O gate é de (\d+)%", cobertura)
    novo = re.search(r"o código novo precisa de (\d+)%", cobertura)
    fail_under = re.search(
        r"^fail_under = (\d+)", (RAIZ / ".coveragerc").read_text(), re.M
    )
    diff_cover = re.search(
        r"^DIFF_COVER_MINIMO \?= (\d+)", (RAIZ / "Makefile").read_text(), re.M
    )

    assert gate is not None
    assert fail_under is not None
    assert gate[1] == fail_under[1]
    assert novo is not None
    assert diff_cover is not None
    assert novo[1] == diff_cover[1]
