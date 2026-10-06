"""Casos de uso do pagamento (Mercado Pago ou simulador, pela mesma porta).

Chamadas ao provedor ficam fora da transacao do MongoDB: a transacao nao
segura conexao enquanto espera a rede e pode ser repetida sem refazer a
chamada externa.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from src.compartilhado.dominio.exceptions import TransicaoStatusInvalidaError
from src.compartilhado.dominio.relogio import agora_utc
from src.pagamento.aplicacao.dtos import PagamentoDTO
from src.pagamento.aplicacao.ports import (
    EstornoEmProcessamentoError,
    GatewayPagamentoRecusouError,
)
from src.pagamento.dominio.exceptions import (
    OrcamentoNaoAprovadoError,
    PagamentoJaSolicitadoError,
    PagamentoNaoEncontradoError,
)
from src.pagamento.dominio.pagamento import (
    ESTORNO_AUTOMATICO,
    NotificacaoRecebida,
    Pagamento,
    ResultadoAprovacao,
    StatusPagamento,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime, timedelta

    from src.compartilhado.aplicacao.unit_of_work import UnitOfWork
    from src.compartilhado.dominio.relogio import Relogio
    from src.pagamento.aplicacao.ports import (
        GatewayPagamento,
        OrcamentosPort,
        SimuladorDePagamento,
        SituacaoNoProvedor,
    )
    from src.pagamento.dominio.repository import PagamentoRepository

_log = logging.getLogger(__name__)


def _obter(pagamentos: PagamentoRepository, pagamento_id: UUID) -> Pagamento:
    pagamento = pagamentos.obter_por_id(pagamento_id)
    if pagamento is None:
        raise PagamentoNaoEncontradoError
    return pagamento


def _obter_da_ordem(
    pagamentos: PagamentoRepository, pagamento_id: UUID, ordem_id: UUID
) -> Pagamento:
    pagamento = _obter(pagamentos, pagamento_id)
    if pagamento.ordem_id != ordem_id:
        msg = "Pagamento nao pertence a ordem de servico informada"
        raise PagamentoNaoEncontradoError(msg)
    return pagamento


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
        """Idempotente por orcamento: repetir devolve o pagamento existente,
        sem nova cobranca no provedor nem novo evento."""
        existente = self._pagamentos.obter_por_orcamento(orcamento_id)
        if existente is not None:
            return PagamentoDTO.de(existente)
        orcamento = self._orcamentos.obter(orcamento_id)
        if orcamento is None or orcamento.ordem_id != ordem_id:
            msg = "Orcamento nao encontrado para a ordem de servico informada"
            raise PagamentoNaoEncontradoError(msg)
        if not orcamento.aprovado:
            raise OrcamentoNaoAprovadoError
        agora = self._relogio()
        expira_em = agora + self._validade
        pagamento_id = uuid4()
        cobranca = self._gateway.criar_cobranca(
            pagamento_id=pagamento_id, itens=orcamento.itens, expira_em=expira_em
        )
        pagamento = Pagamento.solicitar(
            id=pagamento_id,
            ordem_id=ordem_id,
            orcamento_id=orcamento_id,
            valor=orcamento.total,
            provedor=self._gateway.provedor,
            referencia_preferencia=cobranca.referencia,
            checkout_url=cobranca.checkout_url,
            criado_em=agora,
            expira_em=expira_em,
        )
        try:
            self._uow.executar(lambda: self._pagamentos.salvar(pagamento))
        except PagamentoJaSolicitadoError:
            # Outra solicitacao do mesmo orcamento comitou primeiro; a cobranca
            # criada aqui fica orfa no provedor e expira sozinha.
            existente = self._pagamentos.obter_por_orcamento(orcamento_id)
            if existente is None:
                raise
            return PagamentoDTO.de(existente)
        return PagamentoDTO.de(pagamento)


class ProcessarNotificacaoPagamento:
    """Webhook do Mercado Pago (e o simulador): consulta o provedor e aplica.

    O status vem sempre de ``consultar_pagamento``; o corpo da notificacao so
    diz qual pagamento consultar. Dinheiro que entrou numa cobranca que nao o
    aceita (valor diferente ou cobranca ja encerrada) e estornado na hora,
    fora da transacao, com chave de idempotencia derivada da referencia.
    """

    def __init__(
        self,
        uow: UnitOfWork,
        pagamentos: PagamentoRepository,
        gateway: GatewayPagamento,
        relogio: Relogio = agora_utc,
    ) -> None:
        self._uow = uow
        self._pagamentos = pagamentos
        self._gateway = gateway
        self._relogio = relogio

    def executar(self, referencia: str) -> PagamentoDTO | None:
        """``None`` quando a referencia nao corresponde a pagamento nosso."""
        situacao = self._gateway.consultar_pagamento(referencia)
        pagamento_id = _uuid(situacao.referencia_externa) if situacao else None
        if situacao is None or pagamento_id is None:
            _log.warning(
                "notificacao_pagamento_ignorada",
                extra={"referencia": referencia, "motivo": "desconhecido_no_provedor"},
            )
            return None
        consultado: SituacaoNoProvedor = situacao

        def trabalho() -> tuple[Pagamento, ResultadoAprovacao | None] | None:
            pagamento = self._pagamentos.obter_por_id(pagamento_id)
            if pagamento is None:
                return None
            agora = self._relogio()
            pagamento.registrar_notificacao(
                NotificacaoRecebida(
                    recebida_em=agora,
                    referencia_pagamento=consultado.referencia,
                    status_provedor=consultado.status_provedor,
                )
            )
            resultado = None
            if consultado.status is StatusPagamento.APROVADO:
                resultado = pagamento.aplicar_aprovacao(
                    referencia_pagamento=consultado.referencia,
                    valor_cobrado=consultado.valor,
                    agora=agora,
                )
            elif (
                consultado.status is StatusPagamento.RECUSADO
                and pagamento.status is StatusPagamento.PENDENTE
            ):
                pagamento.recusar(
                    motivo=consultado.detalhe or consultado.status_provedor
                )
            # PENDENTE ou ESTORNADO no provedor: so entra no historico.
            self._pagamentos.salvar(pagamento)
            return pagamento, resultado

        aplicado = self._uow.executar(trabalho)
        if aplicado is None:
            _log.warning(
                "notificacao_pagamento_ignorada",
                extra={"referencia": referencia, "motivo": "pagamento_inexistente"},
            )
            return None
        pagamento, resultado = aplicado
        if resultado in ESTORNO_AUTOMATICO:
            self._estornar_automaticamente(pagamento, consultado, resultado)
        return PagamentoDTO.de(pagamento)

    def _estornar_automaticamente(
        self,
        pagamento: Pagamento,
        situacao: SituacaoNoProvedor,
        resultado: ResultadoAprovacao | None,
    ) -> None:
        contexto = {
            "pagamento_id": str(pagamento.id),
            "referencia": situacao.referencia,
            "status": pagamento.status.value,
            "resultado": str(resultado),
        }
        # Falha transitoria propaga: o provedor reenvia a notificacao e o
        # estorno e repetido com a mesma chave.
        try:
            self._gateway.estornar(
                situacao.referencia,
                chave_idempotencia=f"estorno-automatico-{situacao.referencia}",
            )
        except GatewayPagamentoRecusouError as exc:
            _log.error(
                "estorno_automatico_recusado",
                extra={**contexto, "motivo": exc.mensagem},
            )
            return
        _log.warning("pagamento_estornado_automaticamente", extra=contexto)


def _uuid(valor: str | None) -> UUID | None:
    try:
        return UUID(valor) if valor else None
    except ValueError:
        return None


class SimularResultadoPagamento:
    """Simulador (MP_MODE=simulado): o "cliente" paga ou tem o cartao recusado.

    Percorre o mesmo ``ProcessarNotificacaoPagamento`` do webhook real.
    """

    def __init__(
        self,
        simulador: SimuladorDePagamento,
        pagamentos: PagamentoRepository,
        processar: ProcessarNotificacaoPagamento,
    ) -> None:
        self._simulador = simulador
        self._pagamentos = pagamentos
        self._processar = processar

    def executar(self, pagamento_id: UUID, *, aprovar: bool) -> PagamentoDTO:
        pagamento = _obter(self._pagamentos, pagamento_id)
        if pagamento.status is not StatusPagamento.PENDENTE:
            msg = f"Pagamento {pagamento.status} ja foi processado"
            raise TransicaoStatusInvalidaError(msg)
        referencia = self._simulador.registrar_resultado(
            pagamento_id=pagamento.id, valor=pagamento.valor, aprovado=aprovar
        )
        dto = self._processar.executar(referencia)
        if dto is None:  # referencia que o proprio provedor simulado nao conhece
            raise PagamentoNaoEncontradoError
        return dto


class ExpirarPagamentosVencidos:
    """Expira os pendentes com prazo esgotado (processo ``prazos``)."""

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
            # Confirmado ou recusado depois da listagem: a notificacao venceu.
            if pagamento is None or not pagamento.vencido(agora):
                return False
            pagamento.expirar(agora=agora)
            self._pagamentos.salvar(pagamento)
            return True

        try:
            return self._uow.executar(trabalho)
        except Exception:  # noqa: BLE001 - um documento com defeito nao trava a fila
            _log.exception(
                "expiracao_falhou", extra={"pagamento_id": str(pagamento_id)}
            )
            return False


class EstornarPagamento:
    """Compensacao ``EstornarPagamento`` da saga, idempotente pela chave.

    Pagamento aprovado e estornado no provedor; cobranca ainda pendente e
    encerrada sem dinheiro a devolver. Os dois respondem ``PagamentoEstornado``.
    Recusa do provedor vira ``EstornoDePagamentoFalhou`` (a saga vai para
    intervencao manual); falha transitoria, inclusive estorno ainda em
    processamento no provedor, propaga para o consumidor repetir com a mesma
    chave.
    """

    def __init__(
        self,
        uow: UnitOfWork,
        pagamentos: PagamentoRepository,
        gateway: GatewayPagamento,
        relogio: Relogio = agora_utc,
    ) -> None:
        self._uow = uow
        self._pagamentos = pagamentos
        self._gateway = gateway
        self._relogio = relogio

    def executar(
        self,
        *,
        ordem_id: UUID,
        pagamento_id: UUID,
        motivo: str,
        chave_idempotencia: str,
    ) -> PagamentoDTO:
        pagamento = _obter_da_ordem(self._pagamentos, pagamento_id, ordem_id)
        if pagamento.status is StatusPagamento.ESTORNADO:
            return PagamentoDTO.de(pagamento)
        if pagamento.status is StatusPagamento.PENDENTE:
            try:
                return self._executar(
                    pagamento_id,
                    lambda atual: atual.encerrar_cobranca(
                        encerrado_em=self._relogio(),
                        motivo=f"Cobranca encerrada antes do pagamento: {motivo}",
                    ),
                )
            except TransicaoStatusInvalidaError:
                # A aprovacao comitou entre a leitura e a transacao: refaz pelo
                # estado atual (agora o caminho do estorno no provedor).
                return self.executar(
                    ordem_id=ordem_id,
                    pagamento_id=pagamento_id,
                    motivo=motivo,
                    chave_idempotencia=chave_idempotencia,
                )
        referencia = pagamento.referencia_pagamento
        if pagamento.status is not StatusPagamento.APROVADO or referencia is None:
            return self._falhar(
                pagamento_id, f"Pagamento {pagamento.status} nao pode ser estornado"
            )
        try:
            self._gateway.estornar(referencia, chave_idempotencia=chave_idempotencia)
        except EstornoEmProcessamentoError:
            # Conclui so quando o provedor ja mostra o pagamento estornado;
            # senao o consumidor repete com a mesma chave.
            if not self._estornado_no_provedor(referencia):
                raise
        except GatewayPagamentoRecusouError as exc:
            # Uma tentativa anterior pode ter estornado no provedor e caido antes
            # de gravar aqui: o provedor recusa o segundo estorno, mas o
            # pagamento la ja consta estornado.
            if not self._estornado_no_provedor(referencia):
                return self._falhar(pagamento_id, exc.mensagem)
        return self._executar(
            pagamento_id,
            lambda atual: atual.estornar(
                estornado_em=self._relogio(),
                chave_idempotencia=chave_idempotencia,
                motivo=motivo,
            ),
        )

    def _estornado_no_provedor(self, referencia: str) -> bool:
        situacao = self._gateway.consultar_pagamento(referencia)
        return situacao is not None and situacao.status is StatusPagamento.ESTORNADO

    def _executar(
        self, pagamento_id: UUID, mudanca: Callable[[Pagamento], object]
    ) -> PagamentoDTO:
        def trabalho() -> Pagamento:
            atual = _obter(self._pagamentos, pagamento_id)
            # Outro consumidor ja concluiu o mesmo estorno: nada a gravar.
            if atual.status is not StatusPagamento.ESTORNADO:
                mudanca(atual)
                self._pagamentos.salvar(atual)
            return atual

        return PagamentoDTO.de(self._uow.executar(trabalho))

    def _falhar(self, pagamento_id: UUID, motivo: str) -> PagamentoDTO:
        _log.error(
            "estorno_de_pagamento_falhou",
            extra={"pagamento_id": str(pagamento_id), "motivo": motivo},
        )

        def trabalho() -> Pagamento:
            atual = _obter(self._pagamentos, pagamento_id)
            atual.registrar_falha_de_estorno(motivo)
            self._pagamentos.salvar(atual)
            return atual

        return PagamentoDTO.de(self._uow.executar(trabalho))


class ConsultarPagamentos:
    def __init__(self, pagamentos: PagamentoRepository) -> None:
        self._pagamentos = pagamentos

    def por_id(self, pagamento_id: UUID) -> PagamentoDTO:
        return PagamentoDTO.de(_obter(self._pagamentos, pagamento_id))
