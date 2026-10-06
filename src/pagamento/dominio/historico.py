"""Historico do provedor: as tentativas que ele mostrou e os estornos automaticos.

Parte interna do agregado ``Pagamento``: so ele cria e muda um historico. A
consulta ao provedor (webhook ou conciliacao) pode repetir a mesma tentativa
quantas vezes quiser, entao o historico guarda cada par (tentativa, status do
provedor) uma vez e cada estorno automatico uma vez por tentativa; a decisao do
que isso muda no pagamento fica no agregado.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.compartilhado.dominio.value_object import ValueObject
from src.pagamento.dominio.cobranca import exigir_texto, exigir_timezone

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import datetime


@dataclass(frozen=True, slots=True)
class NotificacaoRecebida(ValueObject):
    """Tentativa consultada no provedor e o status que ele deu."""

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


class HistoricoDoProvedor:
    """Notificacoes recebidas e estornos automaticos de um pagamento."""

    def __init__(
        self,
        notificacoes: Iterable[NotificacaoRecebida] = (),
        estornos_automaticos: Iterable[EstornoAutomatico] = (),
    ) -> None:
        self._notificacoes = list(notificacoes)
        self._estornos_automaticos = list(estornos_automaticos)

    @property
    def notificacoes(self) -> tuple[NotificacaoRecebida, ...]:
        return tuple(self._notificacoes)

    @property
    def estornos_automaticos(self) -> tuple[EstornoAutomatico, ...]:
        return tuple(self._estornos_automaticos)

    def registrar_notificacao(self, notificacao: NotificacaoRecebida) -> bool:
        """``False`` se a tentativa ja foi vista com o mesmo status do provedor."""
        repetida = any(
            n.referencia_pagamento == notificacao.referencia_pagamento
            and n.status_provedor == notificacao.status_provedor
            for n in self._notificacoes
        )
        if not repetida:
            self._notificacoes.append(notificacao)
        return not repetida

    def estorno_automatico(self, referencia: str) -> EstornoAutomatico | None:
        """Estorno registrado para a tentativa, ou ``None``."""
        return next(
            (
                e
                for e in self._estornos_automaticos
                if e.referencia_pagamento == referencia
            ),
            None,
        )

    def registrar_estorno_automatico(self, estorno: EstornoAutomatico) -> None:
        """Quem chama confere antes, com ``estorno_automatico``, que e o primeiro."""
        self._estornos_automaticos.append(estorno)
