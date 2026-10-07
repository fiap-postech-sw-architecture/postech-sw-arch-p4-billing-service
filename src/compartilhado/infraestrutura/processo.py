"""Sinais e arquivo de vida dos processos sem HTTP de negocio (relay e consumidor).

Um arquivo so faz o papel do heartbeat do relay do p3 e da prontidao (RFC-004,
secao 6): a liveness confere que ele foi tocado ha pouco; a readiness, tambem
que o conteudo e ``pronto`` (conexao com o broker estabelecida). O caminho fica
no tmpfs do container, onde so roda o usuario do servico.
"""

from __future__ import annotations

import signal
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    import threading
    from pathlib import Path

PRONTO: Final = "pronto"
CONECTANDO: Final = "conectando"


def sinalizar(arquivo: Path, *, pronto: bool) -> None:
    """Toca o arquivo de vida com o estado da conexao."""
    arquivo.write_text(PRONTO if pronto else CONECTANDO, encoding="utf-8")


def instalar_sinais(parar: threading.Event) -> None:
    """Como PID 1 sem handler, o processo ignora SIGTERM e morre por SIGKILL."""
    signal.signal(signal.SIGTERM, lambda *_: parar.set())
    signal.signal(signal.SIGINT, lambda *_: parar.set())
