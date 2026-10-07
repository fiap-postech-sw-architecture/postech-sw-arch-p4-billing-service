"""Unidade de trabalho (transacao + outbox) e repositorios contra MongoDB real."""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal
from functools import partial
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
from bson.decimal128 import Decimal128
from pymongo.errors import ExecutionTimeout, OperationFailure

from src.compartilhado.dominio.exceptions import ValorInvalidoError
from src.compartilhado.infraestrutura import unit_of_work
from src.compartilhado.infraestrutura.mongo import DocumentoInvalidoError
from src.compartilhado.infraestrutura.unit_of_work import (
    MensagemRecebida,
    MongoUnitOfWork,
    processar_mensagem,
)
from src.orcamento.dominio.events import GeracaoDeOrcamentoFalhouEvent
from src.orcamento.dominio.exceptions import OrcamentoJaGeradoError
from src.orcamento.dominio.orcamento import CanalDecisao, Orcamento, StatusOrcamento
from src.orcamento.infraestrutura.repository import MongoOrcamentoRepository
from src.pagamento.dominio.estados import (
    MotivoEstorno,
    StatusNoProvedor,
    StatusPagamento,
)
from src.pagamento.dominio.exceptions import PagamentoJaSolicitadoError
from src.pagamento.dominio.pagamento import Pagamento
from src.pagamento.infraestrutura.repository import MongoPagamentoRepository
from src.precos.dominio.exceptions import PrecoJaCadastradoError
from src.precos.dominio.preco import PrecoPeca, PrecoServico
from src.precos.infraestrutura.repository import (
    MongoPrecoPecaRepository,
    MongoPrecoServicoRepository,
)
from tests.factories import (
    AGORA,
    ATENDENTE_SUB,
    confirmar,
    dinheiro,
    orcamento,
    pagamento,
    situacao,
)
from tests.integracao.apoio import cliente_espiado, eventos_do_outbox

if TYPE_CHECKING:
    from collections.abc import Iterator

    from pymongo.database import Database

    Banco = Database[dict[str, Any]]


@contextmanager
def sem_o_indice_de_prazo(banco: Banco) -> Iterator[None]:
    colecao = banco["pagamentos"]
    colecao.drop_index("status_1_expira_em_1")
    try:
        yield
    finally:
        colecao.create_index([("status", 1), ("expira_em", 1)])


