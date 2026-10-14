"""Rastreamento distribuido da API, do relay e do ``prazos`` (ADR-043).

Os spans saem do provider de producao (``criar_provedor``, com o amostrador
real) para um exportador em memoria, e o PyMongo e o httpx sao instrumentados
pela mesma funcao dos processos. O cliente do MongoDB nasce depois da
instrumentacao: o PyMongo so rastreia os clientes criados depois dela.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import pytest
import respx
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.pymongo import PymongoInstrumentor
from opentelemetry.trace import SpanKind

from src.compartilhado.dominio.relogio import agora_utc
from src.compartilhado.infraestrutura.mensageria.relay import RelayDaOutbox
from src.compartilhado.infraestrutura.mensageria.telemetria import (
    instrumentar_clientes,
)
from src.compartilhado.infraestrutura.mongo import criar_cliente, marcar_versao
from src.compartilhado.infraestrutura.unit_of_work import MongoUnitOfWork
from src.main import criar_app
from src.orcamento.dominio.orcamento import CanalDecisao
from src.orcamento.infraestrutura.repository import MongoOrcamentoRepository
from src.pagamento.aplicacao.use_cases import SolicitarPagamento
from src.pagamento.infraestrutura.mercadopago import (
    ConfiguracaoMercadoPago,
    MercadoPagoGateway,
)
from src.pagamento.infraestrutura.orcamentos import OrcamentosMongoAdapter
from src.pagamento.infraestrutura.repository import MongoPagamentoRepository
from src.prazos import executar_ciclo
from tests.factories import AGORA, orcamento
from tests.integracao.apoio import (
    MetricasEspia,
    PublicadorFalso,
    RelogioFixo,
    configuracao,
    token_do_checkout,
    token_do_link,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

    from opentelemetry.sdk.trace import ReadableSpan
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )
    from pymongo.database import Database

    from src.pagamento.aplicacao.dtos import PagamentoDTO
    from tests.conftest import TelemetriaDeTeste

    Banco = Database[dict[str, Any]]


@dataclass(frozen=True, slots=True)
class Rastreamento:
    exportador: InMemorySpanExporter
    banco: Banco

    @property
    def spans(self) -> tuple[ReadableSpan, ...]:
        return self.exportador.get_finished_spans()

    def do_tipo(self, tipo: SpanKind) -> list[ReadableSpan]:
        return [s for s in self.spans if s.kind is tipo]


@pytest.fixture
def rastreamento(
    _telemetria_da_sessao: TelemetriaDeTeste,
    banco: Banco,
    mongo_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Rastreamento]:
    provedor = _telemetria_da_sessao.provedor
    monkeypatch.setattr("src.main.configurar_telemetria", lambda _processo: provedor)
    instrumentar_clientes(provedor)
    cliente = criar_cliente(mongo_uri)
    _telemetria_da_sessao.exportador.clear()
    try:
        yield Rastreamento(_telemetria_da_sessao.exportador, cliente[banco.name])
    finally:
        cliente.close()
        PymongoInstrumentor().uninstrument()
        HTTPXClientInstrumentor().uninstrument()


def valores_de(spans: Sequence[ReadableSpan]) -> list[str]:
    """Tudo o que o span leva e o Jaeger guardaria: nome, atributos, eventos e
    links (os atributos deles)."""
    valores: list[str] = []
    for span in spans:
        valores.append(span.name)
        valores += [str(v) for v in (span.attributes or {}).values()]
        for evento in span.events:
            valores += [str(v) for v in (evento.attributes or {}).values()]
        for ligacao in span.links:
            valores += [str(v) for v in (ligacao.attributes or {}).values()]
    return valores


def autorizacao(emitir_token: Callable[..., str], papel: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {emitir_token(papel)}"}


def salvar_orcamento_pendente(banco: Banco) -> tuple[str, str]:
    """Orcamento PENDENTE e valido; devolve (id, token do link)."""
    pendente = orcamento(criado_em=agora_utc())
    uow = MongoUnitOfWork(banco)
    uow.executar(lambda: MongoOrcamentoRepository(uow).salvar(pendente))
    assert pendente.valido_ate is not None
    return str(pendente.id), token_do_link(pendente.id, pendente.valido_ate)


def solicitar_pagamento(app: Any, banco: Banco) -> PagamentoDTO:
    """Pagamento SOLICITADO com o checkout do simulador (como o consumidor)."""
    aprovado = orcamento(criado_em=agora_utc())
    aprovado.aprovar(canal=CanalDecisao.LINK, agora=aprovado.criado_em)
    uow = MongoUnitOfWork(banco)
    uow.executar(lambda: MongoOrcamentoRepository(uow).salvar(aprovado))
    uow = MongoUnitOfWork(banco)
    return SolicitarPagamento(
        uow,
        MongoPagamentoRepository(uow),
        OrcamentosMongoAdapter(uow),
        app.state.gateway_pagamento,
        app.state.config.pagamento_validade,
        agora_utc,
    ).executar(ordem_id=aprovado.ordem_id, orcamento_id=aprovado.id)


class TestRequisicao:
    def test_requisicao_gera_um_span_server_com_filhos_de_banco_no_mesmo_trace(
        self,
        rastreamento: Rastreamento,
        jwks_publicado: dict[str, Any],
        emitir_token: Callable[..., str],
    ) -> None:
        app = criar_app(configuracao(), banco=rastreamento.banco)
        with TestClient(app) as api:
            rastreamento.exportador.clear()
            resposta = api.get(
                "/api/v1/precos/servicos", headers=autorizacao(emitir_token, "admin")
            )

        assert resposta.status_code == 200
        [servidor] = rastreamento.do_tipo(SpanKind.SERVER)
        consultas = rastreamento.do_tipo(SpanKind.CLIENT)
        assert servidor.name == "GET /api/v1/precos/servicos"
        assert consultas
        # Nem o span nativo do FastAPI nem os de receive e send do ASGI.
        assert len(rastreamento.spans) == 1 + len(consultas)
        assert {s.context.trace_id for s in rastreamento.spans} == {
            servidor.context.trace_id
        }
        assert {s.parent.span_id for s in consultas if s.parent} == {
            servidor.context.span_id
        }
        assert {s.attributes["db.system"] for s in consultas if s.attributes} == {
            "mongodb"
        }

    def test_requisicao_com_traceparent_continua_o_trace_de_quem_chamou(
        self,
        rastreamento: Rastreamento,
        jwks_publicado: dict[str, Any],
        emitir_token: Callable[..., str],
    ) -> None:
        pai = "4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7"
        app = criar_app(configuracao(), banco=rastreamento.banco)
        with TestClient(app) as api:
            rastreamento.exportador.clear()
            api.get(
                "/api/v1/precos/servicos",
                headers=autorizacao(emitir_token, "admin")
                | {"traceparent": f"00-{pai}-01"},
            )

        [servidor] = rastreamento.do_tipo(SpanKind.SERVER)
        assert format(servidor.context.trace_id, "032x") == pai.split("-")[0]
        assert servidor.parent is not None
        assert format(servidor.parent.span_id, "016x") == pai.split("-")[1]

    def test_evento_gravado_na_requisicao_leva_o_contexto_do_span_dela(
        self,
        rastreamento: Rastreamento,
        jwks_publicado: dict[str, Any],
    ) -> None:
        _, token = salvar_orcamento_pendente(rastreamento.banco)
        app = criar_app(configuracao(), banco=rastreamento.banco)
        with TestClient(app) as api:
            rastreamento.exportador.clear()
            api.post(
                f"/api/v1/publico/orcamentos/{token}/decisao",
                json={"decisao": "aprovar"},
            )

        [servidor] = rastreamento.do_tipo(SpanKind.SERVER)
        linha = rastreamento.banco["outbox"].find_one({"tipo": "OrcamentoAprovado"})
        assert linha is not None
        assert linha["traceparent"].split("-")[1] == format(
            servidor.context.trace_id, "032x"
        )


class TestSemRuido:
    def test_ciclo_ocioso_do_relay_nao_exporta_span(
        self, rastreamento: Rastreamento
    ) -> None:
        relay = RelayDaOutbox(rastreamento.banco, PublicadorFalso(), usuario="billing")

        assert relay.entregar_pendentes(10) == 0

        assert rastreamento.spans == ()

    def test_relay_com_trabalho_exporta_a_publicacao_e_o_banco_dela(
        self, rastreamento: Rastreamento
    ) -> None:
        """O teste de cima nao e vazio: com linha a entregar, ha span."""
        salvar_orcamento_pendente(rastreamento.banco)
        rastreamento.exportador.clear()
        relay = RelayDaOutbox(rastreamento.banco, PublicadorFalso(), usuario="billing")

        assert relay.entregar_pendentes(10) == 1

        [publicacao] = rastreamento.do_tipo(SpanKind.PRODUCER)
        consultas = rastreamento.do_tipo(SpanKind.CLIENT)
        assert consultas
        assert {s.parent.span_id for s in consultas if s.parent} == {
            publicacao.context.span_id
        }

    def test_ciclo_ocioso_do_prazos_nao_exporta_span(
        self, rastreamento: Rastreamento
    ) -> None:
        resultado = executar_ciclo(
            rastreamento.banco,
            gateway=None,
            metricas=MetricasEspia(),
            max_recusas=3,
        )

        assert resultado.vazio
        assert rastreamento.spans == ()

    def test_ciclo_do_prazos_com_trabalho_exporta_o_span_e_o_banco_dele(
        self, rastreamento: Rastreamento
    ) -> None:
        vencido = orcamento(validade=timedelta(hours=1))
        uow = MongoUnitOfWork(rastreamento.banco)
        uow.executar(lambda: MongoOrcamentoRepository(uow).salvar(vencido))
        rastreamento.exportador.clear()

        resultado = executar_ciclo(
            rastreamento.banco,
            gateway=None,
            metricas=MetricasEspia(),
            max_recusas=3,
            relogio=RelogioFixo(AGORA + timedelta(hours=2)),
        )

        assert resultado.orcamentos_expirados == 1
        [ciclo] = [s for s in rastreamento.spans if s.name == "prazos"]
        consultas = rastreamento.do_tipo(SpanKind.CLIENT)
        assert consultas
        assert {s.parent.span_id for s in consultas if s.parent} == {
            ciclo.context.span_id
        }

    def test_raspagem_de_metricas_e_sondas_nao_exportam_span(
        self, rastreamento: Rastreamento, jwks_publicado: dict[str, Any]
    ) -> None:
        marcar_versao(rastreamento.banco)
        app = criar_app(configuracao(), banco=rastreamento.banco)
        with TestClient(app) as api:
            rastreamento.exportador.clear()
            texto = api.get("/metrics").text
            sondas = [
                api.get(caminho).status_code
                for caminho in ("/api/v1/saude", "/api/v1/saude/pronto")
            ]

        # A raspagem consultou o banco (coletor da outbox): sem pai, sem span.
        assert "pytstop_outbox_mais_antiga_segundos" in texto
        assert sondas == [200, 200]
        assert rastreamento.spans == ()


class TestSemTokenNemId:
    """O link do cliente e o checkout do simulador levam token (ADR-039)."""

    def test_link_publico_nao_leva_token_nem_id_em_atributo_algum(
        self, rastreamento: Rastreamento, jwks_publicado: dict[str, Any]
    ) -> None:
        orcamento_id, token = salvar_orcamento_pendente(rastreamento.banco)
        caminho = f"/api/v1/publico/orcamentos/{token}"
        app = criar_app(configuracao(), banco=rastreamento.banco)
        with TestClient(app) as api:
            rastreamento.exportador.clear()
            consulta = api.get(caminho)
            decisao = api.post(f"{caminho}/decisao", json={"decisao": "aprovar"})
            # Token certo e barra a mais: nenhuma rota casa, e o caminho cru
            # tem o token.
            sem_rota = api.post(f"{caminho}/decisao/", json={"decisao": "aprovar"})

        assert (consulta.status_code, decisao.status_code, sem_rota.status_code) == (
            200,
            200,
            404,
        )
        servidores = rastreamento.do_tipo(SpanKind.SERVER)
        assert [s.name for s in servidores] == [
            "GET /api/v1/publico/orcamentos/{token}",
            "POST /api/v1/publico/orcamentos/{token}/decisao",
            "POST",
        ]
        assert [
            s.attributes.get("http.target") for s in servidores if s.attributes
        ] == [
            "/api/v1/publico/orcamentos/{token}",
            "/api/v1/publico/orcamentos/{token}/decisao",
            "nao_roteada",
        ]
        valores = valores_de(rastreamento.spans)
        assert [v for v in valores if token in v or orcamento_id in v] == []

    def test_checkout_do_simulador_nao_leva_token_nem_id_em_atributo_algum(
        self, rastreamento: Rastreamento, jwks_publicado: dict[str, Any]
    ) -> None:
        app = criar_app(configuracao(), banco=rastreamento.banco)
        pagamento = solicitar_pagamento(app, rastreamento.banco)
        token = token_do_checkout(pagamento.checkout_url)
        pagina = f"/simulador/checkout/{pagamento.id}"
        acoes = f"/api/v1/simulador/pagamentos/{pagamento.id}"
        with TestClient(app) as api:
            rastreamento.exportador.clear()
            respostas = [
                api.get(pagina, params={"token": token}),
                api.post(f"{acoes}/recusar", params={"token": token}),
                api.post(f"{acoes}/aprovar", params={"token": token}),
            ]

        assert [r.status_code for r in respostas] == [200, 200, 200]
        servidores = rastreamento.do_tipo(SpanKind.SERVER)
        assert [s.name for s in servidores] == [
            "GET /simulador/checkout/{pagamento_id}",
            "POST /api/v1/simulador/pagamentos/{pagamento_id}/recusar",
            "POST /api/v1/simulador/pagamentos/{pagamento_id}/aprovar",
        ]
        valores = valores_de(rastreamento.spans)
        assert [v for v in valores if token in v or str(pagamento.id) in v] == []

    def test_span_de_banco_leva_o_comando_e_a_colecao_mas_nao_o_documento(
        self, rastreamento: Rastreamento
    ) -> None:
        segredo = "https://pagar.teste/checkout?token=SEGREDO-NO-DOCUMENTO"
        colecao = rastreamento.banco["colecao_de_rastreamento"]
        try:
            with trace.get_tracer("teste").start_as_current_span("trabalho"):
                colecao.insert_one({"checkout_url": segredo})
                colecao.find_one({"checkout_url": segredo})
        finally:
            colecao.drop()

        valores = valores_de(rastreamento.do_tipo(SpanKind.CLIENT))
        assert {"insert", "find", "colecao_de_rastreamento"} <= set(valores)
        assert [v for v in valores if "SEGREDO" in v] == []


class TestHttpx:
    def test_chamada_ao_mercado_pago_vira_span_de_cliente_dentro_do_trace(
        self, rastreamento: Rastreamento
    ) -> None:
        gateway = MercadoPagoGateway(
            ConfiguracaoMercadoPago(
                access_token="token-de-teste-do-mp",  # gitleaks:allow
                notification_url="https://billing.teste/webhook",
            )
        )
        with respx.mock(base_url="https://api.mercadopago.com") as mp:
            rota = mp.get("/v1/payments/123").respond(404, json={"message": "x"})
            with trace.get_tracer("teste").start_as_current_span(
                "requisicao", kind=SpanKind.SERVER
            ) as pai:
                assert gateway.consultar_pagamento("123") is None
            # Fora de um trace (a conciliacao do prazos): sem span.
            assert gateway.consultar_pagamento("123") is None
        gateway.fechar()

        [chamada] = rastreamento.do_tipo(SpanKind.CLIENT)
        assert chamada.parent is not None
        assert chamada.parent.span_id == pai.get_span_context().span_id
        assert chamada.attributes is not None
        assert chamada.attributes["http.url"] == (
            "https://api.mercadopago.com/v1/payments/123"
        )
        # O segredo do provedor viaja no header, que o span nao captura.
        assert [v for v in valores_de([chamada]) if "token-de-teste" in v] == []
        # O traceparent vai no pedido ao provedor; sem pai, com a flag de
        # amostragem (bit 0) desligada.
        com_pai, sem_pai = (c.request.headers["traceparent"] for c in rota.calls)
        assert com_pai.split("-")[1] == format(pai.get_span_context().trace_id, "032x")
        assert int(com_pai.split("-")[3], 16) & 1 == 1
        assert int(sem_pai.split("-")[3], 16) & 1 == 0
