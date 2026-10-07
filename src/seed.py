"""Seed de demonstracao da tabela de precos (``python -m src.seed``).

Idempotente: so cadastra o que falta; preco ja existente (inclusive alterado
pelo admin) nao e sobrescrito. Os SKUs das pecas sao os do estoque da
Execucao (a mesma peca nos dois servicos, ligada pelo ``sku``).
"""

from __future__ import annotations

import sys
from decimal import Decimal
from typing import TYPE_CHECKING

from src.compartilhado.infraestrutura.mongo import conferir_versao, criar_cliente
from src.compartilhado.infraestrutura.unit_of_work import MongoUnitOfWork
from src.configuracao import ConfiguracaoDoBanco
from src.precos.aplicacao.use_cases import PrecosDePecas, PrecosDeServicos
from src.precos.dominio.exceptions import PrecoJaCadastradoError
from src.precos.infraestrutura.repository import (
    MongoPrecoPecaRepository,
    MongoPrecoServicoRepository,
)

if TYPE_CHECKING:
    from pymongo.database import Database

    from src.compartilhado.infraestrutura.mongo import Documento

SERVICOS: tuple[tuple[str, str, str, Decimal], ...] = (
    (
        "SRV-TROCA-OLEO",
        "Troca de oleo",
        "Troca do oleo do motor e verificacao de niveis",
        Decimal("120.00"),
    ),
    (
        "SRV-ALINHAMENTO",
        "Alinhamento e balanceamento",
        "Alinhamento da direcao e balanceamento das quatro rodas",
        Decimal("180.00"),
    ),
    (
        "SRV-FREIOS",
        "Revisao do sistema de freios",
        "Inspecao de pastilhas, discos, fluido e regulagem do freio",
        Decimal("250.00"),
    ),
    (
        "SRV-DIAGNOSTICO",
        "Diagnostico eletronico",
        "Leitura de falhas da central eletronica com scanner",
        Decimal("150.00"),
    ),
    (
        "SRV-SUSPENSAO",
        "Revisao de suspensao",
        "Inspecao de amortecedores, molas, buchas e terminais",
        Decimal("320.00"),
    ),
)

PECAS: tuple[tuple[str, str, Decimal], ...] = (
    ("PEC-OLEO-5W30", "Oleo de motor 5W30 (1 L)", Decimal("45.00")),
    ("PEC-FILTRO-OLEO", "Filtro de oleo", Decimal("35.00")),
    ("PEC-PASTILHA-FREIO", "Jogo de pastilhas de freio", Decimal("160.00")),
    ("PEC-DISCO-FREIO", "Disco de freio", Decimal("220.00")),
    ("PEC-AMORTECEDOR", "Amortecedor", Decimal("390.00")),
    ("PEC-VELA", "Vela de ignicao", Decimal("28.00")),
)


def semear(banco: Database[Documento]) -> tuple[int, int]:
    """Cadastra os precos que faltam; devolve (criados, ja existentes)."""
    uow = MongoUnitOfWork(banco)
    servicos = PrecosDeServicos(uow, MongoPrecoServicoRepository(uow))
    pecas = PrecosDePecas(uow, MongoPrecoPecaRepository(uow))
    criados = existentes = 0
    for codigo, nome, descricao, preco in SERVICOS:
        try:
            servicos.cadastrar(
                codigo=codigo, nome=nome, descricao=descricao, preco=preco
            )
            criados += 1
        except PrecoJaCadastradoError:
            existentes += 1
    for sku, nome, preco in PECAS:
        try:
            pecas.cadastrar(sku=sku, nome=nome, preco=preco)
            criados += 1
        except PrecoJaCadastradoError:
            existentes += 1
    return criados, existentes


def main() -> None:
    config = ConfiguracaoDoBanco.do_ambiente()
    cliente = criar_cliente(config.mongodb_uri)
    try:
        banco = cliente[config.mongodb_banco]
        # Os indices unicos de codigo e sku (do init) fazem o seed idempotente.
        conferir_versao(banco)
        criados, existentes = semear(banco)
    finally:
        cliente.close()
    sys.stdout.write(f"seed de precos: {criados} criados, {existentes} ja existiam\n")


if __name__ == "__main__":  # pragma: no cover
    main()
