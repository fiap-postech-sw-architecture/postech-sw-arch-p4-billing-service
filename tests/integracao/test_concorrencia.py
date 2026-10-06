"""Corridas reais entre transacoes do MongoDB.

A barreira forca a pior intercalacao: as duas transacoes leem o mesmo
estado antes de qualquer escrita. O ``WriteConflict`` faz a perdedora repetir
o trabalho, e a releitura decide o resultado (contramedida *reread value*).
"""

from __future__ import annotations

import threading
from datetime import timedelta
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest

from src.compartilhado.infraestrutura.unit_of_work import MongoUnitOfWork
from src.orcamento.aplicacao.dtos import ItemSolicitado
from src.orcamento.aplicacao.use_cases import (
    CancelarOrcamento,
    DecidirOrcamento,
    ExpirarOrcamentosVencidos,
    GerarOrcamento,
)
from src.orcamento.dominio.exceptions import (
    OrcamentoJaGeradoError,
    OrcamentoVencidoError,
)
from src.orcamento.dominio.orcamento import CanalDecisao, TipoItem
from src.orcamento.infraestrutura.repository import MongoOrcamentoRepository
from src.orcamento.infraestrutura.tabela_de_precos import TabelaDePrecosMongoAdapter
from src.pagamento.aplicacao.ports import GatewayPagamentoRecusouError
from src.pagamento.aplicacao.use_cases import (
    EstornarPagamento,
    ExpirarPagamentosVencidos,
    ProcessarNotificacaoPagamento,
    SolicitarPagamento,
)
from src.pagamento.dominio.exceptions import PagamentoJaSolicitadoError
from src.pagamento.infraestrutura.orcamentos import OrcamentosMongoAdapter
from src.pagamento.infraestrutura.repository import MongoPagamentoRepository
from src.seed import semear
from tests.factories import (
    AGORA,
    ATENDENTE_SUB,
    confirmar,
    orcamento,
    pagamento,
    situacao,
)
from tests.integracao.apoio import (
    LINK,
    GatewayRoteirizado,
    MetricasEspia,
    RelogioFixo,
    eventos_do_outbox,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from pymongo.database import Database

    Banco = Database[dict[str, Any]]


class ComBarreira:
    """Repositorio que, na 1a leitura de ``metodo``, espera a outra thread ler.

    Com ``depois_de``, a thread ainda segura a leitura (snapshot antigo) ate a
    outra comitar: a escrita dela vem depois e esbarra no ``WriteConflict``.
    """

    def __init__(
        self,
        repo: Any,
        metodo: str,
        barreira: threading.Barrier,
        depois_de: threading.Event | None = None,
    ) -> None:
        self._repo = repo
        self._metodo = metodo
        self._barreira = barreira
        self._depois_de = depois_de
        self._primeira = True

    def __getattr__(self, nome: str) -> Any:
        original = getattr(self._repo, nome)
        if nome != self._metodo:
            return original

        def lendo(*args: Any, **kwargs: Any) -> Any:
            resultado = original(*args, **kwargs)
            if self._primeira:
                self._primeira = False
                self._barreira.wait(timeout=10)
                if self._depois_de is not None:
                    self._depois_de.wait(timeout=10)
            return resultado

        return lendo


def em_paralelo(*tarefas: Callable[[], object]) -> list[object]:
    """Roda as tarefas em threads; devolve resultado ou excecao de cada uma."""
    resultados: list[object] = [None] * len(tarefas)

    def rodar(indice: int, tarefa: Callable[[], object]) -> None:
        try:
            resultados[indice] = tarefa()
        except Exception as exc:
            resultados[indice] = exc

    threads = [
        threading.Thread(target=rodar, args=(i, t)) for i, t in enumerate(tarefas)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    return resultados


@pytest.mark.parametrize("vencedor", ["decisao", "expiracao", None])
def test_corrida_entre_decisao_e_expiracao_tem_um_so_vencedor(
    banco: Banco, vencedor: str | None
) -> None:
    """Decisao no ultimo instante do prazo x job de expiracao logo depois.

    As duas leem PENDENTE antes de qualquer escrita. ``vencedor`` escolhe quem
    comita primeiro (``None``: ordem livre); a outra esbarra no conflito,
    rele e respeita o que ja foi gravado. Nunca os dois eventos.
    """
    gerado = orcamento(criado_em=AGORA - timedelta(hours=72))
    uow = MongoUnitOfWork(banco)
    uow.executar(lambda: MongoOrcamentoRepository(uow).salvar(gerado))
    barreira = threading.Barrier(2)
    comitou = {"decisao": threading.Event(), "expiracao": threading.Event()}
    no_limite = RelogioFixo(gerado.valido_ate)
    depois_do_limite = RelogioFixo(gerado.valido_ate + timedelta(milliseconds=1))

    def espera(lado: str) -> threading.Event | None:
        if vencedor is None or vencedor == lado:
            return None
        return comitou[vencedor]

    def decidir() -> object:
        uow = MongoUnitOfWork(banco)
        repo = ComBarreira(
            MongoOrcamentoRepository(uow), "obter_por_id", barreira, espera("decisao")
        )
        try:
            return DecidirOrcamento(uow, repo, LINK, no_limite).por_atendente(
                gerado.id, aprovar=True, decidido_por=ATENDENTE_SUB
            )
        finally:
            comitou["decisao"].set()

    def expirar() -> object:
        uow = MongoUnitOfWork(banco)
        repo = ComBarreira(
            MongoOrcamentoRepository(uow),
            "obter_por_id",
            barreira,
            espera("expiracao"),
        )
        try:
            return ExpirarOrcamentosVencidos(uow, repo, depois_do_limite).executar()
        finally:
            comitou["expiracao"].set()

    decisao, expirados = em_paralelo(decidir, expirar)

    final = MongoOrcamentoRepository(MongoUnitOfWork(banco)).obter_por_id(gerado.id)
    assert final is not None
    tipos = [e["tipo"] for e in eventos_do_outbox(banco)]
    ganhou = vencedor or ("decisao" if final.status.value == "APROVADO" else "")
    if ganhou == "decisao":
        assert final.status.value == "APROVADO"
        assert expirados == 0  # o job releu APROVADO e desistiu
        assert tipos == ["OrcamentoGerado", "OrcamentoAprovado"]
        assert final.decisao is not None
        assert final.decisao.canal is CanalDecisao.ATENDENTE
    else:
        assert final.status.value == "EXPIRADO"
        assert expirados == 1
        assert isinstance(decisao, OrcamentoVencidoError)  # releu EXPIRADO
        assert tipos == ["OrcamentoGerado", "OrcamentoExpirado"]


def test_gerar_orcamento_em_paralelo_para_a_mesma_ordem_nao_duplica(
    banco: Banco,
) -> None:
    semear(banco)
    ordem_id = uuid4()
    barreira = threading.Barrier(2)
    itens = [ItemSolicitado(TipoItem.SERVICO, "SRV-FREIOS", 1)]

    def gerar() -> object:
        uow = MongoUnitOfWork(banco)
        repo = ComBarreira(MongoOrcamentoRepository(uow), "obter_por_ordem", barreira)
        return GerarOrcamento(
            uow, repo, TabelaDePrecosMongoAdapter(uow), LINK, timedelta(hours=72)
        ).executar(ordem_id=ordem_id, itens=itens)

    primeiro, segundo = em_paralelo(gerar, gerar)

    assert not isinstance(primeiro, Exception), primeiro
    assert primeiro == segundo
    assert banco["orcamentos"].count_documents({"ordem_id": ordem_id}) == 1
    # A perdedora rele o orcamento da vencedora e republica a mesma resposta.
    gerados = eventos_do_outbox(banco, "OrcamentoGerado")
    assert len(gerados) == 2
    assert gerados[0]["dados"] == gerados[1]["dados"]


def test_solicitar_pagamento_em_paralelo_para_o_mesmo_orcamento_nao_duplica(
    banco: Banco,
) -> None:
    aprovado = orcamento(criado_em=AGORA)
    aprovado.aprovar(canal=CanalDecisao.LINK, agora=AGORA)
    uow = MongoUnitOfWork(banco)
    uow.executar(lambda: MongoOrcamentoRepository(uow).salvar(aprovado))
    gateway = GatewayRoteirizado()
    barreira = threading.Barrier(2)

    def solicitar() -> object:
        uow = MongoUnitOfWork(banco)
        repo = ComBarreira(MongoPagamentoRepository(uow), "obter_por_ordem", barreira)
        return SolicitarPagamento(
            uow,
            repo,
            OrcamentosMongoAdapter(uow),
            gateway,
            timedelta(minutes=60),
        ).executar(ordem_id=aprovado.ordem_id, orcamento_id=aprovado.id)

    primeiro, segundo = em_paralelo(solicitar, solicitar)

    assert not isinstance(primeiro, Exception), primeiro
    assert primeiro == segundo
    assert banco["pagamentos"].count_documents({}) == 1
    solicitados = eventos_do_outbox(banco, "PagamentoSolicitado")
    assert len(solicitados) == 2
    assert solicitados[0]["dados"] == solicitados[1]["dados"]
    # As duas threads criaram cobranca antes de gravar: a perdedora fica orfa
    # no provedor e expira sozinha (custo aceito da chamada fora da transacao).
    assert len(gateway.cobrancas) == 2


class LeituraAtrasada:
    """Repositorio cuja leitura de ``metodo`` devolve ``None`` ``vezes`` vezes.

    Simula a outra transacao que comitou depois da nossa leitura: a escrita
    esbarra no indice unico (``DuplicateKeyError``) em vez do conflito
    transitorio, e o caso de uso cai no caminho de recuperacao.
    """

    def __init__(self, repo: Any, metodo: str, vezes: int) -> None:
        self._repo = repo
        self._metodo = metodo
        self._vezes = vezes

    def __getattr__(self, nome: str) -> Any:
        original = getattr(self._repo, nome)
        if nome != self._metodo:
            return original

        def lendo(*args: Any, **kwargs: Any) -> Any:
            if self._vezes > 0:
                self._vezes -= 1
                return None
            return original(*args, **kwargs)

        return lendo


def gerar_com_leitura_atrasada(banco: Banco, vezes: int) -> GerarOrcamento:
    uow = MongoUnitOfWork(banco)
    repo = LeituraAtrasada(MongoOrcamentoRepository(uow), "obter_por_ordem", vezes)
    return GerarOrcamento(
        uow, repo, TabelaDePrecosMongoAdapter(uow), LINK, timedelta(hours=72)
    )


def test_gerar_orcamento_que_perde_no_indice_unico_devolve_o_existente(
    banco: Banco,
) -> None:
    semear(banco)
    ordem_id = uuid4()
    itens = [ItemSolicitado(TipoItem.SERVICO, "SRV-FREIOS", 1)]
    primeiro = gerar_com_leitura_atrasada(banco, 0).executar(
        ordem_id=ordem_id, itens=itens
    )

    segundo = gerar_com_leitura_atrasada(banco, 1).executar(
        ordem_id=ordem_id, itens=itens
    )

    assert segundo == primeiro
    assert len(eventos_do_outbox(banco, "OrcamentoGerado")) == 2
    # Se nem a releitura acha o orcamento, o erro do indice sobe.
    with pytest.raises(OrcamentoJaGeradoError):
        gerar_com_leitura_atrasada(banco, 3).executar(ordem_id=ordem_id, itens=itens)


def test_solicitacao_sem_o_pagamento_na_releitura_propaga_o_erro(
    banco: Banco,
) -> None:
    aprovado = orcamento(criado_em=AGORA)
    aprovado.aprovar(canal=CanalDecisao.LINK, agora=AGORA)
    uow = MongoUnitOfWork(banco)
    uow.executar(lambda: MongoOrcamentoRepository(uow).salvar(aprovado))
    gateway = GatewayRoteirizado()

    def solicitar(vezes: int) -> object:
        uow = MongoUnitOfWork(banco)
        repo = LeituraAtrasada(MongoPagamentoRepository(uow), "obter_por_ordem", vezes)
        return SolicitarPagamento(
            uow, repo, OrcamentosMongoAdapter(uow), gateway, timedelta(minutes=60)
        ).executar(ordem_id=aprovado.ordem_id, orcamento_id=aprovado.id)

    solicitar(0)
    with pytest.raises(PagamentoJaSolicitadoError):
        solicitar(2)
    assert banco["pagamentos"].count_documents({}) == 1


def test_pagamento_confirmado_entre_a_listagem_e_a_expiracao_nao_expira(
    banco: Banco,
) -> None:
    pendente = pagamento(criado_em=AGORA)
    uow = MongoUnitOfWork(banco)
    repo = MongoPagamentoRepository(uow)
    uow.executar(lambda: repo.salvar(pendente))

    class ConfirmaDepoisDeListar:
        def __getattr__(self, nome: str) -> Any:
            return getattr(repo, nome)

        def listar_vencidos(self, agora: Any, limite: int) -> list[Any]:
            vencidos = repo.listar_vencidos(agora, limite)
            outro = MongoUnitOfWork(banco)
            outro_repo = MongoPagamentoRepository(outro)

            def aprovar_no_provedor() -> None:
                atual = outro_repo.obter_por_id(pendente.id)
                assert atual is not None
                confirmar(atual)
                outro_repo.salvar(atual)

            outro.executar(aprovar_no_provedor)
            return vencidos

    expirar = ExpirarPagamentosVencidos(
        uow, ConfirmaDepoisDeListar(), RelogioFixo(AGORA + timedelta(hours=2))
    )

    assert expirar.executar() == 0
    tipos = [e["tipo"] for e in eventos_do_outbox(banco)]
    assert tipos == ["PagamentoSolicitado", "PagamentoConfirmado"]


def test_estorno_concluido_por_outro_consumidor_nao_gera_segundo_evento(
    banco: Banco,
) -> None:
    pago = pagamento(criado_em=AGORA)
    confirmar(pago, referencia="123")
    uow = MongoUnitOfWork(banco)
    uow.executar(lambda: MongoPagamentoRepository(uow).salvar(pago))

    class EstornoConcorrente(GatewayRoteirizado):
        def estornar(self, referencia: str, *, chave_idempotencia: str) -> None:
            # Outro consumidor terminou o mesmo estorno enquanto este chamava
            # o provedor (mesma chave: o provedor estornou uma vez so).
            outro = MongoUnitOfWork(banco)
            outro_repo = MongoPagamentoRepository(outro)

            def concluir() -> None:
                atual = outro_repo.obter_por_id(pago.id)
                assert atual is not None
                atual.concluir_compensacao(agora=AGORA, motivo="outro consumidor")
                outro_repo.salvar(atual)

            outro.executar(concluir)

    uow = MongoUnitOfWork(banco)
    metricas = MetricasEspia()
    resultado = EstornarPagamento(
        uow, MongoPagamentoRepository(uow), EstornoConcorrente(), metricas
    ).executar(ordem_id=pago.ordem_id, pagamento_id=pago.id, motivo="este consumidor")

    assert resultado.status == "ESTORNADO"
    assert resultado.motivo == "outro consumidor"
    # O comando repetido republica o desfecho que o outro consumidor gravou.
    estornos = eventos_do_outbox(banco, "PagamentoEstornado")
    assert len(estornos) == 2
    assert estornos[0]["dados"] == estornos[1]["dados"]
    assert metricas.estornos == []


def test_estorno_automatico_registrado_por_outro_consumidor_conta_uma_vez(
    banco: Banco,
) -> None:
    """Webhook e conciliacao veem a mesma aprovacao tardia ao mesmo tempo."""
    cancelado = pagamento(criado_em=AGORA)
    cancelado.concluir_compensacao(agora=AGORA, motivo="cancelamento")
    uow = MongoUnitOfWork(banco)
    uow.executar(lambda: MongoPagamentoRepository(uow).salvar(cancelado))
    tardia = situacao(cancelado, referencia="tardia")

    class OutroConsumidorRegistraAntes(GatewayRoteirizado):
        def estornar(self, referencia: str, *, chave_idempotencia: str) -> None:
            super().estornar(referencia, chave_idempotencia=chave_idempotencia)
            outro = MongoUnitOfWork(banco)
            outro_repo = MongoPagamentoRepository(outro)

            def registrar() -> None:
                atual = outro_repo.obter_por_id(cancelado.id)
                assert atual is not None
                atual.registrar_estorno_automatico(referencia, agora=AGORA)
                outro_repo.salvar(atual)

            outro.executar(registrar)

    metricas = MetricasEspia()
    uow = MongoUnitOfWork(banco)
    resultado = ProcessarNotificacaoPagamento(
        uow, MongoPagamentoRepository(uow), OutroConsumidorRegistraAntes(), metricas, 3
    ).aplicar(tardia)

    assert resultado is not None
    assert resultado.status == "ESTORNADO"
    assert len(eventos_do_outbox(banco, "PagamentoEstornado")) == 1
    assert metricas.estornos == []


def test_estorno_recusado_mas_concluido_por_outro_consumidor_republica(
    banco: Banco,
) -> None:
    pago = pagamento(criado_em=AGORA)
    confirmar(pago, referencia="123")
    uow = MongoUnitOfWork(banco)
    uow.executar(lambda: MongoPagamentoRepository(uow).salvar(pago))

    class RecusaDepoisDoOutro(GatewayRoteirizado):
        def estornar(self, referencia: str, *, chave_idempotencia: str) -> None:
            outro = MongoUnitOfWork(banco)
            outro_repo = MongoPagamentoRepository(outro)

            def concluir() -> None:
                atual = outro_repo.obter_por_id(pago.id)
                assert atual is not None
                atual.concluir_compensacao(agora=AGORA, motivo="outro consumidor")
                outro_repo.salvar(atual)

            outro.executar(concluir)
            raise GatewayPagamentoRecusouError("already refunded")

    uow = MongoUnitOfWork(banco)
    resultado = EstornarPagamento(
        uow, MongoPagamentoRepository(uow), RecusaDepoisDoOutro(), MetricasEspia()
    ).executar(ordem_id=pago.ordem_id, pagamento_id=pago.id, motivo="x")

    assert resultado.status == "ESTORNADO"
    assert eventos_do_outbox(banco, "EstornoDePagamentoFalhou") == []
    assert len(eventos_do_outbox(banco, "PagamentoEstornado")) == 2


def test_compensacao_que_nunca_estabiliza_falha_alto(banco: Banco) -> None:
    """Defeito (leitura sempre velha): para depois de 3 replanejamentos."""
    pendente = pagamento(criado_em=AGORA)
    uow = MongoUnitOfWork(banco)
    repo = MongoPagamentoRepository(uow)
    uow.executar(lambda: repo.salvar(pendente))
    antes = repo.obter_por_id(pendente.id)
    pago = repo.obter_por_id(pendente.id)
    assert pago is not None
    confirmar(pago, referencia="123")
    uow.executar(lambda: repo.salvar(pago))

    class LeituraVelhaForaDaTransacao:
        def __init__(self, uow: MongoUnitOfWork) -> None:
            self._uow = uow
            self._repo = MongoPagamentoRepository(uow)

        def __getattr__(self, nome: str) -> Any:
            return getattr(self._repo, nome)

        def obter_por_ordem(self, ordem_id: Any) -> Any:
            if self._uow.sessao is None:
                return antes
            return self._repo.obter_por_ordem(ordem_id)

    uow = MongoUnitOfWork(banco)
    caso = EstornarPagamento(
        uow, LeituraVelhaForaDaTransacao(uow), GatewayRoteirizado(), MetricasEspia()
    )
    with pytest.raises(RuntimeError, match="nao estabilizou"):
        caso.executar(ordem_id=pendente.ordem_id, pagamento_id=pendente.id, motivo="x")


def test_lapide_que_perde_para_a_geracao_cancela_o_orcamento_gerado(
    banco: Banco,
) -> None:
    """CancelarOrcamento leu a ordem vazia; a geracao comitou antes da lapide."""
    gerado = orcamento(criado_em=AGORA)
    uow = MongoUnitOfWork(banco)
    uow.executar(lambda: MongoOrcamentoRepository(uow).salvar(gerado))

    uow = MongoUnitOfWork(banco)
    repo = LeituraAtrasada(MongoOrcamentoRepository(uow), "obter_por_ordem", 1)
    resultado = CancelarOrcamento(uow, repo, RelogioFixo()).executar(
        ordem_id=gerado.ordem_id, motivo="cancelamento"
    )

    assert (resultado.id, resultado.status) == (gerado.id, "CANCELADO")
    assert banco["orcamentos"].count_documents({}) == 1
    assert len(eventos_do_outbox(banco, "OrcamentoCancelado")) == 1
