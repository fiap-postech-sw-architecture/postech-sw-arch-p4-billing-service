"""Agregado ``Orcamento``: proposta de preco de uma ordem de servico.

Precos ficam congelados nas linhas na geracao; mudancas posteriores na tabela
de precos nao alteram orcamentos ja gerados.

Transicoes (allow-list em ``_TRANSICOES``; RFC-004 secao 7.1)::

    PENDENTE -> APROVADO | RECUSADO | EXPIRADO | CANCELADO
    APROVADO -> CANCELADO

A lapide e o orcamento ja CANCELADO, sem linhas nem validade: o
``CancelarOrcamento`` que chega antes do ``GerarOrcamento`` (passo em voo)
a grava, e o ``GerarOrcamento`` atrasado a encontra pelo ``ordem_id``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from functools import reduce
from operator import add
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

from src.compartilhado.dominio.aggregate_root import AggregateRoot
from src.compartilhado.dominio.dinheiro import Dinheiro
from src.compartilhado.dominio.exceptions import (
    TransicaoStatusInvalidaError,
    ValorInvalidoError,
)
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
        raise ValorInvalidoError(msg)


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
            raise ValorInvalidoError(msg)
        # bool e subclasse de int: True nao pode virar quantidade 1.
        if isinstance(self.quantidade, bool) or self.quantidade <= 0:
            msg = f"Quantidade deve ser inteiro maior que zero: {self.quantidade!r}"
            raise ValorInvalidoError(msg)

    @property
    def subtotal(self) -> Dinheiro:
        return self.preco_unitario * self.quantidade


@dataclass(frozen=True, slots=True)
class Decisao(ValueObject):
    """Quem decidiu: o cliente pelo link ou o atendente em nome dele (com o
    ``sub`` do atendente em ``decidido_por``, trilha de auditoria do ADR-039)."""

    canal: CanalDecisao
    decidido_em: datetime
    decidido_por: str | None = None

    def __post_init__(self) -> None:
        _exigir_timezone("decidido_em", self.decidido_em)
        if self.canal is CanalDecisao.ATENDENTE and not (
            self.decidido_por and self.decidido_por.strip()
        ):
            msg = "Decisao do atendente exige decidido_por (sub do atendente)"
            raise ValorInvalidoError(msg)
        if self.canal is CanalDecisao.LINK and self.decidido_por is not None:
            msg = "Decisao pelo link e do cliente: sem decidido_por"
            raise ValorInvalidoError(msg)


@dataclass(eq=False, kw_only=True)
class Orcamento(AggregateRoot):
    _ordem_id: UUID
    _criado_em: datetime
    # Vazias (e sem validade) so na lapide.
    _linhas: tuple[LinhaOrcamento, ...] = ()
    _valido_ate: datetime | None = None
    _status: StatusOrcamento = StatusOrcamento.PENDENTE
    _decisao: Decisao | None = None
    _motivo_cancelamento: str | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        _exigir_timezone("criado_em", self._criado_em)
        if not self._linhas or self._valido_ate is None:
            self._validar_lapide()
            return
        if len({linha.preco_unitario.moeda for linha in self._linhas}) > 1:
            msg = "Linhas do orcamento devem ter a mesma moeda"
            raise ValorInvalidoError(msg)
        _exigir_timezone("valido_ate", self._valido_ate)
        if self._valido_ate <= self._criado_em:
            msg = "valido_ate deve ser posterior a criado_em"
            raise ValorInvalidoError(msg)
        if self._valido_ate.microsecond:
            # O token do link assina exp = valido_ate em epoch de segundos.
            msg = "valido_ate deve estar em segundo cheio"
            raise ValorInvalidoError(msg)

    def _validar_lapide(self) -> None:
        if (
            self._linhas
            or self._valido_ate is not None
            or self._status is not StatusOrcamento.CANCELADO
        ):
            msg = "Orcamento exige linhas e validade (sem elas, so a lapide CANCELADA)"
            raise ValorInvalidoError(msg)

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
        orcamento._registrar_evento(orcamento.desfecho_da_geracao(link_decisao))
        return orcamento

    @classmethod
    def lapide(
        cls,
        *,
        id: UUID,  # noqa: A002 - mesmo nome do campo herdado de Entity
        ordem_id: UUID,
        cancelado_em: datetime,
        motivo: str,
    ) -> Orcamento:
        """Compensacao que chegou antes do ``GerarOrcamento`` (passo em voo).

        Grava o orcamento ja CANCELADO, sem linhas, e responde
        ``OrcamentoCancelado``; o ``GerarOrcamento`` atrasado e descartado.
        """
        if not motivo.strip():
            msg = "Motivo do cancelamento e obrigatorio"
            raise ValorInvalidoError(msg)
        orcamento = cls(
            id=id,
            _ordem_id=ordem_id,
            _criado_em=cancelado_em,
            _status=StatusOrcamento.CANCELADO,
            _motivo_cancelamento=motivo,
        )
        orcamento._registrar_evento(orcamento.desfecho_do_cancelamento())
        return orcamento

    @property
    def ordem_id(self) -> UUID:
        return self._ordem_id

    @property
    def linhas(self) -> tuple[LinhaOrcamento, ...]:
        return self._linhas

    @property
    def total(self) -> Dinheiro:
        """Soma das linhas (zero na lapide, que nao tem linha)."""
        return reduce(
            add, (linha.subtotal for linha in self._linhas), Dinheiro(Decimal(0))
        )

    @property
    def criado_em(self) -> datetime:
        return self._criado_em

    @property
    def valido_ate(self) -> datetime | None:
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
        return (
            self._status is StatusOrcamento.PENDENTE
            and self._valido_ate is not None
            and agora > self._valido_ate
        )

    def desfecho_da_geracao(self, link_decisao: str) -> OrcamentoGeradoEvent:
        """``OrcamentoGerado`` deste orcamento (republicado na repeticao)."""
        if self._valido_ate is None:
            msg = "A lapide nao foi gerada: nao tem OrcamentoGerado"
            raise TransicaoStatusInvalidaError(msg)
        total = self.total
        return OrcamentoGeradoEvent(
            ordem_id=self._ordem_id,
            orcamento_id=self.id,
            linhas=tuple(
                LinhaOrcamentoGerado(
                    codigo=linha.codigo,
                    descricao=linha.descricao,
                    quantidade=linha.quantidade,
                    preco_unitario=linha.preco_unitario.valor,
                    subtotal=linha.subtotal.valor,
                )
                for linha in self._linhas
            ),
            total=total.valor,
            moeda=total.moeda,
            valido_ate=self._valido_ate,
            link_decisao=link_decisao,
        )

    def desfecho_do_cancelamento(self) -> OrcamentoCanceladoEvent:
        """Resposta ao ``CancelarOrcamento`` com o orcamento ja encerrado.

        Cancelado, recusado ou expirado: ``OrcamentoCancelado``, para a saga
        seguir (RFC-004, secao 4.5); nada muda aqui.
        """
        if self._status not in _ENCERRADOS_SEM_DECISAO_VALIDA:
            msg = f"Orcamento {self._status} ainda pode ser cancelado"
            raise TransicaoStatusInvalidaError(msg)
        return OrcamentoCanceladoEvent(ordem_id=self._ordem_id, orcamento_id=self.id)

    def aprovar(
        self, *, canal: CanalDecisao, agora: datetime, decidido_por: str | None = None
    ) -> None:
        """Decisao do cliente (unica); ``decidido_por`` so com ``canal=atendente``."""
        self._decidir(StatusOrcamento.APROVADO, Decisao(canal, agora, decidido_por))
        self._registrar_evento(
            OrcamentoAprovadoEvent(
                ordem_id=self._ordem_id,
                orcamento_id=self.id,
                decidido_em=agora,
                canal=canal,
                decidido_por=decidido_por,
            )
        )

    def recusar(
        self, *, canal: CanalDecisao, agora: datetime, decidido_por: str | None = None
    ) -> None:
        """Recusa do cliente (unica); ``decidido_por`` so com ``canal=atendente``."""
        self._decidir(StatusOrcamento.RECUSADO, Decisao(canal, agora, decidido_por))
        self._registrar_evento(
            OrcamentoRecusadoEvent(
                ordem_id=self._ordem_id,
                orcamento_id=self.id,
                decidido_em=agora,
                canal=canal,
                decidido_por=decidido_por,
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
        """Compensacao da saga; ``False`` quando ja esta encerrado.

        Orcamento ja cancelado, recusado ou expirado nao muda: o caso de uso
        responde com ``desfecho_do_cancelamento``, e a compensacao que cruzar
        com o encerramento (OS cancelada enquanto o prazo vencia) nao vira
        erro permanente.
        """
        if self._status in _ENCERRADOS_SEM_DECISAO_VALIDA:
            return False
        if not motivo.strip():
            msg = "Motivo do cancelamento e obrigatorio"
            raise ValorInvalidoError(msg)
        self._transitar(StatusOrcamento.CANCELADO)
        self._motivo_cancelamento = motivo
        self._registrar_evento(self.desfecho_do_cancelamento())
        return True

    def _decidir(self, destino: StatusOrcamento, decisao: Decisao) -> None:
        # Prazo esgotado e o mesmo erro antes e depois do job de expiracao.
        if self._status is StatusOrcamento.EXPIRADO or self.vencido(
            decisao.decidido_em
        ):
            raise OrcamentoVencidoError
        self._transitar(destino)
        self._decisao = decisao

    def _transitar(self, destino: StatusOrcamento) -> None:
        if destino not in _TRANSICOES.get(self._status, frozenset()):
            msg = f"Orcamento {self._status} nao pode passar para {destino}"
            raise TransicaoStatusInvalidaError(msg)
        self._status = destino
