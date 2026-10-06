"""Casos de uso do pagamento com MongoDB real e o provedor simulado."""

from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import pytest

from src.compartilhado.dominio.dinheiro import Dinheiro
from src.compartilhado.dominio.exceptions import TransicaoStatusInvalidaError
from src.compartilhado.infraestrutura.unit_of_work import MongoUnitOfWork
from src.orcamento.dominio.orcamento import CanalDecisao
from src.orcamento.infraestrutura.repository import MongoOrcamentoRepository
from src.pagamento.aplicacao.ports import (
    EstornoEmProcessamentoError,
    GatewayPagamentoIndisponivelError,
    GatewayPagamentoRecusouError,
)
from src.pagamento.aplicacao.use_cases import (
    CheckoutNaoEncontradoError,
    ConsultarPagamentos,
    EstornarPagamento,
    ExpirarPagamentosVencidos,
    ProcessarNotificacaoPagamento,
    SimularResultadoPagamento,
    SolicitarPagamento,
)
from src.pagamento.dominio.cobranca import SituacaoNoProvedor
from src.pagamento.dominio.estados import MotivoEstorno, StatusNoProvedor
from src.pagamento.dominio.exceptions import (
    OrcamentoNaoAprovadoError,
    PagamentoNaoEncontradoError,
)
from src.pagamento.infraestrutura.orcamentos import OrcamentosMongoAdapter
from src.pagamento.infraestrutura.repository import MongoPagamentoRepository
from tests.factories import dinheiro, orcamento
from tests.integracao.apoio import (
    GatewayRoteirizado,
    MetricasEspia,
    RelogioFixo,
    eventos_do_outbox,
    token_do_checkout,
)

if TYPE_CHECKING:
    from pymongo.database import Database

    from src.orcamento.dominio.orcamento import Orcamento
    from src.pagamento.aplicacao.dtos import PagamentoDTO

    Banco = Database[dict[str, Any]]


@pytest.fixture
def gateway() -> GatewayRoteirizado:
    return GatewayRoteirizado()


@pytest.fixture
def metricas() -> MetricasEspia:
    return MetricasEspia()


def salvar_orcamento(banco: Banco, relogio: RelogioFixo, *, aprovar: bool) -> Orcamento:
    o = orcamento(criado_em=relogio.agora)
    if aprovar:
        o.aprovar(canal=CanalDecisao.LINK, agora=relogio.agora)
    uow = MongoUnitOfWork(banco)
    uow.executar(lambda: MongoOrcamentoRepository(uow).salvar(o))
    return o


def solicitar(
    banco: Banco, gateway: GatewayRoteirizado, relogio: RelogioFixo
) -> SolicitarPagamento:
    uow = MongoUnitOfWork(banco)
    return SolicitarPagamento(
        uow,
        MongoPagamentoRepository(uow),
        OrcamentosMongoAdapter(uow),
        gateway,
        timedelta(minutes=60),
        relogio,
    )


def processar(
    banco: Banco,
    gateway: GatewayRoteirizado,
    relogio: RelogioFixo,
    metricas: MetricasEspia | None = None,
    *,
    max_recusas: int = 3,
) -> ProcessarNotificacaoPagamento:
    uow = MongoUnitOfWork(banco)
    return ProcessarNotificacaoPagamento(
        uow,
        MongoPagamentoRepository(uow),
        gateway,
        metricas or MetricasEspia(),
        max_recusas,
        relogio,
    )


def simular(
    banco: Banco,
    gateway: GatewayRoteirizado,
    relogio: RelogioFixo,
    *,
    max_recusas: int = 3,
) -> SimularResultadoPagamento:
    return SimularResultadoPagamento(
        gateway,
        MongoPagamentoRepository(MongoUnitOfWork(banco)),
        processar(banco, gateway, relogio, max_recusas=max_recusas),
        relogio,
    )


def pagar(
    banco: Banco,
    gateway: GatewayRoteirizado,
    relogio: RelogioFixo,
    dto: PagamentoDTO,
    *,
    aprovar: bool,
    max_recusas: int = 3,
) -> PagamentoDTO:
    """O cliente aprova ou recusa no checkout simulado (com o token do link)."""
    assert dto.checkout_url is not None
    return simular(banco, gateway, relogio, max_recusas=max_recusas).executar(
        dto.id, token=token_do_checkout(dto.checkout_url), aprovar=aprovar
    )


def estornar(
    banco: Banco,
    gateway: GatewayRoteirizado,
    relogio: RelogioFixo,
    metricas: MetricasEspia | None = None,
) -> EstornarPagamento:
    uow = MongoUnitOfWork(banco)
    return EstornarPagamento(
        uow,
        MongoPagamentoRepository(uow),
        gateway,
        metricas or MetricasEspia(),
        relogio,
    )


def consultar(banco: Banco, pagamento_id: UUID) -> PagamentoDTO:
    return ConsultarPagamentos(MongoPagamentoRepository(MongoUnitOfWork(banco))).por_id(
        pagamento_id
    )


