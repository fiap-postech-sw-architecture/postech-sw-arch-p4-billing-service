"""Pagamentos: consulta interna e webhook do Mercado Pago."""

from __future__ import annotations

import dataclasses
from typing import Annotated
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from starlette.requests import Request

from src.compartilhado.interfaces.autenticacao import (
    Papel,
    UsuarioAutenticado,
    exigir_papel,
)
from src.pagamento.aplicacao.use_cases import (
    ConsultarPagamentos,
    ProcessarNotificacaoPagamento,
)
from src.pagamento.interfaces.assinatura_webhook import assinatura_webhook_valida
from src.pagamento.interfaces.dependencies import (
    obter_consultar_pagamentos,
    obter_processar_notificacao,
)
from src.pagamento.interfaces.schemas import (
    NotificacaoMercadoPagoRequest,
    PagamentoResponse,
    WebhookResponse,
)

_log = structlog.get_logger(__name__)

router = APIRouter(tags=["pagamentos"])


@router.get(
    "/api/v1/pagamentos/{pagamento_id}",
    summary="Consulta um pagamento",
    responses={404: {"description": "Pagamento nao encontrado."}},
)
def obter_pagamento(
    pagamento_id: UUID,
    _usuario: Annotated[UsuarioAutenticado, Depends(exigir_papel(Papel.ATENDENTE))],
    consulta: Annotated[ConsultarPagamentos, Depends(obter_consultar_pagamentos)],
) -> PagamentoResponse:
    return PagamentoResponse.model_validate(
        dataclasses.asdict(consulta.por_id(pagamento_id))
    )


@router.post(
    "/api/v1/webhooks/mercadopago",
    summary="Webhook do Mercado Pago (valida x-signature)",
    responses={
        401: {"description": "x-signature ausente ou invalida."},
        503: {
            "description": (
                "MP_WEBHOOK_SECRET nao configurado ou Mercado Pago indisponivel "
                "na consulta (o provedor reenvia a notificacao)."
            )
        },
    },
)
def receber_notificacao(
    request: Request,
    corpo: NotificacaoMercadoPagoRequest,
    processar: Annotated[
        ProcessarNotificacaoPagamento, Depends(obter_processar_notificacao)
    ],
    data_id: Annotated[str | None, Query(alias="data.id")] = None,
    x_signature: Annotated[str | None, Header()] = None,
    x_request_id: Annotated[str | None, Header()] = None,
) -> WebhookResponse:
    """Confere a assinatura e consulta o pagamento no provedor.

    O corpo so indica o que consultar; status e valor vem sempre do
    ``GET /v1/payments/{id}``. Notificacao de outro tipo responde 200 sem
    processar (o Mercado Pago so para de reenviar com 2xx).
    """
    segredo = request.app.state.config.mp_webhook_secret
    if not segredo:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Webhook do Mercado Pago desabilitado (MP_WEBHOOK_SECRET ausente)",
        )
    corpo_id = corpo.data.id if corpo.data else None
    referencia = data_id or (str(corpo_id) if corpo_id is not None else None)
    if not assinatura_webhook_valida(
        segredo=segredo,
        x_signature=x_signature,
        x_request_id=x_request_id,
        data_id=referencia,
    ):
        _log.warning("webhook_mercadopago_assinatura_invalida")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Assinatura do webhook invalida",
        )
    tipo = request.query_params.get("type") or corpo.type
    if tipo != "payment" or not referencia:
        return WebhookResponse(processado=False)
    # 200 mesmo para pagamento que nao e nosso: reenviar nao mudaria nada.
    return WebhookResponse(processado=processar.executar(referencia) is not None)
