"""Casos de uso do orcamento com MongoDB real, tabela de precos do seed."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest

from src.compartilhado.dominio.exceptions import TransicaoStatusInvalidaError
from src.compartilhado.infraestrutura.unit_of_work import MongoUnitOfWork
from src.orcamento.aplicacao.dtos import ItemSolicitado
from src.orcamento.aplicacao.use_cases import (
    MOTIVO_ITENS_INVALIDOS,
    MOTIVO_SEM_ITENS,
    CancelarOrcamento,
    ConsultarOrcamentos,
    DecidirOrcamento,
    ExpirarOrcamentosVencidos,
    GerarOrcamento,
)
from src.orcamento.dominio.exceptions import (
    LinkDeDecisaoInvalidoError,
    OrcamentoNaoEncontradoError,
    OrcamentoVencidoError,
)
from src.orcamento.dominio.orcamento import TipoItem
from src.orcamento.infraestrutura.repository import MongoOrcamentoRepository
from src.orcamento.infraestrutura.tabela_de_precos import TabelaDePrecosMongoAdapter
from src.precos.aplicacao.use_cases import PrecosDePecas, PrecosDeServicos
from src.precos.infraestrutura.repository import (
    MongoPrecoPecaRepository,
    MongoPrecoServicoRepository,
)
from src.seed import semear
from tests.integracao.apoio import (
    LINK,
    URL_PUBLICA,
    RelogioFixo,
    eventos_do_outbox,
    token_do_link,
)

if TYPE_CHECKING:
    from pymongo.database import Database

    from src.orcamento.aplicacao.dtos import OrcamentoDTO

    Banco = Database[dict[str, Any]]

ITENS = [
    ItemSolicitado(TipoItem.SERVICO, "SRV-TROCA-OLEO", 1),
    ItemSolicitado(TipoItem.PECA, "PEC-OLEO-5W30", 4),
    ItemSolicitado(TipoItem.PECA, "PEC-FILTRO-OLEO", 1),
]


@pytest.fixture(autouse=True)
def tabela_de_precos(banco: Banco) -> None:
    semear(banco)


def gerar(banco: Banco, relogio: RelogioFixo) -> GerarOrcamento:
    uow = MongoUnitOfWork(banco)
    return GerarOrcamento(
        uow,
        MongoOrcamentoRepository(uow),
        TabelaDePrecosMongoAdapter(uow),
        LINK,
        timedelta(hours=72),
        relogio,
    )


def decidir(banco: Banco, relogio: RelogioFixo) -> DecidirOrcamento:
    uow = MongoUnitOfWork(banco)
    return DecidirOrcamento(uow, MongoOrcamentoRepository(uow), LINK, relogio)


def consultar(banco: Banco, relogio: RelogioFixo) -> ConsultarOrcamentos:
    return ConsultarOrcamentos(
        MongoOrcamentoRepository(MongoUnitOfWork(banco)), LINK, relogio
    )


def cancelamento(banco: Banco, relogio: RelogioFixo) -> CancelarOrcamento:
    uow = MongoUnitOfWork(banco)
    return CancelarOrcamento(uow, MongoOrcamentoRepository(uow), relogio)


def gerado(banco: Banco, relogio: RelogioFixo) -> OrcamentoDTO:
    dto = gerar(banco, relogio).executar(ordem_id=uuid4(), itens=ITENS)
    assert dto is not None
    return dto


class TestGerarOrcamento:
    def test_congela_os_precos_e_grava_orcamento_gerado(
        self, banco: Banco, relogio: RelogioFixo
    ) -> None:
        ordem_id = uuid4()

        dto = gerar(banco, relogio).executar(ordem_id=ordem_id, itens=ITENS)

        assert dto is not None
        assert dto.status == "PENDENTE"
        assert dto.total == Decimal("335.00")
        assert [(li.codigo, li.descricao, li.subtotal) for li in dto.linhas] == [
            ("SRV-TROCA-OLEO", "Troca de oleo", Decimal("120.00")),
            ("PEC-OLEO-5W30", "Oleo de motor 5W30 (1 L)", Decimal("180.00")),
            ("PEC-FILTRO-OLEO", "Filtro de oleo", Decimal("35.00")),
        ]
        assert dto.valido_ate == relogio.agora + timedelta(hours=72)
        [envelope] = eventos_do_outbox(banco)
        assert envelope["tipo"] == "OrcamentoGerado"
        assert envelope["correlation_id"] == str(ordem_id)
        dados = envelope["dados"]
        assert dados["orcamento_id"] == str(dto.id)
        assert dados["total"] == "335.00"
        assert dados["moeda"] == "BRL"
        assert dados["valido_ate"] == "2026-10-09T12:00:00.000Z"
        assert dados["linhas"][1] == {
            "codigo": "PEC-OLEO-5W30",
            "descricao": "Oleo de motor 5W30 (1 L)",
            "quantidade": 4,
            "preco_unitario": "45.00",
            "subtotal": "180.00",
        }
        emitido = dados["link_decisao"].removeprefix(
            f"{URL_PUBLICA}/api/v1/publico/orcamentos/"
        )
        assert LINK.validar(emitido, agora=relogio.agora) == dto.id

    def test_valido_ate_em_segundo_cheio_mesmo_com_relogio_em_milissegundos(
        self, banco: Banco
    ) -> None:
        relogio = RelogioFixo(datetime(2026, 10, 6, 12, 0, 0, 123000, tzinfo=UTC))
        dto = gerar(banco, relogio).executar(ordem_id=uuid4(), itens=ITENS)
        assert dto is not None
        # Para cima: o link nunca vence antes do orcamento.
        assert dto.valido_ate == datetime(2026, 10, 9, 12, 0, 1, tzinfo=UTC)
        emitido = token_do_link(dto.id, dto.valido_ate)
        assert LINK.validar(emitido, agora=dto.valido_ate) == dto.id

    def test_mudanca_na_tabela_nao_altera_orcamento_gerado(
        self, banco: Banco, relogio: RelogioFixo
    ) -> None:
        dto = gerado(banco, relogio)
        uow = MongoUnitOfWork(banco)
        PrecosDeServicos(uow, MongoPrecoServicoRepository(uow)).atualizar(
            "SRV-TROCA-OLEO",
            nome="Troca de oleo",
            descricao="d",
            preco=Decimal("999.00"),
            ativo=True,
        )
        assert consultar(banco, relogio).por_id(dto.id).total == Decimal("335.00")

    @pytest.mark.parametrize(
        "itens_da_repeticao",
        [
            pytest.param(ITENS[:1], id="itens-validos"),
            pytest.param(
                [ItemSolicitado(TipoItem.SERVICO, "SRV-NAO-EXISTE", 1)],
                id="itens-invalidos",
            ),
        ],
    )
    def test_repetir_a_geracao_republica_o_orcamento_gerado(
        self,
        banco: Banco,
        relogio: RelogioFixo,
        itens_da_repeticao: list[ItemSolicitado],
    ) -> None:
        ordem_id = uuid4()
        primeiro = gerar(banco, relogio).executar(ordem_id=ordem_id, itens=ITENS)
        relogio.avancar(minutes=5)
        segundo = gerar(banco, relogio).executar(
            ordem_id=ordem_id, itens=itens_da_repeticao
        )

        assert primeiro is not None
        assert segundo == primeiro
        assert banco["orcamentos"].count_documents({}) == 1
        gerados = eventos_do_outbox(banco, "OrcamentoGerado")
        assert len(gerados) == 2
        assert gerados[0]["dados"] == gerados[1]["dados"]
        assert eventos_do_outbox(banco, "GeracaoDeOrcamentoFalhou") == []

    def test_falha_repetida_responde_a_falha_de_novo(
        self, banco: Banco, relogio: RelogioFixo
    ) -> None:
        ordem_id = uuid4()
        for _ in range(2):
            assert gerar(banco, relogio).executar(ordem_id=ordem_id, itens=[]) is None
        falhas = eventos_do_outbox(banco, "GeracaoDeOrcamentoFalhou")
        assert len(falhas) == 2
        assert falhas[0]["dados"] == falhas[1]["dados"]
        assert banco["orcamentos"].count_documents({}) == 0

    def test_geracao_depois_da_lapide_e_descartada(
        self, banco: Banco, relogio: RelogioFixo
    ) -> None:
        ordem_id = uuid4()
        uow = MongoUnitOfWork(banco)
        CancelarOrcamento(uow, MongoOrcamentoRepository(uow), relogio).executar(
            ordem_id=ordem_id, motivo="cancelamento"
        )

        resultado = gerar(banco, relogio).executar(ordem_id=ordem_id, itens=ITENS)

        assert resultado is not None
        assert (resultado.status, resultado.linhas) == ("CANCELADO", ())
        assert [e["tipo"] for e in eventos_do_outbox(banco)] == ["OrcamentoCancelado"]

    def test_codigo_inexistente_ou_inativo_grava_falha(
        self, banco: Banco, relogio: RelogioFixo
    ) -> None:
        uow = MongoUnitOfWork(banco)
        PrecosDePecas(uow, MongoPrecoPecaRepository(uow)).desativar("PEC-VELA")
        ordem_id = uuid4()

        dto = gerar(banco, relogio).executar(
            ordem_id=ordem_id,
            itens=[
                *ITENS,
                ItemSolicitado(TipoItem.SERVICO, "SRV-NAO-EXISTE", 1),
                ItemSolicitado(TipoItem.PECA, "PEC-VELA", 4),
                ItemSolicitado(TipoItem.PECA, "SRV-TROCA-OLEO", 1),
            ],
        )

        assert dto is None
        assert banco["orcamentos"].count_documents({}) == 0
        [envelope] = eventos_do_outbox(banco)
        assert envelope["tipo"] == "GeracaoDeOrcamentoFalhou"
        assert envelope["dados"] == {
            "ordem_id": str(ordem_id),
            "motivo": MOTIVO_ITENS_INVALIDOS,
            "codigos_invalidos": ["SRV-NAO-EXISTE", "PEC-VELA", "SRV-TROCA-OLEO"],
        }

    def test_diagnostico_sem_itens_grava_falha(
        self, banco: Banco, relogio: RelogioFixo
    ) -> None:
        assert gerar(banco, relogio).executar(ordem_id=uuid4(), itens=[]) is None
        [envelope] = eventos_do_outbox(banco)
        assert envelope["dados"]["motivo"] == MOTIVO_SEM_ITENS
        assert envelope["dados"]["codigos_invalidos"] == []


class TestDecidirOrcamento:
    def test_aprovacao_pelo_link(self, banco: Banco, relogio: RelogioFixo) -> None:
        dto = gerado(banco, relogio)
        emitido = token_do_link(dto.id, dto.valido_ate)
        relogio.avancar(hours=1)

        aprovado = decidir(banco, relogio).por_link(emitido, aprovar=True)

        assert aprovado.status == "APROVADO"
        assert aprovado.decisao is not None
        assert (aprovado.decisao.canal, aprovado.decisao.decidido_em) == (
            "link",
            relogio.agora,
        )
        [envelope] = eventos_do_outbox(banco, "OrcamentoAprovado")
        assert envelope["dados"] == {
            "ordem_id": str(dto.ordem_id),
            "orcamento_id": str(dto.id),
            "decidido_em": "2026-10-06T13:00:00.000Z",
            "canal": "link",
        }

    def test_recusa_pelo_atendente(self, banco: Banco, relogio: RelogioFixo) -> None:
        dto = gerado(banco, relogio)
        recusado = decidir(banco, relogio).por_atendente(dto.id, aprovar=False)
        assert recusado.status == "RECUSADO"
        [envelope] = eventos_do_outbox(banco, "OrcamentoRecusado")
        assert envelope["dados"]["canal"] == "atendente"

    def test_segunda_decisao_e_recusada_sem_novo_evento(
        self, banco: Banco, relogio: RelogioFixo
    ) -> None:
        dto = gerado(banco, relogio)
        decidir(banco, relogio).por_atendente(dto.id, aprovar=True)
        with pytest.raises(TransicaoStatusInvalidaError):
            decidir(banco, relogio).por_atendente(dto.id, aprovar=False)
        assert len(eventos_do_outbox(banco)) == 2  # gerado + aprovado

    def test_decisao_depois_do_prazo_mesmo_sem_job(
        self, banco: Banco, relogio: RelogioFixo
    ) -> None:
        dto = gerado(banco, relogio)
        relogio.avancar(hours=72, seconds=1)
        with pytest.raises(OrcamentoVencidoError):
            decidir(banco, relogio).por_atendente(dto.id, aprovar=True)
        assert consultar(banco, relogio).por_id(dto.id).status == "PENDENTE"

    def test_link_invalido_expirado_inexistente_ou_decidido_e_o_mesmo_erro(
        self, banco: Banco, relogio: RelogioFixo
    ) -> None:
        dto = gerado(banco, relogio)
        decidido = gerado(banco, relogio)
        decidir(banco, relogio).por_link(
            token_do_link(decidido.id, decidido.valido_ate), aprovar=False
        )
        caso = decidir(banco, relogio)
        tokens = [
            "adulterado.1.x",
            token_do_link(uuid4(), dto.valido_ate),
            token_do_link(decidido.id, decidido.valido_ate),
        ]
        for emitido in tokens:
            with pytest.raises(LinkDeDecisaoInvalidoError):
                caso.por_link(emitido, aprovar=True)
            with pytest.raises(LinkDeDecisaoInvalidoError):
                consultar(banco, relogio).por_link(emitido)
        relogio.avancar(hours=73)
        with pytest.raises(LinkDeDecisaoInvalidoError):
            caso.por_link(token_do_link(dto.id, dto.valido_ate), aprovar=True)
        assert consultar(banco, relogio).por_id(dto.id).status == "PENDENTE"
        assert len(eventos_do_outbox(banco, "OrcamentoRecusado")) == 1


class TestExpirarECancelar:
    def test_expira_so_os_vencidos_e_e_idempotente(
        self, banco: Banco, relogio: RelogioFixo
    ) -> None:
        vencido_1 = gerado(banco, relogio)
        vencido_2 = gerado(banco, relogio)
        aprovado = gerado(banco, relogio)
        decidir(banco, relogio).por_atendente(aprovado.id, aprovar=True)
        relogio.avancar(hours=1)
        vigente = gerado(banco, relogio)
        relogio.avancar(hours=71, seconds=30)
        uow = MongoUnitOfWork(banco)
        expirar = ExpirarOrcamentosVencidos(uow, MongoOrcamentoRepository(uow), relogio)

        assert expirar.executar() == 2
        assert expirar.executar() == 0

        consulta = consultar(banco, relogio)
        assert consulta.por_id(vencido_1.id).status == "EXPIRADO"
        assert consulta.por_id(vencido_2.id).status == "EXPIRADO"
        assert consulta.por_id(vigente.id).status == "PENDENTE"
        assert consulta.por_id(aprovado.id).status == "APROVADO"
        expirados = eventos_do_outbox(banco, "OrcamentoExpirado")
        assert {e["dados"]["orcamento_id"] for e in expirados} == {
            str(vencido_1.id),
            str(vencido_2.id),
        }

    def test_cancelamento_repetido_republica_a_resposta(
        self, banco: Banco, relogio: RelogioFixo
    ) -> None:
        dto = gerado(banco, relogio)
        cancelar = cancelamento(banco, relogio)

        primeiro = cancelar.executar(
            ordem_id=dto.ordem_id, orcamento_id=dto.id, motivo="cancelamento"
        )
        segundo = cancelar.executar(ordem_id=dto.ordem_id, motivo="reenvio")

        assert primeiro.status == segundo.status == "CANCELADO"
        assert segundo.motivo_cancelamento == "cancelamento"
        cancelados = eventos_do_outbox(banco, "OrcamentoCancelado")
        assert [e["dados"] for e in cancelados] == [
            {"ordem_id": str(dto.ordem_id), "orcamento_id": str(dto.id)}
        ] * 2

    def test_cancelar_exige_que_o_id_informado_seja_o_da_ordem(
        self, banco: Banco, relogio: RelogioFixo
    ) -> None:
        dto = gerado(banco, relogio)
        with pytest.raises(OrcamentoNaoEncontradoError, match="nao pertence"):
            cancelamento(banco, relogio).executar(
                ordem_id=dto.ordem_id, orcamento_id=uuid4(), motivo="x"
            )
        assert consultar(banco, relogio).por_id(dto.id).status == "PENDENTE"

    @pytest.mark.parametrize("encerramento", ["recusa", "expiracao"])
    def test_cancelar_orcamento_encerrado_responde_cancelado_sem_mudar(
        self, banco: Banco, relogio: RelogioFixo, encerramento: str
    ) -> None:
        dto = gerado(banco, relogio)
        if encerramento == "recusa":
            decidir(banco, relogio).por_atendente(dto.id, aprovar=False)
        else:
            relogio.avancar(hours=73)
            uow = MongoUnitOfWork(banco)
            ExpirarOrcamentosVencidos(
                uow, MongoOrcamentoRepository(uow), relogio
            ).executar()
        antes = consultar(banco, relogio).por_id(dto.id)

        resultado = cancelamento(banco, relogio).executar(
            ordem_id=dto.ordem_id, motivo="x"
        )

        assert resultado == antes
        [envelope] = eventos_do_outbox(banco, "OrcamentoCancelado")
        assert envelope["dados"]["orcamento_id"] == str(dto.id)

    def test_cancelamento_antes_da_geracao_grava_lapide(
        self, banco: Banco, relogio: RelogioFixo
    ) -> None:
        ordem_id = uuid4()

        lapide = cancelamento(banco, relogio).executar(
            ordem_id=ordem_id, motivo="cancelamento"
        )

        assert (lapide.status, lapide.linhas, lapide.valido_ate) == (
            "CANCELADO",
            (),
            None,
        )
        [envelope] = eventos_do_outbox(banco, "OrcamentoCancelado")
        assert envelope["dados"] == {
            "ordem_id": str(ordem_id),
            "orcamento_id": str(lapide.id),
        }
        # Repetir responde de novo, sem outra lapide.
        cancelamento(banco, relogio).executar(ordem_id=ordem_id, motivo="reenvio")
        assert banco["orcamentos"].count_documents({"ordem_id": ordem_id}) == 1
        assert len(eventos_do_outbox(banco, "OrcamentoCancelado")) == 2

    def test_documento_com_defeito_nao_trava_a_fila_de_expiracao(
        self, banco: Banco, relogio: RelogioFixo, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Documento antigo sem os campos atuais, vencido antes de todos.
        banco["orcamentos"].insert_one(
            {
                "_id": uuid4(),
                "ordem_id": uuid4(),
                "status": "PENDENTE",
                "valido_ate": relogio.agora - timedelta(days=1),
            }
        )
        saudavel = gerado(banco, relogio)
        relogio.avancar(hours=73)
        uow = MongoUnitOfWork(banco)
        expirar = ExpirarOrcamentosVencidos(uow, MongoOrcamentoRepository(uow), relogio)

        with caplog.at_level(logging.ERROR):
            assert expirar.executar() == 1

        assert consultar(banco, relogio).por_id(saudavel.id).status == "EXPIRADO"
        assert "budget_expiration_failed" in caplog.messages


class TestConsultar:
    def test_por_id_por_ordem_e_por_link(
        self, banco: Banco, relogio: RelogioFixo
    ) -> None:
        dto = gerado(banco, relogio)
        consulta = consultar(banco, relogio)
        assert consulta.por_id(dto.id) == dto
        assert consulta.por_ordem(dto.ordem_id) == [dto]
        assert consulta.por_ordem(uuid4()) == []
        assert consulta.por_link(token_do_link(dto.id, dto.valido_ate)) == dto
        with pytest.raises(OrcamentoNaoEncontradoError):
            consulta.por_id(uuid4())
