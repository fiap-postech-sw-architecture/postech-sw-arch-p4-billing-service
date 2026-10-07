"""Laco de conexao do relay e do consumidor: backoff com jitter mesmo depois de
conectar, e o arquivo de vida sem ``pronto`` fora da conexao."""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

import pika
import pytest
from pika.exceptions import AMQPConnectionError, ChannelWrongStateError

from src.compartilhado.infraestrutura.mensageria import amqp
from src.compartilhado.infraestrutura.mensageria.amqp import CanalAmqp, manter_conectado
from src.compartilhado.infraestrutura.processo import sinalizar

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


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


def _rodar(
    canal: CanalSemBroker,
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


def test_broker_fora_tambem_dobra_a_espera(
    tmp_path: Path, sorteio: SorteioAnotado
) -> None:
    parar = EsperaAnotada(ate=3)
    canal = CanalSemBroker(falha_ao_abrir=True)

    _rodar(canal, _canal_fechado_pelo_broker, parar, tmp_path / "hb")

    assert parar.esperas == [1.0, 2.0, 4.0]
    assert canal.fechamentos == 0


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
