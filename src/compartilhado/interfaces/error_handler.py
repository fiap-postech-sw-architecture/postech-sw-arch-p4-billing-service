"""Envelope de erro da API: ``{"erro": {codigo, mensagem, id_requisicao}}``.

Reaproveitado do p3 @ 08dcffe (``src/compartilhado/interfaces/error_handler.py``),
com a convencao comum aos servicos da fase 4 (OS, Execucao e Billing): o 422
de schema mantem o formato do p3 (``{"detail": [...], "id_requisicao"}``) e o
codigo generico de nao encontrado e ``ENTIDADE_NAO_ENCONTRADA``. Acrescimos do
Billing: 410 (recurso expirado), 503 (dependencia externa fora) e o handler de
``HTTPException`` (401/403 da autenticacao, 404/405 de roteamento).

Os handlers sao ``async`` de proposito, sem ``await`` (por isso o NOSONAR da
regra S7503): o Starlette chama handler async direto no event loop; um sync
iria para o threadpool, e com o pool cheio ate a resposta de erro esperaria
uma thread.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING

import structlog
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from src.compartilhado.dominio.exceptions import (
    DependenciaIndisponivelError,
    DomainException,
    EntidadeDuplicadaError,
    EntidadeNaoEncontradaError,
    RecursoExpiradoError,
    TransicaoStatusInvalidaError,
    ValorInvalidoError,
)
from src.compartilhado.infraestrutura.logging import redigir_pii_erro

if TYPE_CHECKING:
    from fastapi import FastAPI
    from starlette.requests import Request

logger = structlog.get_logger(__name__)

_EXCEPTION_STATUS_MAP: dict[type[DomainException], int] = {
    EntidadeNaoEncontradaError: 404,
    EntidadeDuplicadaError: 409,
    TransicaoStatusInvalidaError: 409,
    RecursoExpiradoError: 410,
    DependenciaIndisponivelError: 503,
}

# DomainException fora do mapa e, por definicao, regra de negocio violada:
# 409 Conflict (nunca 500: a excecao e esperada e carrega codigo proprio).
_STATUS_DEFAULT = 409

_CODIGOS_HTTP: dict[int, str] = {
    401: "NAO_AUTENTICADO",
    403: "ACESSO_NEGADO",
    # Mesmo codigo do 404 de dominio: o cliente trata "nao encontrado" de um jeito so.
    404: "ENTIDADE_NAO_ENCONTRADA",
    405: "METODO_NAO_PERMITIDO",
    503: "SERVICO_INDISPONIVEL",
}

# Mensagem em portugues quando o Starlette usa a frase padrao em ingles
# (rota inexistente, metodo errado).
_MENSAGENS_PADRAO: dict[int, str] = {
    404: "Recurso nao encontrado",
    405: "Metodo nao permitido",
}


def _status_para(exc: DomainException) -> int:
    """Status pela hierarquia da excecao (subclasse mapeada vence o ancestral)."""
    for classe in type(exc).__mro__:
        code = _EXCEPTION_STATUS_MAP.get(classe)
        if code is not None:
            return code
    return _STATUS_DEFAULT


def _obter_request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "desconhecido")


def _criar_envelope(codigo: str, mensagem: str, request_id: str) -> dict[str, object]:
    return {
        "erro": {
            "codigo": codigo,
            "mensagem": mensagem,
            "id_requisicao": request_id,
        }
    }


def _mensagem_http(exc: StarletteHTTPException) -> str:
    detalhe = str(exc.detail)
    if detalhe == HTTPStatus(exc.status_code).phrase:
        return _MENSAGENS_PADRAO.get(exc.status_code, detalhe)
    return detalhe


async def _domain_exception_handler(  # NOSONAR - async de proposito
    request: Request, exc: DomainException
) -> JSONResponse:
    request_id = _obter_request_id(request)
    status_code = _status_para(exc)
    # So o codigo estavel vai para o log, nunca a mensagem (pode ter dado do
    # request).
    logger.warning(
        "domain_exception_handled",
        codigo=exc.codigo,
        status=status_code,
        request_id=request_id,
    )
    return JSONResponse(
        status_code=status_code,
        content=_criar_envelope(exc.codigo, exc.mensagem, request_id),
    )


async def _http_exception_handler(  # NOSONAR - async de proposito
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    request_id = _obter_request_id(request)
    codigo = _CODIGOS_HTTP.get(exc.status_code, f"HTTP_{exc.status_code}")
    logger.warning(
        "http_exception_handled",
        codigo=codigo,
        status=exc.status_code,
        request_id=request_id,
    )
    return JSONResponse(
        status_code=exc.status_code,
        content=_criar_envelope(codigo, _mensagem_http(exc), request_id),
        headers=exc.headers,
    )


async def _request_validation_handler(  # NOSONAR - async de proposito
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    # O detail default do FastAPI ecoa o ``input`` cru de cada campo invalido;
    # cada item aqui carrega so type/loc/msg (a regra violada, nao o valor).
    request_id = _obter_request_id(request)
    detalhes = [
        {"type": erro.get("type"), "loc": erro.get("loc"), "msg": erro.get("msg")}
        for erro in exc.errors()
    ]
    logger.warning(
        "request_validation_handled",
        request_id=request_id,
        erros=[(d["type"], d["loc"]) for d in detalhes],
    )
    return JSONResponse(
        status_code=422,
        content={"detail": detalhes, "id_requisicao": request_id},
    )


async def _valor_invalido_handler(  # NOSONAR - async de proposito
    request: Request, exc: ValorInvalidoError
) -> JSONResponse:
    # Invariante de value object/agregado violada pela entrada: 422 com a
    # mensagem do dominio, redigida de PII. O log leva so o request_id.
    request_id = _obter_request_id(request)
    logger.warning("invalid_value_handled", request_id=request_id)
    return JSONResponse(
        status_code=422,
        content=_criar_envelope(
            "VALOR_INVALIDO", redigir_pii_erro(str(exc)), request_id
        ),
    )


async def _generic_exception_handler(  # NOSONAR - async de proposito
    request: Request, exc: Exception
) -> JSONResponse:
    # Rede de seguranca: o SecurityHeadersMiddleware ja converte o erro das
    # rotas; aqui so chega o que escapar de um middleware mais externo.
    return resposta_erro_interno(request, exc)


def resposta_erro_interno(request: Request, exc: Exception) -> JSONResponse:
    """500 no envelope; o traceback vai para o log, que passa pelo scrub de PII."""
    request_id = _obter_request_id(request)
    logger.error("internal_error", request_id=request_id, exc_info=exc)
    return JSONResponse(
        status_code=500,
        content=_criar_envelope("ERRO_INTERNO", "Erro interno do servidor", request_id),
    )


def registrar_error_handlers(app: FastAPI) -> None:
    """Mapeia excecoes para o envelope de erro.

    ``DomainException`` vira 404/409/410/503 pelo mapa; ``HTTPException``
    (autenticacao, rota inexistente) mantem status e headers;
    ``ValorInvalidoError`` vira 422 ``VALOR_INVALIDO``; o resto, inclusive
    ``ValueError`` de biblioteca ou de adapter, vira 500 com traceback no log.
    """
    app.exception_handler(DomainException)(_domain_exception_handler)
    app.exception_handler(StarletteHTTPException)(_http_exception_handler)
    app.exception_handler(RequestValidationError)(_request_validation_handler)
    app.exception_handler(ValorInvalidoError)(_valor_invalido_handler)
    app.exception_handler(Exception)(_generic_exception_handler)
