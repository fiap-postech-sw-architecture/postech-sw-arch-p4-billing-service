"""Envelope de erro no OpenAPI e as respostas comuns das rotas autenticadas."""

from __future__ import annotations

from typing import Any, Final

from pydantic import BaseModel


class Erro(BaseModel):
    codigo: str
    mensagem: str
    id_requisicao: str


class ErroResponse(BaseModel):
    """``{"erro": {codigo, mensagem, id_requisicao}}``, o envelope de todo erro
    (o 422 de schema e a excecao: ``{detail, id_requisicao}``, formato do p3)."""

    erro: Erro


# Rotas com bearer token do OS Service (ADR-039): o 401 e um so para toda
# falha de credencial e o 503 vem do JWKS fora do ar.
RESPOSTAS_AUTENTICADAS: Final[dict[int | str, dict[str, Any]]] = {
    401: {
        "model": ErroResponse,
        "description": "Credencial ausente, invalida ou expirada (uma resposta "
        "para toda falha).",
    },
    403: {"model": ErroResponse, "description": "Papel sem permissao para a rota."},
    503: {
        "model": ErroResponse,
        "description": "JWKS do OS Service indisponivel; o header `Retry-After` "
        "traz os segundos ate a proxima tentativa.",
    },
}
