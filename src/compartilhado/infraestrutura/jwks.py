"""Validacao dos JWT RS256 emitidos pelo OS Service (ADR-039, RFC-004 secao 8).

Mesmo desenho do Execution Service (base tecnica copiada por servico). A chave
publica vem do JWKS do emissor (``JWKS_URL``, timeout de 2 s). O JWK Set fica
fresco por 10 min; quando a renovacao falha, o ultimo JWK Set bom continua
valendo por ate 1 h (stale-if-error). Um request por vez busca o JWKS, e os
demais nao esperam por ele (com copia em cache seguem com ela); a falha fica
memorizada por 5 s, e 3 falhas seguidas abrem o circuit breaker por 30 s, sem
busca nem lock. Assim um OS pendurado nao enfileira os requests a 2 s cada nem
esgota o threadpool (as rotas publicas seguem respondendo). Sem JWK Set
utilizavel: ``JwksIndisponivelError`` (503 com ``Retry-After``), nunca 401.
``leeway`` de 10 s cobre relogios dessincronizados entre os pods.
"""

from __future__ import annotations

import math
import threading
import time
from typing import TYPE_CHECKING, Any, Final, NamedTuple

import jwt
import structlog
from jwt import PyJWKClient
from jwt.exceptions import ExpiredSignatureError, InvalidTokenError, PyJWTError
from prometheus_client import Counter

from src.compartilhado.infraestrutura.circuit_breaker import CircuitBreaker

if TYPE_CHECKING:
    from collections.abc import Callable

    from jwt import PyJWK

LEEWAY_SEGUNDOS: Final = 10
FRESCO_SEGUNDOS: Final = 600
VELHO_MAXIMO_SEGUNDOS: Final = 3600
MEMORIA_DA_FALHA_SEGUNDOS: Final = 5
TIMEOUT_JWKS_SEGUNDOS: Final = 2
# Token com kid desconhecido forca no maximo uma busca a cada 30 s (rotacao de
# chave sem abrir caminho para um flood de buscas com kid aleatorio).
_INTERVALO_BUSCA_POR_KID_SEGUNDOS: Final = 30
_FALHAS_PARA_ABRIR: Final = 3
_SEGUNDOS_ABERTO: Final = 30

_log = structlog.get_logger(__name__)
JWKS_FALHAS = Counter(
    "pytstop_jwks_falhas", "Buscas ao JWKS do OS Service que falharam."
)


class TokenInvalidoError(Exception):
    """Formato, assinatura, ``iss``, ``aud``, claims ou ``kid`` invalidos."""


class TokenExpiradoError(TokenInvalidoError):
    """``exp`` vencido, alem do ``leeway``."""


class JwksIndisponivelError(Exception):
    """Sem JWK Set utilizavel: o JWKS nao respondeu (ou veio sem chave de
    assinatura) e nao ha copia de ate 1 h. ``retry_after``: segundos ate a
    proxima busca."""

    def __init__(self, mensagem: str, *, retry_after: int) -> None:
        super().__init__(mensagem)
        self.retry_after = retry_after


class _Copia(NamedTuple):
    """Ultimo JWK Set bom (so as chaves de assinatura) e quando chegou."""

    chaves: tuple[PyJWK, ...]
    obtida_em: float


