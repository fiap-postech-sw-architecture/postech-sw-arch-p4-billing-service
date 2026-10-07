"""Relay da outbox contra MongoDB real, com publicador falso (ADR-036).

Claim atomico com lease e fencing, publicacao no contexto gravado, tentativas
so para falha da mensagem e laco do processo sem reivindicar sem conexao. O
broker real entra em ``test_mensageria_rabbitmq.py``.
"""

from __future__ import annotations

import io
import json
import logging
import threading
from datetime import timedelta
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pika
import pytest
from opentelemetry import trace
from opentelemetry.trace import SpanKind
from pika.exceptions import AMQPConnectionError, StreamLostError
from prometheus_client import REGISTRY
from pymongo import MongoClient
from pymongo.errors import AutoReconnect

from src import relay as processo_relay
from src.compartilhado.infraestrutura.logging import configurar_logging
from src.compartilhado.infraestrutura.mensageria.amqp import (
    CanalAmqp,
    MensagemRecusadaError,
)
from src.compartilhado.infraestrutura.mensageria.metricas import ColetorDaOutbox
from src.compartilhado.infraestrutura.mensageria.relay import RelayDaOutbox, _Linha
from src.compartilhado.infraestrutura.mensageria.telemetria import contexto_atual
from src.compartilhado.infraestrutura.processo import CONECTANDO, PRONTO
from src.compartilhado.infraestrutura.unit_of_work import (
    MensagemRecebida,
    MongoUnitOfWork,
    UnidadeDaMensagem,
    processar_mensagem,
)
from src.orcamento.infraestrutura.repository import MongoOrcamentoRepository
from src.relay import rodar
from tests.factories import orcamento
from tests.integracao.apoio import PublicadorFalso, RelogioFixo

if TYPE_CHECKING:
    from pathlib import Path

    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )
    from pymongo.database import Database

    Banco = Database[dict[str, Any]]

_tracer = trace.get_tracer("teste")


def _gravar_orcamentos(banco: Banco, quantidade: int = 1) -> None:
    # Mesmo instante inicial do RelogioFixo dos relays: a linha ja e elegivel.
    uow = MongoUnitOfWork(banco, relogio=RelogioFixo())
    repo = MongoOrcamentoRepository(uow)

    def trabalho() -> None:
        for _ in range(quantidade):
            repo.salvar(orcamento())

    uow.executar(trabalho)


def _linhas(banco: Banco) -> list[dict[str, Any]]:
    return list(banco["outbox"].find().sort("_id"))


def _relay(
    banco: Banco, publicador: PublicadorFalso, relogio: RelogioFixo
) -> RelayDaOutbox:
    return RelayDaOutbox(banco, publicador, usuario="billing", relogio=relogio)


def _publicadas_do_tipo(tipo: str) -> float:
    valor = REGISTRY.get_sample_value(
        "pytstop_mensagens_publicadas_total", {"tipo": tipo}
    )
    return valor or 0.0


