"""Preparacao do banco do Billing: ``python -m src.banco`` (idempotente).

Cria colecoes com validador ``$jsonSchema`` (nivel ``moderate``) e indices de
cada repositorio, e marca a versao. Roda antes da API e do ``prazos``: servico
``init`` no compose e Job de inicializacao no Kubernetes (ADR-037); so
mudancas aditivas. API e ``prazos`` so conferem a versao (readiness e boot). O
``rs.initiate`` do replica set e os usuarios ficam com quem sobe o MongoDB: o
healthcheck do compose e, no Kubernetes, o Job (``k8s/base/replica-set.js``).

``python -m src.banco aguardar`` espera essa versao: o initContainer dos
Deployments, que so sobem os processos depois do Job da versao deles (sem ele,
relay, consumidor e ``prazos`` caem no boot).
"""

from __future__ import annotations

import sys
import time
from typing import TYPE_CHECKING

from pymongo.errors import PyMongoError

from src.compartilhado.infraestrutura.mongo import (
    BancoNaoPreparadoError,
    conferir_versao,
    criar_cliente,
    marcar_versao,
)
from src.compartilhado.infraestrutura.unit_of_work import preparar_outbox
from src.configuracao import ConfiguracaoDoBanco
from src.orcamento.infraestrutura.repository import preparar_orcamentos
from src.pagamento.infraestrutura.repository import preparar_pagamentos
from src.precos.infraestrutura.repository import preparar_precos

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from pymongo.database import Database

    from src.compartilhado.infraestrutura.mongo import Documento


def preparar_banco(banco: Database[Documento]) -> None:
    preparar_precos(banco)
    preparar_orcamentos(banco)
    preparar_pagamentos(banco)
    preparar_outbox(banco)
    marcar_versao(banco)


def aguardar(
    banco: Database[Documento],
    *,
    intervalo: float = 2.0,
    esperar: Callable[[float], None] = time.sleep,
) -> None:
    """Espera o banco preparado nesta versao (o Job de inicializacao).

    Banco fora, usuario ainda nao criado e versao anterior sao espera: o prazo e
    o do rollout no Kubernetes. Erro que nao e do MongoDB sai na hora.
    """
    while True:
        try:
            conferir_versao(banco)
        except (BancoNaoPreparadoError, PyMongoError) as exc:
            sys.stdout.write(
                f"database {banco.name} not ready ({type(exc).__name__}), "
                f"retrying in {intervalo:g} s\n"
            )
            esperar(intervalo)
        else:
            return


def main(args: Sequence[str] = ()) -> None:
    """Sem argumento prepara o banco; com ``aguardar``, espera a preparacao."""
    if list(args) not in ([], ["aguardar"]):
        sys.exit("uso: python -m src.banco [aguardar]")
    config = ConfiguracaoDoBanco.do_ambiente()
    cliente = criar_cliente(config.mongodb_uri)
    try:
        banco = cliente[config.mongodb_banco]
        if args:
            aguardar(banco)
            sys.stdout.write(f"database {config.mongodb_banco} ready\n")
        else:
            preparar_banco(banco)
            sys.stdout.write(f"banco {config.mongodb_banco} preparado\n")
    finally:
        cliente.close()


if __name__ == "__main__":  # pragma: no cover
    main(sys.argv[1:])