class ValidadorDeTokenJWKS:
    """Valida tokens do OS Service; uma instancia por processo (``app.state``)."""

    def __init__(
        self,
        jwks_url: str,
        *,
        emissor: str,
        audiencia: str,
        relogio: Callable[[], float] = time.monotonic,
    ) -> None:
        # Sem o cache do PyJWKClient (ele segura um lock durante o fetch):
        # frescor, copia velha, memoria da falha e breaker ficam aqui.
        self._cliente = PyJWKClient(
            jwks_url, cache_jwk_set=False, timeout=TIMEOUT_JWKS_SEGUNDOS
        )
        self._emissor = emissor
        self._audiencia = audiencia
        self._relogio = relogio
        self._breaker = CircuitBreaker(
            "jwks",
            limite_falhas=_FALHAS_PARA_ABRIR,
            segundos_aberto=_SEGUNDOS_ABERTO,
            relogio=relogio,
        )
        self._busca = threading.Lock()  # uma busca ao emissor por vez
        self._copia: _Copia | None = None  # trocada inteira: leitura sem lock
        self._falhou_em: float | None = None

    def validar(self, token: str) -> dict[str, Any]:
        """Devolve as claims de um token valido.

        Raises:
            TokenExpiradoError: ``exp`` vencido.
            TokenInvalidoError: formato, assinatura, ``iss``, ``aud``, claims
                obrigatorias ou ``kid`` invalidos.
            JwksIndisponivelError: sem JWK Set utilizavel.
        """
        try:
            kid = jwt.get_unverified_header(token).get("kid")
        except InvalidTokenError as exc:
            raise TokenInvalidoError(str(exc)) from exc
        chave = self._chave(kid)
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                chave,
                algorithms=["RS256"],
                issuer=self._emissor,
                audience=self._audiencia,
                leeway=LEEWAY_SEGUNDOS,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except ExpiredSignatureError as exc:
            raise TokenExpiradoError(str(exc)) from exc
        except InvalidTokenError as exc:
            raise TokenInvalidoError(str(exc)) from exc
        return claims

    def _chave(self, kid: object) -> PyJWK:
        chave = _por_kid(self._chaves_de_assinatura(renovar=False), kid)
        if chave is None and self._pode_buscar_por_kid():
            chave = _por_kid(self._chaves_de_assinatura(renovar=True), kid)
        if chave is None:
            msg = "kid desconhecido no JWKS do emissor"
            raise TokenInvalidoError(msg)
        return chave

    def _pode_buscar_por_kid(self) -> bool:
        copia = self._copia
        idade = math.inf if copia is None else self._relogio() - copia.obtida_em
        return idade >= _INTERVALO_BUSCA_POR_KID_SEGUNDOS

    def _chaves_de_assinatura(self, *, renovar: bool) -> tuple[PyJWK, ...]:
        if (fresca := self._copia_fresca(renovar=renovar)) is not None:
            return fresca
        agora = self._relogio()
        if self._breaker.barrado() or self._falha_recente(agora):
            return self._copia_velha(agora)
        # Com copia em cache ninguem espera a busca de outro request; sem
        # nenhuma (boot), espera no maximo o timeout de uma busca.
        if self._copia is not None:
            conseguiu = self._busca.acquire(blocking=False)
        else:
            conseguiu = self._busca.acquire(timeout=TIMEOUT_JWKS_SEGUNDOS)
        if not conseguiu:
            return self._copia_velha(agora)
        try:
            return self._buscar(renovar=renovar)
        finally:
            self._busca.release()

    def _buscar(self, *, renovar: bool) -> tuple[PyJWK, ...]:
        # Outro request pode ter renovado ou falhado enquanto este esperava.
        if (fresca := self._copia_fresca(renovar=renovar)) is not None:
            return fresca
        if self._falha_recente(self._relogio()) or not self._breaker.permitir():
            return self._copia_velha(self._relogio())
        try:
            chaves = tuple(self._cliente.get_signing_keys())
        except (PyJWTError, ValueError, OSError) as exc:
            # ValueError: corpo que nao e JSON (o PyJWKClient deixa passar o
            # JSONDecodeError); OSError: conexao derrubada no meio do corpo.
            # Sem chaves ou so com chave de cifra: PyJWTError (PyJWKSetError).
            self._falhou_em = self._relogio()
            self._breaker.registrar_falha()
            JWKS_FALHAS.inc()
            _log.warning("jwks_refresh_failed", erro=type(exc).__name__)
            return self._copia_velha(self._falhou_em)
        self._breaker.registrar_sucesso()
        self._copia, self._falhou_em = _Copia(chaves, self._relogio()), None
        return chaves

    def _copia_fresca(self, *, renovar: bool) -> tuple[PyJWK, ...] | None:
        copia = self._copia
        if renovar or copia is None:
            return None
        if self._relogio() - copia.obtida_em >= FRESCO_SEGUNDOS:
            return None
        return copia.chaves

    def _falha_recente(self, agora: float) -> bool:
        return (
            self._falhou_em is not None
            and agora - self._falhou_em < MEMORIA_DA_FALHA_SEGUNDOS
        )

    def _copia_velha(self, agora: float) -> tuple[PyJWK, ...]:
        copia = self._copia
        if copia is not None and agora - copia.obtida_em < VELHO_MAXIMO_SEGUNDOS:
            return copia.chaves
        memoria = (
            0.0
            if self._falhou_em is None
            else MEMORIA_DA_FALHA_SEGUNDOS - (agora - self._falhou_em)
        )
        espera = max(memoria, self._breaker.segundos_para_nova_tentativa())
        msg = "JWKS do emissor indisponivel e sem copia valida em cache"
        raise JwksIndisponivelError(msg, retry_after=max(1, math.ceil(espera)))


def _por_kid(chaves: tuple[PyJWK, ...], kid: object) -> PyJWK | None:
    return next((chave for chave in chaves if chave.key_id == kid), None)
