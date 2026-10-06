"""Apoio dos testes de integracao: outbox, relogio controlavel e configuracao."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlsplit

from src.configuracao import Configuracao
from src.pagamento.infraestrutura.simulado import GatewayPagamentoSimulado
from src.pagamento.interfaces.router_simulador import CAMINHO_CHECKOUT

if TYPE_CHECKING:
    from collections.abc import Sequence
    from uuid import UUID

    from pymongo.database import Database

    from src.pagamento.aplicacao.ports import (
        Cobranca,
        ItemCobranca,
        SituacaoNoProvedor,
    )

URL_PUBLICA = "http://billing.teste"
SEGREDO_WEBHOOK = "segredo-do-webhook-de-teste"
# Valor de teste (ENVIRONMENT=test aceita qualquer segredo nao vazio).
SEGREDO_LINK = "segredo-do-link-de-teste-32-bytes!!"


def token_do_checkout(checkout_url: str) -> str:
    """O ``?token=`` que o simulador pos no ``checkout_url``."""
    [token] = parse_qs(urlsplit(checkout_url).query)["token"]
    return token


def eventos_do_outbox(
    banco: Database[dict[str, Any]], tipo: str | None = None
) -> list[dict[str, Any]]:
    """Envelopes gravados no outbox, na ordem do relay (``_id`` UUIDv7)."""
    filtro = {"tipo": tipo} if tipo else {}
    return [doc["envelope"] for doc in banco["outbox"].find(filtro).sort("_id")]


class RelogioFixo:
    """Relogio controlavel; ``avancar`` move o tempo dos casos de uso."""

    def __init__(self, agora: datetime | None = None) -> None:
        self.agora = agora or datetime(2026, 10, 6, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.agora

    def avancar(self, **delta: float) -> datetime:
        self.agora += timedelta(**delta)
        return self.agora


def configuracao(**extra: str) -> Configuracao:
    return Configuracao.do_ambiente(
        {
            "ENVIRONMENT": "test",
            "MP_MODE": "simulado",
            "BILLING_PUBLIC_URL": URL_PUBLICA,
            "JWKS_URL": "http://os.teste/.well-known/jwks.json",
            "ORCAMENTO_LINK_SECRET": SEGREDO_LINK,
            "MP_WEBHOOK_SECRET": SEGREDO_WEBHOOK,
            **extra,
        }
    )


class GatewayRoteirizado(GatewayPagamentoSimulado):
    """Simulador real com ganchos: falhas programadas, respostas fixas e espias."""

    def __init__(self) -> None:
        super().__init__(
            url_checkout=f"{URL_PUBLICA}{CAMINHO_CHECKOUT}", segredo=SEGREDO_LINK
        )
        self.cobrancas: list[UUID] = []
        self.estornos: list[tuple[str, str]] = []
        self.erro_na_cobranca: Exception | None = None
        self.erro_no_estorno: Exception | None = None
        self.respostas: dict[str, SituacaoNoProvedor | None] = {}

    def criar_cobranca(
        self, *, pagamento_id: UUID, itens: Sequence[ItemCobranca], expira_em: datetime
    ) -> Cobranca:
        self.cobrancas.append(pagamento_id)
        if self.erro_na_cobranca:
            raise self.erro_na_cobranca
        return super().criar_cobranca(
            pagamento_id=pagamento_id, itens=itens, expira_em=expira_em
        )

    def consultar_pagamento(self, referencia: str) -> SituacaoNoProvedor | None:
        if referencia in self.respostas:
            return self.respostas[referencia]
        return super().consultar_pagamento(referencia)

    def estornar(self, referencia: str, *, chave_idempotencia: str) -> None:
        self.estornos.append((referencia, chave_idempotencia))
        if self.erro_no_estorno:
            raise self.erro_no_estorno
        super().estornar(referencia, chave_idempotencia=chave_idempotencia)