class TestEntrega:
    def test_publica_o_envelope_com_as_propriedades_do_contrato(
        self, banco: Banco
    ) -> None:
        relogio = RelogioFixo()
        _gravar_orcamentos(banco)
        [linha] = _linhas(banco)
        publicador = PublicadorFalso()
        antes = _publicadas_do_tipo("OrcamentoGerado")

        assert _relay(banco, publicador, relogio).entregar_pendentes(10) == 1

        [(exchange, routing_key, corpo, props)] = publicador.publicadas
        assert (exchange, routing_key) == (
            "pytstop.eventos",
            "evento.billing.orcamento_gerado",
        )
        assert json.loads(corpo) == linha["envelope"]
        assert props.message_id == str(linha["_id"])
        assert props.correlation_id == linha["envelope"]["correlation_id"]
        assert props.type == "OrcamentoGerado"
        assert props.user_id == "billing"
        assert props.content_type == "application/json"
        assert props.delivery_mode == 2
        # Primeira entrega: sem x-tentativa (so a copia de retry o leva).
        assert "x-tentativa" not in props.headers
        [entregue] = _linhas(banco)
        assert entregue["status"] == "entregue"
        assert entregue["entregue_em"] == relogio.agora
        assert "reivindicacao" not in entregue
        assert _publicadas_do_tipo("OrcamentoGerado") == antes + 1

    def test_publica_em_ordem_e_respeita_o_lote(self, banco: Banco) -> None:
        _gravar_orcamentos(banco, 3)
        publicador = PublicadorFalso()
        relay = _relay(banco, publicador, RelogioFixo())

        assert relay.entregar_pendentes(2) == 2
        assert relay.entregar_pendentes(2) == 1
        assert relay.entregar_pendentes(2) == 0
        assert publicador.ids == [str(linha["_id"]) for linha in _linhas(banco)]

    def test_span_producer_e_filho_do_contexto_gravado_na_outbox(
        self, banco: Banco, spans: InMemorySpanExporter
    ) -> None:
        comando = MensagemRecebida(
            id=uuid4(), tipo="GerarOrcamento", correlation_id=uuid4()
        )

        def handler(uow: UnidadeDaMensagem) -> None:
            uow.executar(lambda: MongoOrcamentoRepository(uow).salvar(orcamento()))

        with _tracer.start_as_current_span("process GerarOrcamento") as consumidor:
            processar_mensagem(banco, comando, handler, relogio=RelogioFixo())
        publicador = PublicadorFalso()

        _relay(banco, publicador, RelogioFixo()).entregar_pendentes(1)

        producer = next(
            s for s in spans.get_finished_spans() if s.kind is SpanKind.PRODUCER
        )
        assert producer.name == "publish OrcamentoGerado"
        assert producer.attributes is not None
        assert producer.attributes["messaging.operation.type"] == "send"
        assert producer.parent is not None
        assert producer.parent.span_id == consumidor.get_span_context().span_id
        assert producer.context.trace_id == consumidor.get_span_context().trace_id
        [(*_, props)] = publicador.publicadas
        assert props.headers["traceparent"].split("-")[2] == (
            f"{producer.context.span_id:016x}"
        )

    def test_span_producer_liga_quem_retomou_o_passo(
        self, banco: Banco, spans: InMemorySpanExporter
    ) -> None:
        _gravar_orcamentos(banco)
        with _tracer.start_as_current_span("process GerarOrcamento") as saga:
            pai = contexto_atual()
        with _tracer.start_as_current_span("POST decisao") as requisicao:
            retomou = contexto_atual()
        banco["outbox"].update_one({}, {"$set": {**pai, "retomado_por": retomou}})

        _relay(banco, PublicadorFalso(), RelogioFixo()).entregar_pendentes(1)

        [producer] = [
            s for s in spans.get_finished_spans() if s.kind is SpanKind.PRODUCER
        ]
        assert producer.parent is not None
        assert producer.parent.span_id == saga.get_span_context().span_id
        assert producer.context.trace_id == saga.get_span_context().trace_id
        [link] = producer.links
        assert link.context.trace_id == requisicao.get_span_context().trace_id
        assert link.context.span_id == requisicao.get_span_context().span_id

    def test_linha_sem_quem_retomou_publica_sem_link(
        self, banco: Banco, spans: InMemorySpanExporter
    ) -> None:
        _gravar_orcamentos(banco)

        _relay(banco, PublicadorFalso(), RelogioFixo()).entregar_pendentes(1)

        [producer] = [
            s for s in spans.get_finished_spans() if s.kind is SpanKind.PRODUCER
        ]
        assert producer.links == ()

    def test_log_da_entrega_leva_o_trace_da_publicacao(
        self, banco: Banco, spans: InMemorySpanExporter
    ) -> None:
        _gravar_orcamentos(banco)
        saida = io.StringIO()
        configurar_logging(saida)
        try:
            _relay(banco, PublicadorFalso(), RelogioFixo()).entregar_pendentes(1)
        finally:
            logging.getLogger().handlers.clear()

        [producer] = [
            s for s in spans.get_finished_spans() if s.kind is SpanKind.PRODUCER
        ]
        [entregue] = [
            linha
            for linha in map(json.loads, saida.getvalue().splitlines())
            if linha["event"] == "outbox_message_published"
        ]
        assert entregue["trace_id"] == f"{producer.context.trace_id:032x}"
        assert entregue["span_id"] == f"{producer.context.span_id:016x}"

    def test_linha_reivindicada_nao_poe_o_envelope_no_repr(self, banco: Banco) -> None:
        _gravar_orcamentos(banco)
        [doc] = _linhas(banco)
        doc["reivindicacao"] = uuid4()

        texto = repr(_Linha.de(doc))

        assert doc["envelope"]["dados"]["link_decisao"] not in texto
        assert str(doc["_id"]) in texto

    def test_publica_o_tracestate_gravado_na_outbox(self, banco: Banco) -> None:
        _gravar_orcamentos(banco)
        banco["outbox"].update_one(
            {},
            {
                "$set": {
                    "traceparent": (
                        "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
                    ),
                    "tracestate": "pytstop=abc",
                }
            },
        )
        publicador = PublicadorFalso()

        _relay(banco, publicador, RelogioFixo()).entregar_pendentes(1)

        [(*_, props)] = publicador.publicadas
        assert props.headers["tracestate"] == "pytstop=abc"

    def test_reivindica_pela_proxima_tentativa_e_nao_pela_criacao(
        self, banco: Banco
    ) -> None:
        relogio = RelogioFixo()
        _gravar_orcamentos(banco, 2)
        primeira, segunda = _linhas(banco)
        # A primeira gravada volta de um atraso e fica elegivel depois da
        # segunda: a segunda sai antes.
        banco["outbox"].update_one(
            {"_id": primeira["_id"]},
            {"$set": {"proxima_tentativa_em": relogio.agora + timedelta(seconds=1)}},
        )
        relogio.avancar(seconds=2)
        publicador = PublicadorFalso()

        _relay(banco, publicador, relogio).entregar_pendentes(2)

        assert publicador.ids == [str(segunda["_id"]), str(primeira["_id"])]

    def test_envelope_nao_vai_para_o_log_nem_para_o_span(
        self,
        banco: Banco,
        caplog: pytest.LogCaptureFixture,
        spans: InMemorySpanExporter,
    ) -> None:
        _gravar_orcamentos(banco)
        [linha] = _linhas(banco)
        dados = linha["envelope"]["dados"]

        with caplog.at_level(logging.INFO):
            _relay(banco, PublicadorFalso(), RelogioFixo()).entregar_pendentes(10)

        logs = "\n".join(
            str(r.__dict__) for r in caplog.records if r.name.startswith("src.")
        )
        atributos = "\n".join(
            str(dict(s.attributes or {})) for s in spans.get_finished_spans()
        )
        assert "outbox_message_published" in logs
        for valor in (dados["linhas"][0]["descricao"], dados["link_decisao"]):
            assert valor not in logs
            assert valor not in atributos

    def test_laco_ocioso_nao_abre_span(
        self, banco: Banco, spans: InMemorySpanExporter
    ) -> None:
        assert (
            _relay(banco, PublicadorFalso(), RelogioFixo()).entregar_pendentes(10) == 0
        )
        assert spans.get_finished_spans() == ()