class TestUnidadeDeTrabalho:
    def test_outbox_vai_na_mesma_transacao_do_estado(
        self, banco: Banco, mongo_uri: str
    ) -> None:
        with cliente_espiado(mongo_uri) as (cliente, espia):
            uow = MongoUnitOfWork(cliente[banco.name])
            uow.executar(lambda: MongoOrcamentoRepository(uow).salvar(orcamento()))

        [estado, outbox] = espia.escritas
        assert (estado[0], outbox[0]) == ("orcamentos", "outbox")
        # Mesma sessao, mesmo numero de transacao, nenhum autocommit.
        assert estado[1:] == outbox[1:]
        assert outbox[2] is not None
        assert outbox[3] is False

    def test_estado_e_evento_gravados_na_mesma_transacao(self, banco: Banco) -> None:
        uow = MongoUnitOfWork(banco)
        repo = MongoOrcamentoRepository(uow)
        gerado = orcamento()

        uow.executar(lambda: repo.salvar(gerado))

        assert repo.obter_por_id(gerado.id) is not None
        assert [e["tipo"] for e in eventos_do_outbox(banco)] == ["OrcamentoGerado"]
        documento = banco["outbox"].find_one()
        assert documento is not None
        assert documento["status"] == "pendente"
        assert documento["tentativas"] == 0
        assert documento["envelope"]["id"] == str(documento["_id"])
        assert documento["_id"].version == 7  # ordem de publicacao do relay
        assert gerado.coletar_eventos() == []  # entregues a outbox

    def test_excecao_desfaz_estado_e_outbox(self, banco: Banco) -> None:
        uow = MongoUnitOfWork(banco)
        repo = MongoOrcamentoRepository(uow)
        gerado = orcamento()

        def trabalho() -> None:
            repo.salvar(gerado)
            raise RuntimeError("falha no meio")

        with pytest.raises(RuntimeError, match="falha no meio"):
            uow.executar(trabalho)

        assert repo.obter_por_id(gerado.id) is None
        assert eventos_do_outbox(banco) == []

    def test_evento_avulso_vai_para_o_outbox(self, banco: Banco) -> None:
        uow = MongoUnitOfWork(banco)
        ordem_id = uuid4()
        uow.executar(
            lambda: uow.registrar_evento(
                GeracaoDeOrcamentoFalhouEvent(
                    ordem_id=ordem_id, motivo="x", codigos_invalidos=("SRV-X",)
                )
            )
        )
        [envelope] = eventos_do_outbox(banco)
        assert envelope["correlation_id"] == str(ordem_id)
        assert envelope["dados"]["codigos_invalidos"] == ["SRV-X"]

    def test_escrita_fora_da_transacao_e_recusada(self, banco: Banco) -> None:
        uow = MongoUnitOfWork(banco)
        gerado = orcamento()
        with pytest.raises(RuntimeError, match="fora de uma transacao"):
            MongoOrcamentoRepository(uow).salvar(gerado)
        with pytest.raises(RuntimeError, match="fora de uma transacao"):
            uow.registrar_evento(
                GeracaoDeOrcamentoFalhouEvent(
                    ordem_id=uuid4(), motivo="x", codigos_invalidos=()
                )
            )
        assert banco["orcamentos"].count_documents({}) == 0

    def test_salvar_duas_vezes_na_transacao_grava_os_eventos_uma_vez(
        self, banco: Banco
    ) -> None:
        uow = MongoUnitOfWork(banco)
        repo = MongoOrcamentoRepository(uow)
        gerado = orcamento()

        def trabalho() -> None:
            repo.salvar(gerado)
            gerado.aprovar(canal=CanalDecisao.LINK, agora=AGORA)
            repo.salvar(gerado)

        uow.executar(trabalho)
        tipos = [e["tipo"] for e in eventos_do_outbox(banco)]
        assert tipos == ["OrcamentoGerado", "OrcamentoAprovado"]

    def test_transacao_aninhada_e_recusada(self, banco: Banco) -> None:
        uow = MongoUnitOfWork(banco)
        with pytest.raises(RuntimeError, match="aninhada"):
            uow.executar(lambda: uow.executar(lambda: None))

    def test_conflito_de_escrita_reexecuta_o_trabalho_com_leitura_nova(
        self, banco: Banco
    ) -> None:
        """WriteConflict -> with_transaction repete; a repeticao rele o valor."""
        uow = MongoUnitOfWork(banco)
        repo = MongoOrcamentoRepository(uow)
        gerado = orcamento()
        uow.executar(lambda: repo.salvar(gerado))
        leu = threading.Event()
        segunda_comitou = threading.Event()
        tentativas: list[str] = []

        def primeira() -> None:
            outro_uow = MongoUnitOfWork(banco)
            outro_repo = MongoOrcamentoRepository(outro_uow)

            def trabalho() -> None:
                atual = outro_repo.obter_por_id(gerado.id)
                assert atual is not None
                tentativas.append(atual.status.value)
                if not leu.is_set():
                    leu.set()
                    segunda_comitou.wait(timeout=10)
                if atual.status.value == "PENDENTE":
                    atual.cancelar(motivo="primeira")
                    outro_repo.salvar(atual)

            outro_uow.executar(trabalho)

        thread = threading.Thread(target=primeira)
        thread.start()
        leu.wait(timeout=10)
        segundo = MongoUnitOfWork(banco)
        segundo_repo = MongoOrcamentoRepository(segundo)

        def aprovar() -> None:
            atual = segundo_repo.obter_por_id(gerado.id)
            assert atual is not None
            atual.aprovar(canal=CanalDecisao.LINK, agora=AGORA)
            segundo_repo.salvar(atual)

        segundo.executar(aprovar)
        segunda_comitou.set()
        thread.join(timeout=30)

        # 1a tentativa leu PENDENTE (snapshot antigo) e esbarrou no conflito
        # ao gravar; a repeticao releu APROVADO e nao sobrescreveu nada.
        assert tentativas[0] == "PENDENTE"
        assert tentativas[-1] == "APROVADO"
        final = repo.obter_por_id(gerado.id)
        assert final is not None
        assert final.status.value == "APROVADO"

    @pytest.mark.parametrize("caminho", ["api", "mensagem"])
    def test_falha_transitoria_que_nao_passa_estoura_o_teto_da_transacao(
        self, banco: Banco, monkeypatch: pytest.MonkeyPatch, caminho: str
    ) -> None:
        monkeypatch.setattr(unit_of_work, "LIMITE_DA_TRANSACAO_SEGUNDOS", 0.3)
        chamadas: list[float] = []

        def conflito() -> None:
            chamadas.append(time.monotonic())
            raise OperationFailure(
                "conflito",
                code=112,
                details={"errorLabels": ["TransientTransactionError"]},
            )

        inicio = time.monotonic()
        with pytest.raises(ExecutionTimeout):
            if caminho == "api":
                MongoUnitOfWork(banco).executar(conflito)
            else:
                comando = MensagemRecebida(
                    id=uuid4(), tipo="GerarOrcamento", correlation_id=uuid4()
                )
                processar_mensagem(banco, comando, lambda uow: uow.executar(conflito))

        # Repetiu, mas so ate o teto: sem ele o with_transaction iria a 120 s.
        assert len(chamadas) > 1
        assert time.monotonic() - inicio < 5


