from __future__ import annotations

import importlib
import io
import json
import logging
from typing import TYPE_CHECKING

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel

from src.compartilhado.dominio.exceptions import (
    DependenciaIndisponivelError,
    DomainException,
    EntidadeDuplicadaError,
    EntidadeNaoEncontradaError,
    RecursoExpiradoError,
    TransicaoStatusInvalidaError,
    ValorInvalidoError,
)
from src.compartilhado.infraestrutura.logging import (
    configurar_logging,
    redigir_pii_erro,
    scrub_pii,
)
from src.compartilhado.infraestrutura.metricas import configurar_metricas
from src.compartilhado.interfaces.error_handler import registrar_error_handlers
from src.compartilhado.interfaces.middleware import SecurityHeadersMiddleware

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture
def saida_de_log() -> Iterator[io.StringIO]:
    saida = io.StringIO()
    configurar_logging(saida)
    yield saida
    logging.getLogger().handlers.clear()


def linhas_json(saida: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(linha) for linha in saida.getvalue().splitlines() if linha]


class TestLogging:
    def test_token_do_link_publico_sai_mascarado_no_access_log(
        self, saida_de_log: io.StringIO
    ) -> None:
        logging.getLogger("uvicorn.access").info(
            '"GET /api/v1/publico/orcamentos/%s HTTP/1.1" 200',
            "0e3e4a2b-3c0a-4f5e-8d6e-1b2c3d4e5f60.1791547200.Zm9vYmFy",
        )
        registro = linhas_json(saida_de_log)[-1]
        assert registro["event"] == '"GET /api/v1/publico/orcamentos/*** HTTP/1.1" 200'

    def test_token_do_checkout_simulado_sai_mascarado_no_access_log(
        self, saida_de_log: io.StringIO
    ) -> None:
        logging.getLogger("uvicorn.access").info(
            '"POST /api/v1/simulador/pagamentos/%s/aprovar?token=%s&x=1 HTTP/1.1" 200',
            "0e3e4a2b-3c0a-4f5e-8d6e-1b2c3d4e5f60",
            "0e3e4a2b-3c0a-4f5e-8d6e-1b2c3d4e5f60.1791547200.Zm9vYmFy",
        )
        registro = linhas_json(saida_de_log)[-1]
        assert registro["event"] == (
            '"POST /api/v1/simulador/pagamentos/0e3e4a2b-3c0a-4f5e-8d6e-1b2c3d4e5f60'
            '/aprovar?token=***&x=1 HTTP/1.1" 200'
        )

    def test_pagina_de_checkout_simulado_sai_com_o_token_mascarado(
        self, saida_de_log: io.StringIO
    ) -> None:
        logging.getLogger("uvicorn.access").info(
            '"GET /simulador/checkout/%s?token=%s HTTP/1.1" 200',
            "0e3e4a2b-3c0a-4f5e-8d6e-1b2c3d4e5f60",
            "0e3e4a2b-3c0a-4f5e-8d6e-1b2c3d4e5f60.1791547200.Zm9vYmFy",
        )
        registro = linhas_json(saida_de_log)[-1]
        assert "Zm9vYmFy" not in str(registro["event"])
        assert "?token=***" in str(registro["event"])

    @pytest.mark.parametrize(
        "chave",
        [
            "token",
            "mp_access_token",
            "Authorization",
            "x_signature",
            "webhook_secret",
            "senha_hash",
            "api_key",
            "checkout_url",
            "link_decisao",
            "telefone",
        ],
    )
    def test_chave_sensivel_casa_por_trecho_do_nome(self, chave: str) -> None:
        evento = scrub_pii(None, "info", {"event": "x", chave: "valor-secreto"})
        assert evento[chave] == "***"

    @pytest.mark.parametrize(
        "chave",
        ["motivo", "referencia", "telefone_ok", 7],
        ids=["motivo", "referencia", "telefone_ok", "chave-numerica"],
    )
    def test_chave_comum_nao_e_mascarada(self, chave: str | int) -> None:
        evento = scrub_pii(None, "info", {"event": "x", "dados": {chave: "valor"}})
        assert evento["dados"] == {chave: "valor"}

    def test_extra_de_log_stdlib_vira_campo_e_passa_pelo_scrub(
        self, saida_de_log: io.StringIO
    ) -> None:
        logging.getLogger("src.pagamento.aplicacao.use_cases").warning(
            "notificacao_pagamento_ignorada",
            extra={"referencia": "123", "token": "segredo", "contato": "a@b.com"},
        )
        registro = linhas_json(saida_de_log)[-1]
        assert registro["event"] == "notificacao_pagamento_ignorada"
        assert registro["referencia"] == "123"
        assert registro["token"] == "***"
        assert registro["contato"] == "***"
        assert registro["level"] == "warning"

    def test_scrub_mascara_pii_em_estruturas_aninhadas(self) -> None:
        evento = scrub_pii(
            None,
            "info",
            {
                "event": "x",
                "dados": {"email": "fulano@exemplo.com", "cpf": ["123.456.789-09"]},
                "authorization": "Bearer abc",
            },
        )
        assert evento["authorization"] == "***"
        assert evento["dados"] == {
            "email": "f***@exemplo.com",
            "cpf": ["***.***.789-**"],
        }

    def test_scrub_cobre_cnpj_colecoes_e_limite_de_profundidade(self) -> None:
        profundo: dict[str, object] = {"cpf": "123.456.789-09"}
        for _ in range(8):
            profundo = {"nivel": profundo}
        evento = scrub_pii(
            None,
            "info",
            {
                "event": "cnpj 12.345.678/0001-95",
                "tupla": ("a@b.com",),
                "conjunto": {"a@b.com"},
                "congelado": frozenset({"a@b.com"}),
                "profundo": profundo,
            },
        )
        assert evento["event"] == "cnpj **.***.678/****-**"
        assert evento["tupla"] == ("a***@b.com",)
        assert evento["conjunto"] == {"a***@b.com"}
        assert evento["congelado"] == frozenset({"a***@b.com"})
        # Alem do limite de profundidade o valor segue sem varredura.
        assert "123.456.789-09" in json.dumps(evento["profundo"])

    def test_redigir_trunca_e_mascara(self) -> None:
        texto = redigir_pii_erro("cpf 123.456.789-09 " + "x" * 300)
        assert "123.456" not in texto
        assert len(texto) == 201


