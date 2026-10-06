"""Preparacao do banco: indices de todas as colecoes (idempotente).

Chamado no boot da API, do processo de prazos e do seed, e pelos testes de
integracao. Cada repositorio declara os indices das suas consultas.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.compartilhado.infraestrutura.unit_of_work import criar_indices_outbox
from src.orcamento.infraestrutura.repository import criar_indices_orcamentos
from src.pagamento.infraestrutura.repository import criar_indices_pagamentos
from src.precos.infraestrutura.repository import criar_indices_precos

if TYPE_CHECKING:
    from pymongo.database import Database

    from src.compartilhado.infraestrutura.mongo import Documento


def preparar_banco(banco: Database[Documento]) -> None:
    criar_indices_precos(banco)
    criar_indices_orcamentos(banco)
    criar_indices_pagamentos(banco)
    criar_indices_outbox(banco)
