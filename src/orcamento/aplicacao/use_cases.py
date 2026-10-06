"""Casos de uso do orcamento (API agora; consumidor de comandos no PR seguinte).

Cada mudanca de estado grava o evento do catalogo no outbox na mesma
transacao (ver ``UnitOfWork``).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from uuid import uuid4

from src.compartilhado.dominio.relogio import agora_utc
from src.orcamento.aplicacao.dtos import OrcamentoDTO
from src.orcamento.dominio.events import GeracaoDeOrcamentoFalhouEvent
from src.orcamento.dominio.exceptions import (
    OrcamentoJaGeradoError,
    OrcamentoNaoEncontradoError,
)
from src.orcamento.dominio.orcamento import (
    CanalDecisao,
    LinhaOrcamento,
    Orcamento,
    TipoItem,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime, timedelta
    from uuid import UUID

    from src.compartilhado.aplicacao.unit_of_work import UnitOfWork
    from src.compartilhado.dominio.relogio import Relogio
    from src.orcamento.aplicacao.dtos import ItemSolicitado
    from src.orcamento.aplicacao.link_decisao import LinkDeDecisao
    from src.orcamento.aplicacao.ports import TabelaDePrecosPort
    from src.orcamento.dominio.repository import OrcamentoRepository

_log = logging.getLogger(__name__)

MOTIVO_SEM_ITENS = "Diagnostico sem itens para orcar"
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

        Idempotente por ``ordem_id``: repetir devolve o orcamento existente,
        sem novo documento nem novo evento. Falha (diagnostico vazio ou codigo
        inexistente/inativo) grava ``GeracaoDeOrcamentoFalhou``.
        """
        try:
            return self._uow.executar(lambda: self._gerar(ordem_id, itens))
        except OrcamentoJaGeradoError:
            # Outra geracao da mesma ordem comitou entre a leitura e a escrita.
            existente = self._orcamentos.obter_por_ordem(ordem_id)
            if existente is None:
                raise
            return OrcamentoDTO.de(existente)

    def _gerar(
        self, ordem_id: UUID, itens: Sequence[ItemSolicitado]
    ) -> OrcamentoDTO | None:
        existente = self._orcamentos.obter_por_ordem(ordem_id)
        if existente is not None:
            return OrcamentoDTO.de(existente)
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
        # Segundo cheio: a expiracao do link (epoch em segundos) bate exata.
        valido_ate = (agora + self._validade).replace(microsecond=0)
        orcamento_id = uuid4()
        orcamento = Orcamento.gerar(
            id=orcamento_id,
            ordem_id=ordem_id,
            linhas=linhas,
            criado_em=agora,
            valido_ate=valido_ate,
            link_decisao=self._link.gerar(orcamento_id, valido_ate),
        )
        self._orcamentos.salvar(orcamento)
        return OrcamentoDTO.de(orcamento)

    def _falhar(self, ordem_id: UUID, motivo: str, invalidos: Sequence[str]) -> None:
        self._uow.registrar_evento(
            GeracaoDeOrcamentoFalhouEvent(
                ordem_id=ordem_id, motivo=motivo, codigos_invalidos=tuple(invalidos)
            )
        )


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
        orcamento_id = self._link.validar(token, agora=self._relogio())
        return self._decidir(orcamento_id, aprovar=aprovar, canal=CanalDecisao.LINK)

    def por_atendente(self, orcamento_id: UUID, *, aprovar: bool) -> OrcamentoDTO:
        return self._decidir(
            orcamento_id, aprovar=aprovar, canal=CanalDecisao.ATENDENTE
        )

    def _decidir(
        self, orcamento_id: UUID, *, aprovar: bool, canal: CanalDecisao
    ) -> OrcamentoDTO:
        def trabalho() -> Orcamento:
            # Relido a cada tentativa: se a expiracao comitou antes, a decisao
            # ve o orcamento EXPIRADO e falha (sem sobrescrever).
            orcamento = _obter(self._orcamentos, orcamento_id)
            agora = self._relogio()
            if aprovar:
                orcamento.aprovar(canal=canal, agora=agora)
            else:
                orcamento.recusar(canal=canal, agora=agora)
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
        """Quantidade expirada nesta rodada (cada orcamento na sua transacao)."""
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
                "expiracao_falhou", extra={"orcamento_id": str(orcamento_id)}
            )
            return False


class CancelarOrcamento:
    """Compensacao ``CancelarOrcamento`` da saga. Repetir nao gera outro evento."""

    def __init__(self, uow: UnitOfWork, orcamentos: OrcamentoRepository) -> None:
        self._uow = uow
        self._orcamentos = orcamentos

    def executar(
        self, *, ordem_id: UUID, orcamento_id: UUID, motivo: str
    ) -> OrcamentoDTO:
        def trabalho() -> Orcamento:
            orcamento = _obter(self._orcamentos, orcamento_id)
            if orcamento.ordem_id != ordem_id:
                msg = "Orcamento nao pertence a ordem de servico informada"
                raise OrcamentoNaoEncontradoError(msg)
            if orcamento.cancelar(motivo=motivo):
                self._orcamentos.salvar(orcamento)
            return orcamento

        return OrcamentoDTO.de(self._uow.executar(trabalho))


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
        orcamento_id = self._link.validar(token, agora=self._relogio())
        return OrcamentoDTO.de(_obter(self._orcamentos, orcamento_id))
