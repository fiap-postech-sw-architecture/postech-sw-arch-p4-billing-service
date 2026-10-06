"""Adapter real do ``GatewayPagamento``: Mercado Pago Checkout Pro (RFC-004 §8).

Contrato (documentacao oficial, referencias nos testes de contrato):

- ``POST /checkout/preferences``: itens, ``external_reference`` = id do
  pagamento, ``notification_url`` e expiracao -> ``id`` + ``init_point``;
- ``GET /v1/payments/{id}``: status do pagamento (fonte de verdade);
- ``POST /v1/payments/{id}/refunds``: estorno total com ``X-Idempotency-Key``.

A assinatura do webhook (``x-signature``) e validada na borda HTTP, em
``src/pagamento/interfaces/assinatura_webhook.py``.

Resiliencia: timeout em toda chamada, retry com backoff e jitter so nas
operacoes idempotentes (consulta e estorno com chave) e circuit breaker
compartilhado pelas tres operacoes.
"""

from __future__ import annotations

import re
import secrets
import time
from dataclasses import dataclass
from datetime import UTC
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final

import httpx
from prometheus_client import Counter

from src.compartilhado.dominio.dinheiro import Dinheiro
from src.compartilhado.infraestrutura.circuit_breaker import (
    CircuitBreaker,
    CircuitoAbertoError,
)
from src.pagamento.aplicacao.ports import (
    Cobranca,
    EstornoEmProcessamentoError,
    GatewayPagamentoIndisponivelError,
    GatewayPagamentoRecusouError,
    SituacaoNoProvedor,
)
from src.pagamento.dominio.pagamento import StatusPagamento

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from datetime import datetime
    from uuid import UUID

    from src.pagamento.aplicacao.ports import ItemCobranca

MERCADOPAGO_REQUISICOES = Counter(
    "pytstop_mercadopago_requisicoes_total",
    "Chamadas ao Mercado Pago por operacao e resultado.",
    ["operacao", "resultado"],
)

# Status de uma TENTATIVA de pagamento no Mercado Pago -> status da cobranca.
# No Checkout Pro o comprador tenta de novo depois de uma recusa (outro cartao,
# pix, boleto), entao `rejected` e `cancelled` nao encerram a cobranca: ela so
# fecha sem pagamento quando expira. Os demais (pending, in_process,
# authorized, in_mediation) tambem seguem pendentes.
_STATUS: Final = {
    "approved": StatusPagamento.APROVADO,
    "refunded": StatusPagamento.ESTORNADO,
    "charged_back": StatusPagamento.ESTORNADO,
}
_ESTORNO_CONCLUIDO = "approved"
_ESTORNO_RECUSADO = frozenset({"rejected", "cancelled"})

# Id de pagamento do Mercado Pago (numerico); a regex so impede que um valor
# vindo do webhook mude o caminho da URL (``../``, ``?``).
_REFERENCIA_VALIDA = re.compile(r"[A-Za-z0-9_-]{1,64}")
_NAO_ENCONTRADO = 404
_MUITAS_REQUISICOES = 429
_ERRO_DE_SERVIDOR = 500
_ERRO_DE_CLIENTE = 400
_BACKOFF_BASE_SEGUNDOS = 0.2
_JITTER_MAXIMO_SEGUNDOS = 0.1
_aleatorio = secrets.SystemRandom()


@dataclass(frozen=True, slots=True)
class ConfiguracaoMercadoPago:
    access_token: str
    notification_url: str
    base_url: str = "https://api.mercadopago.com"
    timeout_segundos: float = 5.0
    tentativas: int = 3


class _FalhaTransitoriaError(Exception):
    """Timeout, erro de conexao, 429 ou 5xx: vale repetir e conta no disjuntor."""


