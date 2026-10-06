"""Envelope das mensagens da outbox (RFC-004, secao 5.2).

``para_envelope`` e pura: transforma o evento no corpo JSON que o relay publica
sem nenhuma conversao adicional (UUID, data e Decimal ja viram
string; dinheiro nunca vira float). A gravacao fica na unidade de trabalho.
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING, Any
from uuid import UUID

if TYPE_CHECKING:
    from src.compartilhado.dominio.events import IntegrationEvent

ORIGEM = "billing-service"
VERSAO_DO_ENVELOPE = 1


def para_envelope(evento: IntegrationEvent, *, mensagem_id: UUID) -> dict[str, Any]:
    """Monta o envelope ``{id, tipo, versao, origem, correlation_id, ...}``.

    ``causation_id`` fica nulo enquanto o evento nasce de chamada HTTP, webhook
    ou prazo; quando a causa for um comando da saga, o consumidor de comandos
    (ADR-036) passa o id dele.
    """
    # Campo opcional sem valor fica fora de ``dados`` (ex.: ``decidido_por`` so
    # existe com canal=atendente), como o contrato de cada mensagem o define.
    dados = {
        campo.name: _serializar(valor)
        for campo in fields(evento)
        if campo.name != "ocorrido_em"
        and (valor := getattr(evento, campo.name)) is not None
    }
    return {
        "id": str(mensagem_id),
        "tipo": evento.tipo,
        "versao": VERSAO_DO_ENVELOPE,
        "origem": ORIGEM,
        "correlation_id": str(evento.ordem_id),
        "causation_id": None,
        "ocorrido_em": _iso(evento.ocorrido_em),
        "dados": dados,
    }


def _iso(instante: datetime) -> str:
    return (
        instante.astimezone(UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _serializar(valor: object) -> Any:  # noqa: ANN401 - payload JSON heterogeneo
    # Enum antes de str: StrEnum tambem e str, e o envelope leva o valor puro.
    if isinstance(valor, Enum):
        return _serializar(valor.value)
    if valor is None or isinstance(valor, (str, bool, int)):
        return valor
    if isinstance(valor, (UUID, Decimal)):
        return str(valor)
    if isinstance(valor, datetime):
        return _iso(valor)
    if isinstance(valor, (list, tuple)):
        return [_serializar(item) for item in valor]
    if is_dataclass(valor) and not isinstance(valor, type):
        return {
            campo.name: _serializar(getattr(valor, campo.name))
            for campo in fields(valor)
        }
    msg = f"Tipo nao suportado no envelope da outbox: {type(valor).__name__}"
    raise TypeError(msg)
