"""Agregado ``Pagamento``: cobranca de um orcamento aprovado no provedor.

O status so muda pelo que o provedor confirma na consulta (nunca pelo corpo
do webhook), pelo prazo ou pela compensacao da saga. Transicoes (allow-list
em ``_TRANSICOES``)::

    PENDENTE -> APROVADO | RECUSADO | EXPIRADO | ESTORNADO
    APROVADO -> ESTORNADO

``PENDENTE -> ESTORNADO`` e a compensacao antes do pagamento: a cobranca e
encerrada sem dinheiro a devolver, e uma aprovacao que chegue depois e
estornada automaticamente (ver ``aplicar_aprovacao``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

from src.compartilhado.dominio.aggregate_root import AggregateRoot
from src.compartilhado.dominio.exceptions import TransicaoStatusInvalidaError
from src.compartilhado.dominio.value_object import ValueObject
from src.pagamento.dominio.events import (
    EstornoDePagamentoFalhouEvent,
    PagamentoConfirmadoEvent,
    PagamentoEstornadoEvent,
    PagamentoExpiradoEvent,
    PagamentoRecusadoEvent,
    PagamentoSolicitadoEvent,
)

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from src.compartilhado.dominio.dinheiro import Dinheiro

MOTIVO_PRAZO_ESGOTADO = "Prazo de pagamento esgotado"


class StatusPagamento(StrEnum):
    PENDENTE = "PENDENTE"
    APROVADO = "APROVADO"
    RECUSADO = "RECUSADO"
    EXPIRADO = "EXPIRADO"
    ESTORNADO = "ESTORNADO"


class ResultadoAprovacao(StrEnum):
    """O que uma aprovacao confirmada no provedor fez com o pagamento."""

    CONFIRMADO = "confirmado"
    REPETIDO = "repetido"
    # Dinheiro entrou mas a cobranca nao aceita: o caso de uso estorna.
    VALOR_DIVERGENTE = "valor_divergente"
    ENCERRADO = "encerrado"


ESTORNO_AUTOMATICO: Final = frozenset(
    {ResultadoAprovacao.VALOR_DIVERGENTE, ResultadoAprovacao.ENCERRADO}
)


_TRANSICOES: Final = MappingProxyType(
    {
        StatusPagamento.PENDENTE: frozenset(
            {
                StatusPagamento.APROVADO,
                StatusPagamento.RECUSADO,
                StatusPagamento.EXPIRADO,
                StatusPagamento.ESTORNADO,
            }
        ),
        StatusPagamento.APROVADO: frozenset({StatusPagamento.ESTORNADO}),
    }
)


@dataclass(frozen=True, slots=True)
class NotificacaoRecebida(ValueObject):
    """Historico de notificacoes do provedor, com o status consultado nele."""

    recebida_em: datetime
    referencia_pagamento: str
    status_provedor: str


@dataclass(eq=False, kw_only=True)
class Pagamento(AggregateRoot):
    _ordem_id: UUID
    _orcamento_id: UUID
    _valor: Dinheiro
    _provedor: str
    _referencia_preferencia: str
    _checkout_url: str
    _criado_em: datetime
    _expira_em: datetime
    _status: StatusPagamento = StatusPagamento.PENDENTE
    _referencia_pagamento: str | None = None
    _confirmado_em: datetime | None = None
    _estornado_em: datetime | None = None
    _chave_estorno: str | None = None
    # Motivo do encerramento atual (recusa, expiracao ou estorno).
    _motivo: str | None = None
    _notificacoes: list[NotificacaoRecebida] = field(default_factory=list)

    def __post_init__(self) -> None:
        super().__post_init__()
        if self._valor.valor <= 0:
            msg = f"Valor do pagamento deve ser maior que zero: {self._valor.valor}"
            raise ValueError(msg)
        if not (self._provedor and self._referencia_preferencia and self._checkout_url):
            msg = "Pagamento exige provedor, referencia da cobranca e checkout_url"
            raise ValueError(msg)
        if self._criado_em.tzinfo is None or self._expira_em.tzinfo is None:
            msg = "Datas do pagamento precisam de timezone (UTC)"
            raise ValueError(msg)
        if self._expira_em <= self._criado_em:
            msg = "expira_em deve ser posterior a criado_em"
            raise ValueError(msg)

    @classmethod
    def solicitar(  # noqa: PLR0913 - todos os dados da cobranca, por nome
        cls,
        *,
        id: UUID,  # noqa: A002 - mesmo nome do campo herdado de Entity
        ordem_id: UUID,
        orcamento_id: UUID,
        valor: Dinheiro,
        provedor: str,
        referencia_preferencia: str,
        checkout_url: str,
        criado_em: datetime,
        expira_em: datetime,
    ) -> Pagamento:
        """Cria o pagamento PENDENTE e registra ``PagamentoSolicitado``.

        ``id`` vem pronto porque a cobranca no provedor e criada antes, com o
        id do pagamento como referencia externa.
        """
        pagamento = cls(
            id=id,
            _ordem_id=ordem_id,
            _orcamento_id=orcamento_id,
            _valor=valor,
            _provedor=provedor,
            _referencia_preferencia=referencia_preferencia,
            _checkout_url=checkout_url,
            _criado_em=criado_em,
            _expira_em=expira_em,
        )
        pagamento._registrar_evento(
            PagamentoSolicitadoEvent(
                ordem_id=ordem_id,
                pagamento_id=pagamento.id,
                valor=valor.valor,
                moeda=valor.moeda,
                checkout_url=checkout_url,
                expira_em=expira_em,
            )
        )
        return pagamento

    @property
    def ordem_id(self) -> UUID:
        return self._ordem_id

    @property
    def orcamento_id(self) -> UUID:
        return self._orcamento_id

    @property
    def valor(self) -> Dinheiro:
        return self._valor

    @property
    def provedor(self) -> str:
        return self._provedor

    @property
    def referencia_preferencia(self) -> str:
        return self._referencia_preferencia

    @property
    def checkout_url(self) -> str:
        return self._checkout_url

    @property
    def criado_em(self) -> datetime:
        return self._criado_em

    @property
    def expira_em(self) -> datetime:
        return self._expira_em

    @property
    def status(self) -> StatusPagamento:
        return self._status

    @property
    def referencia_pagamento(self) -> str | None:
        return self._referencia_pagamento

    @property
    def confirmado_em(self) -> datetime | None:
        return self._confirmado_em

    @property
    def estornado_em(self) -> datetime | None:
        return self._estornado_em

    @property
    def chave_estorno(self) -> str | None:
        return self._chave_estorno

    @property
    def motivo(self) -> str | None:
        return self._motivo

    @property
    def notificacoes(self) -> tuple[NotificacaoRecebida, ...]:
        return tuple(self._notificacoes)

    def registrar_notificacao(self, notificacao: NotificacaoRecebida) -> bool:
        """Guarda a notificacao; a mesma referencia e status de novo nao repete."""
        repetida = any(
            n.referencia_pagamento == notificacao.referencia_pagamento
            and n.status_provedor == notificacao.status_provedor
            for n in self._notificacoes
        )
        if not repetida:
            self._notificacoes.append(notificacao)
        return not repetida

    def aplicar_aprovacao(
        self,
        *,
        referencia_pagamento: str,
        valor_cobrado: Dinheiro | None,
        agora: datetime,
    ) -> ResultadoAprovacao:
        """Aprovacao confirmada no provedor.

        So confirma a cobranca PENDENTE pelo valor exato do orcamento. A
        aprovacao do pagamento ja registrado aqui (mesmo depois de estornado) e
        ``REPETIDO``; valor diferente ou cobranca ja encerrada (expirada,
        recusada, encerrada pela saga ou paga por outra transacao) nao muda
        nada aqui e pede estorno automatico (``ESTORNO_AUTOMATICO``).
        """
        if self._referencia_pagamento == referencia_pagamento:
            return ResultadoAprovacao.REPETIDO
        if self._status is not StatusPagamento.PENDENTE:
            return ResultadoAprovacao.ENCERRADO
        if valor_cobrado != self._valor:
            return ResultadoAprovacao.VALOR_DIVERGENTE
        self._transitar(StatusPagamento.APROVADO)
        self._referencia_pagamento = referencia_pagamento
        self._confirmado_em = agora
        self._registrar_evento(
            PagamentoConfirmadoEvent(
                ordem_id=self._ordem_id,
                pagamento_id=self.id,
                valor=self._valor.valor,
                moeda=self._valor.moeda,
                confirmado_em=agora,
                referencia_provedor=referencia_pagamento,
            )
        )
        return ResultadoAprovacao.CONFIRMADO

    def recusar(self, *, motivo: str) -> bool:
        """Recusa confirmada no provedor. Repetir: ``False``."""
        if self._status is StatusPagamento.RECUSADO:
            return False
        self._transitar(StatusPagamento.RECUSADO)
        self._motivo = motivo
        self._registrar_evento(
            PagamentoRecusadoEvent(
                ordem_id=self._ordem_id, pagamento_id=self.id, motivo=motivo
            )
        )
        return True

    def vencido(self, agora: datetime) -> bool:
        """Pendente com o prazo esgotado (candidato a expirar)."""
        return self._status is StatusPagamento.PENDENTE and agora > self._expira_em

    def expirar(self, *, agora: datetime) -> None:
        if not self.vencido(agora):
            msg = "Pagamento so expira pendente e com o prazo esgotado"
            raise TransicaoStatusInvalidaError(msg)
        self._transitar(StatusPagamento.EXPIRADO)
        self._motivo = MOTIVO_PRAZO_ESGOTADO
        self._registrar_evento(
            PagamentoExpiradoEvent(
                ordem_id=self._ordem_id,
                pagamento_id=self.id,
                motivo=MOTIVO_PRAZO_ESGOTADO,
            )
        )

    def estornar(
        self, *, estornado_em: datetime, chave_idempotencia: str, motivo: str
    ) -> bool:
        """Estorno feito no provedor (compensacao da saga). Repetir: ``False``."""
        if self._status is StatusPagamento.ESTORNADO:
            return False
        if self._status is not StatusPagamento.APROVADO:
            msg = f"Pagamento {self._status} nao tem valor a estornar"
            raise TransicaoStatusInvalidaError(msg)
        self._chave_estorno = chave_idempotencia
        self._finalizar_estorno(estornado_em, motivo)
        return True

    def encerrar_cobranca(self, *, encerrado_em: datetime, motivo: str) -> None:
        """Compensacao antes do pagamento: fecha a cobranca PENDENTE.

        Nada a devolver ao cliente; a saga recebe ``PagamentoEstornado`` como
        resposta do passo. Aprovacao que chegue depois cai em ``ENCERRADO``.
        """
        if self._status is not StatusPagamento.PENDENTE:
            msg = f"Pagamento {self._status} nao tem cobranca aberta para encerrar"
            raise TransicaoStatusInvalidaError(msg)
        self._finalizar_estorno(encerrado_em, motivo)

    def _finalizar_estorno(self, instante: datetime, motivo: str) -> None:
        self._transitar(StatusPagamento.ESTORNADO)
        self._estornado_em = instante
        self._motivo = motivo
        self._registrar_evento(
            PagamentoEstornadoEvent(
                ordem_id=self._ordem_id, pagamento_id=self.id, estornado_em=instante
            )
        )

    def registrar_falha_de_estorno(self, motivo: str) -> None:
        """Estorno impossivel: o status nao muda, a saga recebe a falha."""
        self._registrar_evento(
            EstornoDePagamentoFalhouEvent(
                ordem_id=self._ordem_id, pagamento_id=self.id, motivo=motivo
            )
        )

    def _transitar(self, destino: StatusPagamento) -> None:
        if destino not in _TRANSICOES.get(self._status, frozenset()):
            msg = f"Pagamento {self._status} nao pode passar para {destino}"
            raise TransicaoStatusInvalidaError(msg)
        self._status = destino
