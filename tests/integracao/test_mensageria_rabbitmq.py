"""Relay e consumidor contra RabbitMQ 4.3.6 real com a topologia do platform.

As definitions copiadas em ``rabbitmq/`` sobem com TTL de 100 ms nas filas de
retry; cada teste comeca com as filas vazias e o banco limpo.
"""

from __future__ import annotations

import json
import re
import threading
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import pika
import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import SpanKind
from pymongo.errors import AutoReconnect

from src import consumidor as processo_consumidor
from src import relay as processo_relay
from src.compartilhado.dominio.relogio import agora_utc
from src.compartilhado.infraestrutura.mensageria import contratos
from src.compartilhado.infraestrutura.mensageria.amqp import CanalAmqp, parametros
from src.compartilhado.infraestrutura.mensageria.consumidor import (
    EXCHANGE_RETRY,
    ConsumidorDeComandos,
    Desfecho,
)
from src.compartilhado.infraestrutura.mensageria.relay import RelayDaOutbox
from src.compartilhado.infraestrutura.mensageria.telemetria import contexto_atual
from src.compartilhado.infraestrutura.mongo import marcar_versao
from src.compartilhado.infraestrutura.unit_of_work import MongoUnitOfWork
from src.consumidor import FILA, criar_handlers
from src.consumidor import rodar as rodar_consumidor
from src.orcamento.infraestrutura.repository import MongoOrcamentoRepository
from src.relay import rodar as rodar_relay
from src.seed import semear
from tests.factories import orcamento
from tests.integracao.apoio import (
    SEGREDO_LINK,
    BrokerDeTeste,
    GatewayRoteirizado,
    comando,
    configuracao,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping
    from pathlib import Path

    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )
    from pymongo.database import Database

    from src.compartilhado.infraestrutura.mensageria.consumidor import Handler

    Banco = Database[dict[str, Any]]

_tracer = trace.get_tracer("teste")
PRAZO_SEGUNDOS = 20


def _routing_key(tipo: str) -> str:
    return "comando.billing." + re.sub(r"(?<!^)(?=[A-Z])", "_", tipo).lower()


def publicar_comando(
    broker: BrokerDeTeste,
    envelope: dict[str, Any],
    *,
    usuario: str = "os",
    cabecalhos: dict[str, Any] | None = None,
) -> None:
    """Publica como o OS (ou outro usuario), com confirm, em pytstop.comandos."""
    with broker.conectar(usuario) as conexao:
        canal = conexao.channel()
        canal.confirm_delivery()
        canal.basic_publish(
            "pytstop.comandos",
            _routing_key(envelope["tipo"]),
            json.dumps(envelope).encode(),
            pika.BasicProperties(
                message_id=envelope["id"],
                correlation_id=envelope["correlation_id"],
                type=envelope["tipo"],
                user_id=usuario,
                content_type="application/json",
                delivery_mode=2,
                headers=cabecalhos or {},
            ),
            mandatory=True,
        )


def esperar_mensagem(
    broker: BrokerDeTeste, fila: str, *, usuario: str = "admin"
) -> tuple[pika.BasicProperties, dict[str, Any]]:
    with broker.conectar(usuario) as conexao:
        canal = conexao.channel()
        limite = time.monotonic() + PRAZO_SEGUNDOS
        while time.monotonic() < limite:
            metodo, propriedades, corpo = canal.basic_get(fila, auto_ack=True)
            if metodo is not None:
                return propriedades, json.loads(corpo)
            time.sleep(0.05)
    msg = f"nenhuma mensagem em {fila}"
    raise AssertionError(msg)


def esperar(condicao: Callable[[], bool]) -> None:
    limite = time.monotonic() + PRAZO_SEGUNDOS
    while not condicao():
        assert time.monotonic() < limite, "condicao nao aconteceu no prazo"
        time.sleep(0.05)


class ConsumidorAnotado(ConsumidorDeComandos):
    """Anota os cabecalhos de cada entrega (x-tentativa e x-death)."""

    def __init__(self, *args: Any, **opcoes: Any) -> None:
        super().__init__(*args, **opcoes)
        self.entregas: list[dict[str, Any]] = []

    def tratar(self, canal: Any, tag: int, propriedades: Any, corpo: bytes) -> str:
        self.entregas.append(dict(propriedades.headers or {}))
        return super().tratar(canal, tag, propriedades, corpo)