class TestRepositorioDeOrcamento:
    def test_ida_e_volta_preserva_o_agregado(self, banco: Banco) -> None:
        uow = MongoUnitOfWork(banco)
        repo = MongoOrcamentoRepository(uow)
        gerado = orcamento()
        gerado.aprovar(
            canal=CanalDecisao.ATENDENTE,
            agora=AGORA + timedelta(hours=1),
            decidido_por=ATENDENTE_SUB,
        )
        uow.executar(lambda: repo.salvar(gerado))

        lido = repo.obter_por_id(gerado.id)

        assert lido is not None
        assert lido.ordem_id == gerado.ordem_id
        assert lido.linhas == gerado.linhas
        assert lido.total == dinheiro("335.00")
        assert (lido.criado_em, lido.valido_ate) == (
            gerado.criado_em,
            gerado.valido_ate,
        )
        assert lido.status is gerado.status
        assert lido.decisao == gerado.decisao
        assert repo.obter_por_ordem(gerado.ordem_id) == lido
        assert repo.obter_por_ordem(uuid4()) is None

    def test_ida_e_volta_da_lapide(self, banco: Banco) -> None:
        uow = MongoUnitOfWork(banco)
        repo = MongoOrcamentoRepository(uow)
        tumulo = Orcamento.lapide(
            id=uuid4(), ordem_id=uuid4(), cancelado_em=AGORA, motivo="cancelamento"
        )
        uow.executar(lambda: repo.salvar(tumulo))
        lido = repo.obter_por_ordem(tumulo.ordem_id)
        assert lido is not None
        assert (lido.status, lido.linhas, lido.valido_ate, lido.total) == (
            StatusOrcamento.CANCELADO,
            (),
            None,
            dinheiro("0.00"),
        )
        assert lido.motivo_cancelamento == "cancelamento"

    def test_documento_antigo_sem_campos_opcionais_continua_legivel(
        self, banco: Banco
    ) -> None:
        uow = MongoUnitOfWork(banco)
        repo = MongoOrcamentoRepository(uow)
        gerado = orcamento()
        uow.executar(lambda: repo.salvar(gerado))
        banco["orcamentos"].update_one(
            {"_id": gerado.id}, {"$unset": {"decisao": "", "motivo_cancelamento": ""}}
        )
        lido = repo.obter_por_id(gerado.id)
        assert lido is not None
        assert (lido.decisao, lido.motivo_cancelamento) == (None, None)

    @pytest.mark.parametrize(
        "alteracao",
        [
            pytest.param({"$set": {"status": "APROVADO"}}, id="aprovado-sem-decisao"),
            pytest.param(
                {"$set": {"decisao": {"canal": "atendente", "decidido_em": AGORA}}},
                id="atendente-sem-decidido-por",
            ),
        ],
    )
    def test_documento_fora_das_invariantes_e_defeito_de_dado(
        self, banco: Banco, alteracao: dict[str, Any]
    ) -> None:
        # Dado gravado errado nao e erro do chamador: vira 500, nunca 422.
        uow = MongoUnitOfWork(banco)
        repo = MongoOrcamentoRepository(uow)
        gerado = orcamento()
        uow.executar(lambda: repo.salvar(gerado))
        banco["orcamentos"].update_one(
            {"_id": gerado.id}, alteracao, bypass_document_validation=True
        )
        with pytest.raises(DocumentoInvalidoError, match=str(gerado.id)) as erro:
            repo.obter_por_id(gerado.id)
        assert not isinstance(erro.value, ValorInvalidoError)

    def test_dinheiro_persistido_como_decimal128(self, banco: Banco) -> None:
        uow = MongoUnitOfWork(banco)
        gerado = orcamento()
        uow.executar(lambda: MongoOrcamentoRepository(uow).salvar(gerado))
        documento = banco["orcamentos"].find_one({"_id": gerado.id})
        assert documento is not None
        assert documento["total"] == {"valor": Decimal128("335.00"), "moeda": "BRL"}
        preco = documento["linhas"][1]["preco_unitario"]["valor"]
        assert isinstance(preco, Decimal128)
        assert preco.to_decimal() == Decimal("45.00")

    def test_um_orcamento_por_ordem(self, banco: Banco) -> None:
        uow = MongoUnitOfWork(banco)
        repo = MongoOrcamentoRepository(uow)
        ordem_id = uuid4()
        uow.executar(lambda: repo.salvar(orcamento(ordem_id=ordem_id)))
        with pytest.raises(OrcamentoJaGeradoError):
            uow.executar(lambda: repo.salvar(orcamento(ordem_id=ordem_id)))
        assert banco["orcamentos"].count_documents({}) == 1

    def test_vencidos_em_ordem_de_validade(self, banco: Banco) -> None:
        uow = MongoUnitOfWork(banco)
        repo = MongoOrcamentoRepository(uow)
        cedo = orcamento(validade=timedelta(hours=1))
        tarde = orcamento(validade=timedelta(hours=2))
        decidido = orcamento(validade=timedelta(hours=1))
        decidido.aprovar(canal=CanalDecisao.LINK, agora=AGORA)
        vigente = orcamento(validade=timedelta(hours=72))
        for o in (tarde, cedo, decidido, vigente):
            uow.executar(partial(repo.salvar, o))

        agora = AGORA + timedelta(hours=3)
        assert repo.listar_vencidos(agora, 10) == [cedo.id, tarde.id]
        assert repo.listar_vencidos(agora, 1) == [cedo.id]


