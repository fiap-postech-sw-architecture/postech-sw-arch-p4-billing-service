"""Value objects da cobranca no provedor e do que ele informa sobre ela."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.compartilhado.dominio.value_object import ValueObject

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from src.compartilhado.dominio.dinheiro import Dinheiro
    from src.pagamento.dominio.estados import StatusNoProvedor


def exigir_timezone(rotulo: str, instante: datetime) -> None:
    if instante.tzinfo is None:
        msg = f"{rotulo} precisa de timezone (UTC)"
        raise ValueError(msg)


def exigir_texto(rotulo: str, valor: str) -> None:
    if not valor.strip():
        msg = f"{rotulo} nao pode ser vazio"
        raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class Cobranca(ValueObject):
    """Cobranca aberta no provedor para o orcamento aprovado (checkout)."""

    orcamento_id: UUID
    valor: Dinheiro
    provedor: str
    referencia_preferencia: str
    checkout_url: str
    expira_em: datetime

    def __post_init__(self) -> None:
        if self.valor.valor <= 0:
            msg = f"Valor do pagamento deve ser maior que zero: {self.valor.valor}"
            raise ValueError(msg)
        exigir_texto("Provedor", self.provedor)
        exigir_texto("Referencia da cobranca", self.referencia_preferencia)
        exigir_texto("checkout_url", self.checkout_url)
        exigir_timezone("expira_em", self.expira_em)


@dataclass(frozen=True, slots=True)
class SituacaoNoProvedor(ValueObject):
    """Tentativa de pagamento como o provedor a ve na consulta (fonte de verdade).

    ``referencia_externa`` e o id do nosso pagamento; ``status_provedor`` e o
    status bruto do provedor, que vai para o historico.
    """

    referencia: str
    referencia_externa: str | None
    status: StatusNoProvedor
    status_provedor: str
    detalhe: str | None
    valor: Dinheiro | None


@dataclass(frozen=True, slots=True)
class NotificacaoRecebida(ValueObject):
    """Historico: tentativa consultada no provedor e o status que ele deu."""

    recebida_em: datetime
    referencia_pagamento: str
    status_provedor: str

    def __post_init__(self) -> None:
        exigir_timezone("recebida_em", self.recebida_em)
        exigir_texto("Referencia do pagamento", self.referencia_pagamento)
        exigir_texto("Status do provedor", self.status_provedor)


@dataclass(frozen=True, slots=True)
class EstornoAutomatico(ValueObject):
    """Dinheiro devolvido sem pedido da saga: tentativa aprovada que a cobranca
    nao aceitava (encerrada, ou valor/moeda diferentes). ``falha`` preenchida =
    o provedor recusou o estorno e a devolucao fica para intervencao manual.
    """

    referencia_pagamento: str
    registrado_em: datetime
    falha: str | None = None

    def __post_init__(self) -> None:
        exigir_texto("Referencia do pagamento", self.referencia_pagamento)
        exigir_timezone("registrado_em", self.registrado_em)