@contextmanager
def processos(
    banco: Banco,
    broker: BrokerDeTeste,
    tmp_path: Path,
    *,
    handlers: Mapping[str, Handler] | None = None,
    relay: bool = True,
) -> Iterator[ConsumidorAnotado]:
    """Consumidor e relay do Billing em threads, com o usuario billing."""
    parar = threading.Event()
    handlers = handlers or criar_handlers(
        configuracao().comandos, gateway=GatewayRoteirizado(), relogio=agora_utc
    )
    consumidor = ConsumidorAnotado(banco, handlers, fila=FILA, usuario="billing")
    canal_do_consumidor = CanalAmqp(
        parametros(broker.url("billing"), nome="teste-consumidor"),
        filas=(FILA,),
        exchanges=(EXCHANGE_RETRY,),
    )
    threads = [
        threading.Thread(
            target=rodar_consumidor,
            args=(consumidor, canal_do_consumidor),
            kwargs={
                "parar": parar,
                "heartbeat": tmp_path / "consumidor",
                "inatividade": 0.05,
                "espera_maxima": 0.2,
            },
        )
    ]
    if relay:
        canal_do_relay = CanalAmqp(
            parametros(broker.url("billing"), nome="teste-relay"),
            exchanges=(contratos.EXCHANGE_EVENTOS,),
        )
        threads.append(
            threading.Thread(
                target=rodar_relay,
                args=(
                    RelayDaOutbox(banco, canal_do_relay, usuario="billing"),
                    canal_do_relay,
                ),
                kwargs={
                    "parar": parar,
                    "heartbeat": tmp_path / "relay",
                    "intervalo": 0.05,
                    "espera_maxima": 0.2,
                },
            )
        )
    for thread in threads:
        thread.start()
    try:
        esperar(lambda: _pronto(tmp_path / "consumidor"))
        yield consumidor
    finally:
        parar.set()
        for thread in threads:
            thread.join(timeout=10)
            assert not thread.is_alive()


def _pronto(arquivo: Path) -> bool:
    return arquivo.exists() and arquivo.read_text() == "pronto"


