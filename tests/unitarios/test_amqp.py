"""Laco de conexao do relay e do consumidor: backoff com jitter mesmo depois de
conectar, e o arquivo de vida sem ``pronto`` fora da conexao."""

from __future__ import annotations

import errno
import logging
import socket
import threading
from typing import TYPE_CHECKING

import pika
import pytest
from pika.exceptions import (
    AMQPConnectionError,
    ChannelWrongStateError,
    NackError,
    StreamLostError,
)

from src.compartilhado.infraestrutura.mensageria import amqp
from src.compartilhado.infraestrutura.mensageria.amqp import CanalAmqp, manter_conectado
from src.compartilhado.infraestrutura.processo import sinalizar

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from tests.conftest import DnsDeTeste


class EsperaAnotada(threading.Event):
    """``parar`` que anota cada espera (e o arquivo de vida nela) e pede a
    parada na de numero ``ate``."""

    def __init__(self, ate: int, arquivo_de_vida: Path | None = None) -> None:
        super().__init__()
        self.esperas: list[float | None] = []
        self.estados_na_espera: list[str] = []
        self.ate = ate
        self.arquivo_de_vida = arquivo_de_vida

    def wait(self, timeout: float | None = None) -> bool:
        self.esperas.append(timeout)
        if self.arquivo_de_vida is not None:
            self.estados_na_espera.append(self.arquivo_de_vida.read_text())
        if len(self.esperas) >= self.ate:
            self.set()
        return self.is_set()


class SorteioAnotado:
    """Jitter deterministico: anota a faixa pedida e devolve o teto dela."""

    def __init__(self) -> None:
        self.faixas: list[tuple[float, float]] = []

    def uniform(self, inicio: float, fim: float) -> float:
        self.faixas.append((inicio, fim))
        return fim


@pytest.fixture
def sorteio(monkeypatch: pytest.MonkeyPatch) -> SorteioAnotado:
    anotado = SorteioAnotado()
    monkeypatch.setattr(amqp, "_aleatorio", anotado)
    return anotado


class CanalSemBroker(CanalAmqp):
    """Abre sempre (ou falha se mandado), sem conexao de verdade."""

    def __init__(self, falha_ao_abrir: bool = False) -> None:
        super().__init__(pika.ConnectionParameters())
        self.falha_ao_abrir = falha_ao_abrir
        self.aberturas = 0
        self.fechamentos = 0

    def abrir(self) -> None:
        self.aberturas += 1
        if self.falha_ao_abrir:
            raise AMQPConnectionError("broker fora")

    def fechar(self) -> None:
        self.fechamentos += 1


class CanalRoteirizado(CanalSemBroker):
    """Cada abertura segue o roteiro: ``None`` conecta, um erro e levantado."""

    def __init__(self, roteiro: list[Exception | None]) -> None:
        super().__init__()
        self.roteiro = roteiro

    def abrir(self) -> None:
        self.aberturas += 1
        erro = self.roteiro.pop(0)
        if erro is not None:
            raise erro


def _rodar(
    canal: CanalAmqp,
    trabalho: Callable[[], None],
    parar: EsperaAnotada,
    heartbeat: Path,
    cronometro: Callable[[], float] = lambda: 0.0,
) -> None:
    manter_conectado(
        canal,
        trabalho,
        parar=parar,
        heartbeat=heartbeat,
        processo="consumidor",
        cronometro=cronometro,
    )


def _canal_fechado_pelo_broker() -> None:
    msg = "canal fechado pelo broker"
    raise ChannelWrongStateError(msg)


def test_canal_fechado_logo_depois_de_abrir_reconecta_com_backoff(
    tmp_path: Path, sorteio: SorteioAnotado
) -> None:
    parar = EsperaAnotada(ate=6)
    canal = CanalSemBroker()

    _rodar(canal, _canal_fechado_pelo_broker, parar, tmp_path / "hb")

    # Nunca reconexao imediata em laco: 1, 2, 4, 8, 16 s e o teto de 30 s, cada
    # uma sorteada entre a metade e o total.
    assert parar.esperas == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0]
    assert sorteio.faixas == [(0.5, 1.0)] * 6
    assert canal.aberturas == canal.fechamentos == 6


def test_espera_sorteada_fica_entre_a_metade_e_o_atraso(tmp_path: Path) -> None:
    parar = EsperaAnotada(ate=6)

    _rodar(CanalSemBroker(), _canal_fechado_pelo_broker, parar, tmp_path / "hb")

    atrasos = [1.0, 2.0, 4.0, 8.0, 16.0, 30.0]
    for espera, atraso in zip(parar.esperas, atrasos, strict=True):
        assert espera is not None
        assert atraso / 2 <= espera <= atraso
    # Replicas que caem juntas nao voltam no mesmo instante.
    assert parar.esperas != atrasos


