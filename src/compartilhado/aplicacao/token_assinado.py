"""Token de acesso assinado com HMAC-SHA256 e expiracao (link e checkout).

Formato ``<id>.<exp>.<assinatura>``: ``exp`` em epoch de segundos e a
assinatura (base64url sem padding) calculada sobre ``<dominio>:<id>.<exp>``.
O dominio separa os usos da mesma chave: o token do link de decisao do
orcamento nao abre o checkout do simulador, e vice-versa. A assinatura e
conferida em tempo constante antes de qualquer parse.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import math
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from datetime import datetime

_PARTES_DO_TOKEN = 3


class TokenAssinado:
    def __init__(self, *, segredo: str, dominio: str) -> None:
        if not segredo:
            msg = "Segredo do token assinado nao configurado"
            raise ValueError(msg)
        self._chave = segredo.encode()
        self._dominio = dominio

    def emitir(self, recurso_id: UUID, expira_em: datetime) -> str:
        """Token que vale ate ``expira_em`` (arredondado para o segundo acima)."""
        corpo = f"{recurso_id}.{math.ceil(expira_em.timestamp())}"
        return f"{corpo}.{self._assinar(corpo)}"

    def validar(self, token: str, *, agora: datetime) -> UUID | None:
        """Id do recurso, ou ``None`` se o token e invalido ou ja expirou."""
        partes = token.split(".")
        if len(partes) != _PARTES_DO_TOKEN:
            return None
        id_texto, expiracao_texto, assinatura = partes
        esperada = self._assinar(f"{id_texto}.{expiracao_texto}")
        # Bytes, nao str: compare_digest com str nao-ASCII levanta TypeError.
        if not hmac.compare_digest(esperada.encode(), assinatura.encode()):
            return None
        try:
            recurso_id = UUID(id_texto)
            expiracao = int(expiracao_texto)
        except ValueError:
            return None
        return None if agora.timestamp() > expiracao else recurso_id

    def _assinar(self, corpo: str) -> str:
        mensagem = f"{self._dominio}:{corpo}".encode()
        digest = hmac.new(self._chave, mensagem, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
