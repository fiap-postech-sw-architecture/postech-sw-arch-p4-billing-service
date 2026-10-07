"""Relay da outbox contra MongoDB real, com publicador falso (ADR-036).

Claim atomico com lease e fencing, publicacao no contexto gravado, tentativas
so para falha da mensagem e laco do processo sem reivindicar sem conexao. O
broker real entra em ``test_mensageria_rabbitmq.py``.
"""

from __future__ import annotations

import json
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
from src.compartilhado.infraestrutura.mensageria.amqp import (
    CanalAmqp,
    MensagemRecusadaError,
)
from src.compartilhado.infraestrutura.mensageria.metricas import ColetorDaOutbox
from src.compartilhado.infraestrutura.mensageria.relay import RelayDaOutbox
from src.compartilhado.infraestrutura.processo import CONECTANDO, PRONTO
from src.compartilhado.infraestrutura.unit_of_work import (
    MensagemRecebida,
    MongoUnitOfWork,
)
from src.orcamento.infraestrutura.repository import MongoOrcamentoRepository
from src.relay import rodar
from tests.factories import orcamento
from tests.integracao.apoio import RelogioFixo

if TYPE_CHECKING:
    from pathlib import Path

    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )
    from pymongo.database import Database

    Banco = Database[dict[str, Any]]

_tracer = trace.get_tracer("teste")


class PublicadorFalso:
    """Guarda cada publicacao; pode segurar a chamada ou falhar depois dela."""

    def __init__(
        self,
        erro: Exception | None = None,
        segura: threading.Event | None = None,
    ) -> None:
        self.publicadas: list[tuple[str, str, bytes, pika.BasicProperties]] = []
        self.erro = erro
        self.segura = segura
        self.entrou = threading.Event()

    def publicar(
        self,
        exchange: str,
        routing_key: str,
        corpo: bytes,
        propriedades: pika.BasicProperties,
    ) -> None:
        self.entrou.set()
        if self.segura is not None:
            assert self.segura.wait(10)
        self.publicadas.append((exchange, routing_key, corpo, propriedades))
        if self.erro is not None:
            raise self.erro

    @property
    def ids(self) -> list[str]:
        return [propriedades.message_id for *_, propriedades in self.publicadas]


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
        uow = MongoUnitOfWork(banco, relogio=RelogioFixo(), mensagem=comando)
        with _tracer.start_as_current_span("process GerarOrcamento") as consumidor:
            uow.executar(lambda: MongoOrcamentoRepository(uow).salvar(orcamento()))
        publicador = PublicadorFalso()

        _relay(banco, publicador, RelogioFixo()).entregar_pendentes(1)

        producer = next(
            s for s in spans.get_finished_spans() if s.kind is SpanKind.PRODUCER
        )
        assert producer.name == "publish OrcamentoGerado"
        assert producer.parent is not None
        assert producer.parent.span_id == consumidor.get_span_context().span_id
        assert producer.context.trace_id == consumidor.get_span_context().trace_id
        [(*_, props)] = publicador.publicadas
        assert props.headers["traceparent"].split("-")[2] == (
            f"{producer.context.span_id:016x}"
        )


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


class TestDoisRelays:
    def test_concorrentes_nunca_publicam_a_mesma_linha(self, banco: Banco) -> None:
        _gravar_orcamentos(banco, 40)
        relogio = RelogioFixo()
        publicadores = [PublicadorFalso(), PublicadorFalso()]
        largada = threading.Barrier(2)

        def entregar(publicador: PublicadorFalso) -> None:
            relay = _relay(banco, publicador, relogio)
            largada.wait(timeout=10)
            relay.entregar_pendentes(100)

        threads = [threading.Thread(target=entregar, args=(p,)) for p in publicadores]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        publicadas = publicadores[0].ids + publicadores[1].ids
        assert len(publicadas) == len(set(publicadas)) == 40
        assert {linha["status"] for linha in _linhas(banco)} == {"entregue"}

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


class CanalFalso(CanalAmqp):
    """``CanalAmqp`` sem broker: falha ao abrir N vezes, pode publicar com erro
    ate uma abertura dada e para o laco na segunda espera."""

    def __init__(
        self,
        parar: threading.Event,
        *,
        falhas_ao_abrir: int = 0,
        erro_ate_a_abertura: int = 0,
    ) -> None:
        self.falhas_ao_abrir = falhas_ao_abrir
        self.erro_ate_a_abertura = erro_ate_a_abertura
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
        if self.esperas >= 2:
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
    def test_pendentes_e_dead_contados_a_cada_scrape(self, banco: Banco) -> None:
        _gravar_orcamentos(banco, 3)
        banco["outbox"].update_one({}, {"$set": {"status": "dead"}})
        amostras = {
            familia.name: familia.samples[0].value
            for familia in ColetorDaOutbox("outbox", banco).collect()
        }
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
