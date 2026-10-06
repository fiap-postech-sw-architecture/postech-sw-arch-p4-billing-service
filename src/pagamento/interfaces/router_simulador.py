"""Simulador do provedor de pagamento: so registrado com ``MP_MODE=simulado``.

Fora desse modo as rotas nao existem (404). Sem autenticacao: faz o papel do
cliente pagando no checkout do provedor.
"""

from __future__ import annotations

import dataclasses
import html
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from starlette.requests import Request

from src.pagamento.aplicacao.use_cases import (
    ConsultarPagamentos,
    SimularResultadoPagamento,
)
from src.pagamento.interfaces.dependencies import (
    obter_consultar_pagamentos,
    obter_simular_resultado,
)
from src.pagamento.interfaces.schemas import PagamentoResponse

router = APIRouter(tags=["simulador"])

Simular = Annotated[SimularResultadoPagamento, Depends(obter_simular_resultado)]

_RESPOSTAS: dict[int | str, dict[str, object]] = {
    404: {"description": "Pagamento nao encontrado."},
    409: {"description": "Pagamento ja processado."},
}

# Sem CSS nem JS inline (o CSP default-src 'none' do servico bloquearia);
# form-action nao herda default-src, entao os botoes funcionam.
_PAGINA = """<!doctype html>
<html lang="pt-BR">
<head><meta charset="utf-8"><title>Checkout simulado - PytStop</title></head>
<body>
<h1>Checkout simulado</h1>
<p>Ambiente de simulacao: nenhuma cobranca real e feita.</p>
<dl>
<dt>Pagamento</dt><dd>{pagamento_id}</dd>
<dt>Valor</dt><dd>{moeda} {valor}</dd>
<dt>Status</dt><dd>{status}</dd>
</dl>
<form method="post" action="{base}/aprovar">
<button type="submit">Aprovar</button></form>
<form method="post" action="{base}/recusar">
<button type="submit">Recusar</button></form>
</body>
</html>
"""


@router.get(
    "/simulador/checkout/{pagamento_id}",
    response_class=HTMLResponse,
    summary="Pagina de checkout simulada (botoes Aprovar/Recusar)",
    responses={404: {"description": "Pagamento nao encontrado."}},
)
def checkout(
    pagamento_id: UUID,
    request: Request,
    consulta: Annotated[ConsultarPagamentos, Depends(obter_consultar_pagamentos)],
) -> HTMLResponse:
    pagamento = consulta.por_id(pagamento_id)
    url_publica = request.app.state.config.url_publica
    return HTMLResponse(
        _PAGINA.format(
            pagamento_id=html.escape(str(pagamento.id)),
            moeda=html.escape(pagamento.moeda),
            valor=html.escape(str(pagamento.valor)),
            status=html.escape(pagamento.status),
            base=html.escape(
                f"{url_publica}/api/v1/simulador/pagamentos/{pagamento.id}"
            ),
        )
    )


@router.post(
    "/api/v1/simulador/pagamentos/{pagamento_id}/aprovar",
    summary="Simula o pagamento aprovado no provedor",
    responses=_RESPOSTAS,
)
def aprovar(pagamento_id: UUID, simular: Simular) -> PagamentoResponse:
    dto = simular.executar(pagamento_id, aprovar=True)
    return PagamentoResponse.model_validate(dataclasses.asdict(dto))


@router.post(
    "/api/v1/simulador/pagamentos/{pagamento_id}/recusar",
    summary="Simula o pagamento recusado no provedor",
    responses=_RESPOSTAS,
)
def recusar(pagamento_id: UUID, simular: Simular) -> PagamentoResponse:
    dto = simular.executar(pagamento_id, aprovar=False)
    return PagamentoResponse.model_validate(dataclasses.asdict(dto))
