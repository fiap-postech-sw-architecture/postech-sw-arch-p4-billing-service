"""Rastreamento distribuido da API, do relay e do ``prazos`` (ADR-043).

Os spans saem do provider de producao (``criar_provedor``, com o amostrador
real) para um exportador em memoria, e o PyMongo e o httpx sao instrumentados
pela mesma funcao dos processos. O cliente do MongoDB nasce depois da
instrumentacao: o PyMongo so rastreia os clientes criados depois dela.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
import respx
from fastapi.testclient import TestClient
from opentelemetry import baggage, context, trace
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.pymongo import PymongoInstrumentor
from opentelemetry.trace import SpanKind, StatusCode
from pika.exceptions import AMQPConnectionError
from pymongo.errors import BulkWriteError, DuplicateKeyError

from src.compartilhado.dominio.relogio import agora_utc
from src.compartilhado.infraestrutura.mensageria.relay import RelayDaOutbox
from src.compartilhado.infraestrutura.mensageria.telemetria import (
    instrumentar_clientes,
)
from src.compartilhado.infraestrutura.mongo import criar_cliente, marcar_versao
from src.compartilhado.infraestrutura.unit_of_work import MongoUnitOfWork
from src.main import criar_app
from src.orcamento.aplicacao.use_cases import ConsultarOrcamentos
from src.orcamento.dominio.orcamento import CanalDecisao
from src.orcamento.infraestrutura.repository import MongoOrcamentoRepository
from src.pagamento.aplicacao.ports import GatewayPagamentoIndisponivelError
from src.pagamento.aplicacao.use_cases import SolicitarPagamento
from src.pagamento.dominio.estados import StatusNoProvedor
from src.pagamento.infraestrutura.mercadopago import (
    ConfiguracaoMercadoPago,
    MercadoPagoGateway,
)
from src.pagamento.infraestrutura.orcamentos import OrcamentosMongoAdapter
from src.pagamento.infraestrutura.repository import MongoPagamentoRepository
from src.prazos import _UnidadeDoPrazo, executar_ciclo
from tests.factories import AGORA, orcamento, pagamento, situacao
from tests.integracao.apoio import (
    GatewayRoteirizado,
    MetricasEspia,
    PublicadorFalso,
    RelogioFixo,
    configuracao,
    token_do_checkout,
    token_do_link,
)
from tests.spans import tudo_que_os_spans_levam

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from fastapi import FastAPI
    from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )
    from pymongo.database import Database

    from src.pagamento.aplicacao.dtos import PagamentoDTO
    from src.pagamento.dominio.cobranca import SituacaoNoProvedor
    from tests.conftest import TelemetriaDeTeste

    Banco = Database[dict[str, Any]]


TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


@dataclass(frozen=True, slots=True)
class Rastreamento:
    exportador: InMemorySpanExporter
    banco: Banco
    provedor: TracerProvider

    def criar_app(self) -> FastAPI:
        """A API com o provider da sessao, que ela nao encerra (quem injeta o
        provider e dono dele)."""
        return criar_app(configuracao(), banco=self.banco, provedor=self.provedor)

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
) -> Iterator[Rastreamento]:
    provedor = _telemetria_da_sessao.provedor
    instrumentar_clientes(provedor)
    cliente = criar_cliente(mongo_uri)
    _telemetria_da_sessao.exportador.clear()
    try:
        yield Rastreamento(
            _telemetria_da_sessao.exportador, cliente[banco.name], provedor
        )
    finally:
        cliente.close()
        PymongoInstrumentor().uninstrument()
        HTTPXClientInstrumentor().uninstrument()


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


class ProvedorComTentativaEmAndamento(GatewayRoteirizado):
    """Mercado Pago que devolve sempre a mesma tentativa em andamento."""

    def __init__(self, tentativa: SituacaoNoProvedor) -> None:
        super().__init__()
        self._tentativa = tentativa

    def buscar_por_referencia_externa(
        self, referencia_externa: str
    ) -> list[SituacaoNoProvedor]:
        return [self._tentativa]


class TestRequisicao:
    def test_requisicao_gera_um_span_server_com_filhos_de_banco_no_mesmo_trace(
        self,
        rastreamento: Rastreamento,
        jwks_publicado: dict[str, Any],
        emitir_token: Callable[..., str],
    ) -> None:
        app = rastreamento.criar_app()
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
        app = rastreamento.criar_app()
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
        app = rastreamento.criar_app()
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

    def test_tracestate_longo_da_requisicao_nao_vai_para_a_outbox(
        self, rastreamento: Rastreamento, jwks_publicado: dict[str, Any]
    ) -> None:
        """A requisicao HTTP entra pela instrumentacao do FastAPI, que nao passa
        pelo teto de 512 caracteres da mensageria: o valor iria para a outbox, o
        AMQP e cada copia de retry."""
        _, token = salvar_orcamento_pendente(rastreamento.banco)
        grande = ",".join(f"v{i:02d}={'x' * 100}" for i in range(32))
        app = rastreamento.criar_app()
        with TestClient(app) as api:
            api.post(
                f"/api/v1/publico/orcamentos/{token}/decisao",
                json={"decisao": "aprovar"},
                headers={
                    "traceparent": TRACEPARENT,
                    "tracestate": grande,
                },
            )

        linha = rastreamento.banco["outbox"].find_one({"tipo": "OrcamentoAprovado"})
        assert linha is not None
        assert linha.get("tracestate", "") == ""
        assert linha["traceparent"].split("-")[1] == "4bf92f3577b34da6a3ce929d0e0e4736"


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

    def test_ciclo_ocioso_do_prazos_com_conciliacao_nao_exporta_span(
        self, rastreamento: Rastreamento
    ) -> None:
        """Com o Mercado Pago real o ``prazos`` consulta o provedor a cada ciclo e
        reaplica a tentativa em andamento: so a primeira vez escreve (entra no
        historico); depois nao ha escrita, e ciclo sem escrita nao deixa trace."""
        aberto = pagamento(validade=timedelta(hours=1))
        uow = MongoUnitOfWork(rastreamento.banco)
        uow.executar(lambda: MongoPagamentoRepository(uow).salvar(aberto))
        provedor = ProvedorComTentativaEmAndamento(
            situacao(aberto, status=StatusNoProvedor.EM_ANDAMENTO)
        )

        def ciclo() -> int:
            return executar_ciclo(
                rastreamento.banco,
                gateway=provedor,
                metricas=MetricasEspia(),
                max_recusas=3,
                relogio=RelogioFixo(AGORA),
            ).conciliados

        rastreamento.exportador.clear()
        assert ciclo() == 1
        [escrita] = [s for s in rastreamento.spans if s.name == "prazos"]
        escritas = rastreamento.do_tipo(SpanKind.CLIENT)
        assert escritas
        assert {s.parent.span_id for s in escritas if s.parent} == {
            escrita.context.span_id
        }

        rastreamento.exportador.clear()
        assert [ciclo(), ciclo()] == [1, 1]
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
        app = rastreamento.criar_app()
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


class TestSpanDoPrazo:
    """O span do ``prazos`` abre na primeira escrita da transacao."""

    def test_transacao_com_duas_escritas_abre_um_span_so(
        self, rastreamento: Rastreamento
    ) -> None:
        uow = _UnidadeDoPrazo(rastreamento.banco)

        def escrever() -> None:
            MongoOrcamentoRepository(uow).salvar(orcamento())
            MongoPagamentoRepository(uow).salvar(pagamento())

        rastreamento.exportador.clear()
        uow.executar(escrever)

        assert [s.name for s in rastreamento.spans if s.name == "prazos"] == ["prazos"]

    def test_falha_antes_de_escrever_nao_deixa_span(
        self, rastreamento: Rastreamento
    ) -> None:
        uow = _UnidadeDoPrazo(rastreamento.banco)

        def falhar() -> None:
            msg = "antes da primeira escrita"
            raise RuntimeError(msg)

        rastreamento.exportador.clear()
        with pytest.raises(RuntimeError, match="antes da primeira"):
            uow.executar(falhar)

        assert rastreamento.spans == ()

    def test_escrita_fora_da_transacao_segue_recusada_e_sem_span(
        self, rastreamento: Rastreamento
    ) -> None:
        uow = _UnidadeDoPrazo(rastreamento.banco)

        rastreamento.exportador.clear()
        with pytest.raises(RuntimeError, match="fora de uma transacao"):
            MongoOrcamentoRepository(uow).salvar(orcamento())

        assert rastreamento.spans == ()


class TestSemTokenNemId:
    """O link do cliente e o checkout do simulador levam token (ADR-039)."""

    def test_link_publico_nao_leva_token_nem_id_em_atributo_algum(
        self, rastreamento: Rastreamento, jwks_publicado: dict[str, Any]
    ) -> None:
        orcamento_id, token = salvar_orcamento_pendente(rastreamento.banco)
        caminho = f"/api/v1/publico/orcamentos/{token}"
        app = rastreamento.criar_app()
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
        # Requisicao sem falha: nenhum SERVER com status de erro (por onde o
        # alvo cru chegaria ao Jaeger, na descricao).
        assert {s.status.status_code for s in servidores} == {StatusCode.UNSET}
        valores = tudo_que_os_spans_levam(rastreamento.spans)
        assert [v for v in valores if token in v or orcamento_id in v] == []

    def test_checkout_do_simulador_nao_leva_token_nem_id_em_atributo_algum(
        self, rastreamento: Rastreamento, jwks_publicado: dict[str, Any]
    ) -> None:
        app = rastreamento.criar_app()
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
        assert {s.status.status_code for s in servidores} == {StatusCode.UNSET}
        valores = tudo_que_os_spans_levam(rastreamento.spans)
        assert [v for v in valores if token in v or str(pagamento.id) in v] == []

    def test_excecao_da_requisicao_nao_vai_para_o_span(
        self,
        rastreamento: Rastreamento,
        jwks_publicado: dict[str, Any],
        emitir_token: Callable[..., str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A API responde o envelope do 500 e a excecao nao chega ao span: a
        mensagem dela pode ter dado de entrada."""

        def quebrar(*_args: Any, **_opcoes: Any) -> None:
            msg = "SEGREDO-NA-EXCECAO"
            raise RuntimeError(msg)

        monkeypatch.setattr(ConsultarOrcamentos, "por_id", quebrar)
        app = rastreamento.criar_app()
        with TestClient(app, raise_server_exceptions=False) as api:
            rastreamento.exportador.clear()
            resposta = api.get(
                f"/api/v1/orcamentos/{uuid4()}",
                headers=autorizacao(emitir_token, "atendente"),
            )

        assert resposta.status_code == 500
        [servidor] = rastreamento.do_tipo(SpanKind.SERVER)
        assert servidor.status.status_code is StatusCode.ERROR
        assert servidor.status.description in (None, "")
        assert servidor.events == ()
        textos = tudo_que_os_spans_levam(rastreamento.spans)
        assert [t for t in textos if "SEGREDO" in t] == []

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

        valores = tudo_que_os_spans_levam(rastreamento.do_tipo(SpanKind.CLIENT))
        assert {
            "db.statement=insert",
            "db.statement=find",
            "db.mongodb.collection=colecao_de_rastreamento",
        } <= set(valores)
        assert [v for v in valores if "SEGREDO" in v] == []


