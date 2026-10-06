"""Simulador do provedor de pagamento: so registrado com ``MP_MODE=simulado``.

Fora desse modo as rotas nao existem (404). Faz o papel do cliente pagando no
checkout do provedor: sem login, mas so com o token do ``checkout_url``
(ADR-040), e o mesmo 404 para token ausente, invalido, expirado ou de outro
pagamento. A pagina fica fora de ``/api/v1`` (o Ingress expoe
``/simulador/checkout``, ADR-038); as acoes ficam em ``/api/v1/simulador``.
"""

from __future__ import annotations

import dataclasses
import html
from typing import Annotated
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse
from starlette.requests import Request

from src.pagamento.aplicacao.use_cases import SimularResultadoPagamento
from src.pagamento.interfaces.dependencies import obter_simular_resultado
from src.pagamento.interfaces.schemas import PagamentoResponse

CAMINHO_CHECKOUT = "/simulador/checkout"
_CAMINHO_ACOES = "/api/v1/simulador/pagamentos"

router = APIRouter(tags=["simulador"])

Simular = Annotated[SimularResultadoPagamento, Depends(obter_simular_resultado)]
Token = Annotated[
    str | None, Query(description="Token do checkout_url (assinado, com expiracao)")
]

_RESPOSTAS: dict[int | str, dict[str, object]] = {
    404: {"description": "Checkout nao encontrado ou expirado (token invalido)."},
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
<form method="post" action="{aprovar}">
<button type="submit">Aprovar</button></form>
<form method="post" action="{recusar}">
<button type="submit">Recusar</button></form>
</body>
</html>
"""


@router.get(
    CAMINHO_CHECKOUT + "/{pagamento_id}",
    response_class=HTMLResponse,
    summary="Pagina de checkout simulada (botoes Aprovar/Recusar)",
    responses={404: _RESPOSTAS[404]},
)
def checkout(
    pagamento_id: UUID, request: Request, simular: Simular, token: Token = None
) -> HTMLResponse:
    pagamento = simular.consultar(pagamento_id, token)
    base = f"{request.app.state.config.url_publica}{_CAMINHO_ACOES}/{pagamento.id}"
    consulta = f"?token={quote(token or '', safe='')}"
    return HTMLResponse(
        _PAGINA.format(
            pagamento_id=html.escape(str(pagamento.id)),
            moeda=html.escape(pagamento.moeda),
            valor=html.escape(str(pagamento.valor)),
            status=html.escape(pagamento.status),
            aprovar=html.escape(f"{base}/aprovar{consulta}"),
            recusar=html.escape(f"{base}/recusar{consulta}"),
        )
    )


@router.post(
    _CAMINHO_ACOES + "/{pagamento_id}/aprovar",
    summary="Simula o pagamento aprovado no provedor",
    responses=_RESPOSTAS,
)
def aprovar(
    pagamento_id: UUID, simular: Simular, token: Token = None
) -> PagamentoResponse:
    dto = simular.executar(pagamento_id, token=token, aprovar=True)
    return PagamentoResponse.model_validate(dataclasses.asdict(dto))


@router.post(
    _CAMINHO_ACOES + "/{pagamento_id}/recusar",
    summary="Simula o pagamento recusado no provedor",
    responses=_RESPOSTAS,
)
def recusar(
    pagamento_id: UUID, simular: Simular, token: Token = None
) -> PagamentoResponse:
    dto = simular.executar(pagamento_id, token=token, aprovar=False)
    return PagamentoResponse.model_validate(dataclasses.asdict(dto))