class TestFalhas:
    def test_recusa_conta_tentativa_com_os_atrasos_do_p3_ate_dead(
        self, banco: Banco
    ) -> None:
        relogio = RelogioFixo()
        _gravar_orcamentos(banco)
        relay = _relay(
            banco, PublicadorFalso(MensagemRecusadaError("UnroutableError")), relogio
        )

        for tentativa, atraso in enumerate((1, 4, 16, 64), start=1):
            assert relay.entregar_pendentes(1) == 1
            [linha] = _linhas(banco)
            assert (linha["status"], linha["tentativas"]) == ("pendente", tentativa)
            assert linha["proxima_tentativa_em"] == relogio.agora + timedelta(
                seconds=atraso
            )
            assert linha["ultimo_erro"] == "UnroutableError"
            # Antes do atraso a linha nao e elegivel.
            relogio.avancar(seconds=atraso - 0.5)
            assert relay.entregar_pendentes(1) == 0
            relogio.avancar(seconds=0.5)

        assert relay.entregar_pendentes(1) == 1
        [morta] = _linhas(banco)
        assert (morta["status"], morta["tentativas"]) == ("dead", 5)
        assert morta["morta_em"] == relogio.agora
        relogio.avancar(hours=1)
        assert relay.entregar_pendentes(1) == 0

    def test_queda_do_broker_devolve_a_linha_sem_gastar_tentativa(
        self, banco: Banco
    ) -> None:
        relogio = RelogioFixo()
        _gravar_orcamentos(banco)
        relay = _relay(banco, PublicadorFalso(StreamLostError("caiu")), relogio)

        with pytest.raises(StreamLostError):
            relay.entregar_pendentes(10)

        [linha] = _linhas(banco)
        assert (linha["status"], linha["tentativas"]) == ("pendente", 0)
        assert linha["proxima_tentativa_em"] == relogio.agora
        assert "reivindicacao" not in linha

    def test_defeito_ao_publicar_conta_tentativa_em_vez_de_derrubar_o_relay(
        self, banco: Banco, caplog: pytest.LogCaptureFixture
    ) -> None:
        relogio = RelogioFixo()
        _gravar_orcamentos(banco)
        relay = _relay(banco, PublicadorFalso(ValueError("defeito")), relogio)

        with caplog.at_level(logging.ERROR):
            assert relay.entregar_pendentes(1) == 1

        [linha] = _linhas(banco)
        assert (linha["status"], linha["tentativas"]) == ("pendente", 1)
        assert linha["ultimo_erro"] == "ValueError"
        assert any(
            r.getMessage() == "outbox_publish_failed" and r.exc_info
            for r in caplog.records
        )