class TestPropagacao:
    """So ``traceparent`` e ``tracestate`` entram e saem (o padrao do SDK soma o
    ``baggage``)."""

    def test_baggage_do_chamador_nao_e_aceito_pela_api(
        self, rastreamento: Rastreamento, jwks_publicado: dict[str, Any]
    ) -> None:
        app = rastreamento.criar_app()

        @app.get("/baggage-do-chamador")
        def ler() -> dict[str, str]:
            return {chave: str(v) for chave, v in baggage.get_all().items()}

        with TestClient(app) as api:
            resposta = api.get(
                "/baggage-do-chamador",
                headers={
                    "baggage": "usuario=CANARY-BAGAGEM",
                    "traceparent": TRACEPARENT,
                },
            )

        assert resposta.json() == {}

    def test_baggage_nao_vai_para_o_mercado_pago(
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
            ligado = context.attach(baggage.set_baggage("usuario", "CANARY-BAGAGEM"))
            try:
                with trace.get_tracer("teste").start_as_current_span("requisicao"):
                    gateway.consultar_pagamento("123")
            finally:
                context.detach(ligado)
        gateway.fechar()

        [pedido] = [c.request for c in rota.calls]
        assert "traceparent" in pedido.headers
        assert "baggage" not in pedido.headers


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
        assert [
            v for v in tudo_que_os_spans_levam([chamada]) if "token-de-teste" in v
        ] == []
        # O traceparent vai no pedido ao provedor; sem pai, com a flag de
        # amostragem (bit 0) desligada.
        com_pai, sem_pai = (c.request.headers["traceparent"] for c in rota.calls)
        assert com_pai.split("-")[1] == format(pai.get_span_context().trace_id, "032x")
        assert int(com_pai.split("-")[3], 16) & 1 == 1
        assert int(sem_pai.split("-")[3], 16) & 1 == 0


def tipos_de_excecao(span: ReadableSpan) -> list[str]:
    return [
        str((evento.attributes or {}).get("exception.type"))
        for evento in span.events
        if evento.name == "exception"
    ]


class TestSemTextoDeErro:
    """O texto de uma excecao leva o que ela viu: o cabecalho que o httpx nao
    enviou, o CPF de uma requisicao, o documento que o MongoDB recusou. O log o
    passa pelo scrub; o span nao leva texto algum: o processador do provider tira
    a mensagem, o stack trace e a descricao do status de todo span, e ficam o
    tipo da excecao e o motivo em ``pytstop.falha``."""

    def test_excecao_fora_do_middleware_de_seguranca_nao_leva_o_texto(
        self, rastreamento: Rastreamento, jwks_publicado: dict[str, Any]
    ) -> None:
        class Explode:
            """Middleware mais externo que o ``SecurityHeadersMiddleware``."""

            def __init__(self, app: Any) -> None:
                self.app = app

            async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
                if scope["type"] == "http" and scope["path"] == "/explode":
                    msg = "SEGREDO-NO-MIDDLEWARE cpf 123.456.789-09"
                    raise RuntimeError(msg)
                await self.app(scope, receive, send)

        app = rastreamento.criar_app()
        app.add_middleware(Explode)
        with TestClient(app, raise_server_exceptions=False) as api:
            rastreamento.exportador.clear()
            resposta = api.get("/explode")

        assert resposta.status_code == 500
        [servidor] = rastreamento.do_tipo(SpanKind.SERVER)
        assert servidor.status.status_code is StatusCode.ERROR
        assert servidor.status.description in (None, "")
        assert tipos_de_excecao(servidor) == ["RuntimeError"]
        textos = tudo_que_os_spans_levam(rastreamento.spans)
        assert [t for t in textos if "SEGREDO" in t or "123.456" in t] == []

    def test_token_do_mercado_pago_nao_vai_para_o_span_do_httpx(
        self, rastreamento: Rastreamento
    ) -> None:
        """Token com quebra de linha no fim (um Secret criado de arquivo): o h11
        recusa o cabecalho e a mensagem do erro traz ``Bearer <token>``."""
        with socket.socket() as escuta:
            escuta.bind(("127.0.0.1", 0))
            escuta.listen()
            gateway = MercadoPagoGateway(
                ConfiguracaoMercadoPago(
                    access_token="APP_USR-TOKEN-SECRETO\n",  # gitleaks:allow
                    notification_url="https://billing.teste/webhook",
                    base_url=f"http://127.0.0.1:{escuta.getsockname()[1]}",
                    tentativas=1,
                ),
                dormir=lambda _segundos: None,
            )
            with (
                trace.get_tracer("teste").start_as_current_span("requisicao"),
                pytest.raises(GatewayPagamentoIndisponivelError),
            ):
                gateway.consultar_pagamento("123")
            gateway.fechar()

        [chamada] = rastreamento.do_tipo(SpanKind.CLIENT)
        assert chamada.status.status_code is StatusCode.ERROR
        assert chamada.status.description in (None, "")
        assert tipos_de_excecao(chamada) == ["httpx.LocalProtocolError"]
        textos = tudo_que_os_spans_levam(rastreamento.spans)
        assert [t for t in textos if "TOKEN-SECRETO" in t or "Bearer" in t] == []

    @pytest.mark.parametrize(
        ("erro", "escrever"),
        [
            (BulkWriteError, "lote"),
            (DuplicateKeyError, "uma por vez"),
        ],
    )
    def test_erro_do_mongo_no_prazos_nao_leva_o_documento(
        self,
        rastreamento: Rastreamento,
        erro: type[Exception],
        escrever: str,
    ) -> None:
        segredo = "SEGREDO-NO-DOCUMENTO"
        colecao = rastreamento.banco["colecao_de_erro"]
        colecao.create_index("dado", unique=True)
        uow = _UnidadeDoPrazo(rastreamento.banco)
        repositorio = MongoOrcamentoRepository(uow)
        documentos = [{"dado": segredo}, {"dado": segredo}]

        def escrever_e_falhar() -> None:
            repositorio.salvar(orcamento())  # a escrita abre o span do ciclo
            if escrever == "lote":
                colecao.insert_many(documentos, session=uow.sessao)
            else:
                for documento in documentos:
                    colecao.insert_one(dict(documento), session=uow.sessao)

        rastreamento.exportador.clear()
        try:
            with pytest.raises(erro):
                uow.executar(escrever_e_falhar)
        finally:
            colecao.drop()

        [ciclo] = [s for s in rastreamento.spans if s.name == "prazos"]
        assert ciclo.status.status_code is StatusCode.ERROR
        assert ciclo.status.description in (None, "")
        assert (ciclo.attributes or {})["pytstop.falha"] == erro.__name__
        assert tipos_de_excecao(ciclo) == [f"pymongo.errors.{erro.__name__}"]
        textos = tudo_que_os_spans_levam(rastreamento.spans)
        assert [t for t in textos if "SEGREDO" in t] == []

    def test_erro_do_broker_no_relay_nao_leva_o_texto(
        self, rastreamento: Rastreamento
    ) -> None:
        salvar_orcamento_pendente(rastreamento.banco)
        rastreamento.exportador.clear()
        broker = AMQPConnectionError("amqp://billing:SEGREDO-DO-BROKER@rabbitmq")
        relay = RelayDaOutbox(
            rastreamento.banco, PublicadorFalso(erro=broker), usuario="billing"
        )

        with pytest.raises(AMQPConnectionError):
            relay.entregar_pendentes(10)

        [publicacao] = rastreamento.do_tipo(SpanKind.PRODUCER)
        assert publicacao.status.status_code is StatusCode.ERROR
        assert publicacao.status.description in (None, "")
        assert (publicacao.attributes or {})["pytstop.falha"] == "AMQPConnectionError"
        assert tipos_de_excecao(publicacao) == ["pika.exceptions.AMQPConnectionError"]
        textos = tudo_que_os_spans_levam(rastreamento.spans)
        assert [t for t in textos if "SEGREDO" in t] == []
