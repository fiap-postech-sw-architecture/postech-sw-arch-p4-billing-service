from __future__ import annotations

import importlib
import io
import json
import logging
import time
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

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

    @pytest.mark.parametrize(
        ("entrada", "saida"),
        [
            ("(11) 99999-0000", "***"),
            ("(11)99999-0000", "***"),
            ("11 99999-0000", "***"),
            ("11-99999-0000", "***"),
            ("1199999-0000", "***"),
            ("+55 11 99999-0000", "***"),
            ("+5511999990000", "***"),
            ("(11) 3333-4444", "***"),
            ("contato: (11) 99999-0000.", "contato: ***."),
            ("ligar 11 3333-4444, ok", "ligar ***, ok"),
            # Colado a hifen ou a palavra: so o digito hexadecimal antes do DDD
            # (o UUID) barra o telefone; `(` e `+` valem mesmo depois de um `e`.
            ("tel-11 99999-0000", "tel-***"),
            ("fone(11)99999-0000", "fone***"),
            ("fone+5511999990000", "fone***"),
        ],
    )
    def test_telefone_br_e_mascarado(self, entrada: str, saida: str) -> None:
        assert scrub_pii(None, "info", {"event": entrada})["event"] == saida

    @pytest.mark.parametrize(
        "texto",
        [
            "total 1500.00",
            "id 12345",
            "ano 2026",
            "porta 8000",
            "CEP 12345-678",
            "PEC-OLEO-5W30",
            "2026-10-06T12:00:00Z",
            "req-1234-5678",
            # Digito a mais no fim: numero maior, nao telefone.
            "(11) 99999-00001",
        ],
    )
    def test_numero_que_nao_e_telefone_fica_como_esta(self, texto: str) -> None:
        assert scrub_pii(None, "info", {"event": texto})["event"] == texto

    def test_uuid_v4_nunca_e_mascarado_como_telefone(self) -> None:
        # Os ids do servico sao UUID e entram em todo log. O v4 tem trechos
        # `dd-dddd-dddd` entre os grupos: sem a guarda do regex de telefone,
        # cerca de 1,4% saiam como "***" (o primeiro abaixo e um deles).
        ids = [
            UUID("732ffc02-3465-4237-a5f6-12fd4a2b3be0"),
            *(uuid4() for _ in range(10_000)),
        ]
        for id_ in ids:
            for texto in (str(id_), str(id_).upper()):  # o id pode chegar em caixa alta
                evento = scrub_pii(
                    None, "info", {"event": f"ordem {texto}", "ordem_id": texto}
                )
                assert evento == {"event": f"ordem {texto}", "ordem_id": texto}

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

    @pytest.mark.parametrize(
        ("entrada", "saida"),
        [
            ("fulano@exemplo.com", "f***@exemplo.com"),
            (
                "x=maria.silva+tag@mail.empresa.com.br fim",
                "x=m***@mail.empresa.com.br fim",
            ),
            ("(ana_b@ex.co)", "(a***@ex.co)"),
        ],
        ids=["simples", "subdominios-e-tag", "entre-parenteses"],
    )
    def test_email_e_mascarado(self, entrada: str, saida: str) -> None:
        assert scrub_pii(None, "info", {"event": entrada})["event"] == saida

    @pytest.mark.parametrize(
        "hostil",
        ["a." * 40_000, "x@" + "a." * 40_000, "x@" + "a-" * 40_000, "a" * 80_000 + "@"],
        ids=["pontos", "dominio-com-pontos", "dominio-com-hifens", "local-sem-fim"],
    )
    def test_texto_hostil_de_80_kb_e_varrido_em_menos_de_100_ms(
        self, hostil: str
    ) -> None:
        # O id de uma mensagem chega de fora: o scrub nao pode travar o processo.
        inicio = time.perf_counter()
        scrub_pii(None, "info", {"event": hostil})
        assert time.perf_counter() - inicio < 0.1

    def test_redigir_trunca_e_mascara(self) -> None:
        texto = redigir_pii_erro("cpf 123.456.789-09 " + "x" * 300)
        assert "123.456" not in texto
        assert len(texto) == 201


def test_logger_do_pika_so_deixa_passar_erro(saida_de_log: io.StringIO) -> None:
    pika = logging.getLogger("pika.adapters.blocking_connection")
    pika.warning("Published message was returned: body_prefix=%r", b"dados")
    pika.error("connection lost")

    assert [linha["event"] for linha in linhas_json(saida_de_log)] == [
        "connection lost"
    ]


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
    assert metodo.json()["erro"]["mensagem"] == "Metodo nao permitido para este recurso"


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


@pytest.mark.parametrize("prefixo", ["", "/billing"], ids=["sem-prefixo", "borda"])
def test_swagger_sem_csp_restrito_tambem_atras_do_prefixo_da_borda(
    prefixo: str,
) -> None:
    # O uvicorn poe o root_path (ROOT_PATH) na frente do path do scope; o
    # TestClient nao, entao o teste pede o caminho com o prefixo, como o
    # uvicorn o entrega. Com o CSP restrito o Swagger UI nao carrega.
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)
    cliente = TestClient(app, root_path=prefixo)

    for pagina in ("/docs", "/redoc"):
        resposta = cliente.get(f"{prefixo}{pagina}")
        assert resposta.status_code == 200
        assert "Content-Security-Policy" not in resposta.headers
        assert f"{prefixo}/openapi.json" in resposta.text
    openapi = cliente.get(f"{prefixo}/openapi.json")
    assert "Content-Security-Policy" not in openapi.headers
    assert openapi.json().get("servers", []) == ([{"url": prefixo}] if prefixo else [])
    outra = cliente.get(f"{prefixo}/api/v1/docs")
    assert outra.headers["Content-Security-Policy"] == "default-src 'none'"


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