class TestRepositorioDePagamento:
    CAMPOS = (
        "ordem_id",
        "criado_em",
        "cobranca",
        "status",
        "recusas",
        "referencia_pagamento",
        "confirmado_em",
        "encerrado_em",
        "motivo",
        "estornado_em",
        "motivo_estorno",
        "notificacoes",
        "estornos_automaticos",
    )

    def ida_e_volta(self, banco: Banco, original: Pagamento) -> Pagamento:
        uow = MongoUnitOfWork(banco)
        repo = MongoPagamentoRepository(uow)
        uow.executar(lambda: repo.salvar(original))
        lido = repo.obter_por_id(original.id)
        assert lido is not None
        for campo in self.CAMPOS:
            assert getattr(lido, campo) == getattr(original, campo), campo
        return lido

    def test_ida_e_volta_com_historico_recusas_e_estornos(self, banco: Banco) -> None:
        p = pagamento()
        p.aplicar_notificacao(
            situacao(p, referencia="r1", status=StatusNoProvedor.RECUSADO),
            agora=AGORA,
            max_recusas=3,
        )
        confirmar(p, referencia="2")
        p.registrar_estorno_automatico("3", agora=AGORA, falha="payment too old")
        p.concluir_compensacao(agora=AGORA + timedelta(hours=1), motivo="cancelamento")

        lido = self.ida_e_volta(banco, p)

        assert (lido.status, lido.recusas, lido.motivo_estorno) == (
            StatusPagamento.ESTORNADO,
            1,
            MotivoEstorno.COMPENSACAO,
        )
        repo = MongoPagamentoRepository(MongoUnitOfWork(banco))
        assert repo.obter_por_ordem(p.ordem_id) == lido
        assert repo.obter_por_ordem(uuid4()) is None

    def test_ida_e_volta_da_lapide_sem_cobranca(self, banco: Banco) -> None:
        tumulo = Pagamento.lapide(
            id=uuid4(), ordem_id=uuid4(), cancelado_em=AGORA, motivo="cancelamento"
        )
        lido = self.ida_e_volta(banco, tumulo)
        assert lido.cobranca is None
        documento = banco["pagamentos"].find_one({"_id": tumulo.id})
        assert documento is not None
        assert "checkout_url" not in documento

    def test_documento_antigo_sem_campos_novos_continua_legivel(
        self, banco: Banco
    ) -> None:
        p = pagamento()
        uow = MongoUnitOfWork(banco)
        repo = MongoPagamentoRepository(uow)
        uow.executar(lambda: repo.salvar(p))
        # Documento de versao anterior, sem os campos novos (fora do esquema).
        banco["pagamentos"].update_one(
            {"_id": p.id},
            {
                "$unset": {
                    "recusas": "",
                    "estornos_automaticos": "",
                    "motivo_estorno": "",
                }
            },
            bypass_document_validation=True,
        )
        lido = repo.obter_por_id(p.id)
        assert lido is not None
        assert (lido.recusas, lido.estornos_automaticos) == (0, ())

    def test_um_pagamento_por_orcamento_e_um_por_ordem(self, banco: Banco) -> None:
        uow = MongoUnitOfWork(banco)
        repo = MongoPagamentoRepository(uow)
        orcamento_id, ordem_id = uuid4(), uuid4()
        uow.executar(
            lambda: repo.salvar(pagamento(orcamento_id=orcamento_id, ordem_id=ordem_id))
        )
        with pytest.raises(PagamentoJaSolicitadoError):
            uow.executar(lambda: repo.salvar(pagamento(orcamento_id=orcamento_id)))
        with pytest.raises(PagamentoJaSolicitadoError):
            uow.executar(lambda: repo.salvar(pagamento(ordem_id=ordem_id)))

    def test_lapides_de_ordens_diferentes_convivem(self, banco: Banco) -> None:
        # A lapide nao tem orcamento_id: o indice unico dele e parcial.
        uow = MongoUnitOfWork(banco)
        repo = MongoPagamentoRepository(uow)
        for _ in range(2):
            tumulo = Pagamento.lapide(
                id=uuid4(), ordem_id=uuid4(), cancelado_em=AGORA, motivo="x"
            )
            uow.executar(partial(repo.salvar, tumulo))
        assert banco["pagamentos"].count_documents({}) == 2

    def test_a_mesma_tentativa_nao_confirma_dois_pagamentos(self, banco: Banco) -> None:
        uow = MongoUnitOfWork(banco)
        repo = MongoPagamentoRepository(uow)
        primeiro, segundo = pagamento(), pagamento()
        confirmar(primeiro, referencia="mp-1")
        confirmar(segundo, referencia="mp-1")
        uow.executar(lambda: repo.salvar(primeiro))
        with pytest.raises(PagamentoJaSolicitadoError):
            uow.executar(lambda: repo.salvar(segundo))

    def test_vencidos(self, banco: Banco) -> None:
        uow = MongoUnitOfWork(banco)
        repo = MongoPagamentoRepository(uow)
        vencido = pagamento()
        pago = pagamento()
        confirmar(pago)
        for p in (vencido, pago):
            uow.executar(partial(repo.salvar, p))
        assert repo.listar_vencidos(AGORA + timedelta(hours=2), 10) == [vencido.id]

    def test_filas_de_prazo_saem_em_ordem_de_prazo(self, banco: Banco) -> None:
        uow = MongoUnitOfWork(banco)
        repo = MongoPagamentoRepository(uow)
        cedo = pagamento(validade=timedelta(minutes=10))
        tarde = pagamento(validade=timedelta(minutes=60))
        pago = pagamento(validade=timedelta(minutes=5))
        confirmar(pago)
        # Gravados do que vence por ultimo para o que vence primeiro: a ordem de
        # insercao nao e a do prazo.
        for p in (tarde, cedo, pago):
            uow.executar(partial(repo.salvar, p))
        depois = AGORA + timedelta(hours=2)

        # Sem o indice (status, expira_em) o MongoDB varre a colecao na ordem de
        # insercao: so o sort explicito da consulta garante a ordem (com o
        # indice ela viria dele, por acaso).
        with sem_o_indice_de_prazo(banco):
            assert repo.listar_solicitados(10) == [cedo.id, tarde.id]
            assert repo.listar_vencidos(depois, 10) == [cedo.id, tarde.id]
            # Fila maior que o limite: a conciliacao e a expiracao chegam
            # primeiro a quem vence antes.
            assert repo.listar_solicitados(1) == [cedo.id]
            assert repo.listar_vencidos(depois, 1) == [cedo.id]


