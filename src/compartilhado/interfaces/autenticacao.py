"""Autenticacao por JWT RS256 emitido pelo OS Service (RFC-004 §7).

O Billing so valida: assinatura pela chave publica do JWKS do OS Service
(cache de 10 min, timeout de 2s), ``iss``, ``aud`` e ``exp``. Sem segredo
compartilhado. Revogacao (logout) vale so no OS; aqui o limite e a expiracao
curta do token (tradeoff registrado na RFC-004).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated

import jwt
import structlog
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

# Runtime (nao TYPE_CHECKING): o FastAPI le a anotacao da dependency.
from starlette.requests import Request  # noqa: TC002

if TYPE_CHECKING:
    from collections.abc import Callable

_log = structlog.get_logger(__name__)

_CACHE_JWKS_SEGUNDOS = 600
_TIMEOUT_JWKS_SEGUNDOS = 2
# Diferenca de relogio entre os pods do OS e do Billing (exp, iat e nbf).
_TOLERANCIA_RELOGIO = timedelta(seconds=30)
_CABECALHO_BEARER = {"WWW-Authenticate": "Bearer"}


class Papel(StrEnum):
    ADMIN = "admin"
    ATENDENTE = "atendente"
    MECANICO = "mecanico"


@dataclass(frozen=True, slots=True)
class UsuarioAutenticado:
    sub: str
    papel: Papel


def _nao_autenticado(mensagem: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=mensagem,
        headers=_CABECALHO_BEARER,
    )


class VerificadorDeToken:
    def __init__(self, *, jwks_url: str, emissor: str, audiencia: str) -> None:
        self._jwks = jwt.PyJWKClient(
            jwks_url,
            cache_jwk_set=True,
            lifespan=_CACHE_JWKS_SEGUNDOS,
            timeout=_TIMEOUT_JWKS_SEGUNDOS,
        )
        self._emissor = emissor
        self._audiencia = audiencia

    def verificar(self, token: str) -> UsuarioAutenticado:
        """Valida o token e devolve o usuario; levanta ``HTTPException``.

        401 para token ausente, malformado, expirado, de outro emissor ou
        audiencia, ou que nao seja de acesso; 503 quando o JWKS do OS Service
        nao responde (o token pode ser valido, o Billing e que nao consegue
        provar); 403 para papel desconhecido.
        """
        try:
            chave = self._jwks.get_signing_key_from_jwt(token)
        except jwt.PyJWKClientConnectionError:
            _log.warning("jwks_indisponivel")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Servico de autenticacao indisponivel; tente novamente",
            ) from None
        except (jwt.PyJWKClientError, jwt.InvalidTokenError):
            raise _nao_autenticado("Token invalido") from None
        try:
            claims = jwt.decode(
                token,
                chave.key,
                algorithms=["RS256"],
                audience=self._audiencia,
                issuer=self._emissor,
                options={"require": ["exp", "iss", "aud", "sub"]},
                leeway=_TOLERANCIA_RELOGIO,
            )
        except jwt.ExpiredSignatureError:
            raise _nao_autenticado("Token expirado") from None
        except jwt.InvalidTokenError:
            raise _nao_autenticado("Token invalido") from None
        # Refresh token do OS (``type=refresh``) nao autentica requisicao.
        tipo = claims.get("type")
        if tipo is not None and tipo != "access":
            raise _nao_autenticado("Token nao e do tipo access")
        papel = claims.get("papel")
        if not isinstance(papel, str) or papel not in set(Papel):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Papel nao autorizado",
            )
        return UsuarioAutenticado(sub=str(claims["sub"]), papel=Papel(papel))


_bearer = HTTPBearer(auto_error=False)


def exigir_papel(*papeis: Papel) -> Callable[..., UsuarioAutenticado]:
    """Dependency que exige um dos ``papeis``; ``admin`` passa em todas."""
    permitidos = frozenset({Papel.ADMIN, *papeis})

    def verificar(
        request: Request,
        credenciais: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    ) -> UsuarioAutenticado:
        if credenciais is None:
            raise _nao_autenticado("Token de autenticacao nao fornecido")
        verificador: VerificadorDeToken = request.app.state.verificador_de_token
        usuario = verificador.verificar(credenciais.credentials)
        if usuario.papel not in permitidos:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Papel nao autorizado",
            )
        return usuario

    return verificar
