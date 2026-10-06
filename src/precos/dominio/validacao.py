"""Servico de dominio: validacao de itens contra a tabela de precos.

Usado pela validacao sincrona da Execucao (``POST /api/v1/precos/validacao``,
RFC-004 §5) e pela geracao do orcamento. Recebe os precos ja carregados; nao
conhece repositorio.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from src.precos.dominio.preco import PrecoPeca, PrecoServico


def codigos_invalidos(
    *,
    servicos_solicitados: Iterable[str],
    pecas_solicitadas: Iterable[str],
    servicos: Mapping[str, PrecoServico],
    pecas: Mapping[str, PrecoPeca],
) -> list[str]:
    """Codigos inexistentes ou inativos, na ordem pedida e sem repeticao.

    Servicos primeiro, depois pecas. ``servicos``/``pecas`` sao os precos
    encontrados, indexados por codigo e por sku.
    """
    invalidos: list[str] = []
    candidatos: list[tuple[str, PrecoServico | PrecoPeca | None]] = [
        (codigo, servicos.get(codigo)) for codigo in servicos_solicitados
    ]
    candidatos += [(sku, pecas.get(sku)) for sku in pecas_solicitadas]
    for codigo, preco in candidatos:
        if (preco is None or not preco.ativo) and codigo not in invalidos:
            invalidos.append(codigo)
    return invalidos