class Corpo(BaseModel):
    numero: int


@pytest.fixture
def cliente() -> TestClient:
    app = FastAPI()

    erros: dict[str, Exception] = {
        "nao-encontrado": EntidadeNaoEncontradaError("sumiu"),
        "duplicado": EntidadeDuplicadaError(),
        "transicao": TransicaoStatusInvalidaError(),
        "expirado": RecursoExpiradoError(),
        "indisponivel": DependenciaIndisponivelError(),
        "regra": DomainException("regra violada"),
        "valor": ValorInvalidoError("cpf 123.456.789-09 invalido"),
        # ValueError de biblioteca (ex.: corpo que nao e JSON) e defeito do
        # servidor: 500, nunca 422 com a mensagem interna.
        "valor-de-biblioteca": ValueError("Expecting value: line 1 column 1"),
        "http": HTTPException(401, "sem token", headers={"WWW-Authenticate": "Bearer"}),
        "bug": RuntimeError("bug"),
    }

    @app.get("/erro/{nome}")
    def levantar(nome: str) -> None:
        raise erros[nome]

    @app.post("/corpo")
    def corpo(body: Corpo) -> dict[str, int]:
        return {"numero": body.numero}

    app.add_middleware(SecurityHeadersMiddleware)
    registrar_error_handlers(app)
    configurar_metricas(app)
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize(
    ("nome", "status", "codigo"),
    [
        ("nao-encontrado", 404, "ENTIDADE_NAO_ENCONTRADA"),
        ("duplicado", 409, "ENTIDADE_DUPLICADA"),
        ("transicao", 409, "TRANSICAO_STATUS_INVALIDA"),
        ("expirado", 410, "RECURSO_EXPIRADO"),
        ("indisponivel", 503, "DEPENDENCIA_INDISPONIVEL"),
        ("regra", 409, "VIOLACAO_REGRA_NEGOCIO"),
        ("valor", 422, "VALOR_INVALIDO"),
        ("valor-de-biblioteca", 500, "ERRO_INTERNO"),
        ("http", 401, "NAO_AUTENTICADO"),
        ("bug", 500, "ERRO_INTERNO"),
    ],
)
def test_envelope_de_erro(
    cliente: TestClient, nome: str, status: int, codigo: str
) -> None:
    resposta = cliente.get(f"/erro/{nome}", headers={"X-Request-ID": "req-123"})
    assert resposta.status_code == status
    erro = resposta.json()["erro"]
    assert erro["codigo"] == codigo
    assert erro["id_requisicao"] == "req-123"
    assert "123.456.789" not in erro["mensagem"]
    assert "Expecting value" not in erro["mensagem"]


def _subclasses(base: type[DomainException]) -> set[type[DomainException]]:
    diretas = set(base.__subclasses__())
    return diretas | {c for d in diretas for c in _subclasses(d)}