def test_arquivo_de_vida_nao_diz_pronto_durante_a_espera(
    tmp_path: Path, sorteio: SorteioAnotado
) -> None:
    arquivo = tmp_path / "hb"
    parar = EsperaAnotada(ate=3, arquivo_de_vida=arquivo)

    def pronto_e_cai() -> None:
        # O laco real marca pronto a cada volta conectado.
        sinalizar(arquivo, pronto=True)
        _canal_fechado_pelo_broker()

    _rodar(CanalSemBroker(), pronto_e_cai, parar, arquivo)

    assert parar.estados_na_espera == ["conectando"] * 3


def test_arquivo_de_vida_diz_conectando_com_o_broker_fora_desde_o_boot(
    tmp_path: Path, sorteio: SorteioAnotado
) -> None:
    arquivo = tmp_path / "hb"  # o processo acabou de subir: o arquivo nao existe
    parar = EsperaAnotada(ate=3, arquivo_de_vida=arquivo)

    _rodar(
        CanalSemBroker(falha_ao_abrir=True),
        _canal_fechado_pelo_broker,
        parar,
        arquivo,
    )

    # Sem nenhuma conexao o laco nunca chega ao ``finally``: so o sinal antes de
    # abrir cria o arquivo, e a liveness (pela idade dele) depende disso.
    assert parar.estados_na_espera == ["conectando"] * 3


def test_broker_fora_tambem_dobra_a_espera(
    tmp_path: Path, sorteio: SorteioAnotado
) -> None:
    parar = EsperaAnotada(ate=3)
    canal = CanalSemBroker(falha_ao_abrir=True)

    _rodar(canal, _canal_fechado_pelo_broker, parar, tmp_path / "hb")

    assert parar.esperas == [1.0, 2.0, 4.0]
    assert canal.fechamentos == 0


def test_nome_do_broker_sem_resolucao_no_boot_espera_com_backoff_fora_da_prontidao(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    sorteio: SorteioAnotado,
    dns: DnsDeTeste,
) -> None:
    # Service headless do broker sem pod pronto: o nome some do DNS e o pika
    # levanta socket.gaierror, um OSError que ele nao embrulha em AMQPError.
    dns.nomes["rabbitmq.teste"] = None
    canal = CanalAmqp(
        amqp.parametros(
            "amqp://billing:x@rabbitmq.teste:5672/%2F",  # gitleaks:allow (teste)
            nome="billing-consumidor",
        )
    )
    arquivo = tmp_path / "hb"
    parar = EsperaAnotada(ate=3, arquivo_de_vida=arquivo)

    def nao_conectou() -> None:
        pytest.fail("o trabalho nao roda sem conexao")

    with caplog.at_level(logging.INFO):
        _rodar(canal, nao_conectou, parar, arquivo)

    assert parar.esperas == [1.0, 2.0, 4.0]
    assert parar.estados_na_espera == ["conectando"] * 3
    erros = [
        registro.__dict__["erro"]
        for registro in caplog.records
        if registro.getMessage() == "broker_unavailable"
    ]
    assert erros == ["gaierror"] * 3


def test_nome_do_broker_some_do_dns_depois_de_conectar_e_a_volta_reconecta(
    tmp_path: Path, sorteio: SorteioAnotado
) -> None:
    sem_dns = socket.gaierror(socket.EAI_NONAME, "Name or service not known")
    canal = CanalRoteirizado([None, sem_dns, sem_dns, None])
    arquivo = tmp_path / "hb"
    parar = EsperaAnotada(ate=99, arquivo_de_vida=arquivo)
    chamadas: list[None] = []

    def trabalho() -> None:
        sinalizar(arquivo, pronto=True)
        chamadas.append(None)
        if len(chamadas) == 1:
            raise StreamLostError("broker caiu")
        parar.set()

    _rodar(canal, trabalho, parar, arquivo)

    # Conectou, caiu, o nome nao resolveu duas vezes e voltou: dobrou a espera
    # a cada tentativa e nunca ficou pronto fora da conexao.
    assert (canal.aberturas, len(chamadas)) == (4, 2)
    assert parar.esperas[:3] == [1.0, 2.0, 4.0]
    assert parar.estados_na_espera == ["conectando"] * len(parar.esperas)


@pytest.mark.parametrize(
    ("arquivo", "erro"),
    [
        pytest.param("hb", r"No space left", id="trabalho"),
        pytest.param("sem-diretorio/hb", r"No such file", id="arquivo-de-vida"),
    ],
)
def test_oserror_que_nao_e_do_broker_derruba_o_processo_em_vez_de_reconectar(
    tmp_path: Path, arquivo: str, erro: str
) -> None:
    # Disco cheio ou sem o diretorio do arquivo de vida nao e broker fora:
    # reconectar esconderia o defeito, e o processo cai para ser reiniciado.
    def disco_cheio() -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    parar = EsperaAnotada(ate=3)

    with pytest.raises(OSError, match=erro):
        _rodar(CanalSemBroker(), disco_cheio, parar, tmp_path / arquivo)

    assert parar.esperas == []


