"""Envelope de erro da API: ``{"erro": {codigo, mensagem, id_requisicao}}``.

Reaproveitado do p3 @ 08dcffe (``src/compartilhado/interfaces/error_handler.py``).
Acrescimos do Billing: 410 (link expirado), 503 (dependencia externa fora), o
handler de ``HTTPException`` (401/403 da autenticacao, 404/405 de roteamento) e
o 422 de schema no mesmo envelope (com ``detalhes``): o p3 mantinha
``{"detail": [...]}`` porque a UI lia esse formato; aqui os clientes sao os
outros servicos e todo erro sai igual.
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
    404: "NAO_ENCONTRADO",
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


def _criar_envelope(
    codigo: str,
    mensagem: str,
    request_id: str,
    detalhes: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    erro: dict[str, object] = {
        "codigo": codigo,
        "mensagem": mensagem,
        "id_requisicao": request_id,
    }
    if detalhes is not None:
        erro["detalhes"] = detalhes
    return {"erro": erro}


def registrar_error_handlers(app: FastAPI) -> None:
    """Registra os handlers que convertem excecoes no envelope de erro."""

    @app.exception_handler(DomainException)
    async def _domain_exception_handler(
        request: Request, exc: DomainException
    ) -> JSONResponse:
        request_id = _obter_request_id(request)
        status_code = _status_para(exc)
        # So o codigo estavel vai para o log, nunca a mensagem (pode ter dado
        # do request).
        logger.warning(
            "dominio_excecao_tratada",
            codigo=exc.codigo,
            status=status_code,
            request_id=request_id,
        )
        return JSONResponse(
            status_code=status_code,
            content=_criar_envelope(exc.codigo, exc.mensagem, request_id),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        request_id = _obter_request_id(request)
        codigo = _CODIGOS_HTTP.get(exc.status_code, f"HTTP_{exc.status_code}")
        mensagem = str(exc.detail)
        if mensagem == HTTPStatus(exc.status_code).phrase:
            mensagem = _MENSAGENS_PADRAO.get(exc.status_code, mensagem)
        logger.warning(
            "http_excecao_tratada",
            codigo=codigo,
            status=exc.status_code,
            request_id=request_id,
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=_criar_envelope(codigo, mensagem, request_id),
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _request_validation_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # O detail default do FastAPI ecoa o ``input`` cru de cada campo
        # invalido; cada item aqui carrega so type/loc/msg (a regra violada,
        # nao o valor recebido).
        request_id = _obter_request_id(request)
        detalhes = [
            {"type": erro.get("type"), "loc": erro.get("loc"), "msg": erro.get("msg")}
            for erro in exc.errors()
        ]
        logger.warning(
            "validacao_schema_tratada_422",
            request_id=request_id,
            erros=[(d["type"], d["loc"]) for d in detalhes],
        )
        return JSONResponse(
            status_code=422,
            content=_criar_envelope(
                "REQUISICAO_INVALIDA", "Requisicao invalida", request_id, detalhes
            ),
        )

    @app.exception_handler(ValueError)
    async def _value_error_handler(request: Request, exc: ValueError) -> JSONResponse:
        # Invariante de value object/agregado violada: 422 com a mensagem do
        # dominio, redigida de PII.
        request_id = _obter_request_id(request)
        logger.warning("value_error_tratado_422", request_id=request_id)
        return JSONResponse(
            status_code=422,
            content=_criar_envelope(
                "VALOR_INVALIDO", redigir_pii_erro(str(exc)), request_id
            ),
        )

    @app.exception_handler(Exception)
    async def _generic_exception_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        request_id = _obter_request_id(request)
        logger.exception("erro_interno", request_id=request_id)
        return JSONResponse(
            status_code=500,
            content=_criar_envelope(
                "ERRO_INTERNO", "Erro interno do servidor", request_id
            ),
        )