def solicitado(
    banco: Banco, gateway: GatewayRoteirizado, relogio: RelogioFixo
) -> PagamentoDTO:
    o = salvar_orcamento(banco, relogio, aprovar=True)
    return solicitar(banco, gateway, relogio).executar(
        ordem_id=o.ordem_id, orcamento_id=o.id
    )


def confirmado(
    banco: Banco, gateway: GatewayRoteirizado, relogio: RelogioFixo
) -> PagamentoDTO:
    dto = solicitado(banco, gateway, relogio)
    return pagar(banco, gateway, relogio, dto, aprovar=True)


def tentativa(
    pagamento_id: UUID | str | None,
    status: StatusNoProvedor = StatusNoProvedor.APROVADO,
    *,
    referencia: str = "999",
    valor: Dinheiro | None = None,
    bruto: str = "approved",
) -> SituacaoNoProvedor:
    return SituacaoNoProvedor(
        referencia=referencia,
        referencia_externa=str(pagamento_id) if pagamento_id else None,
        status=status,
        status_provedor=bruto,
        detalhe=None,
        valor=valor or dinheiro("335.00"),
    )


def tipos(banco: Banco) -> list[str]:
    """Eventos do pagamento na outbox (sem os do orcamento de apoio)."""
    return [
        e["tipo"]
        for e in eventos_do_outbox(banco)
        if not e["tipo"].startswith("Orcamento")
    ]


class TestSolicitarPagamento:
    def test_cria_cobranca_e_grava_pagamento_solicitado(
        self, banco: Banco, gateway: GatewayRoteirizado, relogio: RelogioFixo
    ) -> None:
        o = salvar_orcamento(banco, relogio, aprovar=True)

        dto = solicitar(banco, gateway, relogio).executar(
            ordem_id=o.ordem_id, orcamento_id=o.id
        )

        assert dto.status == "SOLICITADO"
        assert (dto.valor, dto.moeda, dto.provedor, dto.recusas) == (
            Decimal("335.00"),
            "BRL",
            "simulado",
            0,
        )
        assert dto.checkout_url is not None
        assert dto.checkout_url.startswith(
            f"http://billing.teste/simulador/checkout/{dto.id}?token="
        )
        assert dto.expira_em == relogio.agora + timedelta(minutes=60)
        assert gateway.cobrancas == [dto.id]
        [envelope] = eventos_do_outbox(banco, "PagamentoSolicitado")
        assert envelope["dados"] == {
            "ordem_id": str(o.ordem_id),
            "pagamento_id": str(dto.id),
            "valor": "335.00",
            "moeda": "BRL",
            "checkout_url": dto.checkout_url,
            "expira_em": "2026-10-06T13:00:00.000Z",
        }

    def test_solicitar_duas_vezes_nao_duplica(
        self, banco: Banco, gateway: GatewayRoteirizado, relogio: RelogioFixo
    ) -> None:
        o = salvar_orcamento(banco, relogio, aprovar=True)
        caso = solicitar(banco, gateway, relogio)

        primeiro = caso.executar(ordem_id=o.ordem_id, orcamento_id=o.id)
        segundo = caso.executar(ordem_id=o.ordem_id, orcamento_id=o.id)

        assert segundo == primeiro
        assert banco["pagamentos"].count_documents({}) == 1
        assert len(gateway.cobrancas) == 1

    def test_orcamento_nao_aprovado(
        self, banco: Banco, gateway: GatewayRoteirizado, relogio: RelogioFixo
    ) -> None:
        o = salvar_orcamento(banco, relogio, aprovar=False)
        with pytest.raises(OrcamentoNaoAprovadoError):
            solicitar(banco, gateway, relogio).executar(
                ordem_id=o.ordem_id, orcamento_id=o.id
            )
        assert gateway.cobrancas == []

    def test_orcamento_inexistente_ou_de_outra_ordem(
        self, banco: Banco, gateway: GatewayRoteirizado, relogio: RelogioFixo
    ) -> None:
        o = salvar_orcamento(banco, relogio, aprovar=True)
        caso = solicitar(banco, gateway, relogio)
        with pytest.raises(PagamentoNaoEncontradoError, match="Orcamento"):
            caso.executar(ordem_id=uuid4(), orcamento_id=o.id)
        with pytest.raises(PagamentoNaoEncontradoError):
            caso.executar(ordem_id=o.ordem_id, orcamento_id=uuid4())

    def test_provedor_fora_nao_grava_nada(
        self, banco: Banco, gateway: GatewayRoteirizado, relogio: RelogioFixo
    ) -> None:
        o = salvar_orcamento(banco, relogio, aprovar=True)
        gateway.erro_na_cobranca = GatewayPagamentoIndisponivelError()
        with pytest.raises(GatewayPagamentoIndisponivelError):
            solicitar(banco, gateway, relogio).executar(
                ordem_id=o.ordem_id, orcamento_id=o.id
            )
        assert banco["pagamentos"].count_documents({}) == 0
        assert eventos_do_outbox(banco, "PagamentoSolicitado") == []


