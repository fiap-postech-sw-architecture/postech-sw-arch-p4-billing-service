"""Preparacao do banco do Billing: ``python -m src.banco`` (idempotente).

Cria colecoes com validador ``$jsonSchema`` (nivel ``moderate``) e indices de
cada repositorio, e marca a versao. Roda antes da API e do ``prazos``: servico
``init`` no compose e Job de inicializacao no Kubernetes (ADR-037); so
mudancas aditivas. API e ``prazos`` so conferem a versao (readiness e boot). O
``rs.initiate`` do replica set fica com quem sobe o MongoDB.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from src.compartilhado.infraestrutura.mongo import criar_cliente, marcar_versao
from src.compartilhado.infraestrutura.unit_of_work import preparar_outbox
from src.configuracao import ConfiguracaoDoBanco
from src.orcamento.infraestrutura.repository import preparar_orcamentos
from src.pagamento.infraestrutura.repository import preparar_pagamentos
from src.precos.infraestrutura.repository import preparar_precos

if TYPE_CHECKING:
    from pymongo.database import Database

    from src.compartilhado.infraestrutura.mongo import Documento


def preparar_banco(banco: Database[Documento]) -> None:
    preparar_precos(banco)
    preparar_orcamentos(banco)
    preparar_pagamentos(banco)
    preparar_outbox(banco)
    marcar_versao(banco)


def main() -> None:
    config = ConfiguracaoDoBanco.do_ambiente()
    cliente = criar_cliente(config.mongodb_uri)
    try:
        preparar_banco(cliente[config.mongodb_banco])
    finally:
        cliente.close()
    sys.stdout.write(f"banco {config.mongodb_banco} preparado\n")


if __name__ == "__main__":  # pragma: no cover
    main()