def _gerar(itens: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    dados = {
        "ordem_id": str(uuid4()),
        "itens": (
            [{"tipo": "servico", "codigo": "SRV-TROCA-OLEO", "quantidade": 1}]
            if itens is None
            else itens
        ),
    }
    return comando("GerarOrcamento", dados)


class TestPontaAPonta:
    def test_comando_vira_evento_validado_com_o_trace_encadeado(
        self,
        banco: Banco,
        broker: BrokerDeTeste,
        tmp_path: Path,
        spans: InMemorySpanExporter,
    ) -> None:
        semear(banco)
        mensagem = _gerar()
        with _tracer.start_as_current_span(
            "publish GerarOrcamento", kind=SpanKind.PRODUCER
        ) as publicacao:
            cabecalhos = contexto_atual()

        with processos(banco, broker, tmp_path):
            publicar_comando(broker, mensagem, cabecalhos=cabecalhos)
            props, evento = esperar_mensagem(broker, "os.eventos", usuario="os")

        # Contrato: o envelope e o dados que o OS vai validar.
        contratos.validar(evento)
        assert evento["tipo"] == "OrcamentoGerado"
        assert evento["causation_id"] == mensagem["id"]
        assert evento["correlation_id"] == mensagem["correlation_id"]
        assert (props.user_id, props.type) == ("billing", "OrcamentoGerado")
        assert (props.message_id, props.correlation_id) == (
            evento["id"],
            mensagem["correlation_id"],
        )
        # Trace: publicacao do OS -> consumo no Billing -> publicacao do evento.
        terminados = spans.get_finished_spans()
        consumo = next(s for s in terminados if s.kind is SpanKind.CONSUMER)
        envio = next(s for s in terminados if s.name == "publish OrcamentoGerado")
        assert consumo.parent is not None
        assert consumo.parent.span_id == publicacao.get_span_context().span_id
        assert envio.parent is not None
        assert envio.parent.span_id == consumo.context.span_id
        _, trace_id, span_id, _ = props.headers["traceparent"].split("-")
        assert int(trace_id, 16) == publicacao.get_span_context().trace_id
        assert int(span_id, 16) == envio.context.span_id
        [linha] = banco["outbox"].find()
        assert linha["status"] == "entregue"

    def test_reentrega_do_mesmo_id_nao_repete_o_efeito(
        self, banco: Banco, broker: BrokerDeTeste, tmp_path: Path
    ) -> None:
        semear(banco)
        mensagem = _gerar()
        with processos(banco, broker, tmp_path):
            publicar_comando(broker, mensagem)
            publicar_comando(broker, mensagem)
            primeiro = esperar_mensagem(broker, "os.eventos")[1]
            segundo = esperar_mensagem(broker, "os.eventos")[1]

        assert banco["orcamentos"].count_documents({}) == 1
        assert banco["mensagens_processadas"].count_documents({}) == 1
        assert [primeiro["tipo"], segundo["tipo"]] == ["OrcamentoGerado"] * 2
        assert primeiro["dados"]["orcamento_id"] == segundo["dados"]["orcamento_id"]


class FalhaTransitoria:
    """Handler que falha ``vezes`` com o banco fora e depois processa."""

    def __init__(self, vezes: int) -> None:
        self.vezes = vezes
        self.chamadas = 0

    def __call__(self, dados: Mapping[str, Any], uow: MongoUnitOfWork) -> Desfecho:
        self.chamadas += 1
        if self.chamadas <= self.vezes:
            msg = "banco fora"
            raise AutoReconnect(msg)
        uow.concluir_mensagem()
        return Desfecho.PROCESSADA


def _cancelar() -> dict[str, Any]:
    return comando(
        "CancelarOrcamento", {"ordem_id": str(uuid4()), "motivo": "cancelamento"}
    )


def _filas_de_retry(entrega: Mapping[str, Any]) -> set[str]:
    return {morte["queue"] for morte in entrega.get("x-death", [])}


class TestRetryEDlq:
    def test_transitorio_passa_pela_fila_de_cada_nivel_e_volta(
        self, banco: Banco, broker: BrokerDeTeste, tmp_path: Path
    ) -> None:
        handler = FalhaTransitoria(vezes=2)
        mensagem = _cancelar()
        with processos(
            banco,
            broker,
            tmp_path,
            handlers={"CancelarOrcamento": handler},
            relay=False,
        ) as consumidor:
            publicar_comando(broker, mensagem)
            esperar(lambda: handler.chamadas == 3)

        primeira, segunda, terceira = consumidor.entregas
        assert "x-tentativa" not in primeira
        assert segunda["x-tentativa"] == 1
        assert "billing.comandos.retry.1s" in _filas_de_retry(segunda)
        assert terceira["x-tentativa"] == 2
        assert "billing.comandos.retry.5s" in _filas_de_retry(terceira)
        assert banco["mensagens_processadas"].find_one({"_id": UUID(mensagem["id"])})

    def test_sexta_falha_vai_para_a_dlq(
        self, banco: Banco, broker: BrokerDeTeste, tmp_path: Path
    ) -> None:
        handler = FalhaTransitoria(vezes=99)
        mensagem = _cancelar()
        with processos(
            banco,
            broker,
            tmp_path,
            handlers={"CancelarOrcamento": handler},
            relay=False,
        ):
            publicar_comando(broker, mensagem)
            props, morta = esperar_mensagem(broker, "billing.comandos.dlq")

        assert morta == mensagem
        assert props.headers["x-tentativa"] == 5
        assert handler.chamadas == 6

    @pytest.mark.parametrize(
        ("usuario", "itens"),
        [
            ("os", []),
            (
                "admin",
                [{"tipo": "servico", "codigo": "SRV-TROCA-OLEO", "quantidade": 1}],
            ),
        ],
        ids=["fora-do-contrato", "user-id-de-outro-produtor"],
    )
    def test_erro_permanente_vai_direto_para_a_dlq(
        self,
        banco: Banco,
        broker: BrokerDeTeste,
        tmp_path: Path,
        usuario: str,
        itens: list[dict[str, Any]],
    ) -> None:
        mensagem = _gerar(itens=itens)
        handler = FalhaTransitoria(vezes=0)
        with processos(
            banco, broker, tmp_path, handlers={"GerarOrcamento": handler}, relay=False
        ):
            publicar_comando(broker, mensagem, usuario=usuario)
            props, morta = esperar_mensagem(broker, "billing.comandos.dlq")

        assert morta["id"] == mensagem["id"]
        assert "x-tentativa" not in (props.headers or {})
        assert handler.chamadas == 0


class Defeito:
    """Handler com uma excecao que ninguem classificou (defeito do codigo)."""

    def __init__(self) -> None:
        self.chamadas = 0

    def __call__(self, dados: Mapping[str, Any], uow: MongoUnitOfWork) -> Desfecho:
        self.chamadas += 1
        raise KeyError(dados["ordem_id"])


def test_excecao_nao_classificada_vai_direto_para_a_dlq(
    banco: Banco, broker: BrokerDeTeste, tmp_path: Path
) -> None:
    handler = Defeito()
    mensagem = _cancelar()
    with processos(
        banco, broker, tmp_path, handlers={"CancelarOrcamento": handler}, relay=False
    ):
        publicar_comando(broker, mensagem)
        props, morta = esperar_mensagem(broker, "billing.comandos.dlq")

    assert morta == mensagem
    # Direto: uma chamada, sem copia de retry (sem x-tentativa) e sem ack.
    assert "x-tentativa" not in (props.headers or {})
    assert handler.chamadas == 1
    assert banco["mensagens_processadas"].count_documents({}) == 0


def _gravar_evento(banco: Banco) -> None:
    uow = MongoUnitOfWork(banco)
    uow.executar(lambda: MongoOrcamentoRepository(uow).salvar(orcamento()))


class TestRelayNoBroker:
    def test_mensagem_sem_rota_conta_tentativa_e_nao_vira_entregue(
        self, banco: Banco, broker: BrokerDeTeste
    ) -> None:
        _gravar_evento(banco)
        canal = CanalAmqp(
            parametros(broker.url("billing"), nome="teste-sem-rota"),
            exchanges=(contratos.EXCHANGE_EVENTOS,),
        )
        with broker.conectar() as admin:
            ligacao = ("os.eventos", "pytstop.eventos", "evento.billing.#")
            admin.channel().queue_unbind(*ligacao)
            try:
                canal.abrir()
                RelayDaOutbox(banco, canal, usuario="billing").entregar_pendentes(1)
            finally:
                canal.fechar()
                admin.channel().queue_bind(*ligacao)

        [linha] = banco["outbox"].find()
        assert (linha["status"], linha["tentativas"]) == ("pendente", 1)
        assert linha["ultimo_erro"] == "UnroutableError"

    def test_routing_key_sem_permissao_conta_tentativa_e_reconecta(
        self, banco: Banco, broker: BrokerDeTeste
    ) -> None:
        _gravar_evento(banco)
        # Outro servico: a permissao de topico do billing recusa (403).
        banco["outbox"].update_one({}, {"$set": {"routing_key": "evento.os.alheio"}})
        canal = CanalAmqp(
            parametros(broker.url("billing"), nome="teste-sem-permissao"),
            exchanges=(contratos.EXCHANGE_EVENTOS,),
        )
        canal.abrir()
        try:
            RelayDaOutbox(banco, canal, usuario="billing").entregar_pendentes(1)
        finally:
            canal.fechar()

        [linha] = banco["outbox"].find()
        assert (linha["status"], linha["tentativas"]) == ("pendente", 1)
        assert linha["ultimo_erro"] == "canal fechado pelo broker (403)"

    def test_broker_parado_nao_gasta_tentativa_e_entrega_na_volta(
        self, banco: Banco, broker: BrokerDeTeste, tmp_path: Path
    ) -> None:
        with processos(banco, broker, tmp_path):
            esperar(lambda: _pronto(tmp_path / "relay"))
            codigo, saida = broker.container.exec(["rabbitmqctl", "stop_app"])
            assert codigo == 0, saida
            try:
                esperar(lambda: (tmp_path / "relay").read_text() == "conectando")
                _gravar_evento(banco)
                time.sleep(1)  # o relay tenta reconectar e nao reivindica nada
                [parada] = banco["outbox"].find()
                assert (parada["status"], parada["tentativas"]) == ("pendente", 0)
            finally:
                codigo, saida = broker.container.exec(["rabbitmqctl", "start_app"])
                assert codigo == 0, saida
            _, evento = esperar_mensagem(broker, "os.eventos")

        [entregue] = banco["outbox"].find()
        assert (entregue["status"], entregue["tentativas"]) == ("entregue", 0)
        assert evento["id"] == str(entregue["_id"])


class TestProcessos:
    def test_relay_e_consumidor_sobem_pelo_main_e_param_no_sinal(
        self,
        banco: Banco,
        broker: BrokerDeTeste,
        mongo_uri: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        semear(banco)
        marcar_versao(banco)  # a limpeza do banco a cada teste leva a marca junto
        ambiente = {
            "ENVIRONMENT": "test",
            # O consumidor sobe com o Mercado Pago real (GerarOrcamento nao o usa).
            "MP_MODE": "mercadopago",
            "MP_ACCESS_TOKEN": "TEST-token-de-teste",  # gitleaks:allow
            "MONGODB_URI": mongo_uri,
            "MONGODB_DB": banco.name,
            "RABBITMQ_URL": broker.url("billing"),
            "RELAY_HEARTBEAT": str(tmp_path / "relay"),
            "CONSUMIDOR_HEARTBEAT": str(tmp_path / "consumidor"),
            "BILLING_PUBLIC_URL": "http://billing.teste",
            "ORCAMENTO_LINK_SECRET": SEGREDO_LINK,
        }
        for nome, valor in ambiente.items():
            monkeypatch.setenv(nome, valor)
        portas: list[int] = []

        def servidor_de_metricas(porta: int) -> tuple[ServidorFalso, None]:
            portas.append(porta)
            return ServidorFalso(), None

        sinais: list[threading.Event] = []
        for modulo in (processo_relay, processo_consumidor):
            monkeypatch.setattr(modulo, "start_http_server", servidor_de_metricas)
            monkeypatch.setattr(
                modulo, "configurar_telemetria", lambda _processo: TracerProvider()
            )
            # SIGTERM de mentira: o Event que o handler do sinal acionaria.
            monkeypatch.setattr(modulo, "instalar_sinais", sinais.append)
        threads = [
            threading.Thread(target=processo.main)
            for processo in (processo_relay, processo_consumidor)
        ]
        for thread in threads:
            thread.start()
        try:
            esperar(lambda: _pronto(tmp_path / "relay"))
            esperar(lambda: _pronto(tmp_path / "consumidor"))
            publicar_comando(broker, _gerar())
            _, evento = esperar_mensagem(broker, "os.eventos")
        finally:
            for sinal in sinais:
                sinal.set()
            for thread in threads:
                thread.join(timeout=10)

        assert evento["tipo"] == "OrcamentoGerado"
        assert not any(thread.is_alive() for thread in threads)
        assert portas == [8000, 8000]
        assert len(sinais) == 2
        assert (tmp_path / "relay").read_text() == "conectando"

    def test_parada_pedida_antes_de_conectar_fecha_tudo_sem_consumir(
        self,
        banco: Banco,
        broker: BrokerDeTeste,
        mongo_uri: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        marcar_versao(banco)
        ambiente = {
            "ENVIRONMENT": "test",
            "MP_MODE": "simulado",
            "MONGODB_URI": mongo_uri,
            "MONGODB_DB": banco.name,
            "RABBITMQ_URL": broker.url("billing"),
            "RELAY_HEARTBEAT": str(tmp_path / "relay"),
            "CONSUMIDOR_HEARTBEAT": str(tmp_path / "consumidor"),
        }
        for nome, valor in ambiente.items():
            monkeypatch.setenv(nome, valor)
        for modulo in (processo_relay, processo_consumidor):
            monkeypatch.setattr(
                modulo, "start_http_server", lambda _porta: (ServidorFalso(), None)
            )
            monkeypatch.setattr(
                modulo, "configurar_telemetria", lambda _processo: TracerProvider()
            )
        parar = threading.Event()
        parar.set()

        processo_relay.main(parar)
        processo_consumidor.main(parar)

        assert (tmp_path / "relay").read_text() == "conectando"
        assert (tmp_path / "consumidor").read_text() == "conectando"


class ServidorFalso:
    def shutdown(self) -> None:
        return None