def test_conexao_estavel_que_cai_reconecta_na_hora(
    tmp_path: Path, sorteio: SorteioAnotado
) -> None:
    # 1a conexao durou 30 s (estavel): reconecta sem esperar; a 2a caiu em 0,5 s.
    instantes = iter([0.0, 30.0, 100.0, 100.5])
    parar = EsperaAnotada(ate=1)
    canal = CanalSemBroker()

    _rodar(
        canal,
        _canal_fechado_pelo_broker,
        parar,
        tmp_path / "hb",
        cronometro=lambda: next(instantes),
    )

    assert canal.aberturas == 2
    assert parar.esperas == [1.0]


def test_consumo_cancelado_pelo_broker_reconecta_com_backoff(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, sorteio: SorteioAnotado
) -> None:
    parar = EsperaAnotada(ate=2)
    canal = CanalSemBroker()

    with caplog.at_level(logging.WARNING):
        # O gerador do consume() acaba sem excecao: o trabalho so retorna.
        _rodar(canal, lambda: None, parar, tmp_path / "hb")

    assert parar.esperas == [1.0, 2.0]
    assert [r.getMessage() for r in caplog.records].count(
        "broker_cancelled_consumer"
    ) == 2
    assert (tmp_path / "hb").read_text() == "conectando"


def test_bloqueio_do_broker_e_anotado_e_logado(
    caplog: pytest.LogCaptureFixture,
) -> None:
    canal = CanalSemBroker()
    bloqueio = pika.frame.Method(
        0, pika.spec.Connection.Blocked(reason="low on memory")
    )

    with caplog.at_level(logging.INFO):
        canal._ao_bloquear(None, bloqueio)
        bloqueada = canal.bloqueada
        canal._ao_desbloquear(
            None, pika.frame.Method(0, pika.spec.Connection.Unblocked())
        )

    assert (bloqueada, canal.bloqueada) == (True, False)
    [aviso, fim] = caplog.records
    assert (aviso.getMessage(), aviso.__dict__["motivo"]) == (
        "broker_connection_blocked",
        "low on memory",
    )
    assert fim.getMessage() == "broker_connection_unblocked"


def test_parametros_da_conexao_sao_os_do_contrato_de_operacao() -> None:
    params = amqp.parametros(
        "amqp://billing:x@rabbitmq:5672/%2F",  # gitleaks:allow (teste)
        nome="billing-relay",
    )

    # Heartbeat de 30 s, bloqueio abaixo do prazo de encerramento, socket de 5 s
    # e uma tentativa por abertura (o laco do processo faz o backoff).
    assert (
        params.heartbeat,
        params.blocked_connection_timeout,
        params.socket_timeout,
        params.connection_attempts,
    ) == (30, 8, 5, 1)
    assert params.client_properties == {"connection_name": "billing-relay"}


def test_nack_do_broker_e_recusa_da_mensagem_e_nao_queda_da_conexao() -> None:
    class CanalQueNacka:
        is_open = True

        def basic_publish(self, *_args: object, **_opcoes: object) -> None:
            raise NackError([])

    canal = CanalSemBroker()
    canal._publicacao = CanalQueNacka()

    with pytest.raises(amqp.MensagemRecusadaError, match="NackError"):
        canal.publicar(
            "pytstop.eventos", "evento.billing.x", b"{}", pika.BasicProperties()
        )


def test_logs_de_queda_do_broker_nao_levam_o_texto_da_excecao(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, sorteio: SorteioAnotado
) -> None:
    # As excecoes do pika trazem a URL, com a senha.
    url = "amqp://billing:SENHA-DO-BROKER@rabbitmq:5672/%2F"  # gitleaks:allow (teste)

    class NaoAbre(CanalSemBroker):
        def abrir(self) -> None:
            raise AMQPConnectionError(url)

    def cai() -> None:
        raise StreamLostError(url)

    with caplog.at_level(logging.INFO):
        _rodar(NaoAbre(), cai, EsperaAnotada(ate=1), tmp_path / "hb")
        _rodar(CanalSemBroker(), cai, EsperaAnotada(ate=1), tmp_path / "hb")

    mensagens = [registro.getMessage() for registro in caplog.records]
    assert {"broker_unavailable", "broker_connection_lost"} <= set(mensagens)
    assert "SENHA-DO-BROKER" not in "\n".join(str(r.__dict__) for r in caplog.records)
