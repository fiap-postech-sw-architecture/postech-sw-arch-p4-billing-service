"""Configuracao do servico lida do ambiente, validada no boot (falha rapido).

Sem ``ENVIRONMENT`` o servico assume producao (falha fechada): enderecos e o
segredo do link precisam vir explicitos, e o segredo de demonstracao publico
no repo e recusado (mesma postura do ``validar_segredos_no_startup`` do p3).
Desenvolvimento e testes declaram ``ENVIRONMENT=development|test``; qualquer
outro valor e erro de configuracao. ``MP_MODE`` nao tem padrao, e o simulador
(que aprova pagamento sem provedor) so sobe em producao com
``SIMULADOR_PERMITIDO=true`` explicito (ADR-040).
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

_AMBIENTES_DE_DESENVOLVIMENTO = frozenset({"development", "test"})
_AMBIENTES = _AMBIENTES_DE_DESENVOLVIMENTO | {"production"}
_TAMANHO_MINIMO_SEGREDO = 32

# Valor de demonstracao (compose/testes); proibido fora de development/test.
SEGREDO_LINK_DEMO = "demo-link-orcamento-pytstop-nao-usar-em-producao"

_PADROES_DE_DESENVOLVIMENTO = {
    "MONGODB_URI": "mongodb://localhost:27017/?directConnection=true",
    "JWKS_URL": "http://localhost:8001/.well-known/jwks.json",
    "BILLING_PUBLIC_URL": "http://localhost:8002",
    "ORCAMENTO_LINK_SECRET": SEGREDO_LINK_DEMO,
}


class ModoMercadoPago(StrEnum):
    SIMULADO = "simulado"
    MERCADOPAGO = "mercadopago"


@dataclass(frozen=True, slots=True)
class Configuracao:
    ambiente: str
    mongodb_uri: str = field(repr=False)
    mongodb_banco: str
    jwks_url: str
    jwt_emissor: str
    jwt_audiencia: str
    url_publica: str
    link_segredo: str = field(repr=False)
    orcamento_validade: timedelta
    pagamento_validade: timedelta
    mp_modo: ModoMercadoPago
    mp_access_token: str | None = field(repr=False)
    mp_webhook_secret: str | None = field(repr=False)
    mp_api_url: str
    mp_notification_url: str
    mp_timeout_segundos: float
    prazos_intervalo_segundos: float

    @classmethod
    def do_ambiente(cls, env: Mapping[str, str] | None = None) -> Configuracao:
        env = os.environ if env is None else env
        ambiente = _ambiente(env)
        desenvolvimento = ambiente in _AMBIENTES_DE_DESENVOLVIMENTO

        def exigir(nome: str) -> str:
            valor = env.get(nome) or (
                _PADROES_DE_DESENVOLVIMENTO[nome] if desenvolvimento else ""
            )
            if not valor:
                msg = f"{nome} obrigatoria quando ENVIRONMENT={ambiente}"
                raise ValueError(msg)
            return valor

        segredo = exigir("ORCAMENTO_LINK_SECRET")
        if not desenvolvimento:
            _validar_segredo_de_producao(segredo)
        modo = _modo(env.get("MP_MODE", ""))
        if (
            modo is ModoMercadoPago.SIMULADO
            and not desenvolvimento
            and env.get("SIMULADOR_PERMITIDO", "").strip().lower() != "true"
        ):
            msg = (
                f"MP_MODE=simulado recusado com ENVIRONMENT={ambiente}: o simulador "
                "aprova pagamento sem provedor (SIMULADOR_PERMITIDO=true so em demo)"
            )
            raise ValueError(msg)
        access_token = env.get("MP_ACCESS_TOKEN") or None
        webhook_secret = env.get("MP_WEBHOOK_SECRET") or None
        if modo is ModoMercadoPago.MERCADOPAGO and not (
            access_token and webhook_secret
        ):
            msg = "MP_MODE=mercadopago exige MP_ACCESS_TOKEN e MP_WEBHOOK_SECRET"
            raise ValueError(msg)
        url_publica = exigir("BILLING_PUBLIC_URL").rstrip("/")
        return cls(
            ambiente=ambiente,
            mongodb_uri=exigir("MONGODB_URI"),
            mongodb_banco=env.get("MONGODB_DB", "billing"),
            jwks_url=exigir("JWKS_URL"),
            jwt_emissor=env.get("JWT_ISSUER", "pytstop-os-service"),
            jwt_audiencia=env.get("JWT_AUDIENCE", "pytstop"),
            url_publica=url_publica,
            link_segredo=segredo,
            orcamento_validade=timedelta(
                hours=_positivo(env, "ORCAMENTO_VALIDADE_HORAS", 72)
            ),
            pagamento_validade=timedelta(
                minutes=_positivo(env, "PAGAMENTO_VALIDADE_MINUTOS", 60)
            ),
            mp_modo=modo,
            mp_access_token=access_token,
            mp_webhook_secret=webhook_secret,
            mp_api_url=env.get("MP_API_URL", "https://api.mercadopago.com"),
            mp_notification_url=env.get(
                "MP_NOTIFICATION_URL", f"{url_publica}/api/v1/webhooks/mercadopago"
            ),
            mp_timeout_segundos=_positivo(env, "MP_TIMEOUT_SEGUNDOS", 5),
            prazos_intervalo_segundos=_positivo(env, "PRAZOS_INTERVALO_SEGUNDOS", 30),
        )


def _validar_segredo_de_producao(segredo: str) -> None:
    if segredo == SEGREDO_LINK_DEMO:
        msg = "ORCAMENTO_LINK_SECRET usa o valor de demonstracao publico no repo"
        raise ValueError(msg)
    if len(segredo.encode()) < _TAMANHO_MINIMO_SEGREDO:
        msg = (
            f"ORCAMENTO_LINK_SECRET precisa de >= {_TAMANHO_MINIMO_SEGREDO} bytes "
            "(ex.: openssl rand -hex 32)"
        )
        raise ValueError(msg)


def _ambiente(env: Mapping[str, str]) -> str:
    valor = env.get("ENVIRONMENT", "production")
    ambiente = valor.strip().lower()
    if ambiente not in _AMBIENTES:
        msg = f"ENVIRONMENT invalido: {valor!r} (use {', '.join(sorted(_AMBIENTES))})"
        raise ValueError(msg)
    return ambiente


def _modo(valor: str) -> ModoMercadoPago:
    if not valor:
        msg = "MP_MODE obrigatoria: simulado ou mercadopago"
        raise ValueError(msg)
    try:
        return ModoMercadoPago(valor.strip().lower())
    except ValueError:
        msg = f"MP_MODE invalido: {valor!r} (use simulado ou mercadopago)"
        raise ValueError(msg) from None


def _positivo(env: Mapping[str, str], nome: str, padrao: float) -> float:
    try:
        valor = float(env.get(nome, padrao))
    except ValueError:
        msg = f"{nome} deve ser numerico"
        raise ValueError(msg) from None
    if not math.isfinite(valor) or valor <= 0:
        msg = f"{nome} deve ser um numero finito maior que zero"
        raise ValueError(msg)
    return valor
