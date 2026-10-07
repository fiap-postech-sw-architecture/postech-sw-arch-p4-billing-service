"""Contratos das mensagens copiados do platform (``contratos/``, SHA em ``ORIGEM``).

O envelope e o ``dados`` de cada tipo seguem o JSON Schema 2020-12 do contrato:
conferidos na gravacao da outbox (fora do contrato e defeito deste servico) e
no consumo (fora do contrato e erro permanente: a mensagem vai para a DLQ).
Leitor tolerante: os schemas nao fecham ``additionalProperties``, entao campo
novo nao invalida; ``versao`` desconhecida invalida (ADR-036; RFC-004, secao 5.5).

Na imagem o codigo roda do site-packages e o ``contratos/`` fica em
``/app/contratos`` (``CONTRATOS_DIR``); fora dela vale o da raiz do repositorio.
"""

from __future__ import annotations

import json
import os
import re
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import best_match

if TYPE_CHECKING:
    from collections.abc import Mapping

VERSAO: Final = 1
EXCHANGE_EVENTOS: Final = "pytstop.eventos"
_ENVELOPE: Final = "envelope"
_PADRAO: Final = Path(__file__).resolve().parents[4] / "contratos"


class MensagemForaDoContratoError(Exception):
    """Envelope ou ``dados`` fora do schema, tipo sem contrato ou ``versao``
    desconhecida. A mensagem diz onde e qual regra falhou, nunca o valor (o
    ``dados`` traz texto livre)."""


def diretorio() -> Path:
    return Path(os.environ.get("CONTRATOS_DIR") or _PADRAO)


class ContratosAusentesError(RuntimeError):
    """Diretorio dos contratos sem o schema do envelope: configuracao errada."""


@cache
def _validadores() -> dict[str, Draft202012Validator]:
    # Indexados pelo nome do arquivo: o tipo recebido so escolhe uma chave do
    # dicionario, nunca monta um caminho de arquivo.
    validadores = {
        caminho.name.removesuffix(".schema.json"): Draft202012Validator(
            json.loads(caminho.read_text(encoding="utf-8")),
            format_checker=FormatChecker(),
        )
        for caminho in (diretorio() / "schemas").glob("*.schema.json")
    }
    if _ENVELOPE not in validadores:
        # Sem schema nada valida: cada comando iria para a DLQ e cada evento
        # abortaria a transacao, com o processo pronto. Falha no boot.
        msg = f"Contratos ausentes em {diretorio()}: confira CONTRATOS_DIR"
        raise ContratosAusentesError(msg)
    return validadores


def tipos_com_contrato() -> frozenset[str]:
    """Tipos de mensagem com schema copiado (sem o do envelope); carrega os
    schemas, entao serve de conferencia no boot dos processos."""
    return frozenset(_validadores()) - {_ENVELOPE}


def _conferir(validador: Draft202012Validator, instancia: object, onde: str) -> None:
    erro = best_match(validador.iter_errors(instancia))
    if erro is not None:
        msg = f"{onde} fora do contrato em {erro.json_path} ({erro.validator})"
        raise MensagemForaDoContratoError(msg)


def validar(envelope: Mapping[str, Any]) -> None:
    """Confere o envelope, a ``versao`` e o ``dados`` do tipo."""
    validadores = _validadores()
    _conferir(validadores[_ENVELOPE], envelope, "envelope")
    if envelope["versao"] != VERSAO:
        msg = f"versao {envelope['versao']} desconhecida (conhecida: {VERSAO})"
        raise MensagemForaDoContratoError(msg)
    # O pattern do envelope (inicial maiuscula) ja impede o tipo "envelope".
    validador = validadores.get(envelope["tipo"])
    if validador is None:
        msg = f"tipo {envelope['tipo']} sem contrato neste servico"
        raise MensagemForaDoContratoError(msg)
    _conferir(validador, envelope["dados"], "dados")


def routing_key_do_evento(tipo: str) -> str:
    """``evento.billing.<tipo em snake_case>`` (RFC-004, secao 5.3)."""
    return "evento.billing." + re.sub(r"(?<!^)(?=[A-Z])", "_", tipo).lower()
