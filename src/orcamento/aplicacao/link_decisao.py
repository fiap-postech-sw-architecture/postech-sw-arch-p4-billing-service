"""Link publico de decisao do orcamento (ADR-039, RFC-004 secao 8).

O token (``TokenAssinado``, dominio proprio) assina ``orcamento_id`` e
``exp`` = ``valido_ate``. Quem tem o link decide o orcamento, entao o token e
uma credencial: nao vai para log (mascarado no scrubber) e qualquer falha
(adulterado, expirado) e o mesmo ``LinkDeDecisaoInvalidoError`` (404).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from src.compartilhado.aplicacao.token_assinado import TokenAssinado
from src.orcamento.dominio.exceptions import LinkDeDecisaoInvalidoError

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

_DOMINIO_DE_ASSINATURA = "link-de-decisao"
# Caminho da rota publica que o link abre: a API a monta (router_publico) e o
# consumidor dos comandos gera os links com ele, sem carregar a pilha HTTP.
CAMINHO_DO_LINK: Final = "/api/v1/publico/orcamentos"


class LinkDeDecisao:
    def __init__(self, *, segredo: str, url_base: str) -> None:
        """``url_base``: a URL publica do Billing seguida de ``CAMINHO_DO_LINK``."""
        self._token = TokenAssinado(segredo=segredo, dominio=_DOMINIO_DE_ASSINATURA)
        self._url_base = url_base.rstrip("/")

    def gerar(self, orcamento_id: UUID, expira_em: datetime) -> str:
        """URL completa que o cliente recebe (expira junto com o orcamento)."""
        return f"{self._url_base}/{self._token.emitir(orcamento_id, expira_em)}"

    def validar(self, token: str, *, agora: datetime) -> UUID:
        """Id do orcamento; token invalido ou expirado e o mesmo erro."""
        orcamento_id = self._token.validar(token, agora=agora)
        if orcamento_id is None:
            raise LinkDeDecisaoInvalidoError
        return orcamento_id