def test_404_de_dominio_so_foge_do_codigo_generico_nas_rotas_com_token() -> None:
    # Convencao dos tres servicos: o cliente trata "nao encontrado" de um jeito
    # so. Excecao nova de 404 entra aqui de proposito, e nao por esquecimento.
    importlib.import_module("src.main")  # carrega as excecoes de todos os contextos
    codigos = {c.__name__: c.codigo for c in _subclasses(EntidadeNaoEncontradaError)}
    assert codigos == {
        "OrcamentoNaoEncontradoError": "ENTIDADE_NAO_ENCONTRADA",
        "PrecoNaoEncontradoError": "ENTIDADE_NAO_ENCONTRADA",
        "PagamentoNaoEncontradoError": "ENTIDADE_NAO_ENCONTRADA",
        # Rotas publicas com token: o mesmo 404 para qualquer falha do token.
        "LinkDeDecisaoInvalidoError": "LINK_DECISAO_INVALIDO",
        "CheckoutNaoEncontradoError": "CHECKOUT_NAO_ENCONTRADO",
    }


def test_500_sai_com_headers_de_seguranca_e_request_id(cliente: TestClient) -> None:
    resposta = cliente.get("/erro/bug", headers={"X-Request-ID": "req-500"})
    assert resposta.status_code == 500
    assert resposta.headers["X-Request-ID"] == "req-500"
    assert resposta.headers["X-Content-Type-Options"] == "nosniff"
    assert resposta.headers["Cache-Control"] == "no-store"
    assert resposta.headers["Strict-Transport-Security"].startswith("max-age=")
    assert resposta.json()["erro"]["id_requisicao"] == "req-500"


def test_http_exception_preserva_headers(cliente: TestClient) -> None:
    resposta = cliente.get("/erro/http")
    assert resposta.headers["WWW-Authenticate"] == "Bearer"


def test_rota_inexistente_tambem_usa_o_envelope_em_portugues(
    cliente: TestClient,
) -> None:
    resposta = cliente.get("/nao/existe")
    assert resposta.status_code == 404
    assert resposta.json()["erro"]["codigo"] == "ENTIDADE_NAO_ENCONTRADA"
    assert resposta.json()["erro"]["mensagem"] == "Recurso nao encontrado"
    metodo = cliente.delete("/corpo")
    assert metodo.status_code == 405
    assert metodo.json()["erro"]["mensagem"] == "Metodo nao permitido"


def test_422_de_schema_no_formato_do_p3_sem_ecoar_o_valor(cliente: TestClient) -> None:
    resposta = cliente.post(
        "/corpo", json={"numero": "123.456.789-09"}, headers={"X-Request-ID": "r-422"}
    )
    assert resposta.status_code == 422
    assert "123.456.789-09" not in resposta.text
    corpo = resposta.json()
    assert set(corpo) == {"detail", "id_requisicao"}
    assert corpo["id_requisicao"] == "r-422"
    assert corpo["detail"] == [
        {
            "type": "int_parsing",
            "loc": ["body", "numero"],
            "msg": "Input should be a valid integer, unable to parse string as an "
            "integer",
        }
    ]


def test_headers_de_seguranca_e_request_id(cliente: TestClient) -> None:
    resposta = cliente.post("/corpo", json={"numero": 1})
    assert {
        nome: resposta.headers[nome]
        for nome in (
            "X-Content-Type-Options",
            "X-Frame-Options",
            "Strict-Transport-Security",
            "Cache-Control",
            "Content-Security-Policy",
        )
    } == {
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
        "Cache-Control": "no-store",
        "Content-Security-Policy": "default-src 'none'",
    }
    assert resposta.headers["X-Request-ID"]
    invalido = cliente.post(
        "/corpo", json={"numero": 1}, headers={"X-Request-ID": "x" * 200}
    )
    assert invalido.headers["X-Request-ID"] != "x" * 200


def test_metricas_por_template_de_rota(cliente: TestClient) -> None:
    cliente.get("/erro/nao-encontrado")
    cliente.get("/qualquer/coisa")
    texto = cliente.get("/metrics").text
    assert 'rota="/erro/{nome}"' in texto
    assert 'rota="nao_roteada"' in texto
    assert "/erro/nao-encontrado" not in texto


def test_rede_de_seguranca_sem_o_middleware_tambem_responde_o_envelope() -> None:
    app = FastAPI()

    @app.get("/bug")
    def bug() -> None:
        msg = "bug"
        raise RuntimeError(msg)

    registrar_error_handlers(app)
    resposta = TestClient(app, raise_server_exceptions=False).get("/bug")
    assert resposta.status_code == 500
    assert resposta.json()["erro"] == {
        "codigo": "ERRO_INTERNO",
        "mensagem": "Erro interno do servidor",
        "id_requisicao": "desconhecido",
    }
