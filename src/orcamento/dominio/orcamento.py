"""Agregado ``Orcamento``: proposta de preco de uma ordem de servico.

Precos ficam congelados nas linhas na geracao; mudancas posteriores na tabela
de precos nao alteram orcamentos ja gerados.

Transicoes (allow-list em ``_TRANSICOES``)::

    PENDENTE -> APROVADO | RECUSADO | EXPIRADO | CANCELADO
    APROVADO -> CANCELADO
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from functools import reduce
from operator import add
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

from src.compartilhado.dominio.aggregate_root import AggregateRoot
from src.compartilhado.dominio.exceptions import TransicaoStatusInvalidaError
from src.compartilhado.dominio.value_object import ValueObject
from src.orcamento.dominio.events import (
    LinhaOrcamentoGerado,
    OrcamentoAprovadoEvent,
    OrcamentoCanceladoEvent,
    OrcamentoExpiradoEvent,
    OrcamentoGeradoEvent,
    OrcamentoRecusadoEvent,
)
from src.orcamento.dominio.exceptions import OrcamentoVencidoError

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime
    from uuid import UUID

    from src.compartilhado.dominio.dinheiro import Dinheiro


class TipoItem(StrEnum):
    SERVICO = "servico"
    PECA = "peca"


class StatusOrcamento(StrEnum):
    PENDENTE = "PENDENTE"
    APROVADO = "APROVADO"
    RECUSADO = "RECUSADO"
    EXPIRADO = "EXPIRADO"
    CANCELADO = "CANCELADO"


class CanalDecisao(StrEnum):
    LINK = "link"
    ATENDENTE = "atendente"


_TRANSICOES: Final = MappingProxyType(
    {
        StatusOrcamento.PENDENTE: frozenset(
            {
                StatusOrcamento.APROVADO,
                StatusOrcamento.RECUSADO,
                StatusOrcamento.EXPIRADO,
                StatusOrcamento.CANCELADO,
            }
        ),
        StatusOrcamento.APROVADO: frozenset({StatusOrcamento.CANCELADO}),
    }
)


_ENCERRADOS_SEM_DECISAO_VALIDA: Final = frozenset(
    {StatusOrcamento.CANCELADO, StatusOrcamento.RECUSADO, StatusOrcamento.EXPIRADO}
)


def _exigir_timezone(rotulo: str, instante: datetime) -> None:
    if instante.tzinfo is None:
        msg = f"{rotulo} precisa de timezone (UTC)"
        raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class LinhaOrcamento(ValueObject):
    tipo: TipoItem
    codigo: str
    descricao: str
    quantidade: int
    preco_unitario: Dinheiro

    def __post_init__(self) -> None:
        if not self.codigo or not self.descricao:
            msg = "Linha do orcamento exige codigo e descricao"
            raise ValueError(msg)
        # bool e subclasse de int: True nao pode virar quantidade 1.
        if isinstance(self.quantidade, bool) or self.quantidade <= 0:
            msg = f"Quantidade deve ser inteiro maior que zero: {self.quantidade!r}"
            raise ValueError(msg)

    @property
    def subtotal(self) -> Dinheiro:
        return self.preco_unitario * self.quantidade


@dataclass(frozen=True, slots=True)
class Decisao(ValueObject):
    canal: CanalDecisao
    decidido_em: datetime


@dataclass(eq=False, kw_only=True)
class Orcamento(AggregateRoot):
    _ordem_id: UUID
    _linhas: tuple[LinhaOrcamento, ...]
    _criado_em: datetime
    _valido_ate: datetime
    _status: StatusOrcamento = StatusOrcamento.PENDENTE
    _decisao: Decisao | None = None
    _motivo_cancelamento: str | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self._linhas:
            msg = "Orcamento deve ter ao menos uma linha"
            raise ValueError(msg)
        if len({linha.preco_unitario.moeda for linha in self._linhas}) > 1:
            msg = "Linhas do orcamento devem ter a mesma moeda"
            raise ValueError(msg)
        _exigir_timezone("criado_em", self._criado_em)
        _exigir_timezone("valido_ate", self._valido_ate)
        if self._valido_ate <= self._criado_em:
            msg = "valido_ate deve ser posterior a criado_em"
            raise ValueError(msg)

    @classmethod
    def gerar(
        cls,
        *,
        id: UUID,  # noqa: A002 - mesmo nome do campo herdado de Entity
        ordem_id: UUID,
        linhas: Sequence[LinhaOrcamento],
        criado_em: datetime,
        valido_ate: datetime,
        link_decisao: str,
    ) -> Orcamento:
        """Cria o orcamento PENDENTE e registra ``OrcamentoGerado``.

        ``id`` vem pronto porque o link de decisao (assinado sobre o id) e
        montado antes, pela aplicacao.
        """
        orcamento = cls(
            id=id,
            _ordem_id=ordem_id,
            _linhas=tuple(linhas),
            _criado_em=criado_em,
            _valido_ate=valido_ate,
        )
        total = orcamento.total
        orcamento._registrar_evento(
            OrcamentoGeradoEvent(
                ordem_id=ordem_id,
                orcamento_id=orcamento.id,
                linhas=tuple(
                    LinhaOrcamentoGerado(
                        codigo=linha.codigo,
                        descricao=linha.descricao,
                        quantidade=linha.quantidade,
                        preco_unitario=linha.preco_unitario.valor,
                        subtotal=linha.subtotal.valor,
                    )
                    for linha in orcamento.linhas
                ),
                total=total.valor,
                moeda=total.moeda,
                valido_ate=valido_ate,
                link_decisao=link_decisao,
            )
        )
        return orcamento

    @property
    def ordem_id(self) -> UUID:
        return self._ordem_id

    @property
    def linhas(self) -> tuple[LinhaOrcamento, ...]:
        return self._linhas

    @property
    def total(self) -> Dinheiro:
        return reduce(add, (linha.subtotal for linha in self._linhas))

    @property
    def criado_em(self) -> datetime:
        return self._criado_em

    @property
    def valido_ate(self) -> datetime:
        return self._valido_ate

    @property
    def status(self) -> StatusOrcamento:
        return self._status

    @property
    def decisao(self) -> Decisao | None:
        return self._decisao

    @property
    def motivo_cancelamento(self) -> str | None:
        return self._motivo_cancelamento

    def vencido(self, agora: datetime) -> bool:
        """Pendente com o prazo de decisao esgotado (candidato a expirar)."""
        return self._status is StatusOrcamento.PENDENTE and agora > self._valido_ate

    def aprovar(self, *, canal: CanalDecisao, agora: datetime) -> None:
        self._decidir(StatusOrcamento.APROVADO, canal, agora)
        self._registrar_evento(
            OrcamentoAprovadoEvent(
                ordem_id=self._ordem_id,
                orcamento_id=self.id,
                decidido_em=agora,
                canal=canal,
            )
        )

    def recusar(self, *, canal: CanalDecisao, agora: datetime) -> None:
        self._decidir(StatusOrcamento.RECUSADO, canal, agora)
        self._registrar_evento(
            OrcamentoRecusadoEvent(
                ordem_id=self._ordem_id,
                orcamento_id=self.id,
                decidido_em=agora,
                canal=canal,
            )
        )

    def expirar(self, *, agora: datetime) -> None:
        if not self.vencido(agora):
            msg = "Orcamento so expira pendente e com o prazo de decisao esgotado"
            raise TransicaoStatusInvalidaError(msg)
        self._transitar(StatusOrcamento.EXPIRADO)
        self._registrar_evento(
            OrcamentoExpiradoEvent(ordem_id=self._ordem_id, orcamento_id=self.id)
        )

    def cancelar(self, *, motivo: str) -> bool:
        """Compensacao da saga; ``False`` quando nao ha o que cancelar.

        Orcamento ja cancelado, recusado ou expirado nao muda: o evento desse
        encerramento ja esta no outbox, e a compensacao que cruzar com ele
        (OS cancelada enquanto o prazo vencia) nao vira erro permanente.
        """
        if self._status in _ENCERRADOS_SEM_DECISAO_VALIDA:
            return False
        if not motivo.strip():
            msg = "Motivo do cancelamento e obrigatorio"
            raise ValueError(msg)
        self._transitar(StatusOrcamento.CANCELADO)
        self._motivo_cancelamento = motivo
        self._registrar_evento(
            OrcamentoCanceladoEvent(ordem_id=self._ordem_id, orcamento_id=self.id)
        )
        return True

    def _decidir(
        self, destino: StatusOrcamento, canal: CanalDecisao, agora: datetime
    ) -> None:
        # Prazo esgotado e o mesmo erro antes e depois do job de expiracao.
        if self._status is StatusOrcamento.EXPIRADO or self.vencido(agora):
            raise OrcamentoVencidoError
        self._transitar(destino)
        self._decisao = Decisao(canal=canal, decidido_em=agora)

    def _transitar(self, destino: StatusOrcamento) -> None:
        if destino not in _TRANSICOES.get(self._status, frozenset()):
            msg = f"Orcamento {self._status} nao pode passar para {destino}"
            raise TransicaoStatusInvalidaError(msg)
        self._status = destino
