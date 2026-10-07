"""Relay e consumidor contra RabbitMQ 4.3.6 real com a topologia do platform.

As definitions copiadas em ``contratos/rabbitmq/`` sobem com TTL de 100 ms nas
filas de retry; cada teste comeca com as filas vazias e o banco limpo.
"""

from __future__ import annotations

import io
import json
import logging
import re
import struct
import threading
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import httpx
import pika
import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import SpanKind
from pymongo.errors import AutoReconnect

from src import consumidor as processo_consumidor
from src import relay as processo_relay
from src.compartilhado.aplicacao.mensageria import Desfecho
from src.compartilhado.dominio.relogio import agora_utc
from src.compartilhado.infraestrutura.logging import configurar_logging
from src.compartilhado.infraestrutura.mensageria import contratos
from src.compartilhado.infraestrutura.mensageria import processo as boot
from src.compartilhado.infraestrutura.mensageria.amqp import CanalAmqp, parametros
from src.compartilhado.infraestrutura.mensageria.consumidor import (
    EXCHANGE_RETRY,
    ConsumidorDeComandos,
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
    SENHA_DO_ADMIN,
    BrokerDeTeste,
    GatewayRoteirizado,
    comando,
    configuracao,
    definicoes_de_teste,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping
    from pathlib import Path

    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )
    from pymongo.database import Database

    from src.compartilhado.infraestrutura.mensageria.consumidor import Handler
    from src.compartilhado.infraestrutura.unit_of_work import UnidadeDaMensagem

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
    heartbeat: int | None = None,
    bloqueio_maximo: float | None = None,
) -> Iterator[ConsumidorAnotado]:
    """Consumidor e relay do Billing em threads, com o usuario billing."""
    parar = threading.Event()
    handlers = handlers or criar_handlers(
        configuracao().comandos, gateway=GatewayRoteirizado(), relogio=agora_utc
    )
    consumidor = ConsumidorAnotado(banco, handlers, fila=FILA, usuario="billing")
    parametros_do_consumidor = parametros(
        broker.url("billing"), nome="teste-consumidor"
    )
    if heartbeat is not None:
        parametros_do_consumidor.heartbeat = heartbeat
    canal_do_consumidor = CanalAmqp(
        parametros_do_consumidor, filas=(FILA,), exchanges=(EXCHANGE_RETRY,)
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
        parametros_do_relay = parametros(broker.url("billing"), nome="teste-relay")
        if bloqueio_maximo is not None:
            parametros_do_relay.blocked_connection_timeout = bloqueio_maximo
        canal_do_relay = CanalAmqp(
            parametros_do_relay, exchanges=(contratos.EXCHANGE_EVENTOS,)
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
        consumidor.fechar()


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

    def __call__(self, dados: Mapping[str, Any], uow: UnidadeDaMensagem) -> Desfecho:
        self.chamadas += 1
        if self.chamadas <= self.vezes:
            msg = "banco fora"
            raise AutoReconnect(msg)
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


class HandlerLento:
    """Handler que demora mais que o heartbeat negociado da conexao."""

    def __init__(self, segundos: float) -> None:
        self.segundos = segundos
        self.chamadas = 0

    def __call__(self, dados: Mapping[str, Any], uow: UnidadeDaMensagem) -> Desfecho:
        self.chamadas += 1
        time.sleep(self.segundos)
        return Desfecho.PROCESSADA


def test_handler_mais_lento_que_o_heartbeat_nao_derruba_a_conexao(
    banco: Banco,
    broker: BrokerDeTeste,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    handler = HandlerLento(segundos=6)
    with (
        caplog.at_level(logging.WARNING),
        processos(
            banco,
            broker,
            tmp_path,
            handlers={"CancelarOrcamento": handler},
            relay=False,
            heartbeat=2,
        ),
    ):
        publicar_comando(broker, _cancelar())
        esperar(lambda: banco["mensagens_processadas"].count_documents({}) == 1)

    # A thread da conexao atendeu o broker enquanto o handler rodava: nenhuma
    # queda, uma execucao so e o ack dado (nada voltou para a fila).
    assert handler.chamadas == 1
    assert "broker_connection_lost" not in [r.getMessage() for r in caplog.records]
    with broker.conectar() as conexao:
        fila = conexao.channel().queue_declare(FILA, passive=True)
    assert fila.method.message_count == 0


def _propriedades_com_timestamp_em_ms(envelope: dict[str, Any]) -> Any:
    """Header ``timestamp`` do tipo AMQP T com epoch em milissegundos, como
    alguns clientes publicam. O decoder do pika do consumidor falha nele (o
    ano passa de 9999) e derruba a conexao a cada entrega; o encoder do pika
    nao o gera, entao a tabela dos headers sai codificada aqui."""
    propriedades = pika.BasicProperties(
        message_id=envelope["id"],
        correlation_id=envelope["correlation_id"],
        type=envelope["tipo"],
        user_id="os",
        delivery_mode=2,
        headers={},
    )
    codificar = propriedades.encode

    def encode() -> list[bytes]:
        pecas: list[bytes] = codificar()
        chave = b"timestamp"
        valor = struct.pack(">cQ", b"T", 1_760_000_000_000)
        entrada = bytes([len(chave)]) + chave + valor
        # Sem content_type nem content_encoding, a tabela vem logo depois das
        # flags (uma peca so, vazia).
        assert pecas[1] == struct.pack(">I", 0)
        pecas[1] = struct.pack(">I", len(entrada)) + entrada
        return pecas

    propriedades.encode = encode
    return propriedades


def _publicar_com_header_ilegivel(
    broker: BrokerDeTeste, envelope: dict[str, Any]
) -> None:
    with broker.conectar("os") as conexao:
        canal = conexao.channel()
        canal.confirm_delivery()
        canal.basic_publish(
            "pytstop.comandos",
            _routing_key(envelope["tipo"]),
            json.dumps(envelope).encode(),
            _propriedades_com_timestamp_em_ms(envelope),
            mandatory=True,
        )


def _tirar_pela_api(broker: BrokerDeTeste, fila: str) -> dict[str, Any]:
    """Tira a mensagem da fila pela API de gerenciamento: o pika do teste
    tambem falharia no header."""
    with httpx.Client(auth=("admin", SENHA_DO_ADMIN), timeout=10) as http:
        limite = time.monotonic() + PRAZO_SEGUNDOS
        while time.monotonic() < limite:
            resposta = http.post(
                f"{broker.api}/queues/%2F/{fila}/get",
                json={"count": 1, "ackmode": "ack_requeue_false", "encoding": "auto"},
            )
            resposta.raise_for_status()
            if mensagens := resposta.json():
                mensagem: dict[str, Any] = mensagens[0]
                return mensagem
            time.sleep(0.05)
    msg = f"nenhuma mensagem em {fila}"
    raise AssertionError(msg)


def test_header_ilegivel_vai_para_a_dlq_sem_levar_a_mensagem_de_tras(
    banco: Banco, broker: BrokerDeTeste, tmp_path: Path
) -> None:
    semear(banco)
    venenosa, valida = _gerar(), _gerar()
    # As duas ja na fila quando o consumidor sobe: com prefetch maior que 1 elas
    # sairiam no mesmo lote, voltariam juntas a cada queda e iriam juntas para
    # a DLQ no limite de entregas.
    _publicar_com_header_ilegivel(broker, venenosa)
    publicar_comando(broker, valida)
    with processos(banco, broker, tmp_path):
        _, evento = esperar_mensagem(broker, "os.eventos")
        morta = _tirar_pela_api(broker, "billing.comandos.dlq")

    # Prefetch 1: so a venenosa voltava a fila a cada queda da conexao, e o
    # limite de entregas da topologia a tirou de la sem tocar na valida.
    assert evento["causation_id"] == valida["id"]
    assert json.loads(morta["payload"])["id"] == venenosa["id"]
    assert morta["properties"]["headers"]["x-first-death-reason"] == "delivery_limit"
    assert banco["mensagens_processadas"].count_documents({}) == 1


class Defeito:
    """Handler com uma excecao que ninguem classificou (defeito do codigo)."""

    def __init__(self) -> None:
        self.chamadas = 0

    def __call__(self, dados: Mapping[str, Any], uow: UnidadeDaMensagem) -> Desfecho:
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


@contextmanager
def retry_negado(broker: BrokerDeTeste) -> Iterator[None]:
    """Permissao de topico do billing no pytstop.retry como na topologia antiga
    (so a chave ``billing.comandos``): o broker recusa toda copia de retry."""
    url = f"{broker.api}/topic-permissions/%2F/billing"
    with httpx.Client(auth=("admin", SENHA_DO_ADMIN), timeout=10) as http:
        http.put(
            url,
            json={
                "exchange": "pytstop.retry",
                "write": "^billing\\.comandos\\z",
                "read": "^\\z",
            },
        ).raise_for_status()
        try:
            yield
        finally:
            [original] = [
                permissao
                for permissao in definicoes_de_teste()["topic_permissions"]
                if (permissao["user"], permissao["exchange"])
                == ("billing", "pytstop.retry")
            ]
            http.put(
                url,
                json={
                    chave: original[chave] for chave in ("exchange", "write", "read")
                },
            ).raise_for_status()


def test_copia_de_retry_recusada_leva_a_original_direto_para_a_dlq(
    banco: Banco, broker: BrokerDeTeste, tmp_path: Path
) -> None:
    handler = FalhaTransitoria(vezes=99)
    mensagem = _cancelar()
    with (
        retry_negado(broker),
        processos(
            banco,
            broker,
            tmp_path,
            handlers={"CancelarOrcamento": handler},
            relay=False,
        ),
    ):
        publicar_comando(broker, mensagem)
        props, morta = esperar_mensagem(broker, "billing.comandos.dlq")

    # O canal de consumo segue aberto: a original vai para a DLQ na hora, sem
    # voltar para a fila em laco ate o limite de entregas.
    assert morta == mensagem
    assert "x-tentativa" not in (props.headers or {})
    assert handler.chamadas == 1


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

    def test_devolucao_do_broker_nao_poe_o_corpo_no_log(
        self, banco: Banco, broker: BrokerDeTeste
    ) -> None:
        _gravar_evento(banco)
        canal = CanalAmqp(
            parametros(broker.url("billing"), nome="teste-devolucao"),
            exchanges=(contratos.EXCHANGE_EVENTOS,),
        )
        saida = io.StringIO()
        configurar_logging(saida)
        try:
            with broker.conectar() as admin:
                ligacao = ("os.eventos", "pytstop.eventos", "evento.billing.#")
                admin.channel().queue_unbind(*ligacao)
                try:
                    canal.abrir()
                    RelayDaOutbox(banco, canal, usuario="billing").entregar_pendentes(1)
                finally:
                    canal.fechar()
                    admin.channel().queue_bind(*ligacao)
        finally:
            logging.getLogger().handlers.clear()

        # O pika loga a mensagem devolvida em WARNING com o comeco do corpo; o
        # logger dele so deixa passar erro.
        log = saida.getvalue()
        assert "outbox_message_refused" in log
        assert "body_prefix" not in log
        assert "ocorrido_em" not in log

    def test_routing_key_sem_permissao_conta_tentativa_e_segue_na_mesma_conexao(
        self, banco: Banco, broker: BrokerDeTeste
    ) -> None:
        _gravar_evento(banco)
        _gravar_evento(banco)
        # A primeira linha vai para outro servico: a permissao de topico do
        # billing recusa (403) e o broker fecha o canal de publicacao.
        primeira = banco["outbox"].find_one(sort=[("_id", 1)])
        assert primeira is not None
        banco["outbox"].update_one(
            {"_id": primeira["_id"]}, {"$set": {"routing_key": "evento.os.alheio"}}
        )
        canal = CanalAmqp(
            parametros(broker.url("billing"), nome="teste-sem-permissao"),
            exchanges=(contratos.EXCHANGE_EVENTOS,),
        )
        canal.abrir()
        try:
            # A segunda sai num canal de publicacao reaberto, sem reconectar.
            assert (
                RelayDaOutbox(banco, canal, usuario="billing").entregar_pendentes(2)
                == 2
            )
        finally:
            canal.fechar()

        recusada, entregue = banco["outbox"].find().sort("_id")
        assert (recusada["status"], recusada["tentativas"]) == ("pendente", 1)
        assert recusada["ultimo_erro"] == "canal fechado pelo broker (403)"
        assert (entregue["status"], entregue["tentativas"]) == ("entregue", 0)

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


def _mensagens(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [registro.getMessage() for registro in caplog.records]


@contextmanager
def alarme_de_memoria(broker: BrokerDeTeste) -> Iterator[None]:
    """Alarme de memoria do broker: toda conexao que publica fica bloqueada."""
    codigo, saida = broker.container.exec(
        ["rabbitmqctl", "set_vm_memory_high_watermark", "0"]
    )
    assert codigo == 0, saida
    try:
        yield
    finally:
        codigo, saida = broker.container.exec(
            ["rabbitmqctl", "set_vm_memory_high_watermark", "0.6"]
        )
        assert codigo == 0, saida


def test_alarme_do_broker_nao_gasta_tentativa_nem_perde_o_evento(
    banco: Banco,
    broker: BrokerDeTeste,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with (
        caplog.at_level(logging.INFO),
        processos(banco, broker, tmp_path, bloqueio_maximo=1),
    ):
        esperar(lambda: _pronto(tmp_path / "relay"))
        with alarme_de_memoria(broker):
            _gravar_evento(banco)
            # O broker bloqueia o relay quando ele publica; o publish preso cai
            # no teto do bloqueio, a conexao cai e a linha volta sem contar
            # tentativa, a cada reconexao, ate o alarme passar.
            esperar(lambda: _mensagens(caplog).count("broker_connection_lost") >= 2)
            [bloqueada] = banco["outbox"].find()
            assert bloqueada["tentativas"] == 0
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
        monkeypatch.setattr(boot, "start_http_server", servidor_de_metricas)
        monkeypatch.setattr(
            boot, "configurar_telemetria", lambda _processo: TracerProvider()
        )
        # SIGTERM de mentira: o Event que o handler do sinal acionaria.
        monkeypatch.setattr(boot, "instalar_sinais", sinais.append)
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
        assert portas == [9100, 9100]
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
        monkeypatch.setattr(
            boot, "start_http_server", lambda _porta: (ServidorFalso(), None)
        )
        monkeypatch.setattr(
            boot, "configurar_telemetria", lambda _processo: TracerProvider()
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
