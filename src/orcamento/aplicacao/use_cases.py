"""Casos de uso do orcamento: os da API e os dos comandos da saga, que o
consumidor de comandos (ADR-036) chama.

Cada mudanca de estado grava o evento do catalogo na outbox na mesma
transacao (ver ``UnitOfWork``).
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

from src.compartilhado.dominio.exceptions import (
    TransicaoStatusInvalidaError,
    ValorInvalidoError,
)
from src.compartilhado.dominio.relogio import agora_utc
from src.orcamento.aplicacao.dtos import OrcamentoDTO
from src.orcamento.dominio.events import GeracaoDeOrcamentoFalhouEvent
from src.orcamento.dominio.exceptions import (
    LinkDeDecisaoInvalidoError,
    OrcamentoJaGeradoError,
    OrcamentoNaoEncontradoError,
    OrcamentoVencidoError,
)
from src.orcamento.dominio.orcamento import (
    CanalDecisao,
    LinhaOrcamento,
    Orcamento,
    StatusOrcamento,
    TipoItem,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime
    from uuid import UUID

    from src.compartilhado.aplicacao.unit_of_work import UnitOfWork
    from src.compartilhado.dominio.relogio import Relogio
    from src.orcamento.aplicacao.dtos import ItemSolicitado
    from src.orcamento.aplicacao.link_decisao import LinkDeDecisao
    from src.orcamento.aplicacao.ports import Cotacao, TabelaDePrecosPort
    from src.orcamento.dominio.repository import OrcamentoRepository

_log = logging.getLogger(__name__)

MOTIVO_SEM_ITENS = "Diagnostico sem itens para orcar"
# Codigo do log de comando ignorado: o original que chegou depois da lapide.
MOTIVO_LAPIDE = "LAPIDE"
MOTIVO_ITENS_INVALIDOS = "Itens inexistentes ou inativos na tabela de precos"


def _obter(orcamentos: OrcamentoRepository, orcamento_id: UUID) -> Orcamento:
    orcamento = orcamentos.obter_por_id(orcamento_id)
    if orcamento is None:
        raise OrcamentoNaoEncontradoError
    return orcamento


class GerarOrcamento:
    """Comando ``GerarOrcamento``: congela os precos vigentes nas linhas."""

    def __init__(
        self,
        uow: UnitOfWork,
        orcamentos: OrcamentoRepository,
        precos: TabelaDePrecosPort,
        link: LinkDeDecisao,
        validade: timedelta,
        relogio: Relogio = agora_utc,
    ) -> None:
        self._uow = uow
        self._orcamentos = orcamentos
        self._precos = precos
        self._link = link
        self._validade = validade
        self._relogio = relogio

    def executar(
        self, *, ordem_id: UUID, itens: Sequence[ItemSolicitado]
    ) -> OrcamentoDTO | None:
        """Gera o orcamento da ordem; ``None`` quando a geracao falha.

        Idempotente por ``ordem_id``: repetir nao gera outro orcamento e
        republica o ``OrcamentoGerado`` registrado (o reenvio do orquestrador
        espera a resposta). Falha (diagnostico vazio, codigo inexistente ou
        inativo, quantidade fora do limite ou total acima do teto) responde
        ``GeracaoDeOrcamentoFalhou``; sem orcamento gravado, a repeticao
        reavalia os mesmos itens e responde a falha de novo. A lapide
        (cancelamento que chegou antes) descarta o comando, sem resposta.
        """
        try:
            return self._uow.executar(lambda: self._gerar(ordem_id, itens))
        except OrcamentoJaGeradoError:
            # Outra geracao (ou a lapide) da mesma ordem comitou entre a
            # leitura e a escrita: a nova tentativa a encontra.
            return self._uow.executar(lambda: self._gerar(ordem_id, itens))

    def _gerar(
        self, ordem_id: UUID, itens: Sequence[ItemSolicitado]
    ) -> OrcamentoDTO | None:
        existente = self._orcamentos.obter_por_ordem(ordem_id)
        if existente is not None:
            return self._repetido(existente)
        if not itens:
            self._falhar(ordem_id, MOTIVO_SEM_ITENS, ())
            return None
        cotacao = self._precos.cotar(
            servicos=_codigos(itens, TipoItem.SERVICO),
            pecas=_codigos(itens, TipoItem.PECA),
        )
        if cotacao.invalidos:
            self._falhar(ordem_id, MOTIVO_ITENS_INVALIDOS, cotacao.invalidos)
            return None
        try:
            orcamento = self._montar(ordem_id, itens, cotacao)
        except ValorInvalidoError as exc:
            # Quantidade fora do limite ou total acima do teto do Dinheiro: a
            # mensagem do dominio e o motivo (curta, sem o valor recebido).
            self._falhar(ordem_id, str(exc), ())
            return None
        self._orcamentos.salvar(orcamento)
        return OrcamentoDTO.de(orcamento)

    def _montar(
        self, ordem_id: UUID, itens: Sequence[ItemSolicitado], cotacao: Cotacao
    ) -> Orcamento:
        linhas = []
        for item in itens:
            tabela = (
                cotacao.servicos if item.tipo is TipoItem.SERVICO else cotacao.pecas
            )
            preco = tabela[item.codigo]
            linhas.append(
                LinhaOrcamento(
                    tipo=item.tipo,
                    codigo=item.codigo,
                    descricao=preco.descricao,
                    quantidade=item.quantidade,
                    preco_unitario=preco.preco_unitario,
                )
            )
        agora = self._relogio()
        valido_ate = _segundo_cheio_acima(agora + self._validade)
        orcamento_id = uuid4()
        return Orcamento.gerar(
            id=orcamento_id,
            ordem_id=ordem_id,
            linhas=linhas,
            criado_em=agora,
            valido_ate=valido_ate,
            link_decisao=self._link.gerar(orcamento_id, valido_ate),
        )

    def _repetido(self, existente: Orcamento) -> OrcamentoDTO:
        if existente.valido_ate is None:
            # A compensacao chegou antes (lapide): sem efeito e sem resposta.
            _log.info(
                "command_ignored",
                extra={
                    "comando": "GerarOrcamento",
                    "motivo": MOTIVO_LAPIDE,
                    "ordem_id": str(existente.ordem_id),
                },
            )
            return OrcamentoDTO.de(existente)
        link = self._link.gerar(existente.id, existente.valido_ate)
        self._uow.registrar_evento(existente.desfecho_da_geracao(link))
        return OrcamentoDTO.de(existente)

    def _falhar(self, ordem_id: UUID, motivo: str, invalidos: Sequence[str]) -> None:
        self._uow.registrar_evento(
            GeracaoDeOrcamentoFalhouEvent(
                ordem_id=ordem_id, motivo=motivo, codigos_invalidos=tuple(invalidos)
            )
        )


def _segundo_cheio_acima(instante: datetime) -> datetime:
    """``valido_ate`` em segundo cheio: o token do link assina ``exp`` em epoch
    de segundos (``exp = valido_ate``). Para cima, nunca antes de ``agora``."""
    if not instante.microsecond:
        return instante
    return instante.replace(microsecond=0) + timedelta(seconds=1)


def _codigos(itens: Sequence[ItemSolicitado], tipo: TipoItem) -> list[str]:
    # Ordem de chegada sem repeticao: a lista de invalidos sai deterministica.
    return list(dict.fromkeys(item.codigo for item in itens if item.tipo is tipo))


class DecidirOrcamento:
    """Decisao do cliente: pelo link publico assinado ou pelo atendente."""

    def __init__(
        self,
        uow: UnitOfWork,
        orcamentos: OrcamentoRepository,
        link: LinkDeDecisao,
        relogio: Relogio = agora_utc,
    ) -> None:
        self._uow = uow
        self._orcamentos = orcamentos
        self._link = link
        self._relogio = relogio

    def por_link(self, token: str, *, aprovar: bool) -> OrcamentoDTO:
        """Decisao unica pelo link; qualquer falha e o mesmo 404 (ADR-039)."""
        orcamento_id = self._link.validar(token, agora=self._relogio())
        try:
            return self._decidir(orcamento_id, aprovar=aprovar, canal=CanalDecisao.LINK)
        except (
            OrcamentoNaoEncontradoError,
            OrcamentoVencidoError,
            TransicaoStatusInvalidaError,
        ):
            raise LinkDeDecisaoInvalidoError from None

    def por_atendente(
        self, orcamento_id: UUID, *, aprovar: bool, decidido_por: str
    ) -> OrcamentoDTO:
        """Decisao em nome do cliente; ``decidido_por`` = ``sub`` do atendente."""
        return self._decidir(
            orcamento_id,
            aprovar=aprovar,
            canal=CanalDecisao.ATENDENTE,
            decidido_por=decidido_por,
        )

    def _decidir(
        self,
        orcamento_id: UUID,
        *,
        aprovar: bool,
        canal: CanalDecisao,
        decidido_por: str | None = None,
    ) -> OrcamentoDTO:
        def trabalho() -> Orcamento:
            # Relido a cada tentativa: se a expiracao comitou antes, a decisao
            # ve o orcamento EXPIRADO e falha (sem sobrescrever).
            orcamento = _obter(self._orcamentos, orcamento_id)
            decidir = orcamento.aprovar if aprovar else orcamento.recusar
            decidir(canal=canal, agora=self._relogio(), decidido_por=decidido_por)
            self._orcamentos.salvar(orcamento)
            return orcamento

        return OrcamentoDTO.de(self._uow.executar(trabalho))


class ExpirarOrcamentosVencidos:
    """Expira os pendentes com prazo esgotado (processo ``prazos``)."""

    def __init__(
        self,
        uow: UnitOfWork,
        orcamentos: OrcamentoRepository,
        relogio: Relogio = agora_utc,
    ) -> None:
        self._uow = uow
        self._orcamentos = orcamentos
        self._relogio = relogio

    def executar(self, *, limite: int = 100) -> int:
        """Quantidade expirada neste ciclo (cada orcamento na sua transacao)."""
        agora = self._relogio()
        vencidos = self._orcamentos.listar_vencidos(agora, limite)
        return sum(self._expirar(orcamento_id, agora) for orcamento_id in vencidos)

    def _expirar(self, orcamento_id: UUID, agora: datetime) -> bool:
        def trabalho() -> bool:
            orcamento = self._orcamentos.obter_por_id(orcamento_id)
            # Decidido ou cancelado depois da listagem: a outra escrita venceu.
            if orcamento is None or not orcamento.vencido(agora):
                return False
            orcamento.expirar(agora=agora)
            self._orcamentos.salvar(orcamento)
            return True

        try:
            return self._uow.executar(trabalho)
        except Exception:  # noqa: BLE001 - um documento com defeito nao trava a fila
            _log.exception(
                "budget_expiration_failed", extra={"orcamento_id": str(orcamento_id)}
            )
            return False


class CancelarOrcamento:
    """Compensacao ``CancelarOrcamento`` da saga, sempre com resposta.

    Acha o orcamento pelo ``ordem_id`` (o ``orcamento_id`` so vem quando o
    orquestrador ja o conhece). Pendente ou aprovado: cancela. Ja encerrado
    (inclusive na repeticao): responde ``OrcamentoCancelado`` sem mudar nada.
    Sem orcamento (``GerarOrcamento`` ainda em voo): grava a lapide.
    """

    def __init__(
        self,
        uow: UnitOfWork,
        orcamentos: OrcamentoRepository,
        relogio: Relogio = agora_utc,
    ) -> None:
        self._uow = uow
        self._orcamentos = orcamentos
        self._relogio = relogio

    def executar(
        self, *, ordem_id: UUID, orcamento_id: UUID | None = None, motivo: str
    ) -> OrcamentoDTO:
        try:
            return self._uow.executar(
                lambda: self._cancelar(ordem_id, orcamento_id, motivo)
            )
        except OrcamentoJaGeradoError:
            # A geracao (ou outra lapide) comitou primeiro: cancela o que existe.
            return self._uow.executar(
                lambda: self._cancelar(ordem_id, orcamento_id, motivo)
            )

    def _cancelar(
        self, ordem_id: UUID, orcamento_id: UUID | None, motivo: str
    ) -> OrcamentoDTO:
        orcamento = self._orcamentos.obter_por_ordem(ordem_id)
        if orcamento is None:
            orcamento = Orcamento.lapide(
                id=uuid4(),
                ordem_id=ordem_id,
                cancelado_em=self._relogio(),
                motivo=motivo,
            )
            self._orcamentos.salvar(orcamento)
        elif orcamento_id is not None and orcamento.id != orcamento_id:
            msg = "Orcamento nao pertence a ordem de servico informada"
            raise OrcamentoNaoEncontradoError(msg)
        elif orcamento.cancelar(motivo=motivo):
            self._orcamentos.salvar(orcamento)
        else:
            self._uow.registrar_evento(orcamento.desfecho_do_cancelamento())
        return OrcamentoDTO.de(orcamento)


class ConsultarOrcamentos:
    def __init__(
        self,
        orcamentos: OrcamentoRepository,
        link: LinkDeDecisao,
        relogio: Relogio = agora_utc,
    ) -> None:
        self._orcamentos = orcamentos
        self._link = link
        self._relogio = relogio

    def por_id(self, orcamento_id: UUID) -> OrcamentoDTO:
        return OrcamentoDTO.de(_obter(self._orcamentos, orcamento_id))

    def por_ordem(self, ordem_id: UUID) -> list[OrcamentoDTO]:
        orcamento = self._orcamentos.obter_por_ordem(ordem_id)
        return [OrcamentoDTO.de(orcamento)] if orcamento else []

    def por_link(self, token: str) -> OrcamentoDTO:
        """So o orcamento ainda PENDENTE; decidido, encerrado ou inexistente e o
        mesmo 404 do link invalido (o token nao vira oraculo de estado)."""
        orcamento_id = self._link.validar(token, agora=self._relogio())
        orcamento = self._orcamentos.obter_por_id(orcamento_id)
        if orcamento is None or orcamento.status is not StatusOrcamento.PENDENTE:
            raise LinkDeDecisaoInvalidoError
        return OrcamentoDTO.de(orcamento)