class MercadoPagoGateway:
    provedor = "mercadopago"

    def __init__(
        self,
        config: ConfiguracaoMercadoPago,
        *,
        breaker: CircuitBreaker | None = None,
        dormir: Callable[[float], None] = time.sleep,
    ) -> None:
        self._config = config
        self._breaker = breaker or CircuitBreaker(
            "mercadopago", falha=_FalhaTransitoriaError
        )
        self._dormir = dormir
        self._http = httpx.Client(
            base_url=config.base_url,
            timeout=config.timeout_segundos,
            headers={"Authorization": f"Bearer {config.access_token}"},
        )

    def fechar(self) -> None:
        self._http.close()

    def criar_cobranca(
        self,
        *,
        pagamento_id: UUID,
        itens: Sequence[ItemCobranca],
        expira_em: datetime,
    ) -> Cobranca:
        corpo = {
            "items": [
                {
                    "id": item.codigo,
                    "title": item.descricao,
                    "quantity": item.quantidade,
                    "currency_id": item.preco_unitario.moeda,
                    "unit_price": _numero_json(item.preco_unitario.valor),
                }
                for item in itens
            ],
            "external_reference": str(pagamento_id),
            "notification_url": self._config.notification_url,
            # Checkout fecha no prazo e pix/boleto gerados nele vencem junto:
            # nada pode ser pago depois que a cobranca expira aqui.
            "expires": True,
            "expiration_date_to": _data_mp(expira_em),
            "date_of_expiration": _data_mp(expira_em),
        }
        # Criar preferencia nao e idempotente: uma tentativa so.
        resposta = self._enviar(
            "criar_cobranca", "POST", "/checkout/preferences", repetir=False, json=corpo
        )
        dados = resposta.json()
        return Cobranca(referencia=str(dados["id"]), checkout_url=dados["init_point"])

    def consultar_pagamento(self, referencia: str) -> SituacaoNoProvedor | None:
        if not _REFERENCIA_VALIDA.fullmatch(referencia):
            return None
        resposta = self._enviar(
            "consultar_pagamento",
            "GET",
            f"/v1/payments/{referencia}",
            repetir=True,
            aceitar_404=True,
        )
        if resposta.status_code == _NAO_ENCONTRADO:
            return None
        # parse_float=Decimal: o valor cobrado nunca passa por float.
        dados = resposta.json(parse_float=Decimal)
        status = str(dados["status"])
        externa = dados.get("external_reference")
        return SituacaoNoProvedor(
            referencia=str(dados["id"]),
            referencia_externa=str(externa) if externa else None,
            status=_STATUS.get(status, StatusPagamento.PENDENTE),
            status_provedor=status,
            detalhe=dados.get("status_detail"),
            valor=_valor(dados),
        )

    def estornar(self, referencia: str, *, chave_idempotencia: str) -> None:
        if not _REFERENCIA_VALIDA.fullmatch(referencia):
            msg = "Referencia de pagamento invalida para estorno"
            raise GatewayPagamentoRecusouError(msg)
        resposta = self._enviar(
            "estornar",
            "POST",
            f"/v1/payments/{referencia}/refunds",
            repetir=True,
            json={},
            headers={"X-Idempotency-Key": chave_idempotencia},
        )
        status = _status_do_estorno(resposta)
        if status in _ESTORNO_RECUSADO:
            msg = f"Mercado Pago recusou o estorno (status {status})"
            raise GatewayPagamentoRecusouError(msg)
        # Qualquer outra resposta (in_process, sem status) so conclui quando o
        # pagamento aparecer estornado na consulta.
        if status != _ESTORNO_CONCLUIDO:
            raise EstornoEmProcessamentoError

    def _enviar(
        self,
        operacao: str,
        metodo: str,
        caminho: str,
        *,
        repetir: bool,
        aceitar_404: bool = False,
        **opcoes: Any,  # noqa: ANN401 - repassadas ao httpx (json, headers)
    ) -> httpx.Response:
        tentativas = self._config.tentativas if repetir else 1
        tentativa = 1
        while True:
            try:
                resposta = self._breaker.chamar(
                    lambda: self._requisitar(metodo, caminho, opcoes)
                )
            except CircuitoAbertoError:
                MERCADOPAGO_REQUISICOES.labels(operacao, "circuito_aberto").inc()
                raise GatewayPagamentoIndisponivelError from None
            except _FalhaTransitoriaError:
                MERCADOPAGO_REQUISICOES.labels(operacao, "falha_transitoria").inc()
                if tentativa >= tentativas:
                    raise GatewayPagamentoIndisponivelError from None
                self._dormir(
                    _BACKOFF_BASE_SEGUNDOS * 2 ** (tentativa - 1)
                    + _aleatorio.uniform(0, _JITTER_MAXIMO_SEGUNDOS)
                )
                tentativa += 1
                continue
            return self._verificar(operacao, resposta, aceitar_404=aceitar_404)

    def _requisitar(
        self, metodo: str, caminho: str, opcoes: dict[str, Any]
    ) -> httpx.Response:
        try:
            resposta = self._http.request(metodo, caminho, **opcoes)
        except httpx.TransportError as exc:
            raise _FalhaTransitoriaError(type(exc).__name__) from exc
        if (
            resposta.status_code == _MUITAS_REQUISICOES
            or resposta.status_code >= _ERRO_DE_SERVIDOR
        ):
            raise _FalhaTransitoriaError(f"HTTP {resposta.status_code}")
        return resposta

    def _verificar(
        self, operacao: str, resposta: httpx.Response, *, aceitar_404: bool
    ) -> httpx.Response:
        if resposta.status_code == _NAO_ENCONTRADO and aceitar_404:
            MERCADOPAGO_REQUISICOES.labels(operacao, "nao_encontrado").inc()
            return resposta
        if resposta.status_code >= _ERRO_DE_CLIENTE:
            MERCADOPAGO_REQUISICOES.labels(operacao, "recusado").inc()
            raise GatewayPagamentoRecusouError(_mensagem_de_erro(resposta))
        MERCADOPAGO_REQUISICOES.labels(operacao, "sucesso").inc()
        return resposta


def _data_mp(instante: datetime) -> str:
    return instante.astimezone(UTC).isoformat(timespec="milliseconds")


def _status_do_estorno(resposta: httpx.Response) -> str | None:
    """Status do reembolso criado (``None`` quando o corpo nao traz)."""
    try:
        corpo = resposta.json()
    except ValueError:
        return None
    status = corpo.get("status") if isinstance(corpo, dict) else None
    return str(status) if status is not None else None


def _numero_json(valor: Decimal) -> float:
    """``unit_price`` do Checkout Pro e numero JSON.

    Dinheiro tem 2 casas e a API limita a 10 digitos: o ``repr`` do float e
    exatamente o decimal. A conferencia falha alto se um dia nao for.
    """
    numero = float(valor)
    if Decimal(repr(numero)) != valor:
        msg = f"Valor {valor} nao tem representacao exata como numero JSON"
        raise ValueError(msg)
    return numero


def _valor(dados: dict[str, Any]) -> Dinheiro | None:
    valor = dados.get("transaction_amount")
    if valor is None:
        return None
    return Dinheiro(
        valor=Decimal(str(valor)), moeda=str(dados.get("currency_id", "BRL"))
    )


def _mensagem_de_erro(resposta: httpx.Response) -> str:
    try:
        corpo = resposta.json()
    except ValueError:
        corpo = None
    mensagem = corpo.get("message") if isinstance(corpo, dict) else None
    return f"Mercado Pago respondeu {resposta.status_code}: {mensagem or 'sem detalhe'}"
