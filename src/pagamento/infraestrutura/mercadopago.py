"""Adapter real do ``GatewayPagamento``: Mercado Pago Checkout Pro (RFC-004 §8).

Contrato (documentacao oficial, referencias nos testes de contrato):

- ``POST /checkout/preferences``: itens, ``external_reference`` = id do
  pagamento, ``notification_url`` e expiracao -> ``id`` + ``init_point``;
- ``PUT /checkout/preferences/{id}``: expira a preferencia agora (cancela o
  checkout na compensacao);
- ``GET /v1/payments/{id}``: status do pagamento (fonte de verdade);
- ``GET /v1/payments/search?external_reference=``: tentativas da cobranca
  (conciliacao ativa, ADR-040 passo 4);
- ``POST /v1/payments/{id}/refunds``: estorno total com ``X-Idempotency-Key``.

A assinatura do webhook (``x-signature``) e validada na borda HTTP, em
``src/pagamento/interfaces/assinatura_webhook.py``.

Resiliencia: timeout em toda chamada, retry com backoff e jitter so nas
operacoes idempotentes (consulta, cancelamento e estorno com chave) e circuit
breaker compartilhado por todas as operacoes. A leitura do corpo acontece
dentro da chamada protegida: resposta fora do contrato (3xx, corpo que nao e
JSON, campo faltando ou invalido) e falha transitoria como um 5xx, conta no
disjuntor e aparece como ``resultado="resposta_invalida"`` na metrica.
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
import structlog
from prometheus_client import Counter

from src.compartilhado.dominio.dinheiro import Dinheiro
from src.compartilhado.dominio.relogio import agora_utc
from src.compartilhado.infraestrutura.circuit_breaker import (
    CircuitBreaker,
    CircuitoAbertoError,
)
from src.pagamento.aplicacao.ports import (
    CobrancaCriada,
    EstornoEmProcessamentoError,
    GatewayPagamentoIndisponivelError,
    GatewayPagamentoRecusouError,
)
from src.pagamento.dominio.cobranca import SituacaoNoProvedor
from src.pagamento.dominio.estados import StatusNoProvedor

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from datetime import datetime
    from uuid import UUID

    from src.compartilhado.dominio.relogio import Relogio
    from src.pagamento.aplicacao.ports import ItemCobranca

_log = structlog.get_logger(__name__)

MERCADOPAGO_REQUISICOES = Counter(
    "pytstop_mercadopago_requisicoes_total",
    "Chamadas ao Mercado Pago por operacao e resultado.",
    ["operacao", "resultado"],
)

# Status de uma TENTATIVA de pagamento no Mercado Pago. `rejected` e uma recusa
# (o agregado conta ate PAGAMENTO_MAX_RECUSAS); `refunded` e estorno. Os demais
# nao mudam a cobranca: pendente ou em analise, tentativa cancelada (boleto ou
# pix vencido) e `charged_back`, que e contestacao do portador, nao estorno.
_STATUS: Final = {
    "approved": StatusNoProvedor.APROVADO,
    "rejected": StatusNoProvedor.RECUSADO,
    "refunded": StatusNoProvedor.ESTORNADO,
    "pending": StatusNoProvedor.EM_ANDAMENTO,
    "in_process": StatusNoProvedor.EM_ANDAMENTO,
    "authorized": StatusNoProvedor.EM_ANDAMENTO,
    "in_mediation": StatusNoProvedor.EM_ANDAMENTO,
    "cancelled": StatusNoProvedor.EM_ANDAMENTO,
    "charged_back": StatusNoProvedor.EM_ANDAMENTO,
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
_REDIRECIONAMENTO = 300
# O que a leitura de um corpo fora do contrato levanta: JSON invalido
# (ValueError), campo ausente (LookupError), tipo errado (TypeError,
# AttributeError) e numero invalido (decimal.InvalidOperation e ArithmeticError).
_CORPO_FORA_DO_CONTRATO = (
    ValueError,
    LookupError,
    TypeError,
    AttributeError,
    ArithmeticError,
)
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

    resultado = "falha_transitoria"


class _RespostaInvalidaError(_FalhaTransitoriaError):
    """2xx com corpo fora do contrato ou 3xx: tratada como falha transitoria."""

    resultado = "resposta_invalida"


class _PagamentoNaoEncontradoError(GatewayPagamentoRecusouError):
    """404 do provedor: na consulta, a referencia nao existe la."""


@dataclass(frozen=True, slots=True)
class _Recusa:
    """4xx (menos 429): o provedor respondeu e recusou; nao conta no disjuntor."""

    status: int
    mensagem: str


class MercadoPagoGateway:
    provedor = "mercadopago"

    def __init__(
        self,
        config: ConfiguracaoMercadoPago,
        *,
        breaker: CircuitBreaker | None = None,
        dormir: Callable[[float], None] = time.sleep,
        relogio: Relogio = agora_utc,
    ) -> None:
        self._config = config
        self._breaker = breaker or CircuitBreaker(
            "mercadopago", falha=_FalhaTransitoriaError
        )
        self._dormir = dormir
        self._relogio = relogio
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
    ) -> CobrancaCriada:
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
        return self._enviar(
            "criar_cobranca",
            "POST",
            "/checkout/preferences",
            ler=_ler_cobranca,
            repetir=False,
            json=corpo,
        )

    def cancelar_cobranca(self, referencia_preferencia: str) -> None:
        """Expira a preferencia agora: o checkout e os pix/boletos gerados nele
        deixam de aceitar pagamento. Repetir da o mesmo resultado (retry ok)."""
        if not _REFERENCIA_VALIDA.fullmatch(referencia_preferencia):
            msg = "Referencia de preferencia invalida para cancelamento"
            raise GatewayPagamentoRecusouError(msg)
        agora = _data_mp(self._relogio())
        self._enviar(
            "cancelar_cobranca",
            "PUT",
            f"/checkout/preferences/{referencia_preferencia}",
            ler=_ignorar_corpo,
            repetir=True,
            json={
                "expires": True,
                "expiration_date_to": agora,
                "date_of_expiration": agora,
            },
        )

    def consultar_pagamento(self, referencia: str) -> SituacaoNoProvedor | None:
        if not _REFERENCIA_VALIDA.fullmatch(referencia):
            return None
        try:
            return self._enviar(
                "consultar_pagamento",
                "GET",
                f"/v1/payments/{referencia}",
                ler=_ler_situacao,
                repetir=True,
            )
        except _PagamentoNaoEncontradoError:
            return None

    def buscar_por_referencia_externa(
        self, referencia_externa: str
    ) -> list[SituacaoNoProvedor]:
        return self._enviar(
            "buscar_pagamentos",
            "GET",
            "/v1/payments/search",
            ler=_ler_tentativas,
            repetir=True,
            params={
                "external_reference": referencia_externa,
                "sort": "date_created",
                "criteria": "asc",
            },
        )

    def estornar(self, referencia: str, *, chave_idempotencia: str) -> None:
        if not _REFERENCIA_VALIDA.fullmatch(referencia):
            msg = "Referencia de pagamento invalida para estorno"
            raise GatewayPagamentoRecusouError(msg)
        status = self._enviar(
            "estornar",
            "POST",
            f"/v1/payments/{referencia}/refunds",
            ler=_ler_status_do_estorno,
            repetir=True,
            json={},
            headers={"X-Idempotency-Key": chave_idempotencia},
        )
        if status in _ESTORNO_RECUSADO:
            msg = f"Mercado Pago recusou o estorno (status {status})"
            raise GatewayPagamentoRecusouError(msg)
        # Qualquer outra resposta (in_process, sem status) so conclui quando o
        # pagamento aparecer estornado na consulta.
        if status != _ESTORNO_CONCLUIDO:
            raise EstornoEmProcessamentoError

    def _enviar[T](
        self,
        operacao: str,
        metodo: str,
        caminho: str,
        *,
        ler: Callable[[httpx.Response], T],
        repetir: bool,
        **opcoes: Any,  # noqa: ANN401 - repassadas ao httpx (json, headers)
    ) -> T:
        tentativas = self._config.tentativas if repetir else 1
        tentativa = 1
        while True:
            try:
                resultado = self._breaker.chamar(
                    lambda: self._requisitar(metodo, caminho, opcoes, ler)
                )
            except CircuitoAbertoError:
                MERCADOPAGO_REQUISICOES.labels(operacao, "circuito_aberto").inc()
                raise GatewayPagamentoIndisponivelError from None
            except _FalhaTransitoriaError as exc:
                MERCADOPAGO_REQUISICOES.labels(operacao, exc.resultado).inc()
                if tentativa >= tentativas:
                    raise GatewayPagamentoIndisponivelError from None
                self._dormir(
                    _BACKOFF_BASE_SEGUNDOS * 2 ** (tentativa - 1)
                    + _aleatorio.uniform(0, _JITTER_MAXIMO_SEGUNDOS)
                )
                tentativa += 1
                continue
            if isinstance(resultado, _Recusa):
                if resultado.status == _NAO_ENCONTRADO:
                    MERCADOPAGO_REQUISICOES.labels(operacao, "nao_encontrado").inc()
                    raise _PagamentoNaoEncontradoError(resultado.mensagem)
                MERCADOPAGO_REQUISICOES.labels(operacao, "recusado").inc()
                raise GatewayPagamentoRecusouError(resultado.mensagem)
            MERCADOPAGO_REQUISICOES.labels(operacao, "sucesso").inc()
            return resultado

    def _requisitar[T](
        self,
        metodo: str,
        caminho: str,
        opcoes: dict[str, Any],
        ler: Callable[[httpx.Response], T],
    ) -> T | _Recusa:
        try:
            resposta = self._http.request(metodo, caminho, **opcoes)
        except httpx.TransportError as exc:
            raise _FalhaTransitoriaError(type(exc).__name__) from exc
        status = resposta.status_code
        if status == _MUITAS_REQUISICOES or status >= _ERRO_DE_SERVIDOR:
            raise _FalhaTransitoriaError(f"HTTP {status}")
        if status >= _ERRO_DE_CLIENTE:
            return _Recusa(status, _mensagem_de_erro(resposta))
        if status >= _REDIRECIONAMENTO:
            raise _RespostaInvalidaError(f"HTTP {status}")
        try:
            return ler(resposta)
        except _CORPO_FORA_DO_CONTRATO as exc:
            raise _RespostaInvalidaError(type(exc).__name__) from exc


def _ler_cobranca(resposta: httpx.Response) -> CobrancaCriada:
    dados = resposta.json()
    return CobrancaCriada(
        referencia=_texto(dados, "id"), checkout_url=_texto(dados, "init_point")
    )


def _ignorar_corpo(_resposta: httpx.Response) -> None:
    return None


def _ler_situacao(resposta: httpx.Response) -> SituacaoNoProvedor:
    # parse_float=Decimal: o valor cobrado nunca passa por float.
    return _situacao(resposta.json(parse_float=Decimal))


def _ler_tentativas(resposta: httpx.Response) -> list[SituacaoNoProvedor]:
    resultados = resposta.json(parse_float=Decimal)["results"]
    if not isinstance(resultados, list):
        msg = "results deveria ser uma lista"
        raise TypeError(msg)
    return [_situacao(pagamento) for pagamento in resultados]


def _ler_status_do_estorno(resposta: httpx.Response) -> str | None:
    """Status do reembolso criado (``None`` quando o corpo nao traz)."""
    corpo = resposta.json()
    if not isinstance(corpo, dict):
        msg = "reembolso deveria ser um objeto"
        raise TypeError(msg)
    status = corpo.get("status")
    return str(status) if status is not None else None


def _texto(dados: dict[str, Any], campo: str) -> str:
    """Campo obrigatorio como texto (id numerico do provedor vira texto)."""
    valor = dados[campo]
    if isinstance(valor, bool) or not isinstance(valor, str | int) or valor == "":
        msg = f"{campo} ausente ou invalido"
        raise ValueError(msg)
    return str(valor)


def _situacao(dados: dict[str, Any]) -> SituacaoNoProvedor:
    """So id, status, detalhe, valor e moeda: payer e cartao ficam de fora."""
    status = _texto(dados, "status")
    externa = dados.get("external_reference")
    detalhe = dados.get("status_detail")
    return SituacaoNoProvedor(
        referencia=_texto(dados, "id"),
        referencia_externa=str(externa) if externa else None,
        status=_status_no_provedor(status),
        status_provedor=status,
        detalhe=str(detalhe) if detalhe is not None else None,
        valor=_valor(dados),
    )


def _status_no_provedor(status: str) -> StatusNoProvedor:
    conhecido = _STATUS.get(status)
    if conhecido is None:
        # Status novo no provedor: nao muda a cobranca, mas fica visivel.
        MERCADOPAGO_REQUISICOES.labels(
            "consultar_pagamento", "status_desconhecido"
        ).inc()
        _log.warning("mercadopago_unknown_status", status=status)
        return StatusNoProvedor.EM_ANDAMENTO
    return conhecido


def _data_mp(instante: datetime) -> str:
    return instante.astimezone(UTC).isoformat(timespec="milliseconds")


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