class PublicadorQueEspera(PublicadorFalso):
    """So publica quando todos os relays da corrida ja reivindicaram."""

    def __init__(self, todos_reivindicaram: threading.Barrier) -> None:
        super().__init__()
        self.todos_reivindicaram = todos_reivindicaram

    def publicar(
        self,
        exchange: str,
        routing_key: str,
        corpo: bytes,
        propriedades: pika.BasicProperties,
    ) -> None:
        self.todos_reivindicaram.wait(timeout=10)
        super().publicar(exchange, routing_key, corpo, propriedades)


class TestDoisRelays:
    @pytest.mark.parametrize("rodada", range(25))
    def test_concorrentes_nunca_reivindicam_a_mesma_linha(
        self, banco: Banco, rodada: int
    ) -> None:
        """Quatro relays largam juntos, cada um reivindica uma linha e so
        publica quando todos reivindicaram: a janela do claim fica aberta para
        todos ao mesmo tempo (com um laco longo, um relay a monopolizava e o
        claim nao atomico passava em uma de cada doze execucoes)."""
        relays = 4
        _gravar_orcamentos(banco, relays)
        relogio = RelogioFixo()
        largada = threading.Barrier(relays)
        todos_reivindicaram = threading.Barrier(relays)
        publicadores = [PublicadorQueEspera(todos_reivindicaram) for _ in range(relays)]

        def entregar(publicador: PublicadorFalso) -> None:
            relay = _relay(banco, publicador, relogio)
            largada.wait(timeout=10)
            relay.entregar_pendentes(1)

        threads = [threading.Thread(target=entregar, args=(p,)) for p in publicadores]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        publicadas = [mensagem for p in publicadores for mensagem in p.ids]
        assert len(publicadas) == len(set(publicadas)) == relays
        assert {linha["status"] for linha in _linhas(banco)} == {"entregue"}

    def test_lease_vale_exatamente_30_segundos(self, banco: Banco) -> None:
        _gravar_orcamentos(banco)
        relogio = RelogioFixo()
        solta = threading.Event()
        lento = PublicadorFalso(segura=solta)
        primeiro = threading.Thread(
            target=_relay(banco, lento, relogio).entregar_pendentes, args=(1,)
        )
        primeiro.start()
        assert lento.entrou.wait(10)
        segundo = PublicadorFalso()
        try:
            relogio.avancar(seconds=29, milliseconds=999)
            assert _relay(banco, segundo, relogio).entregar_pendentes(1) == 0
            relogio.avancar(milliseconds=1)
            assert _relay(banco, segundo, relogio).entregar_pendentes(1) == 1
        finally:
            solta.set()
            primeiro.join(timeout=10)

    @pytest.mark.parametrize(
        "erro_do_primeiro",
        [None, MensagemRecusadaError("NackError")],
        ids=["primeiro-confirma-depois", "primeiro-falha-depois"],
    )
    def test_lease_vencido_e_retomado_sem_o_primeiro_mexer_na_linha(
        self, banco: Banco, erro_do_primeiro: Exception | None
    ) -> None:
        _gravar_orcamentos(banco)
        relogio = RelogioFixo()
        solta = threading.Event()
        lento = PublicadorFalso(erro_do_primeiro, segura=solta)
        primeiro = threading.Thread(
            target=_relay(banco, lento, relogio).entregar_pendentes, args=(1,)
        )
        primeiro.start()
        assert lento.entrou.wait(10)
        [reivindicada] = _linhas(banco)
        assert reivindicada["status"] == "em_entrega"

        # Lease de 30 s ainda valido: ninguem mais pega a linha.
        segundo = PublicadorFalso()
        assert _relay(banco, segundo, relogio).entregar_pendentes(1) == 0
        relogio.avancar(seconds=31)
        assert _relay(banco, segundo, relogio).entregar_pendentes(1) == 1
        retomada = _linhas(banco)[0]
        assert retomada["status"] == "entregue"

        solta.set()
        primeiro.join(timeout=10)
        # O primeiro publicou (entrega pelo menos uma vez), mas nao marcou nem
        # contou tentativa: a linha e de quem a retomou.
        assert len(lento.publicadas) == len(segundo.publicadas) == 1
        assert _linhas(banco) == [retomada]
        assert retomada["tentativas"] == 0

    @pytest.mark.parametrize(
        "erro_do_primeiro",
        [None, MensagemRecusadaError("NackError")],
        ids=["primeiro-confirma", "primeiro-falha"],
    )
    def test_relay_atrasado_nao_mexe_na_linha_que_outro_esta_entregando(
        self,
        banco: Banco,
        erro_do_primeiro: Exception | None,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Os dois em voo ao mesmo tempo: so o token distingue o dono da linha."""
        caplog.set_level(logging.INFO)
        _gravar_orcamentos(banco)
        relogio = RelogioFixo()
        solta_o_primeiro, solta_o_segundo = threading.Event(), threading.Event()
        primeiro = PublicadorFalso(erro_do_primeiro, segura=solta_o_primeiro)
        segundo = PublicadorFalso(segura=solta_o_segundo)
        thread_do_primeiro = threading.Thread(
            target=_relay(banco, primeiro, relogio).entregar_pendentes, args=(1,)
        )
        thread_do_primeiro.start()
        assert primeiro.entrou.wait(10)
        relogio.avancar(seconds=31)
        thread_do_segundo = threading.Thread(
            target=_relay(banco, segundo, relogio).entregar_pendentes, args=(1,)
        )
        thread_do_segundo.start()
        assert segundo.entrou.wait(10)
        [do_segundo] = _linhas(banco)

        solta_o_primeiro.set()
        thread_do_primeiro.join(timeout=10)
        # O primeiro terminou (publicou ou falhou), e a linha segue do segundo.
        assert _linhas(banco) == [do_segundo]
        assert do_segundo["status"] == "em_entrega"
        mensagens = [r.getMessage() for r in caplog.records]
        assert "outbox_claim_lost" in mensagens
        assert "outbox_message_refused" not in mensagens

        solta_o_segundo.set()
        thread_do_segundo.join(timeout=10)
        [entregue] = _linhas(banco)
        assert (entregue["status"], entregue["tentativas"]) == ("entregue", 0)


class CanalFalso(CanalAmqp):
    """``CanalAmqp`` sem broker: falha ao abrir N vezes, pode publicar com erro
    ate uma abertura dada e para o laco na segunda espera."""

    def __init__(
        self,
        parar: threading.Event,
        *,
        falhas_ao_abrir: int = 0,
        erro_ate_a_abertura: int = 0,
        bloqueada_nas_esperas: int = 0,
    ) -> None:
        super().__init__(pika.ConnectionParameters())
        self.falhas_ao_abrir = falhas_ao_abrir
        self.erro_ate_a_abertura = erro_ate_a_abertura
        self.bloqueada_nas_esperas = bloqueada_nas_esperas
        self.aberturas = 0
        self.esperas = 0
        self.fechamentos = 0
        self.parar = parar
        self.publicador = PublicadorFalso()

    def abrir(self) -> None:
        self.aberturas += 1
        if self.aberturas <= self.falhas_ao_abrir:
            raise AMQPConnectionError("broker fora")
        self.publicador.erro = (
            StreamLostError("caiu")
            if self.aberturas <= self.erro_ate_a_abertura
            else None
        )

    def publicar(
        self,
        exchange: str,
        routing_key: str,
        corpo: bytes,
        propriedades: pika.BasicProperties,
    ) -> None:
        self.publicador.publicar(exchange, routing_key, corpo, propriedades)

    def aguardar(self, segundos: float) -> None:
        self.esperas += 1
        # Connection.Blocked nas primeiras esperas, depois o Unblocked.
        self.bloqueada = self.esperas < self.bloqueada_nas_esperas
        if self.esperas >= 2 + self.bloqueada_nas_esperas:
            self.parar.set()

    def fechar(self) -> None:
        self.fechamentos += 1


class RelayEspiado(RelayDaOutbox):
    """Anota o estado do arquivo de vida a cada lote que o laco pede."""

    def __init__(self, banco: Banco, canal: CanalFalso, heartbeat: Path) -> None:
        super().__init__(banco, canal, usuario="billing", relogio=RelogioFixo())
        self.heartbeat = heartbeat
        self.estados: list[str] = []

    def entregar_pendentes(self, limite: int) -> int:
        self.estados.append(self.heartbeat.read_text())
        return super().entregar_pendentes(limite)


def _banco_inalcancavel() -> MongoClient[dict[str, Any]]:
    return MongoClient(
        "mongodb://127.0.0.1:1/?directConnection=true&serverSelectionTimeoutMS=100",
        uuidRepresentation="standard",
    )


class TestLacoDoProcesso:
    def test_sem_conexao_nao_reivindica_e_reconecta(
        self, banco: Banco, tmp_path: Path
    ) -> None:
        _gravar_orcamentos(banco)
        parar = threading.Event()
        canal = CanalFalso(parar, falhas_ao_abrir=3)
        heartbeat = tmp_path / "relay-heartbeat"
        relay = RelayEspiado(banco, canal, heartbeat)

        rodar(relay, canal, parar=parar, heartbeat=heartbeat, espera_maxima=0.01)

        # So conectado pede lote: as tres aberturas falhas nao reivindicaram.
        assert canal.aberturas == 4
        assert canal.fechamentos == 1
        assert relay.estados == [PRONTO, PRONTO]
        assert heartbeat.read_text() == CONECTANDO
        [linha] = _linhas(banco)
        assert (linha["status"], linha["tentativas"]) == ("entregue", 0)
        assert len(canal.publicador.publicadas) == 1

    def test_conexao_perdida_reconecta_sem_contar_tentativa(
        self, banco: Banco, tmp_path: Path
    ) -> None:
        _gravar_orcamentos(banco)
        parar = threading.Event()
        canal = CanalFalso(parar, erro_ate_a_abertura=1)
        relay = RelayDaOutbox(banco, canal, usuario="billing", relogio=RelogioFixo())

        rodar(relay, canal, parar=parar, heartbeat=tmp_path / "hb", espera_maxima=0.01)

        assert canal.aberturas == 2
        assert canal.fechamentos == 2
        [linha] = _linhas(banco)
        assert (linha["status"], linha["tentativas"]) == ("entregue", 0)

    def test_conexao_bloqueada_pelo_broker_nao_reivindica_ate_desbloquear(
        self, banco: Banco, tmp_path: Path
    ) -> None:
        _gravar_orcamentos(banco)
        parar = threading.Event()
        canal = CanalFalso(parar, bloqueada_nas_esperas=2)
        canal.bloqueada = True
        heartbeat = tmp_path / "relay-heartbeat"
        relay = RelayEspiado(banco, canal, heartbeat)

        rodar(relay, canal, parar=parar, heartbeat=heartbeat)

        # Duas esperas bloqueadas sem pedir lote; desbloqueada, entrega.
        assert len(relay.estados) == 2
        assert canal.esperas == 4
        [linha] = _linhas(banco)
        assert (linha["status"], linha["tentativas"]) == ("entregue", 0)

    def test_banco_fora_nao_derruba_o_laco(self, tmp_path: Path) -> None:
        inalcancavel = _banco_inalcancavel()
        parar = threading.Event()
        canal = CanalFalso(parar)
        relay = RelayDaOutbox(inalcancavel["billing"], canal, usuario="billing")
        try:
            rodar(relay, canal, parar=parar, heartbeat=tmp_path / "hb")
        finally:
            inalcancavel.close()
        assert canal.esperas == 2


class TestMetricas:
    def test_pendentes_em_entrega_e_dead_contados_a_cada_scrape(
        self, banco: Banco
    ) -> None:
        _gravar_orcamentos(banco, 4)
        situacoes = ("pendente", "em_entrega", "dead", "entregue")
        for linha, status in zip(_linhas(banco), situacoes, strict=True):
            banco["outbox"].update_one(
                {"_id": linha["_id"]}, {"$set": {"status": status}}
            )
        amostras = {
            familia.name: familia.samples[0].value
            for familia in ColetorDaOutbox("outbox", banco).collect()
        }
        # Pendente e em entrega ainda nao foram confirmadas pelo broker.
        assert amostras == {"outbox_pendentes": 2, "outbox_dead": 1}

    def test_banco_fora_deixa_os_gauges_sem_dado(self) -> None:
        inalcancavel = _banco_inalcancavel()
        try:
            coletor = ColetorDaOutbox("outbox", inalcancavel["billing"])
            assert list(coletor.collect()) == []
        finally:
            inalcancavel.close()


class TestCasosDeBorda:
    def test_lote_cheio_repete_sem_esperar(
        self, banco: Banco, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(processo_relay, "LOTE", 1)
        _gravar_orcamentos(banco, 2)
        parar = threading.Event()
        canal = CanalFalso(parar)
        heartbeat = tmp_path / "relay-heartbeat"
        relay = RelayEspiado(banco, canal, heartbeat)

        processo_relay.rodar(relay, canal, parar=parar, heartbeat=heartbeat)

        # Dois lotes cheios seguidos sem espera; depois, ocioso, espera.
        assert len(relay.estados) == 4
        assert canal.esperas == 2
        assert {linha["status"] for linha in _linhas(banco)} == {"entregue"}

    def test_sem_banco_para_devolver_o_lease_devolve_sozinho(
        self, banco: Banco, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _gravar_orcamentos(banco)
        relogio = RelogioFixo()
        relay = _relay(banco, PublicadorFalso(StreamLostError("caiu")), relogio)
        outbox = banco["outbox"]

        def banco_fora(*_args: object, **_opcoes: object) -> None:
            raise AutoReconnect("banco fora")

        monkeypatch.setattr(type(outbox), "update_one", banco_fora)
        with pytest.raises(StreamLostError):
            relay.entregar_pendentes(1)
        monkeypatch.undo()

        [presa] = _linhas(banco)
        assert presa["status"] == "em_entrega"
        relogio.avancar(seconds=31)
        outro = PublicadorFalso()
        assert _relay(banco, outro, relogio).entregar_pendentes(1) == 1
        assert outro.ids == [str(presa["_id"])]