class TestRepositorioDePrecos:
    def test_servicos_por_codigo_e_paginacao(self, banco: Banco) -> None:
        uow = MongoUnitOfWork(banco)
        repo = MongoPrecoServicoRepository(uow)
        for codigo in ("SRV-B", "SRV-A", "SRV-C"):
            preco = PrecoServico.cadastrar(
                codigo=codigo, nome=codigo, descricao="d", preco=dinheiro("10.00")
            )
            uow.executar(partial(repo.salvar, preco))

        assert [p.codigo for p in repo.listar(offset=1, limit=2)] == ["SRV-B", "SRV-C"]
        assert repo.contar() == 3
        assert set(repo.obter_por_codigos(["SRV-A", "SRV-X"])) == {"SRV-A"}
        lido = repo.obter_por_codigo("SRV-A")
        assert lido is not None
        assert lido.preco == dinheiro("10.00")
        assert repo.obter_por_codigo("SRV-X") is None

    def test_codigo_e_sku_unicos(self, banco: Banco) -> None:
        uow = MongoUnitOfWork(banco)
        servicos = MongoPrecoServicoRepository(uow)
        pecas = MongoPrecoPecaRepository(uow)

        def servico() -> PrecoServico:
            return PrecoServico.cadastrar(
                codigo="SRV-A", nome="A", descricao="d", preco=dinheiro("1.00")
            )

        def peca() -> PrecoPeca:
            return PrecoPeca.cadastrar(sku="PEC-A", nome="A", preco=dinheiro("1.00"))

        uow.executar(lambda: servicos.salvar(servico()))
        uow.executar(lambda: pecas.salvar(peca()))
        with pytest.raises(PrecoJaCadastradoError, match="SRV-A"):
            uow.executar(lambda: servicos.salvar(servico()))
        with pytest.raises(PrecoJaCadastradoError, match="PEC-A"):
            uow.executar(lambda: pecas.salvar(peca()))
        assert [p.sku for p in pecas.listar(offset=0, limit=10)] == ["PEC-A"]
        assert pecas.contar() == 1
        assert set(pecas.obter_por_codigos(["PEC-A"])) == {"PEC-A"}
