"""Casos de uso do pagamento com MongoDB real e o provedor simulado."""

from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import pytest

from src.compartilhado.dominio.exceptions import TransicaoStatusInvalidaError
from src.compartilhado.infraestrutura.unit_of_work import MongoUnitOfWork
from src.orcamento.dominio.orcamento import CanalDecisao
from src.orcamento.infraestrutura.repository import MongoOrcamentoRepository
from src.pagamento.aplicacao.ports import (
    EstornoEmProcessamentoError,
    GatewayPagamentoIndisponivelError,
    GatewayPagamentoRecusouError,
    SituacaoNoProvedor,
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
from src.pagamento.dominio.exceptions import (
    OrcamentoNaoAprovadoError,
    PagamentoNaoEncontradoError,
)
from src.pagamento.dominio.pagamento import StatusPagamento
from src.pagamento.infraestrutura.orcamentos import OrcamentosMongoAdapter
from src.pagamento.infraestrutura.repository import MongoPagamentoRepository
from tests.factories import dinheiro, orcamento
from tests.integracao.apoio import (
    GatewayRoteirizado,
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
    banco: Banco, gateway: GatewayRoteirizado, relogio: RelogioFixo
) -> ProcessarNotificacaoPagamento:
    uow = MongoUnitOfWork(banco)
    return ProcessarNotificacaoPagamento(
        uow, MongoPagamentoRepository(uow), gateway, relogio
    )


def simular(
    banco: Banco, gateway: GatewayRoteirizado, relogio: RelogioFixo
) -> SimularResultadoPagamento:
    return SimularResultadoPagamento(
        gateway,
        MongoPagamentoRepository(MongoUnitOfWork(banco)),
        processar(banco, gateway, relogio),
        relogio,
    )


def pagar(
    banco: Banco,
    gateway: GatewayRoteirizado,
    relogio: RelogioFixo,
    dto: PagamentoDTO,
    *,
    aprovar: bool,
) -> PagamentoDTO:
    """O cliente aprova ou recusa no checkout simulado (com o token do link)."""
    return simular(banco, gateway, relogio).executar(
        dto.id, token=token_do_checkout(dto.checkout_url), aprovar=aprovar
    )


def estornar(
    banco: Banco, gateway: GatewayRoteirizado, relogio: RelogioFixo
) -> EstornarPagamento:
    uow = MongoUnitOfWork(banco)
    return EstornarPagamento(uow, MongoPagamentoRepository(uow), gateway, relogio)


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


def aprovado(
    banco: Banco, gateway: GatewayRoteirizado, relogio: RelogioFixo
) -> PagamentoDTO:
    dto = solicitado(banco, gateway, relogio)
    return pagar(banco, gateway, relogio, dto, aprovar=True)


class TestSolicitarPagamento:
    def test_cria_cobranca_e_grava_pagamento_solicitado(
        self, banco: Banco, gateway: GatewayRoteirizado, relogio: RelogioFixo
    ) -> None:
        o = salvar_orcamento(banco, relogio, aprovar=True)

        dto = solicitar(banco, gateway, relogio).executar(
            ordem_id=o.ordem_id, orcamento_id=o.id
        )

        assert dto.status == "PENDENTE"
        assert (dto.valor, dto.moeda, dto.provedor) == (
            Decimal("335.00"),
            "BRL",
            "simulado",
        )
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
        assert len(eventos_do_outbox(banco, "PagamentoSolicitado")) == 1
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
        assert confirmado.status == "APROVADO"
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

    def test_notificacao_repetida_nao_duplica_historico_nem_evento(
        self, banco: Banco, gateway: GatewayRoteirizado, relogio: RelogioFixo
    ) -> None:
        dto = solicitado(banco, gateway, relogio)
        referencia = gateway.registrar_resultado(
            pagamento_id=dto.id, valor=dinheiro("335.00"), aprovado=True
        )
        caso = processar(banco, gateway, relogio)
        caso.executar(referencia)
        repetido = caso.executar(referencia)
        assert repetido is not None
        assert len(repetido.notificacoes) == 1
        assert len(eventos_do_outbox(banco, "PagamentoConfirmado")) == 1
        assert gateway.estornos == []

    def test_recusa_consultada_no_provedor(
        self, banco: Banco, gateway: GatewayRoteirizado, relogio: RelogioFixo
    ) -> None:
        dto = solicitado(banco, gateway, relogio)
        referencia = gateway.registrar_resultado(
            pagamento_id=dto.id, valor=dinheiro("335.00"), aprovado=False
        )
        recusado = processar(banco, gateway, relogio).executar(referencia)
        assert recusado is not None
        assert (recusado.status, recusado.motivo) == (
            "RECUSADO",
            "cc_rejected_other_reason",
        )
        [envelope] = eventos_do_outbox(banco, "PagamentoRecusado")
        assert envelope["dados"]["motivo"] == "cc_rejected_other_reason"

    def situacao(
        self,
        pagamento_id: UUID | str | None,
        status: StatusPagamento,
        *,
        valor: str | None = "335.00",
        bruto: str = "approved",
    ) -> SituacaoNoProvedor:
        return SituacaoNoProvedor(
            referencia="999",
            referencia_externa=str(pagamento_id) if pagamento_id else None,
            status=status,
            status_provedor=bruto,
            detalhe=None,
            valor=dinheiro(valor) if valor else None,
        )

    def test_pendente_no_provedor_so_entra_no_historico(
        self, banco: Banco, gateway: GatewayRoteirizado, relogio: RelogioFixo
    ) -> None:
        dto = solicitado(banco, gateway, relogio)
        gateway.respostas["999"] = self.situacao(
            dto.id, StatusPagamento.PENDENTE, bruto="in_process"
        )
        resultado = processar(banco, gateway, relogio).executar("999")
        assert resultado is not None
        assert resultado.status == "PENDENTE"
        assert [n.status_provedor for n in resultado.notificacoes] == ["in_process"]

    def test_valor_divergente_nao_confirma(
        self,
        banco: Banco,
        gateway: GatewayRoteirizado,
        relogio: RelogioFixo,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        dto = solicitado(banco, gateway, relogio)
        gateway.respostas["999"] = self.situacao(
            dto.id, StatusPagamento.APROVADO, valor="1.00"
        )
        with caplog.at_level(logging.WARNING):
            resultado = processar(banco, gateway, relogio).executar("999")
        assert resultado is not None
        assert resultado.status == "PENDENTE"
        assert eventos_do_outbox(banco, "PagamentoConfirmado") == []
        # Dinheiro em valor errado volta ao cliente na hora.
        assert gateway.estornos == [("999", "estorno-automatico-999")]
        assert "pagamento_estornado_automaticamente" in caplog.messages

    def test_aprovacao_num_pagamento_encerrado_e_estornada_na_hora(
        self,
        banco: Banco,
        gateway: GatewayRoteirizado,
        relogio: RelogioFixo,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        dto = solicitado(banco, gateway, relogio)
        relogio.avancar(minutes=61)
        uow = MongoUnitOfWork(banco)
        ExpirarPagamentosVencidos(
            uow, MongoPagamentoRepository(uow), relogio
        ).executar()
        gateway.respostas["999"] = self.situacao(dto.id, StatusPagamento.APROVADO)

        with caplog.at_level(logging.WARNING):
            resultado = processar(banco, gateway, relogio).executar("999")

        assert resultado is not None
        assert resultado.status == "EXPIRADO"
        assert eventos_do_outbox(banco, "PagamentoConfirmado") == []
        assert gateway.estornos == [("999", "estorno-automatico-999")]
        assert "pagamento_estornado_automaticamente" in caplog.messages

    def test_estorno_automatico_recusado_fica_registrado_no_log(
        self,
        banco: Banco,
        gateway: GatewayRoteirizado,
        relogio: RelogioFixo,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        dto = aprovado(banco, gateway, relogio)
        gateway.respostas["999"] = self.situacao(dto.id, StatusPagamento.APROVADO)
        gateway.erro_no_estorno = GatewayPagamentoRecusouError("payment too old")
        with caplog.at_level(logging.ERROR):
            resultado = processar(banco, gateway, relogio).executar("999")
        assert resultado is not None
        assert resultado.status == "APROVADO"
        assert "estorno_automatico_recusado" in caplog.messages

    def test_estorno_automatico_com_provedor_fora_propaga_para_reenvio(
        self, banco: Banco, gateway: GatewayRoteirizado, relogio: RelogioFixo
    ) -> None:
        dto = aprovado(banco, gateway, relogio)
        gateway.respostas["999"] = self.situacao(dto.id, StatusPagamento.APROVADO)
        gateway.erro_no_estorno = GatewayPagamentoIndisponivelError()
        with pytest.raises(GatewayPagamentoIndisponivelError):
            processar(banco, gateway, relogio).executar("999")
        # A notificacao ficou registrada; o reenvio repete so o estorno.
        assert len(consultar(banco, dto.id).notificacoes) == 2

    def test_recusa_depois_de_aprovado_e_ignorada(
        self, banco: Banco, gateway: GatewayRoteirizado, relogio: RelogioFixo
    ) -> None:
        dto = aprovado(banco, gateway, relogio)
        gateway.respostas["999"] = self.situacao(
            dto.id, StatusPagamento.RECUSADO, bruto="rejected"
        )
        resultado = processar(banco, gateway, relogio).executar("999")
        assert resultado is not None
        assert resultado.status == "APROVADO"

    @pytest.mark.parametrize("externa", [None, "nao-e-uuid"])
    def test_referencia_que_nao_e_nossa_e_ignorada(
        self,
        banco: Banco,
        gateway: GatewayRoteirizado,
        relogio: RelogioFixo,
        externa: str | None,
    ) -> None:
        gateway.respostas["999"] = self.situacao(externa, StatusPagamento.APROVADO)
        assert processar(banco, gateway, relogio).executar("999") is None
        assert processar(banco, gateway, relogio).executar("sim-nada") is None

    def test_pagamento_inexistente_e_ignorado(
        self, banco: Banco, gateway: GatewayRoteirizado, relogio: RelogioFixo
    ) -> None:
        gateway.respostas["999"] = self.situacao(uuid4(), StatusPagamento.APROVADO)
        assert processar(banco, gateway, relogio).executar("999") is None
        assert eventos_do_outbox(banco) == []


class TestSimulador:
    def test_aprovar_e_recusar_pelo_simulador(
        self, banco: Banco, gateway: GatewayRoteirizado, relogio: RelogioFixo
    ) -> None:
        pago = aprovado(banco, gateway, relogio)
        assert pago.status == "APROVADO"
        assert pago.referencia_pagamento is not None
        assert pago.referencia_pagamento.startswith("sim-")

        outro = solicitado(banco, gateway, relogio)
        recusado = pagar(banco, gateway, relogio, outro, aprovar=False)
        assert recusado.status == "RECUSADO"

    def test_pagamento_ja_processado(
        self, banco: Banco, gateway: GatewayRoteirizado, relogio: RelogioFixo
    ) -> None:
        pago = aprovado(banco, gateway, relogio)
        with pytest.raises(TransicaoStatusInvalidaError, match="APROVADO"):
            pagar(banco, gateway, relogio, pago, aprovar=False)

    def test_checkout_exige_o_token_do_pagamento(
        self, banco: Banco, gateway: GatewayRoteirizado, relogio: RelogioFixo
    ) -> None:
        dto = solicitado(banco, gateway, relogio)
        outro = solicitado(banco, gateway, relogio)
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
    def test_expira_so_pendentes_vencidos(
        self, banco: Banco, gateway: GatewayRoteirizado, relogio: RelogioFixo
    ) -> None:
        pendente = solicitado(banco, gateway, relogio)
        pago = aprovado(banco, gateway, relogio)
        relogio.avancar(minutes=60, seconds=1)
        uow = MongoUnitOfWork(banco)
        expirar = ExpirarPagamentosVencidos(uow, MongoPagamentoRepository(uow), relogio)

        assert expirar.executar() == 1
        assert expirar.executar() == 0

        assert consultar(banco, pendente.id).status == "EXPIRADO"
        assert consultar(banco, pago.id).status == "APROVADO"
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
                "status": "PENDENTE",
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
        assert "expiracao_falhou" in caplog.messages


class TestEstornarPagamento:
    def test_estorno_com_chave_de_idempotencia(
        self, banco: Banco, gateway: GatewayRoteirizado, relogio: RelogioFixo
    ) -> None:
        pago = aprovado(banco, gateway, relogio)
        relogio.avancar(minutes=10)

        estornado = estornar(banco, gateway, relogio).executar(
            ordem_id=pago.ordem_id,
            pagamento_id=pago.id,
            motivo="Falha na reserva de pecas",
            chave_idempotencia="msg-estornar-1",
        )

        assert estornado.status == "ESTORNADO"
        assert estornado.motivo == "Falha na reserva de pecas"
        assert gateway.estornos == [(pago.referencia_pagamento, "msg-estornar-1")]
        [envelope] = eventos_do_outbox(banco, "PagamentoEstornado")
        assert envelope["dados"] == {
            "ordem_id": str(pago.ordem_id),
            "pagamento_id": str(pago.id),
            "estornado_em": "2026-10-06T12:10:00.000Z",
        }
        documento = banco["pagamentos"].find_one({"_id": pago.id})
        assert documento is not None
        assert documento["chave_estorno"] == "msg-estornar-1"

    def test_repetir_o_estorno_nao_chama_o_provedor_de_novo(
        self, banco: Banco, gateway: GatewayRoteirizado, relogio: RelogioFixo
    ) -> None:
        pago = aprovado(banco, gateway, relogio)
        caso = estornar(banco, gateway, relogio)
        for chave in ("msg-1", "msg-2"):
            caso.executar(
                ordem_id=pago.ordem_id,
                pagamento_id=pago.id,
                motivo="x",
                chave_idempotencia=chave,
            )
        assert len(gateway.estornos) == 1
        assert len(eventos_do_outbox(banco, "PagamentoEstornado")) == 1

    def test_cobranca_pendente_e_encerrada_sem_chamar_o_provedor(
        self, banco: Banco, gateway: GatewayRoteirizado, relogio: RelogioFixo
    ) -> None:
        pendente = solicitado(banco, gateway, relogio)
        resultado = estornar(banco, gateway, relogio).executar(
            ordem_id=pendente.ordem_id,
            pagamento_id=pendente.id,
            motivo="OS cancelada",
            chave_idempotencia="msg",
        )
        assert resultado.status == "ESTORNADO"
        assert resultado.motivo == "Cobranca encerrada antes do pagamento: OS cancelada"
        assert gateway.estornos == []
        [envelope] = eventos_do_outbox(banco, "PagamentoEstornado")
        assert envelope["dados"]["pagamento_id"] == str(pendente.id)

        # O cliente paga depois: o dinheiro volta na hora.
        referencia = gateway.registrar_resultado(
            pagamento_id=pendente.id, valor=dinheiro("335.00"), aprovado=True
        )
        final = processar(banco, gateway, relogio).executar(referencia)
        assert final is not None
        assert final.status == "ESTORNADO"
        assert gateway.estornos == [(referencia, f"estorno-automatico-{referencia}")]

    def test_pagamento_recusado_gera_falha_de_estorno(
        self, banco: Banco, gateway: GatewayRoteirizado, relogio: RelogioFixo
    ) -> None:
        pendente = solicitado(banco, gateway, relogio)
        pagar(banco, gateway, relogio, pendente, aprovar=False)
        resultado = estornar(banco, gateway, relogio).executar(
            ordem_id=pendente.ordem_id,
            pagamento_id=pendente.id,
            motivo="x",
            chave_idempotencia="msg",
        )
        assert resultado.status == "RECUSADO"
        assert gateway.estornos == []
        [envelope] = eventos_do_outbox(banco, "EstornoDePagamentoFalhou")
        assert (
            envelope["dados"]["motivo"] == "Pagamento RECUSADO nao pode ser estornado"
        )

    def test_estorno_em_processamento_mas_ja_estornado_na_consulta_conclui(
        self, banco: Banco, gateway: GatewayRoteirizado, relogio: RelogioFixo
    ) -> None:
        pago = aprovado(banco, gateway, relogio)
        assert pago.referencia_pagamento is not None
        # O provedor ja mostra o pagamento estornado (tentativa anterior).
        gateway.estornar(pago.referencia_pagamento, chave_idempotencia="msg-1")
        gateway.erro_no_estorno = EstornoEmProcessamentoError()
        resultado = estornar(banco, gateway, relogio).executar(
            ordem_id=pago.ordem_id,
            pagamento_id=pago.id,
            motivo="x",
            chave_idempotencia="msg-1",
        )
        assert resultado.status == "ESTORNADO"
        assert len(eventos_do_outbox(banco, "PagamentoEstornado")) == 1

    def test_aprovacao_entre_a_leitura_e_o_encerramento_vira_estorno(
        self, banco: Banco, gateway: GatewayRoteirizado, relogio: RelogioFixo
    ) -> None:
        pendente = solicitado(banco, gateway, relogio)
        repo = MongoPagamentoRepository(MongoUnitOfWork(banco))
        antes = repo.obter_por_id(pendente.id)
        pago = pagar(banco, gateway, relogio, pendente, aprovar=True)

        class LeituraAntiga:
            """1a leitura devolve o pagamento ainda PENDENTE (leitura velha)."""

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
            uow, LeituraAntiga(MongoPagamentoRepository(uow)), gateway, relogio
        )
        resultado = caso.executar(
            ordem_id=pendente.ordem_id,
            pagamento_id=pendente.id,
            motivo="OS cancelada",
            chave_idempotencia="msg-9",
        )

        assert resultado.status == "ESTORNADO"
        # Releu APROVADO e estornou no provedor de verdade.
        assert gateway.estornos == [(pago.referencia_pagamento, "msg-9")]
        assert resultado.motivo == "OS cancelada"

    def test_estorno_em_processamento_no_provedor_e_repetido_depois(
        self, banco: Banco, gateway: GatewayRoteirizado, relogio: RelogioFixo
    ) -> None:
        pago = aprovado(banco, gateway, relogio)
        gateway.erro_no_estorno = EstornoEmProcessamentoError()
        with pytest.raises(EstornoEmProcessamentoError):
            estornar(banco, gateway, relogio).executar(
                ordem_id=pago.ordem_id,
                pagamento_id=pago.id,
                motivo="x",
                chave_idempotencia="msg-1",
            )
        assert consultar(banco, pago.id).status == "APROVADO"
        assert eventos_do_outbox(banco, "PagamentoEstornado") == []

    def test_recusa_do_provedor_gera_falha_de_estorno(
        self, banco: Banco, gateway: GatewayRoteirizado, relogio: RelogioFixo
    ) -> None:
        pago = aprovado(banco, gateway, relogio)
        gateway.erro_no_estorno = GatewayPagamentoRecusouError(
            "Mercado Pago respondeu 400: payment too old to refund"
        )
        resultado = estornar(banco, gateway, relogio).executar(
            ordem_id=pago.ordem_id,
            pagamento_id=pago.id,
            motivo="x",
            chave_idempotencia="msg",
        )
        assert resultado.status == "APROVADO"
        [envelope] = eventos_do_outbox(banco, "EstornoDePagamentoFalhou")
        assert envelope["dados"] == {
            "ordem_id": str(pago.ordem_id),
            "pagamento_id": str(pago.id),
            "motivo": "Mercado Pago respondeu 400: payment too old to refund",
        }

    def test_recusa_porque_ja_estornado_no_provedor_conclui_o_estorno(
        self, banco: Banco, gateway: GatewayRoteirizado, relogio: RelogioFixo
    ) -> None:
        # Tentativa anterior estornou no provedor e caiu antes de gravar aqui.
        pago = aprovado(banco, gateway, relogio)
        assert pago.referencia_pagamento is not None
        gateway.estornar(pago.referencia_pagamento, chave_idempotencia="msg-antiga")
        gateway.erro_no_estorno = GatewayPagamentoRecusouError("already refunded")

        resultado = estornar(banco, gateway, relogio).executar(
            ordem_id=pago.ordem_id,
            pagamento_id=pago.id,
            motivo="x",
            chave_idempotencia="msg-nova",
        )

        assert resultado.status == "ESTORNADO"
        assert eventos_do_outbox(banco, "EstornoDePagamentoFalhou") == []
        assert len(eventos_do_outbox(banco, "PagamentoEstornado")) == 1

    def test_falha_transitoria_propaga_sem_gravar(
        self, banco: Banco, gateway: GatewayRoteirizado, relogio: RelogioFixo
    ) -> None:
        pago = aprovado(banco, gateway, relogio)
        gateway.erro_no_estorno = GatewayPagamentoIndisponivelError()
        antes = len(eventos_do_outbox(banco))
        with pytest.raises(GatewayPagamentoIndisponivelError):
            estornar(banco, gateway, relogio).executar(
                ordem_id=pago.ordem_id,
                pagamento_id=pago.id,
                motivo="x",
                chave_idempotencia="msg",
            )
        assert consultar(banco, pago.id).status == "APROVADO"
        assert len(eventos_do_outbox(banco)) == antes

    def test_exige_a_ordem_dona_do_pagamento(
        self, banco: Banco, gateway: GatewayRoteirizado, relogio: RelogioFixo
    ) -> None:
        pago = aprovado(banco, gateway, relogio)
        with pytest.raises(PagamentoNaoEncontradoError, match="nao pertence"):
            estornar(banco, gateway, relogio).executar(
                ordem_id=uuid4(),
                pagamento_id=pago.id,
                motivo="x",
                chave_idempotencia="msg",
            )