class TestNotificacao:
    def test_aprovacao_consultada_no_provedor_confirma(
        self, banco: Banco, gateway: GatewayRoteirizado, relogio: RelogioFixo
    ) -> None:
        dto = solicitado(banco, gateway, relogio)
        referencia = gateway.registrar_resultado(
            pagamento_id=dto.id, valor=dinheiro("335.00"), aprovado=True
        )
        relogio.avancar(minutes=5)

        confirmado = processar(banco, gateway, relogio).executar(referencia)

        assert confirmado is not None
        assert confirmado.status == "CONFIRMADO"
        assert confirmado.referencia_pagamento == referencia
        assert [n.status_provedor for n in confirmado.notificacoes] == ["approved"]
        [envelope] = eventos_do_outbox(banco, "PagamentoConfirmado")
        assert envelope["dados"] == {
            "ordem_id": str(dto.ordem_id),
            "pagamento_id": str(dto.id),
            "valor": "335.00",
            "moeda": "BRL",
            "confirmado_em": "2026-10-06T12:05:00.000Z",
            "referencia_provedor": referencia,
        }

    def test_notificacao_repetida_nao_grava_nem_duplica_evento(
        self, banco: Banco, gateway: GatewayRoteirizado, relogio: RelogioFixo
    ) -> None:
        dto = solicitado(banco, gateway, relogio)
        referencia = gateway.registrar_resultado(
            pagamento_id=dto.id, valor=dinheiro("335.00"), aprovado=True
        )
        caso = processar(banco, gateway, relogio)
        caso.executar(referencia)
        antes = banco["pagamentos"].find_one({"_id": dto.id})

        relogio.avancar(minutes=1)
        repetido = caso.executar(referencia)

        assert repetido is not None
        assert len(repetido.notificacoes) == 1
        assert len(eventos_do_outbox(banco, "PagamentoConfirmado")) == 1
        assert banco["pagamentos"].find_one({"_id": dto.id}) == antes
        assert gateway.estornos == []

    def test_recusas_contam_ate_o_maximo(
        self, banco: Banco, gateway: GatewayRoteirizado, relogio: RelogioFixo
    ) -> None:
        dto = solicitado(banco, gateway, relogio)
        caso = processar(banco, gateway, relogio, max_recusas=2)
        primeira = caso.aplicar(
            tentativa(
                dto.id, StatusNoProvedor.RECUSADO, referencia="r1", bruto="rejected"
            )
        )
        assert primeira is not None
        assert (primeira.status, primeira.recusas) == ("SOLICITADO", 1)
        assert eventos_do_outbox(banco, "PagamentoRecusado") == []

        segunda = caso.aplicar(
            tentativa(
                dto.id, StatusNoProvedor.RECUSADO, referencia="r2", bruto="rejected"
            )
        )

        assert segunda is not None
        assert (segunda.status, segunda.recusas, segunda.motivo) == (
            "RECUSADO",
            2,
            "rejected",
        )
        [envelope] = eventos_do_outbox(banco, "PagamentoRecusado")
        assert envelope["dados"]["motivo"] == "rejected"

    def test_em_andamento_no_provedor_so_entra_no_historico(
        self, banco: Banco, gateway: GatewayRoteirizado, relogio: RelogioFixo
    ) -> None:
        dto = solicitado(banco, gateway, relogio)
        gateway.respostas["999"] = tentativa(
            dto.id, StatusNoProvedor.EM_ANDAMENTO, bruto="in_process"
        )
        resultado = processar(banco, gateway, relogio).executar("999")
        assert resultado is not None
        assert resultado.status == "SOLICITADO"
        assert [n.status_provedor for n in resultado.notificacoes] == ["in_process"]

    @pytest.mark.parametrize(
        "valor",
        [
            pytest.param(dinheiro("1.00"), id="valor-divergente"),
            pytest.param(
                Dinheiro(Decimal("335.00"), moeda="USD"), id="moeda-divergente"
            ),
        ],
    )
    def test_valor_ou_moeda_divergente_e_estornado_e_registrado(
        self,
        banco: Banco,
        gateway: GatewayRoteirizado,
        relogio: RelogioFixo,
        metricas: MetricasEspia,
        valor: Dinheiro,
    ) -> None:
        dto = solicitado(banco, gateway, relogio)
        gateway.respostas["999"] = tentativa(dto.id, valor=valor)

        resultado = processar(banco, gateway, relogio, metricas).executar("999")

        assert resultado is not None
        assert resultado.status == "SOLICITADO"
        assert eventos_do_outbox(banco, "PagamentoConfirmado") == []
        assert gateway.estornos == [("999", f"estorno-{dto.id}-999")]
        assert [e.referencia_pagamento for e in resultado.estornos_automaticos] == [
            "999"
        ]
        [envelope] = eventos_do_outbox(banco, "PagamentoEstornado")
        assert envelope["dados"]["motivo"] == "pagamento_apos_encerramento"
        assert metricas.estornos == [MotivoEstorno.PAGAMENTO_APOS_ENCERRAMENTO]

    def test_aprovacao_depois_da_expiracao_termina_estornada(
        self,
        banco: Banco,
        gateway: GatewayRoteirizado,
        relogio: RelogioFixo,
        metricas: MetricasEspia,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        dto = solicitado(banco, gateway, relogio)
        relogio.avancar(minutes=61)
        uow = MongoUnitOfWork(banco)
        ExpirarPagamentosVencidos(
            uow, MongoPagamentoRepository(uow), relogio
        ).executar()
        gateway.respostas["999"] = tentativa(dto.id)

        with caplog.at_level(logging.WARNING):
            resultado = processar(banco, gateway, relogio, metricas).executar("999")

        assert resultado is not None
        assert (resultado.status, resultado.motivo_estorno) == (
            "ESTORNADO",
            "pagamento_apos_encerramento",
        )
        assert tipos(banco) == [
            "PagamentoSolicitado",
            "PagamentoExpirado",
            "PagamentoEstornado",
        ]
        assert gateway.estornos == [("999", f"estorno-{dto.id}-999")]
        assert "automatic_refund_done" in caplog.messages

        # O provedor reenvia: nada e estornado de novo.
        processar(banco, gateway, relogio, metricas).executar("999")
        assert len(gateway.estornos) == 1
        assert len(metricas.estornos) == 1

    def test_estorno_automatico_recusado_fica_marcado_no_agregado(
        self,
        banco: Banco,
        gateway: GatewayRoteirizado,
        relogio: RelogioFixo,
        metricas: MetricasEspia,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        dto = confirmado(banco, gateway, relogio)
        gateway.respostas["999"] = tentativa(dto.id)
        gateway.erro_no_estorno = GatewayPagamentoRecusouError("payment too old")
        with caplog.at_level(logging.ERROR):
            resultado = processar(banco, gateway, relogio, metricas).executar("999")
        assert resultado is not None
        assert resultado.status == "CONFIRMADO"
        [estorno] = resultado.estornos_automaticos
        assert (estorno.referencia_pagamento, estorno.falha) == (
            "999",
            "payment too old",
        )
        assert eventos_do_outbox(banco, "PagamentoEstornado") == []
        assert metricas.estornos_automaticos_recusados == 1
        assert "automatic_refund_refused" in caplog.messages

    def test_estorno_automatico_recusado_mas_ja_estornado_no_provedor_conclui(
        self, banco: Banco, gateway: GatewayRoteirizado, relogio: RelogioFixo
    ) -> None:
        # Pago depois da data de expiracao da preferencia: o proprio provedor
        # estorna, e o nosso pedido de estorno e recusado.
        dto = solicitado(banco, gateway, relogio)
        relogio.avancar(minutes=61)
        uow = MongoUnitOfWork(banco)
        ExpirarPagamentosVencidos(
            uow, MongoPagamentoRepository(uow), relogio
        ).executar()
        caso = processar(banco, gateway, relogio)
        gateway.erro_no_estorno = GatewayPagamentoRecusouError("already refunded")
        gateway.respostas["999"] = tentativa(
            dto.id, StatusNoProvedor.ESTORNADO, bruto="refunded"
        )

        resultado = caso.aplicar(tentativa(dto.id))

        assert resultado is not None
        assert resultado.status == "ESTORNADO"
        assert resultado.estornos_automaticos[0].falha is None

    def test_estorno_automatico_com_provedor_fora_propaga_para_reenvio(
        self, banco: Banco, gateway: GatewayRoteirizado, relogio: RelogioFixo
    ) -> None:
        dto = confirmado(banco, gateway, relogio)
        gateway.respostas["999"] = tentativa(dto.id)
        gateway.erro_no_estorno = GatewayPagamentoIndisponivelError()
        with pytest.raises(GatewayPagamentoIndisponivelError):
            processar(banco, gateway, relogio).executar("999")
        # A notificacao ficou registrada, o estorno nao: o reenvio o repete.
        depois = consultar(banco, dto.id)
        assert len(depois.notificacoes) == 2
        assert depois.estornos_automaticos == ()
        gateway.erro_no_estorno = None
        processar(banco, gateway, relogio).executar("999")
        assert len(gateway.estornos) == 2
        assert len(consultar(banco, dto.id).estornos_automaticos) == 1

    def test_recusa_depois_de_confirmado_e_ignorada(
        self, banco: Banco, gateway: GatewayRoteirizado, relogio: RelogioFixo
    ) -> None:
        dto = confirmado(banco, gateway, relogio)
        gateway.respostas["999"] = tentativa(
            dto.id, StatusNoProvedor.RECUSADO, bruto="rejected"
        )
        resultado = processar(banco, gateway, relogio).executar("999")
        assert resultado is not None
        assert (resultado.status, resultado.recusas) == ("CONFIRMADO", 0)

    @pytest.mark.parametrize(
        "externa", [None, "nao-e-uuid"], ids=["sem-referencia", "referencia-invalida"]
    )
    def test_referencia_que_nao_e_nossa_e_ignorada(
        self,
        banco: Banco,
        gateway: GatewayRoteirizado,
        relogio: RelogioFixo,
        externa: str | None,
    ) -> None:
        gateway.respostas["999"] = tentativa(externa)
        assert processar(banco, gateway, relogio).executar("999") is None
        assert processar(banco, gateway, relogio).executar("sim-nada") is None

    def test_pagamento_inexistente_e_ignorado(
        self, banco: Banco, gateway: GatewayRoteirizado, relogio: RelogioFixo
    ) -> None:
        gateway.respostas["999"] = tentativa(uuid4())
        assert processar(banco, gateway, relogio).executar("999") is None
        assert eventos_do_outbox(banco) == []


class TestSimulador:
    def test_aprovar_pelo_simulador(
        self, banco: Banco, gateway: GatewayRoteirizado, relogio: RelogioFixo
    ) -> None:
        pago = confirmado(banco, gateway, relogio)
        assert pago.status == "CONFIRMADO"
        assert pago.referencia_pagamento is not None
        assert pago.referencia_pagamento.startswith("sim-")

    def test_recusa_do_simulador_conta_como_a_do_provedor(
        self, banco: Banco, gateway: GatewayRoteirizado, relogio: RelogioFixo
    ) -> None:
        dto = solicitado(banco, gateway, relogio)
        primeira = pagar(banco, gateway, relogio, dto, aprovar=False, max_recusas=2)
        assert (primeira.status, primeira.recusas) == ("SOLICITADO", 1)
        segunda = pagar(banco, gateway, relogio, dto, aprovar=False, max_recusas=2)
        assert (segunda.status, segunda.recusas) == ("RECUSADO", 2)

        outro = solicitado(banco, gateway, relogio)
        uma = pagar(banco, gateway, relogio, outro, aprovar=False, max_recusas=1)
        assert uma.status == "RECUSADO"

    def test_pagamento_ja_processado(
        self, banco: Banco, gateway: GatewayRoteirizado, relogio: RelogioFixo
    ) -> None:
        pago = confirmado(banco, gateway, relogio)
        with pytest.raises(TransicaoStatusInvalidaError, match="CONFIRMADO"):
            pagar(banco, gateway, relogio, pago, aprovar=False)

    def test_checkout_exige_o_token_do_pagamento(
        self, banco: Banco, gateway: GatewayRoteirizado, relogio: RelogioFixo
    ) -> None:
        dto = solicitado(banco, gateway, relogio)
        outro = solicitado(banco, gateway, relogio)
        assert dto.checkout_url is not None
        assert outro.checkout_url is not None
        caso = simular(banco, gateway, relogio)
        for token in (None, "x.y.z", token_do_checkout(outro.checkout_url)):
            with pytest.raises(CheckoutNaoEncontradoError):
                caso.executar(dto.id, token=token, aprovar=True)
            with pytest.raises(CheckoutNaoEncontradoError):
                caso.consultar(dto.id, token)
        assert caso.consultar(dto.id, token_do_checkout(dto.checkout_url)) == dto
        assert eventos_do_outbox(banco, "PagamentoConfirmado") == []

    def test_referencia_que_o_provedor_nao_reconhece(
        self, banco: Banco, gateway: GatewayRoteirizado, relogio: RelogioFixo
    ) -> None:
        dto = solicitado(banco, gateway, relogio)
        assert dto.checkout_url is not None

        class SimuladorEsquecido(GatewayRoteirizado):
            def registrar_resultado(self, **_: Any) -> str:
                return "sim-esquecido"

        esquecido = SimuladorEsquecido()
        caso = SimularResultadoPagamento(
            esquecido,
            MongoPagamentoRepository(MongoUnitOfWork(banco)),
            processar(banco, esquecido, relogio),
            relogio,
        )
        with pytest.raises(PagamentoNaoEncontradoError):
            caso.executar(
                dto.id, token=token_do_checkout(dto.checkout_url), aprovar=True
            )


class TestExpirarPagamentos:
    def test_expira_so_solicitados_vencidos(
        self, banco: Banco, gateway: GatewayRoteirizado, relogio: RelogioFixo
    ) -> None:
        pendente = solicitado(banco, gateway, relogio)
        pago = confirmado(banco, gateway, relogio)
        relogio.avancar(minutes=60, seconds=1)
        uow = MongoUnitOfWork(banco)
        expirar = ExpirarPagamentosVencidos(uow, MongoPagamentoRepository(uow), relogio)

        assert expirar.executar() == 1
        assert expirar.executar() == 0

        assert consultar(banco, pendente.id).status == "EXPIRADO"
        assert consultar(banco, pago.id).status == "CONFIRMADO"
        [envelope] = eventos_do_outbox(banco, "PagamentoExpirado")
        assert envelope["dados"] == {
            "ordem_id": str(pendente.ordem_id),
            "pagamento_id": str(pendente.id),
            "motivo": "Prazo de pagamento esgotado",
        }


class TestFilaDeExpiracao:
    def test_documento_com_defeito_nao_trava_a_fila(
        self,
        banco: Banco,
        gateway: GatewayRoteirizado,
        relogio: RelogioFixo,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        banco["pagamentos"].insert_one(
            {
                "_id": uuid4(),
                "orcamento_id": uuid4(),
                "status": "SOLICITADO",
                "expira_em": relogio.agora - timedelta(days=1),
            }
        )
        pendente = solicitado(banco, gateway, relogio)
        relogio.avancar(minutes=61)
        uow = MongoUnitOfWork(banco)
        expirar = ExpirarPagamentosVencidos(uow, MongoPagamentoRepository(uow), relogio)

        with caplog.at_level(logging.ERROR):
            assert expirar.executar() == 1

        assert consultar(banco, pendente.id).status == "EXPIRADO"
        assert "payment_expiration_failed" in caplog.messages


class TestEstornarPagamento:
    def test_confirmado_e_estornado_com_a_chave_do_pagamento(
        self,
        banco: Banco,
        gateway: GatewayRoteirizado,
        relogio: RelogioFixo,
        metricas: MetricasEspia,
    ) -> None:
        pago = confirmado(banco, gateway, relogio)
        relogio.avancar(minutes=10)

        estornado = estornar(banco, gateway, relogio, metricas).executar(
            ordem_id=pago.ordem_id, pagamento_id=pago.id, motivo="reserva_falhou"
        )

        assert (estornado.status, estornado.motivo, estornado.motivo_estorno) == (
            "ESTORNADO",
            "reserva_falhou",
            "compensacao",
        )
        assert gateway.estornos == [(pago.referencia_pagamento, f"estorno-{pago.id}")]
        [envelope] = eventos_do_outbox(banco, "PagamentoEstornado")
        assert envelope["dados"] == {
            "ordem_id": str(pago.ordem_id),
            "pagamento_id": str(pago.id),
            "estornado_em": "2026-10-06T12:10:00.000Z",
            "motivo": "compensacao",
        }
        assert metricas.estornos == [MotivoEstorno.COMPENSACAO]

    def test_repetir_republica_o_estornado_sem_chamar_o_provedor(
        self, banco: Banco, gateway: GatewayRoteirizado, relogio: RelogioFixo
    ) -> None:
        pago = confirmado(banco, gateway, relogio)
        caso = estornar(banco, gateway, relogio)
        respostas = [
            caso.executar(ordem_id=pago.ordem_id, pagamento_id=pago.id, motivo="x")
            for _ in range(2)
        ]
        assert [r.status for r in respostas] == ["ESTORNADO", "ESTORNADO"]
        assert len(gateway.estornos) == 1
        estornos = eventos_do_outbox(banco, "PagamentoEstornado")
        assert len(estornos) == 2
        assert estornos[0]["dados"] == estornos[1]["dados"]
        assert eventos_do_outbox(banco, "EstornoDePagamentoFalhou") == []

    def test_solicitado_e_cancelado_no_provedor_e_responde_cancelado(
        self, banco: Banco, gateway: GatewayRoteirizado, relogio: RelogioFixo
    ) -> None:
        pendente = solicitado(banco, gateway, relogio)
        relogio.avancar(minutes=5)

        resultado = estornar(banco, gateway, relogio).executar(
            ordem_id=pendente.ordem_id, pagamento_id=pendente.id, motivo="cancelamento"
        )

        assert (resultado.status, resultado.motivo) == ("CANCELADO", "cancelamento")
        assert gateway.cancelamentos == [pendente.referencia_preferencia]
        assert gateway.estornos == []
        [envelope] = eventos_do_outbox(banco, "PagamentoCancelado")
        assert envelope["dados"] == {
            "ordem_id": str(pendente.ordem_id),
            "pagamento_id": str(pendente.id),
            "cancelado_em": "2026-10-06T12:05:00.000Z",
        }

        # O cliente paga depois: o dinheiro volta e o pagamento termina estornado.
        gateway.respostas["tardia"] = tentativa(pendente.id, referencia="tardia")
        final = processar(banco, gateway, relogio).executar("tardia")
        assert final is not None
        assert final.status == "ESTORNADO"
        assert gateway.estornos == [("tardia", f"estorno-{pendente.id}-tardia")]

    @pytest.mark.parametrize("encerramento", ["recusa", "expiracao"])
    def test_recusado_ou_expirado_responde_cancelado_sem_efeito(
        self,
        banco: Banco,
        gateway: GatewayRoteirizado,
        relogio: RelogioFixo,
        encerramento: str,
    ) -> None:
        pendente = solicitado(banco, gateway, relogio)
        if encerramento == "recusa":
            pagar(banco, gateway, relogio, pendente, aprovar=False, max_recusas=1)
        else:
            relogio.avancar(minutes=61)
            uow = MongoUnitOfWork(banco)
            ExpirarPagamentosVencidos(
                uow, MongoPagamentoRepository(uow), relogio
            ).executar()
        antes = consultar(banco, pendente.id)

        resultado = estornar(banco, gateway, relogio).executar(
            ordem_id=pendente.ordem_id, pagamento_id=pendente.id, motivo="x"
        )

        assert resultado == antes
        assert gateway.cancelamentos == []
        assert gateway.estornos == []
        assert antes.encerrado_em is not None
        [envelope] = eventos_do_outbox(banco, "PagamentoCancelado")
        assert envelope["dados"]["cancelado_em"] == (
            antes.encerrado_em.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        )
        assert eventos_do_outbox(banco, "EstornoDePagamentoFalhou") == []

    def test_ja_estornado_no_provedor_so_registra(
        self,
        banco: Banco,
        gateway: GatewayRoteirizado,
        relogio: RelogioFixo,
        metricas: MetricasEspia,
    ) -> None:
        # Estorno feito pelo painel (intervencao manual) antes da retomada.
        pago = confirmado(banco, gateway, relogio)
        assert pago.referencia_pagamento is not None
        gateway.estornar(pago.referencia_pagamento, chave_idempotencia="painel")
        gateway.estornos.clear()

        resultado = estornar(banco, gateway, relogio, metricas).executar(
            ordem_id=pago.ordem_id, pagamento_id=pago.id, motivo="retomada"
        )

        assert resultado.status == "ESTORNADO"
        assert gateway.estornos == []
        assert len(eventos_do_outbox(banco, "PagamentoEstornado")) == 1
        assert metricas.estornos == [MotivoEstorno.COMPENSACAO]

    def test_estorno_em_processamento_mas_ja_estornado_na_consulta_conclui(
        self, banco: Banco, gateway: GatewayRoteirizado, relogio: RelogioFixo
    ) -> None:
        pago = confirmado(banco, gateway, relogio)
        assert pago.referencia_pagamento is not None
        referencia = pago.referencia_pagamento

        class EstornaEmProcessamento(GatewayRoteirizado):
            def estornar(self, ref: str, *, chave_idempotencia: str) -> None:
                gateway.estornar(ref, chave_idempotencia=chave_idempotencia)
                raise EstornoEmProcessamentoError

            def consultar_pagamento(self, ref: str) -> SituacaoNoProvedor | None:
                return gateway.consultar_pagamento(ref)

        uow = MongoUnitOfWork(banco)
        resultado = EstornarPagamento(
            uow,
            MongoPagamentoRepository(uow),
            EstornaEmProcessamento(),
            MetricasEspia(),
            relogio,
        ).executar(ordem_id=pago.ordem_id, pagamento_id=pago.id, motivo="x")
        assert resultado.status == "ESTORNADO"
        assert gateway.estornos == [(referencia, f"estorno-{pago.id}")]

    def test_aprovacao_entre_a_leitura_e_o_cancelamento_vira_estorno(
        self, banco: Banco, gateway: GatewayRoteirizado, relogio: RelogioFixo
    ) -> None:
        pendente = solicitado(banco, gateway, relogio)
        repo = MongoPagamentoRepository(MongoUnitOfWork(banco))
        antes = repo.obter_por_id(pendente.id)
        pago = pagar(banco, gateway, relogio, pendente, aprovar=True)

        class LeituraAntiga:
            """1a leitura devolve o pagamento ainda SOLICITADO (leitura velha)."""

            def __init__(self, repo: MongoPagamentoRepository) -> None:
                self._repo = repo
                self._primeira = True

            def __getattr__(self, nome: str) -> Any:
                return getattr(self._repo, nome)

            def obter_por_id(self, pagamento_id: UUID) -> Any:
                if self._primeira:
                    self._primeira = False
                    return antes
                return self._repo.obter_por_id(pagamento_id)

        uow = MongoUnitOfWork(banco)
        caso = EstornarPagamento(
            uow,
            LeituraAntiga(MongoPagamentoRepository(uow)),
            gateway,
            MetricasEspia(),
            relogio,
        )
        resultado = caso.executar(
            ordem_id=pendente.ordem_id, pagamento_id=pendente.id, motivo="cancelamento"
        )

        assert resultado.status == "ESTORNADO"
        # Fechou o checkout (plano velho), releu CONFIRMADO e estornou de verdade.
        assert gateway.cancelamentos == [pendente.referencia_preferencia]
        assert gateway.estornos == [(pago.referencia_pagamento, f"estorno-{pago.id}")]
        assert tipos(banco)[-1] == "PagamentoEstornado"

    def test_estorno_em_processamento_no_provedor_e_repetido_depois(
        self, banco: Banco, gateway: GatewayRoteirizado, relogio: RelogioFixo
    ) -> None:
        pago = confirmado(banco, gateway, relogio)
        gateway.erro_no_estorno = EstornoEmProcessamentoError()
        with pytest.raises(EstornoEmProcessamentoError):
            estornar(banco, gateway, relogio).executar(
                ordem_id=pago.ordem_id, pagamento_id=pago.id, motivo="x"
            )
        assert consultar(banco, pago.id).status == "CONFIRMADO"
        assert eventos_do_outbox(banco, "PagamentoEstornado") == []

    def test_recusa_do_provedor_gera_falha_de_estorno(
        self,
        banco: Banco,
        gateway: GatewayRoteirizado,
        relogio: RelogioFixo,
        metricas: MetricasEspia,
    ) -> None:
        pago = confirmado(banco, gateway, relogio)
        gateway.erro_no_estorno = GatewayPagamentoRecusouError(
            "Mercado Pago respondeu 400: payment too old to refund " + "x" * 600
        )
        resultado = estornar(banco, gateway, relogio, metricas).executar(
            ordem_id=pago.ordem_id, pagamento_id=pago.id, motivo="x"
        )
        assert resultado.status == "CONFIRMADO"
        [envelope] = eventos_do_outbox(banco, "EstornoDePagamentoFalhou")
        motivo = envelope["dados"]["motivo"]
        assert motivo.startswith("Mercado Pago respondeu 400: payment too old")
        assert len(motivo) == 500
        assert metricas.estornos == []

    def test_recusa_porque_ja_estornado_no_provedor_conclui_o_estorno(
        self, banco: Banco, gateway: GatewayRoteirizado, relogio: RelogioFixo
    ) -> None:
        # Tentativa anterior estornou no provedor e caiu antes de gravar aqui.
        pago = confirmado(banco, gateway, relogio)
        assert pago.referencia_pagamento is not None
        referencia = pago.referencia_pagamento

        class EstornoAntigo(GatewayRoteirizado):
            def consultar_pagamento(self, ref: str) -> SituacaoNoProvedor | None:
                # A 1a consulta ainda mostra aprovado; o estorno e recusado
                # porque a tentativa anterior ja estornou.
                return gateway.consultar_pagamento(ref)

            def estornar(self, ref: str, *, chave_idempotencia: str) -> None:
                gateway.estornar(ref, chave_idempotencia="tentativa-anterior")
                raise GatewayPagamentoRecusouError("already refunded")

        uow = MongoUnitOfWork(banco)
        resultado = EstornarPagamento(
            uow,
            MongoPagamentoRepository(uow),
            EstornoAntigo(),
            MetricasEspia(),
            relogio,
        ).executar(ordem_id=pago.ordem_id, pagamento_id=pago.id, motivo="x")

        assert resultado.status == "ESTORNADO"
        assert gateway.estornos == [(referencia, "tentativa-anterior")]
        assert eventos_do_outbox(banco, "EstornoDePagamentoFalhou") == []
        assert len(eventos_do_outbox(banco, "PagamentoEstornado")) == 1

    def test_falha_transitoria_propaga_sem_gravar(
        self, banco: Banco, gateway: GatewayRoteirizado, relogio: RelogioFixo
    ) -> None:
        pago = confirmado(banco, gateway, relogio)
        gateway.erro_no_estorno = GatewayPagamentoIndisponivelError()
        antes = len(eventos_do_outbox(banco))
        with pytest.raises(GatewayPagamentoIndisponivelError):
            estornar(banco, gateway, relogio).executar(
                ordem_id=pago.ordem_id, pagamento_id=pago.id, motivo="x"
            )
        assert consultar(banco, pago.id).status == "CONFIRMADO"
        assert len(eventos_do_outbox(banco)) == antes

    def test_cancelamento_com_provedor_fora_propaga_sem_gravar(
        self, banco: Banco, gateway: GatewayRoteirizado, relogio: RelogioFixo
    ) -> None:
        pendente = solicitado(banco, gateway, relogio)
        gateway.erro_no_cancelamento = GatewayPagamentoIndisponivelError()
        with pytest.raises(GatewayPagamentoIndisponivelError):
            estornar(banco, gateway, relogio).executar(
                ordem_id=pendente.ordem_id, pagamento_id=pendente.id, motivo="x"
            )
        assert consultar(banco, pendente.id).status == "SOLICITADO"
        assert eventos_do_outbox(banco, "PagamentoCancelado") == []

    def test_exige_a_ordem_dona_do_pagamento(
        self, banco: Banco, gateway: GatewayRoteirizado, relogio: RelogioFixo
    ) -> None:
        pago = confirmado(banco, gateway, relogio)
        with pytest.raises(PagamentoNaoEncontradoError, match="nao pertence"):
            estornar(banco, gateway, relogio).executar(
                ordem_id=uuid4(), pagamento_id=pago.id, motivo="x"
            )
