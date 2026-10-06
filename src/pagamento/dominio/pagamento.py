"""Agregado ``Pagamento``: cobranca de um orcamento aprovado no provedor.

O status so muda pelo que o provedor confirma na consulta (nunca pelo corpo
do webhook), pelo prazo ou pela compensacao da saga (ADR-040). Estados,
transicoes e planos em ``estados.py``; value objects em ``cobranca.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from src.compartilhado.dominio.aggregate_root import AggregateRoot
from src.compartilhado.dominio.exceptions import (
    TransicaoStatusInvalidaError,
    ValorInvalidoError,
)
from src.pagamento.dominio.cobranca import (
    EstornoAutomatico,
    NotificacaoRecebida,
    exigir_texto,
    exigir_timezone,
)
from src.pagamento.dominio.estados import (
    ENCERRADOS_SEM_PAGAMENTO,
    TRANSICOES,
    MotivoEstorno,
    PlanoDeCompensacao,
    ResultadoNotificacao,
    StatusNoProvedor,
    StatusPagamento,
)
from src.pagamento.dominio.events import (
    EstornoDePagamentoFalhouEvent,
    PagamentoCanceladoEvent,
    PagamentoConfirmadoEvent,
    PagamentoEstornadoEvent,
    PagamentoExpiradoEvent,
    PagamentoRecusadoEvent,
    PagamentoSolicitadoEvent,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime
    from uuid import UUID

    from src.pagamento.dominio.cobranca import Cobranca, SituacaoNoProvedor

MOTIVO_PRAZO_ESGOTADO = "Prazo de pagamento esgotado"
# maxLength do ``motivo`` nos contratos de mensagem: texto do provedor e cortado.
TAMANHO_MAXIMO_MOTIVO: Final = 500

_PLANOS: Final = {
    StatusPagamento.SOLICITADO: PlanoDeCompensacao.CANCELAR_COBRANCA,
    StatusPagamento.CONFIRMADO: PlanoDeCompensacao.ESTORNAR_NO_PROVEDOR,
    StatusPagamento.ESTORNADO: PlanoDeCompensacao.RESPONDER_ESTORNADO,
}


def _motivo(texto: str) -> str:
    exigir_texto("Motivo", texto)
    return texto[:TAMANHO_MAXIMO_MOTIVO]


@dataclass(eq=False, kw_only=True)
class Pagamento(AggregateRoot):
    """Uma cobranca por ordem: tentativas no provedor, recusas contadas,
    prazo, compensacao e estornos (os automaticos inclusive)."""

    _ordem_id: UUID
    _criado_em: datetime
    # None so na lapide: compensacao que chegou antes do SolicitarPagamento.
    _cobranca: Cobranca | None = None
    _status: StatusPagamento = StatusPagamento.SOLICITADO
    _recusas: int = 0
    _referencia_pagamento: str | None = None
    _confirmado_em: datetime | None = None
    # Recusa, expiracao ou cancelamento: quando e por que a cobranca fechou.
    _encerrado_em: datetime | None = None
    _motivo: str | None = None
    _estornado_em: datetime | None = None
    _motivo_estorno: MotivoEstorno | None = None
    _notificacoes: list[NotificacaoRecebida] = field(default_factory=list)
    _estornos_automaticos: list[EstornoAutomatico] = field(default_factory=list)

    def __post_init__(self) -> None:
        super().__post_init__()
        exigir_timezone("criado_em", self._criado_em)
        if self._cobranca is None and self._status is not StatusPagamento.CANCELADO:
            msg = "Pagamento sem cobranca so existe como lapide CANCELADA"
            raise ValorInvalidoError(msg)
        if self._cobranca is not None and self._cobranca.expira_em <= self._criado_em:
            msg = "expira_em deve ser posterior a criado_em"
            raise ValorInvalidoError(msg)
        if isinstance(self._recusas, bool) or self._recusas < 0:
            msg = "recusas deve ser um inteiro nao negativo"
            raise ValorInvalidoError(msg)
        self._exigir_campos_do_status()

    @classmethod
    def solicitar(
        cls,
        *,
        id: UUID,  # noqa: A002 - mesmo nome do campo herdado de Entity
        ordem_id: UUID,
        cobranca: Cobranca,
        criado_em: datetime,
    ) -> Pagamento:
        """Cria o pagamento SOLICITADO e registra ``PagamentoSolicitado``.

        ``id`` vem pronto porque a cobranca no provedor e criada antes, com o
        id do pagamento como referencia externa.
        """
        pagamento = cls(
            id=id, _ordem_id=ordem_id, _criado_em=criado_em, _cobranca=cobranca
        )
        pagamento._registrar_evento(pagamento.desfecho_da_solicitacao())
        return pagamento

    @classmethod
    def lapide(
        cls,
        *,
        id: UUID,  # noqa: A002 - mesmo nome do campo herdado de Entity
        ordem_id: UUID,
        cancelado_em: datetime,
        motivo: str,
    ) -> Pagamento:
        """Compensacao que chegou antes do ``SolicitarPagamento`` (passo em voo).

        Grava o pagamento ja CANCELADO e sem cobranca, e responde
        ``PagamentoCancelado``; o ``SolicitarPagamento`` atrasado encontra a
        lapide pelo ``ordem_id`` e e descartado (RFC-004, secao 4.5).
        """
        pagamento = cls(
            id=id,
            _ordem_id=ordem_id,
            _criado_em=cancelado_em,
            _status=StatusPagamento.CANCELADO,
            _encerrado_em=cancelado_em,
            _motivo=_motivo(motivo),
        )
        pagamento._registrar_evento(pagamento.desfecho_da_compensacao())
        return pagamento

    @classmethod
    def reconstituir(  # noqa: PLR0913 - reidratacao recebe cada campo persistido
        cls,
        *,
        id: UUID,  # noqa: A002 - mesmo nome do campo herdado de Entity
        ordem_id: UUID,
        criado_em: datetime,
        cobranca: Cobranca | None,
        status: StatusPagamento,
        recusas: int,
        referencia_pagamento: str | None,
        confirmado_em: datetime | None,
        encerrado_em: datetime | None,
        motivo: str | None,
        estornado_em: datetime | None,
        motivo_estorno: MotivoEstorno | None,
        notificacoes: Sequence[NotificacaoRecebida],
        estornos_automaticos: Sequence[EstornoAutomatico],
    ) -> Pagamento:
        """Reidrata do armazenamento: as invariantes (inclusive a coerencia
        status x campos) valem de novo, sem evento."""
        return cls(
            id=id,
            _ordem_id=ordem_id,
            _criado_em=criado_em,
            _cobranca=cobranca,
            _status=status,
            _recusas=recusas,
            _referencia_pagamento=referencia_pagamento,
            _confirmado_em=confirmado_em,
            _encerrado_em=encerrado_em,
            _motivo=motivo,
            _estornado_em=estornado_em,
            _motivo_estorno=motivo_estorno,
            _notificacoes=list(notificacoes),
            _estornos_automaticos=list(estornos_automaticos),
        )

    @property
    def ordem_id(self) -> UUID:
        return self._ordem_id

    @property
    def criado_em(self) -> datetime:
        return self._criado_em

    @property
    def cobranca(self) -> Cobranca | None:
        return self._cobranca

    @property
    def status(self) -> StatusPagamento:
        return self._status

    @property
    def recusas(self) -> int:
        return self._recusas

    @property
    def referencia_pagamento(self) -> str | None:
        """Tentativa do provedor que confirmou o pagamento."""
        return self._referencia_pagamento

    @property
    def confirmado_em(self) -> datetime | None:
        return self._confirmado_em

    @property
    def encerrado_em(self) -> datetime | None:
        return self._encerrado_em

    @property
    def motivo(self) -> str | None:
        return self._motivo

    @property
    def estornado_em(self) -> datetime | None:
        return self._estornado_em

    @property
    def motivo_estorno(self) -> MotivoEstorno | None:
        return self._motivo_estorno

    @property
    def notificacoes(self) -> tuple[NotificacaoRecebida, ...]:
        return tuple(self._notificacoes)

    @property
    def estornos_automaticos(self) -> tuple[EstornoAutomatico, ...]:
        return tuple(self._estornos_automaticos)

    def desfecho_da_solicitacao(self) -> PagamentoSolicitadoEvent:
        """``PagamentoSolicitado`` desta cobranca (republicado na repeticao)."""
        cobranca = self._cobranca
        if cobranca is None:
            msg = "A lapide nao tem cobranca: nao tem PagamentoSolicitado"
            raise TransicaoStatusInvalidaError(msg)
        return PagamentoSolicitadoEvent(
            ordem_id=self._ordem_id,
            pagamento_id=self.id,
            valor=cobranca.valor.valor,
            moeda=cobranca.valor.moeda,
            checkout_url=cobranca.checkout_url,
            expira_em=cobranca.expira_em,
        )

    def vencido(self, agora: datetime) -> bool:
        """Solicitado com o prazo esgotado (candidato a expirar)."""
        return (
            self._status is StatusPagamento.SOLICITADO
            and self._cobranca is not None
            and agora > self._cobranca.expira_em
        )

    def expirar(self, *, agora: datetime) -> None:
        """Fecha a cobranca vencida sem pagamento e registra ``PagamentoExpirado``.

        So SOLICITADO expira (a allow-list recusa os demais), e so depois do prazo.
        """
        if self._status is StatusPagamento.SOLICITADO and not self.vencido(agora):
            msg = "Pagamento so expira com o prazo esgotado"
            raise TransicaoStatusInvalidaError(msg)
        self._encerrar(StatusPagamento.EXPIRADO, agora, MOTIVO_PRAZO_ESGOTADO)
        self._registrar_evento(
            PagamentoExpiradoEvent(
                ordem_id=self._ordem_id,
                pagamento_id=self.id,
                motivo=MOTIVO_PRAZO_ESGOTADO,
            )
        )

    def aplicar_notificacao(
        self, situacao: SituacaoNoProvedor, *, agora: datetime, max_recusas: int
    ) -> ResultadoNotificacao:
        """Aplica uma tentativa consultada no provedor (webhook ou conciliacao).

        ``approved`` pelo valor e moeda do orcamento confirma a cobranca
        SOLICITADA; aprovacao que a cobranca nao aceita (encerrada ou valor
        diferente) pede estorno automatico. Cada ``rejected`` novo soma uma
        recusa, e a de numero ``max_recusas`` leva a RECUSADO. Tentativa ja
        vista nao muda nada (``SEM_MUDANCA``: o caso de uso nem grava).
        """
        if isinstance(max_recusas, bool) or max_recusas < 1:
            msg = "max_recusas deve ser um inteiro maior que zero"
            raise ValorInvalidoError(msg)
        nova = self._registrar_notificacao(
            NotificacaoRecebida(
                recebida_em=agora,
                referencia_pagamento=situacao.referencia,
                status_provedor=situacao.status_provedor,
            )
        )
        resultado = None
        if situacao.status is StatusNoProvedor.APROVADO:
            resultado = self._aplicar_aprovacao(situacao, agora)
        elif situacao.status is StatusNoProvedor.RECUSADO and nova:
            resultado = self._contar_recusa(situacao, agora, max_recusas)
        if resultado is not None:
            return resultado
        return (
            ResultadoNotificacao.REGISTRADA
            if nova
            else ResultadoNotificacao.SEM_MUDANCA
        )

    def registrar_estorno_automatico(
        self, referencia: str, *, agora: datetime, falha: str | None = None
    ) -> bool:
        """Resultado do estorno de uma tentativa que a cobranca nao aceitava.

        Concluido: cobranca encerrada sem pagamento vira ESTORNADO, e sai
        ``PagamentoEstornado`` (motivo ``pagamento_apos_encerramento``).
        Recusado pelo provedor (``falha``): fica marcado para intervencao
        manual, sem evento (nenhuma saga espera por ele). Repetir: ``False``.
        """
        if self._estorno_automatico(referencia) is not None:
            return False
        self._estornos_automaticos.append(
            EstornoAutomatico(
                referencia_pagamento=referencia,
                registrado_em=agora,
                falha=None if falha is None else _motivo(falha),
            )
        )
        if falha is not None:
            return True
        motivo = MotivoEstorno.PAGAMENTO_APOS_ENCERRAMENTO
        if self._status in ENCERRADOS_SEM_PAGAMENTO:
            self._transitar(StatusPagamento.ESTORNADO)
            self._estornado_em = agora
            self._motivo_estorno = motivo
        self._registrar_evento(
            PagamentoEstornadoEvent(
                ordem_id=self._ordem_id,
                pagamento_id=self.id,
                estornado_em=agora,
                motivo=motivo,
            )
        )
        return True

    def compensar(self) -> PlanoDeCompensacao:
        """O que ``EstornarPagamento`` faz neste estado (sem mudar nada)."""
        return _PLANOS.get(self._status, PlanoDeCompensacao.RESPONDER_CANCELADO)

    def concluir_compensacao(self, *, agora: datetime, motivo: str) -> None:
        """Depois do I/O no provedor: SOLICITADO -> CANCELADO (checkout fechado,
        ``PagamentoCancelado``) ou CONFIRMADO -> ESTORNADO (dinheiro devolvido,
        ``PagamentoEstornado`` com motivo ``compensacao``)."""
        texto = _motivo(motivo)
        if self._status is StatusPagamento.SOLICITADO:
            self._encerrar(StatusPagamento.CANCELADO, agora, texto)
            self._registrar_evento(self.desfecho_da_compensacao())
            return
        if self._status is not StatusPagamento.CONFIRMADO:
            msg = f"Pagamento {self._status} nao tem compensacao a concluir"
            raise TransicaoStatusInvalidaError(msg)
        self._transitar(StatusPagamento.ESTORNADO)
        self._estornado_em = agora
        self._motivo_estorno = MotivoEstorno.COMPENSACAO
        self._motivo = texto
        self._registrar_evento(self.desfecho_da_compensacao())

    def desfecho_da_compensacao(
        self,
    ) -> PagamentoCanceladoEvent | PagamentoEstornadoEvent:
        """Resposta registrada ao ``EstornarPagamento`` (republicada na repeticao).

        Encerrado sem pagamento (cancelado, recusado ou expirado):
        ``PagamentoCancelado``, nada a devolver. Estornado: ``PagamentoEstornado``.
        """
        if self._estornado_em is not None and self._motivo_estorno is not None:
            return PagamentoEstornadoEvent(
                ordem_id=self._ordem_id,
                pagamento_id=self.id,
                estornado_em=self._estornado_em,
                motivo=self._motivo_estorno,
            )
        if self._status in ENCERRADOS_SEM_PAGAMENTO and self._encerrado_em:
            return PagamentoCanceladoEvent(
                ordem_id=self._ordem_id,
                pagamento_id=self.id,
                cancelado_em=self._encerrado_em,
            )
        msg = f"Pagamento {self._status} ainda nao tem desfecho de compensacao"
        raise TransicaoStatusInvalidaError(msg)

    def registrar_falha_de_estorno(self, motivo: str) -> None:
        """O provedor recusou o estorno da compensacao: o status nao muda e a
        saga recebe ``EstornoDePagamentoFalhou`` (intervencao manual)."""
        if self._status is not StatusPagamento.CONFIRMADO:
            msg = f"Pagamento {self._status} nao tem estorno pendente"
            raise TransicaoStatusInvalidaError(msg)
        self._registrar_evento(
            EstornoDePagamentoFalhouEvent(
                ordem_id=self._ordem_id, pagamento_id=self.id, motivo=_motivo(motivo)
            )
        )

    def _aplicar_aprovacao(
        self, situacao: SituacaoNoProvedor, agora: datetime
    ) -> ResultadoNotificacao | None:
        referencia = situacao.referencia
        if referencia == self._referencia_pagamento or self._estorno_automatico(
            referencia
        ):
            return None
        cobranca = self._cobranca
        aceita = self._status is StatusPagamento.SOLICITADO and cobranca is not None
        if not aceita or cobranca is None or situacao.valor != cobranca.valor:
            return ResultadoNotificacao.ESTORNO_AUTOMATICO
        self._transitar(StatusPagamento.CONFIRMADO)
        self._referencia_pagamento = referencia
        self._confirmado_em = agora
        self._registrar_evento(
            PagamentoConfirmadoEvent(
                ordem_id=self._ordem_id,
                pagamento_id=self.id,
                valor=cobranca.valor.valor,
                moeda=cobranca.valor.moeda,
                confirmado_em=agora,
                referencia_provedor=referencia,
            )
        )
        return ResultadoNotificacao.CONFIRMADO

    def _contar_recusa(
        self, situacao: SituacaoNoProvedor, agora: datetime, max_recusas: int
    ) -> ResultadoNotificacao | None:
        if self._status is not StatusPagamento.SOLICITADO:
            return None
        self._recusas += 1
        if self._recusas < max_recusas:
            return ResultadoNotificacao.RECUSA_CONTADA
        motivo = _motivo(situacao.detalhe or situacao.status_provedor)
        self._encerrar(StatusPagamento.RECUSADO, agora, motivo)
        self._registrar_evento(
            PagamentoRecusadoEvent(
                ordem_id=self._ordem_id, pagamento_id=self.id, motivo=motivo
            )
        )
        return ResultadoNotificacao.RECUSADO

    def _registrar_notificacao(self, notificacao: NotificacaoRecebida) -> bool:
        repetida = any(
            n.referencia_pagamento == notificacao.referencia_pagamento
            and n.status_provedor == notificacao.status_provedor
            for n in self._notificacoes
        )
        if not repetida:
            self._notificacoes.append(notificacao)
        return not repetida

    def _estorno_automatico(self, referencia: str) -> EstornoAutomatico | None:
        return next(
            (
                e
                for e in self._estornos_automaticos
                if e.referencia_pagamento == referencia
            ),
            None,
        )

    def _encerrar(self, destino: StatusPagamento, agora: datetime, motivo: str) -> None:
        self._transitar(destino)
        self._encerrado_em = agora
        self._motivo = motivo

    def _transitar(self, destino: StatusPagamento) -> None:
        if destino not in TRANSICOES.get(self._status, frozenset()):
            msg = f"Pagamento {self._status} nao pode passar para {destino}"
            raise TransicaoStatusInvalidaError(msg)
        self._status = destino

    def _exigir_campos_do_status(self) -> None:
        # Coerencia status x campos, inclusive na reidratacao do documento.
        exigidos: tuple[object, ...] = ()
        if self._status is StatusPagamento.CONFIRMADO:
            exigidos = (self._referencia_pagamento, self._confirmado_em)
        elif self._status is StatusPagamento.ESTORNADO:
            exigidos = (self._estornado_em, self._motivo_estorno)
        elif self._status in ENCERRADOS_SEM_PAGAMENTO:
            exigidos = (self._encerrado_em, self._motivo)
        if any(valor is None for valor in exigidos):
            msg = f"Pagamento {self._status} com dados incompletos"
            raise ValorInvalidoError(msg)
        for rotulo, instante in (
            ("confirmado_em", self._confirmado_em),
            ("encerrado_em", self._encerrado_em),
            ("estornado_em", self._estornado_em),
        ):
            if instante is not None:
                exigir_timezone(rotulo, instante)
