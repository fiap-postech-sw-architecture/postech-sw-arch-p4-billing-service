"""Laco de conexao do relay e do consumidor: backoff mesmo depois de conectar."""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

from pika.exceptions import AMQPConnectionError, ChannelWrongStateError

from src.compartilhado.infraestrutura.mensageria.amqp import CanalAmqp, manter_conectado

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import pytest


class EsperaAnotada(threading.Event):
    """``parar`` que anota cada espera e pede a parada na de numero ``ate``."""

    def __init__(self, ate: int) -> None:
        super().__init__()
        self.esperas: list[float | None] = []
        self.ate = ate

    def wait(self, timeout: float | None = None) -> bool:
        self.esperas.append(timeout)
        if len(self.esperas) >= self.ate:
            self.set()
        return self.is_set()


class CanalSemBroker(CanalAmqp):
    """Abre sempre (ou falha se mandado), sem conexao de verdade."""

    def __init__(self, falha_ao_abrir: bool = False) -> None:
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
    tmp_path: Path,
) -> None:
    parar = EsperaAnotada(ate=6)
    canal = CanalSemBroker()

    _rodar(canal, _canal_fechado_pelo_broker, parar, tmp_path / "hb")

    # Nunca reconexao imediata em laco: 1, 2, 4, 8, 16 s e o teto de 30 s.
    assert parar.esperas == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0]
    assert canal.aberturas == canal.fechamentos == 6


def test_broker_fora_tambem_dobra_a_espera(tmp_path: Path) -> None:
    parar = EsperaAnotada(ate=3)
    canal = CanalSemBroker(falha_ao_abrir=True)

    _rodar(canal, _canal_fechado_pelo_broker, parar, tmp_path / "hb")

    assert parar.esperas == [1.0, 2.0, 4.0]
    assert canal.fechamentos == 0


def test_conexao_estavel_que_cai_reconecta_na_hora(tmp_path: Path) -> None:
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
    tmp_path: Path, caplog: pytest.LogCaptureFixture
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
