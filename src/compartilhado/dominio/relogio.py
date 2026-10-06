"""Relogio do dominio: instante atual em UTC, injetavel nos casos de uso."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

type Relogio = Callable[[], datetime]


def agora_utc() -> datetime:
    """Instante atual em UTC truncado em milissegundos.

    O BSON date guarda milissegundos: truncar na origem faz o valor lido do
    MongoDB ser igual ao gravado (comparacoes e testes de ida e volta exatos).
    """
    agora = datetime.now(UTC)
    return agora.replace(microsecond=agora.microsecond - agora.microsecond % 1000)
