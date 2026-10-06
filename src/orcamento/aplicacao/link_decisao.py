"""Link publico de decisao do orcamento, assinado com HMAC-SHA256.

Token = ``<orcamento_id>.<expiracao_epoch>.<assinatura>``, com a assinatura
(base64url, sem padding) calculada sobre ``<orcamento_id>.<expiracao_epoch>``
com o segredo ``ORCAMENTO_LINK_SECRET``. Quem tem o link decide o orcamento,
entao o token e uma credencial: nao vai para log (mascarado no scrubber) e a
validacao compara em tempo constante e checa a expiracao.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from typing import TYPE_CHECKING
from uuid import UUID

from src.orcamento.dominio.exceptions import (
    LinkDeDecisaoExpiradoError,
    LinkDeDecisaoInvalidoError,
)

if TYPE_CHECKING:
    from datetime import datetime

CAMINHO_PUBLICO = "/api/v1/publico/orcamentos"
_PARTES_DO_TOKEN = 3


class LinkDeDecisao:
    def __init__(self, *, segredo: str, url_base: str) -> None:
        if not segredo:
            msg = "Segredo do link de decisao nao configurado"
            raise ValueError(msg)
        self._chave = segredo.encode()
        self._url_base = url_base.rstrip("/")

    def gerar(self, orcamento_id: UUID, expira_em: datetime) -> str:
        """URL completa que o cliente recebe (expira junto com o orcamento)."""
        return (
            f"{self._url_base}{CAMINHO_PUBLICO}/{self.token(orcamento_id, expira_em)}"
        )

    def token(self, orcamento_id: UUID, expira_em: datetime) -> str:
        corpo = f"{orcamento_id}.{int(expira_em.timestamp())}"
        return f"{corpo}.{self._assinar(corpo)}"

    def validar(self, token: str, *, agora: datetime) -> UUID:
        """Devolve o id do orcamento ou levanta erro de link invalido/expirado.

        A assinatura e conferida antes de qualquer parse: conteudo que nao foi
        emitido por este servico nem chega a ser interpretado.
        """
        partes = token.split(".")
        if len(partes) != _PARTES_DO_TOKEN:
            raise LinkDeDecisaoInvalidoError
        id_texto, expiracao_texto, assinatura = partes
        esperada = self._assinar(f"{id_texto}.{expiracao_texto}")
        # Bytes, nao str: compare_digest com str nao-ASCII levanta TypeError.
        if not hmac.compare_digest(esperada.encode(), assinatura.encode()):
            raise LinkDeDecisaoInvalidoError
        try:
            orcamento_id = UUID(id_texto)
            expiracao = int(expiracao_texto)
        except ValueError:
            raise LinkDeDecisaoInvalidoError from None
        if agora.timestamp() > expiracao:
            raise LinkDeDecisaoExpiradoError
        return orcamento_id

    def _assinar(self, corpo: str) -> str:
        digest = hmac.new(self._chave, corpo.encode(), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
