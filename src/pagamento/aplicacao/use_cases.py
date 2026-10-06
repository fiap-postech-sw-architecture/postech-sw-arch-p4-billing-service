"""Casos de uso do pagamento (Mercado Pago ou simulador, pela mesma porta).

Chamadas ao provedor ficam fora da transacao do MongoDB: a transacao nao
segura conexao enquanto espera a rede e pode ser repetida sem refazer a
chamada externa. As regras (o que cada notificacao e cada compensacao fazem)
ficam no agregado; aqui so a orquestracao do I/O.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from src.compartilhado.dominio.exceptions import (
    EntidadeNaoEncontradaError,
    TransicaoStatusInvalidaError,
)
from src.compartilhado.dominio.relogio import agora_utc
from src.pagamento.aplicacao.dtos import PagamentoDTO
from src.pagamento.aplicacao.ports import (
    EstornoEmProcessamentoError,
    GatewayPagamentoRecusouError,
)
from src.pagamento.dominio.cobranca import Cobranca
from src.pagamento.dominio.estados import (
    MotivoEstorno,
    PlanoDeCompensacao,
    ResultadoNotificacao,
    StatusNoProvedor,
    StatusPagamento,
)
from src.pagamento.dominio.exceptions import (
    OrcamentoNaoAprovadoError,
    PagamentoJaSolicitadoError,
    PagamentoNaoEncontradoError,
)
from src.pagamento.dominio.pagamento import Pagamento

if TYPE_CHECKING:
    from datetime import datetime, timedelta

    from src.compartilhado.aplicacao.unit_of_work import UnitOfWork
    from src.compartilhado.dominio.relogio import Relogio
    from src.pagamento.aplicacao.ports import (
        GatewayPagamento,
        MetricasDePagamento,
        OrcamentosPort,
        SimuladorDePagamento,
    )
    from src.pagamento.dominio.cobranca import SituacaoNoProvedor
    from src.pagamento.dominio.repository import PagamentoRepository

_log = logging.getLogger(__name__)

# O estado so avanca (SOLICITADO -> CONFIRMADO -> ESTORNADO): replanejar a
# compensacao mais vezes que isso seria defeito, nao corrida.
_TENTATIVAS_DE_COMPENSACAO = 3


class CheckoutNaoEncontradoError(EntidadeNaoEncontradaError):
    """Token ausente, invalido, expirado ou de outro pagamento: o mesmo 404."""

    codigo = "CHECKOUT_NAO_ENCONTRADO"
    mensagem_padrao = "Checkout nao encontrado ou expirado"


class _PlanoMudouError(Exception):
    """O pagamento mudou entre a leitura e a gravacao: replanejar."""


def chave_de_estorno(pagamento_id: UUID) -> str:
    """``X-Idempotency-Key`` do estorno da compensacao (ADR-040, RFC-004 8)."""
    return f"estorno-{pagamento_id}"


def chave_de_estorno_automatico(pagamento_id: UUID, referencia: str) -> str:
    """Uma chave por tentativa estornada: uma segunda tentativa aprovada do
    mesmo checkout nao pode reaproveitar a resposta de outro estorno."""
    return f"estorno-{pagamento_id}-{referencia}"


def _obter(pagamentos: PagamentoRepository, pagamento_id: UUID) -> Pagamento:
    pagamento = pagamentos.obter_por_id(pagamento_id)
    if pagamento is None:
        raise PagamentoNaoEncontradoError
    return pagamento


def _uuid(valor: str | None) -> UUID | None:
    try:
        return UUID(valor) if valor else None
    except ValueError:
        return None


class SolicitarPagamento:
    """Comando ``SolicitarPagamento``: cria a cobranca do orcamento aprovado."""

    def __init__(
        self,
        uow: UnitOfWork,
        pagamentos: PagamentoRepository,
        orcamentos: OrcamentosPort,
        gateway: GatewayPagamento,
        validade: timedelta,
        relogio: Relogio = agora_utc,
    ) -> None:
        self._uow = uow
        self._pagamentos = pagamentos
        self._orcamentos = orcamentos
        self._gateway = gateway
        self._validade = validade
        self._relogio = relogio

    def executar(self, *, ordem_id: UUID, orcamento_id: UUID) -> PagamentoDTO:
        """Idempotente pela ordem: repetir nao cria outra cobranca e republica
        o ``PagamentoSolicitado`` registrado. Se a compensacao chegou antes
        (lapide), o comando e descartado: nenhuma cobranca, nenhuma resposta."""
        existente = self._pagamentos.obter_por_ordem(ordem_id)
        if existente is not None:
            return self._repetido(existente)
        orcamento = self._orcamentos.obter(orcamento_id)
        if orcamento is None or orcamento.ordem_id != ordem_id:
            msg = "Orcamento nao encontrado para a ordem de servico informada"
            raise PagamentoNaoEncontradoError(msg)
        if not orcamento.aprovado:
            raise OrcamentoNaoAprovadoError
        agora = self._relogio()
        expira_em = agora + self._validade
        pagamento_id = uuid4()
        criada = self._gateway.criar_cobranca(
            pagamento_id=pagamento_id, itens=orcamento.itens, expira_em=expira_em
        )
        pagamento = Pagamento.solicitar(
            id=pagamento_id,
            ordem_id=ordem_id,
            cobranca=Cobranca(
                orcamento_id=orcamento_id,
                valor=orcamento.total,
                provedor=self._gateway.provedor,
                referencia_preferencia=criada.referencia,
                checkout_url=criada.checkout_url,
                expira_em=expira_em,
            ),
            criado_em=agora,
        )
        try:
            self._uow.executar(lambda: self._pagamentos.salvar(pagamento))
        except PagamentoJaSolicitadoError:
            # Outra solicitacao (ou a lapide) da mesma ordem comitou primeiro; a
            # cobranca criada aqui fica orfa no provedor e expira sozinha (o
            # link dela nunca e publicado).
            existente = self._pagamentos.obter_por_ordem(ordem_id)
            if existente is None:
                raise
            return self._repetido(existente)
        return PagamentoDTO.de(pagamento)

    def _repetido(self, existente: Pagamento) -> PagamentoDTO:
        if existente.cobranca is None:
            _log.info(
                "command_discarded",
                extra={
                    "comando": "SolicitarPagamento",
                    "ordem_id": str(existente.ordem_id),
                },
            )
        else:
            evento = existente.desfecho_da_solicitacao()
            self._uow.executar(lambda: self._uow.registrar_evento(evento))
        return PagamentoDTO.de(existente)


class ProcessarNotificacaoPagamento:
    """Webhook do Mercado Pago, simulador e conciliacao: consulta e aplica.

    O status vem sempre da consulta ao provedor; o corpo da notificacao so diz
    qual tentativa consultar. Dinheiro que entrou numa cobranca que nao o aceita
    (encerrada, ou valor/moeda diferentes) e estornado na hora, fora da
    transacao, e o resultado fica gravado no agregado.
    """

    def __init__(
        self,
        uow: UnitOfWork,
        pagamentos: PagamentoRepository,
        gateway: GatewayPagamento,
        metricas: MetricasDePagamento,
        max_recusas: int,
        relogio: Relogio = agora_utc,
    ) -> None:
        self._uow = uow
        self._pagamentos = pagamentos
        self._gateway = gateway
        self._metricas = metricas
        self._max_recusas = max_recusas
        self._relogio = relogio

    def executar(self, referencia: str) -> PagamentoDTO | None:
        """``None`` quando a referencia nao corresponde a pagamento nosso."""
        situacao = self._gateway.consultar_pagamento(referencia)
        if situacao is None:
            _ignorada(referencia, "unknown_at_provider")
            return None
        return self.aplicar(situacao)

    def aplicar(self, situacao: SituacaoNoProvedor) -> PagamentoDTO | None:
        """Aplica uma tentativa ja consultada (a conciliacao chega por aqui)."""
        pagamento_id = _uuid(situacao.referencia_externa)
        if pagamento_id is None:
            _ignorada(situacao.referencia, "unknown_at_provider")
            return None

        def trabalho() -> tuple[Pagamento, ResultadoNotificacao] | None:
            pagamento = self._pagamentos.obter_por_id(pagamento_id)
            if pagamento is None:
                return None
            resultado = pagamento.aplicar_notificacao(
                situacao, agora=self._relogio(), max_recusas=self._max_recusas
            )
            # Notificacao repetida sem novidade nao grava nada.
            if resultado is not ResultadoNotificacao.SEM_MUDANCA:
                self._pagamentos.salvar(pagamento)
            return pagamento, resultado

        aplicado = self._uow.executar(trabalho)
        if aplicado is None:
            _ignorada(situacao.referencia, "payment_not_found")
            return None
        pagamento, resultado = aplicado
        if resultado is ResultadoNotificacao.ESTORNO_AUTOMATICO:
            pagamento = self._estornar_automaticamente(pagamento, situacao.referencia)
        return PagamentoDTO.de(pagamento)

    def _estornar_automaticamente(
        self, pagamento: Pagamento, referencia: str
    ) -> Pagamento:
        # Falha transitoria propaga: o provedor reenvia a notificacao (ou a
        # conciliacao repete) e o estorno sai de novo com a mesma chave.
        falha: str | None = None
        try:
            self._gateway.estornar(
                referencia,
                chave_idempotencia=chave_de_estorno_automatico(
                    pagamento.id, referencia
                ),
            )
        except GatewayPagamentoRecusouError as exc:
            if not _estornado_no_provedor(self._gateway, referencia):
                falha = exc.mensagem

        def trabalho() -> tuple[Pagamento, bool]:
            atual = _obter(self._pagamentos, pagamento.id)
            novo = atual.registrar_estorno_automatico(
                referencia, agora=self._relogio(), falha=falha
            )
            if novo:
                self._pagamentos.salvar(atual)
            return atual, novo

        atual, novo = self._uow.executar(trabalho)
        if novo:
            self._registrar_estorno_automatico(atual, referencia, falha)
        return atual

    def _registrar_estorno_automatico(
        self, pagamento: Pagamento, referencia: str, falha: str | None
    ) -> None:
        contexto = {
            "pagamento_id": str(pagamento.id),
            "referencia": referencia,
            "status": pagamento.status.value,
        }
        if falha is None:
            self._metricas.estorno_concluido(MotivoEstorno.PAGAMENTO_APOS_ENCERRAMENTO)
            _log.warning("automatic_refund_done", extra=contexto)
        else:
            # Ninguem espera por esse estorno: a devolucao fica para o operador.
            self._metricas.estorno_automatico_falhou()
            _log.error("automatic_refund_refused", extra=contexto)


def _ignorada(referencia: str, motivo: str) -> None:
    _log.warning(
        "payment_notification_ignored",
        extra={"referencia": referencia, "motivo": motivo},
    )


def _estornado_no_provedor(gateway: GatewayPagamento, referencia: str) -> bool:
    situacao = gateway.consultar_pagamento(referencia)
    return situacao is not None and situacao.status is StatusNoProvedor.ESTORNADO


class SimularResultadoPagamento:
    """Simulador (MP_MODE=simulado): o "cliente" paga ou tem o cartao recusado.

    So com o token do ``checkout_url`` (o mesmo 404 para token ausente,
    invalido, expirado ou de outro pagamento). Percorre o mesmo
    ``ProcessarNotificacaoPagamento`` do webhook real, entao a recusa conta
    como a do provedor (``PAGAMENTO_MAX_RECUSAS``).
    """

    def __init__(
        self,
        simulador: SimuladorDePagamento,
        pagamentos: PagamentoRepository,
        processar: ProcessarNotificacaoPagamento,
        relogio: Relogio = agora_utc,
    ) -> None:
        self._simulador = simulador
        self._pagamentos = pagamentos
        self._processar = processar
        self._relogio = relogio

    def consultar(self, pagamento_id: UUID, token: str | None) -> PagamentoDTO:
        """Pagamento da pagina de checkout."""
        return PagamentoDTO.de(self._autorizado(pagamento_id, token))

    def executar(
        self, pagamento_id: UUID, *, token: str | None, aprovar: bool
    ) -> PagamentoDTO:
        pagamento = self._autorizado(pagamento_id, token)
        cobranca = pagamento.cobranca
        if pagamento.status is not StatusPagamento.SOLICITADO or cobranca is None:
            msg = f"Pagamento {pagamento.status} ja foi processado"
            raise TransicaoStatusInvalidaError(msg)
        referencia = self._simulador.registrar_resultado(
            pagamento_id=pagamento.id, valor=cobranca.valor, aprovado=aprovar
        )
        dto = self._processar.executar(referencia)
        if dto is None:  # referencia que o proprio provedor simulado nao conhece
            raise PagamentoNaoEncontradoError
        return dto

    def _autorizado(self, pagamento_id: UUID, token: str | None) -> Pagamento:
        autorizado = self._simulador.checkout_autorizado(
            pagamento_id, token, agora=self._relogio()
        )
        pagamento = self._pagamentos.obter_por_id(pagamento_id) if autorizado else None
        if pagamento is None:
            raise CheckoutNaoEncontradoError
        return pagamento


class ExpirarPagamentosVencidos:
    """Expira os solicitados com prazo esgotado (processo ``prazos``)."""

    def __init__(
        self,
        uow: UnitOfWork,
        pagamentos: PagamentoRepository,
        relogio: Relogio = agora_utc,
    ) -> None:
        self._uow = uow
        self._pagamentos = pagamentos
        self._relogio = relogio

    def executar(self, *, limite: int = 100) -> int:
        """Quantidade expirada nesta rodada (cada pagamento na sua transacao)."""
        agora = self._relogio()
        vencidos = self._pagamentos.listar_vencidos(agora, limite)
        return sum(self._expirar(pagamento_id, agora) for pagamento_id in vencidos)

    def _expirar(self, pagamento_id: UUID, agora: datetime) -> bool:
        def trabalho() -> bool:
            pagamento = self._pagamentos.obter_por_id(pagamento_id)
            # Confirmado, recusado ou cancelado depois da listagem: a outra
            # escrita venceu (atualizacao condicional pelo status relido).
            if pagamento is None or not pagamento.vencido(agora):
                return False
            pagamento.expirar(agora=agora)
            self._pagamentos.salvar(pagamento)
            return True

        try:
            return self._uow.executar(trabalho)
        except Exception:  # noqa: BLE001 - um documento com defeito nao trava a fila
            _log.exception(
                "payment_expiration_failed", extra={"pagamento_id": str(pagamento_id)}
            )
            return False


class EstornarPagamento:
    """Compensacao ``EstornarPagamento`` da saga, em qualquer estado (ADR-040).

    O agregado diz o plano: SOLICITADO fecha o checkout no provedor e responde
    ``PagamentoCancelado``; CONFIRMADO confere no provedor (estornado pelo
    painel = so registra), estorna com ``X-Idempotency-Key = estorno-{id}`` e
    responde ``PagamentoEstornado``; ja encerrado republica o desfecho
    registrado, sem chamar o provedor. Recusa do provedor vira
    ``EstornoDePagamentoFalhou``; falha transitoria (inclusive estorno ainda em
    processamento) propaga para o consumidor repetir.
    """

    def __init__(
        self,
        uow: UnitOfWork,
        pagamentos: PagamentoRepository,
        gateway: GatewayPagamento,
        metricas: MetricasDePagamento,
        relogio: Relogio = agora_utc,
    ) -> None:
        self._uow = uow
        self._pagamentos = pagamentos
        self._gateway = gateway
        self._metricas = metricas
        self._relogio = relogio

    def executar(
        self, *, ordem_id: UUID, pagamento_id: UUID | None = None, motivo: str
    ) -> PagamentoDTO:
        """Acha o pagamento pelo ``ordem_id`` (o ``pagamento_id`` so vem quando
        o orquestrador ja o conhece); sem pagamento (``SolicitarPagamento``
        ainda em voo), grava a lapide CANCELADA e responde ``PagamentoCancelado``.
        """
        for _ in range(_TENTATIVAS_DE_COMPENSACAO):
            pagamento = self._pagamentos.obter_por_ordem(ordem_id)
            try:
                if pagamento is None:
                    return self._gravar_lapide(ordem_id, motivo)
                if pagamento_id is not None and pagamento.id != pagamento_id:
                    msg = "Pagamento nao pertence a ordem de servico informada"
                    raise PagamentoNaoEncontradoError(msg)
                plano = pagamento.compensar()
                if plano is PlanoDeCompensacao.CANCELAR_COBRANCA:
                    return self._cancelar(pagamento, motivo)
                if plano is PlanoDeCompensacao.ESTORNAR_NO_PROVEDOR:
                    return self._estornar(pagamento, motivo)
                return self._republicar(pagamento.id)
            except (_PlanoMudouError, PagamentoJaSolicitadoError):
                # O pagamento mudou (ou nasceu) entre a leitura e a gravacao.
                continue
        msg = f"Compensacao do pagamento {pagamento_id} nao estabilizou"
        raise RuntimeError(msg)

    def _gravar_lapide(self, ordem_id: UUID, motivo: str) -> PagamentoDTO:
        lapide = Pagamento.lapide(
            id=uuid4(), ordem_id=ordem_id, cancelado_em=self._relogio(), motivo=motivo
        )
        # Indice unico por ordem: se o SolicitarPagamento gravar antes, a
        # gravacao falha e a compensacao replaneja com o pagamento real.
        self._uow.executar(lambda: self._pagamentos.salvar(lapide))
        return PagamentoDTO.de(lapide)

    def _cancelar(self, pagamento: Pagamento, motivo: str) -> PagamentoDTO:
        # SOLICITADO sempre tem cobranca (so a lapide nao tem, e ela e CANCELADA).
        cobranca = pagamento.cobranca
        self._gateway.cancelar_cobranca(
            cobranca.referencia_preferencia if cobranca else ""
        )
        return self._concluir(
            pagamento.id, PlanoDeCompensacao.CANCELAR_COBRANCA, motivo
        )

    def _estornar(self, pagamento: Pagamento, motivo: str) -> PagamentoDTO:
        referencia = pagamento.referencia_pagamento or ""
        # Estornado pelo painel (intervencao manual): so registra e responde.
        if not _estornado_no_provedor(self._gateway, referencia):
            try:
                self._gateway.estornar(
                    referencia, chave_idempotencia=chave_de_estorno(pagamento.id)
                )
            except EstornoEmProcessamentoError:
                if not _estornado_no_provedor(self._gateway, referencia):
                    raise
            except GatewayPagamentoRecusouError as exc:
                # Uma tentativa anterior pode ter estornado e caido antes de
                # gravar aqui: o provedor recusa o segundo estorno.
                if not _estornado_no_provedor(self._gateway, referencia):
                    return self._falhar(pagamento.id, exc.mensagem)
        dto = self._concluir(
            pagamento.id, PlanoDeCompensacao.ESTORNAR_NO_PROVEDOR, motivo
        )
        self._metricas.estorno_concluido(MotivoEstorno.COMPENSACAO)
        return dto

    def _concluir(
        self, pagamento_id: UUID, plano: PlanoDeCompensacao, motivo: str
    ) -> PagamentoDTO:
        def trabalho() -> Pagamento:
            atual = _obter(self._pagamentos, pagamento_id)
            if atual.compensar() is not plano:
                raise _PlanoMudouError
            atual.concluir_compensacao(agora=self._relogio(), motivo=motivo)
            self._pagamentos.salvar(atual)
            return atual

        return PagamentoDTO.de(self._uow.executar(trabalho))

    def _republicar(self, pagamento_id: UUID) -> PagamentoDTO:
        def trabalho() -> Pagamento:
            # Encerrado so pode virar ESTORNADO (aprovacao tardia): o desfecho
            # relido aqui e o atual. So a resposta vai de novo para a outbox.
            atual = _obter(self._pagamentos, pagamento_id)
            self._uow.registrar_evento(atual.desfecho_da_compensacao())
            return atual

        return PagamentoDTO.de(self._uow.executar(trabalho))

    def _falhar(self, pagamento_id: UUID, motivo: str) -> PagamentoDTO:
        def trabalho() -> Pagamento:
            atual = _obter(self._pagamentos, pagamento_id)
            if atual.compensar() is not PlanoDeCompensacao.ESTORNAR_NO_PROVEDOR:
                raise _PlanoMudouError
            atual.registrar_falha_de_estorno(motivo)
            self._pagamentos.salvar(atual)
            return atual

        atual = self._uow.executar(trabalho)
        _log.error("refund_refused", extra={"pagamento_id": str(pagamento_id)})
        return PagamentoDTO.de(atual)


class ConsultarPagamentos:
    def __init__(self, pagamentos: PagamentoRepository) -> None:
        self._pagamentos = pagamentos

    def por_id(self, pagamento_id: UUID) -> PagamentoDTO:
        return PagamentoDTO.de(_obter(self._pagamentos, pagamento_id))
