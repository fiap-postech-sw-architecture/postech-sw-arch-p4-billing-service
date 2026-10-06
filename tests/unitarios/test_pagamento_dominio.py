from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from src.compartilhado.dominio.exceptions import TransicaoStatusInvalidaError
from src.pagamento.dominio.events import (
    EstornoDePagamentoFalhouEvent,
    PagamentoConfirmadoEvent,
    PagamentoEstornadoEvent,
    PagamentoExpiradoEvent,
    PagamentoRecusadoEvent,
    PagamentoSolicitadoEvent,
)
from src.pagamento.dominio.pagamento import (
    ESTORNO_AUTOMATICO,
    MOTIVO_PRAZO_ESGOTADO,
    NotificacaoRecebida,
    Pagamento,
    ResultadoAprovacao,
    StatusPagamento,
)
from tests.factories import AGORA, confirmar, dinheiro, pagamento

DENTRO_DO_PRAZO = AGORA + timedelta(minutes=10)
DEPOIS_DO_PRAZO = AGORA + timedelta(minutes=61)


def aprovado() -> Pagamento:
    p = pagamento()
    confirmar(p, referencia="123", agora=DENTRO_DO_PRAZO)
    p.limpar_eventos()
    return p


class TestSolicitacao:
    def test_solicitar_registra_evento_com_checkout(self) -> None:
        ordem_id = uuid4()
        solicitado = pagamento(ordem_id=ordem_id)

        assert solicitado.status is StatusPagamento.PENDENTE
        assert solicitado.coletar_eventos() == [
            PagamentoSolicitadoEvent(
                ordem_id=ordem_id,
                pagamento_id=solicitado.id,
                valor=Decimal("335.00"),
                moeda="BRL",
                checkout_url=solicitado.checkout_url,
                expira_em=AGORA + timedelta(minutes=60),
            )
        ]
        assert solicitado.provedor == "simulado"
        assert solicitado.referencia_preferencia.startswith("sim-pref-")

    def test_valor_zero_e_invalido(self) -> None:
        with pytest.raises(ValueError, match="maior que zero"):
            pagamento(valor="0.00")

    def test_dados_do_provedor_sao_obrigatorios(self) -> None:
        with pytest.raises(ValueError, match="checkout_url"):
            Pagamento(
                _ordem_id=uuid4(),
                _orcamento_id=uuid4(),
                _valor=dinheiro("1.00"),
                _provedor="simulado",
                _referencia_preferencia="",
                _checkout_url="http://x",
                _criado_em=AGORA,
                _expira_em=AGORA + timedelta(minutes=1),
            )

    def test_datas_com_timezone_e_ordenadas(self) -> None:
        with pytest.raises(ValueError, match="timezone"):
            pagamento(criado_em=datetime(2026, 10, 6, 12, 0))
        with pytest.raises(ValueError, match="posterior"):
            pagamento(validade=timedelta(0))


class TestAprovacao:
    def test_aprovacao_pelo_valor_exato_confirma(self) -> None:
        p = pagamento()
        p.limpar_eventos()

        resultado = p.aplicar_aprovacao(
            referencia_pagamento="123",
            valor_cobrado=dinheiro("335.00"),
            agora=DENTRO_DO_PRAZO,
        )

        assert resultado is ResultadoAprovacao.CONFIRMADO
        assert (p.status, p.referencia_pagamento, p.confirmado_em) == (
            StatusPagamento.APROVADO,
            "123",
            DENTRO_DO_PRAZO,
        )
        assert p.coletar_eventos() == [
            PagamentoConfirmadoEvent(
                ordem_id=p.ordem_id,
                pagamento_id=p.id,
                valor=Decimal("335.00"),
                moeda="BRL",
                confirmado_em=DENTRO_DO_PRAZO,
                referencia_provedor="123",
            )
        ]

    def test_mesma_aprovacao_de_novo_e_repetida(self) -> None:
        p = aprovado()
        resultado = p.aplicar_aprovacao(
            referencia_pagamento="123",
            valor_cobrado=dinheiro("335.00"),
            agora=DEPOIS_DO_PRAZO,
        )
        assert resultado is ResultadoAprovacao.REPETIDO
        assert p.confirmado_em == DENTRO_DO_PRAZO
        assert p.coletar_eventos() == []

    def test_aprovacao_atrasada_do_pagamento_ja_estornado_e_repetida(self) -> None:
        # Logo depois do estorno o provedor ainda pode responder "approved":
        # o mesmo pagamento nao pode ser estornado de novo.
        p = aprovado()
        p.estornar(estornado_em=AGORA, chave_idempotencia="msg", motivo="saga")
        resultado = p.aplicar_aprovacao(
            referencia_pagamento="123",
            valor_cobrado=dinheiro("335.00"),
            agora=DEPOIS_DO_PRAZO,
        )
        assert resultado is ResultadoAprovacao.REPETIDO
        assert p.status is StatusPagamento.ESTORNADO

    @pytest.mark.parametrize("valor", ["334.99", "335.01", None])
    def test_valor_diferente_nao_confirma(self, valor: str | None) -> None:
        p = pagamento()
        p.limpar_eventos()
        resultado = p.aplicar_aprovacao(
            referencia_pagamento="123",
            valor_cobrado=dinheiro(valor) if valor else None,
            agora=DENTRO_DO_PRAZO,
        )
        assert resultado is ResultadoAprovacao.VALOR_DIVERGENTE
        assert p.status is StatusPagamento.PENDENTE
        assert p.coletar_eventos() == []

    def test_aprovacao_de_cobranca_encerrada_nao_muda_nada(self) -> None:
        pago_duas_vezes = aprovado()
        expirado = pagamento()
        expirado.expirar(agora=DEPOIS_DO_PRAZO)
        encerrado = pagamento()
        encerrado.encerrar_cobranca(encerrado_em=AGORA, motivo="saga")
        for p, status in (
            (pago_duas_vezes, StatusPagamento.APROVADO),
            (expirado, StatusPagamento.EXPIRADO),
            (encerrado, StatusPagamento.ESTORNADO),
        ):
            p.limpar_eventos()
            resultado = p.aplicar_aprovacao(
                referencia_pagamento="outra-transacao",
                valor_cobrado=dinheiro("335.00"),
                agora=DEPOIS_DO_PRAZO,
            )
            assert resultado is ResultadoAprovacao.ENCERRADO
            assert p.status is status
            assert p.coletar_eventos() == []
        assert {
            ResultadoAprovacao.ENCERRADO,
            ResultadoAprovacao.VALOR_DIVERGENTE,
        } == ESTORNO_AUTOMATICO

    def test_aprovacao_depois_do_prazo_ainda_vale_se_pendente(self) -> None:
        # O dinheiro entrou: a aprovacao do provedor vence o nosso prazo.
        p = pagamento()
        resultado = p.aplicar_aprovacao(
            referencia_pagamento="1",
            valor_cobrado=dinheiro("335.00"),
            agora=DEPOIS_DO_PRAZO,
        )
        assert resultado is ResultadoAprovacao.CONFIRMADO


class TestRecusaEExpiracao:
    def test_recusar_registra_motivo_e_evento(self) -> None:
        p = pagamento()
        p.limpar_eventos()
        assert p.recusar(motivo="cc_rejected_other_reason")
        assert (p.status, p.motivo) == (
            StatusPagamento.RECUSADO,
            "cc_rejected_other_reason",
        )
        assert p.coletar_eventos() == [
            PagamentoRecusadoEvent(
                ordem_id=p.ordem_id,
                pagamento_id=p.id,
                motivo="cc_rejected_other_reason",
            )
        ]
        assert p.recusar(motivo="de novo") is False

    def test_aprovado_nao_pode_ser_recusado(self) -> None:
        with pytest.raises(TransicaoStatusInvalidaError):
            aprovado().recusar(motivo="x")

    def test_expirar_so_pendente_e_vencido(self) -> None:
        p = pagamento()
        p.limpar_eventos()
        assert not p.vencido(AGORA + timedelta(minutes=60))
        with pytest.raises(TransicaoStatusInvalidaError, match="prazo"):
            p.expirar(agora=DENTRO_DO_PRAZO)

        p.expirar(agora=DEPOIS_DO_PRAZO)

        assert (p.status, p.motivo) == (StatusPagamento.EXPIRADO, MOTIVO_PRAZO_ESGOTADO)
        assert p.coletar_eventos() == [
            PagamentoExpiradoEvent(
                ordem_id=p.ordem_id, pagamento_id=p.id, motivo=MOTIVO_PRAZO_ESGOTADO
            )
        ]
        assert not aprovado().vencido(DEPOIS_DO_PRAZO)


class TestEstorno:
    def test_estornar_aprovado_registra_chave_e_evento(self) -> None:
        p = aprovado()
        assert p.estornar(
            estornado_em=DEPOIS_DO_PRAZO, chave_idempotencia="msg-1", motivo="Saga"
        )
        assert (p.status, p.estornado_em, p.chave_estorno, p.motivo) == (
            StatusPagamento.ESTORNADO,
            DEPOIS_DO_PRAZO,
            "msg-1",
            "Saga",
        )
        assert p.coletar_eventos() == [
            PagamentoEstornadoEvent(
                ordem_id=p.ordem_id, pagamento_id=p.id, estornado_em=DEPOIS_DO_PRAZO
            )
        ]

    def test_estornar_de_novo_nao_faz_nada(self) -> None:
        p = aprovado()
        p.estornar(estornado_em=AGORA, chave_idempotencia="msg-1", motivo="Saga")
        p.limpar_eventos()
        assert (
            p.estornar(estornado_em=AGORA, chave_idempotencia="msg-2", motivo="x")
            is False
        )
        assert p.chave_estorno == "msg-1"
        assert p.coletar_eventos() == []

    def test_pendente_nao_tem_valor_a_estornar(self) -> None:
        with pytest.raises(TransicaoStatusInvalidaError, match="PENDENTE"):
            pagamento().estornar(
                estornado_em=AGORA, chave_idempotencia="msg", motivo="x"
            )

    def test_encerrar_cobranca_pendente_responde_estornado(self) -> None:
        p = pagamento()
        p.limpar_eventos()

        p.encerrar_cobranca(encerrado_em=DENTRO_DO_PRAZO, motivo="OS cancelada")

        assert (p.status, p.estornado_em, p.motivo, p.chave_estorno) == (
            StatusPagamento.ESTORNADO,
            DENTRO_DO_PRAZO,
            "OS cancelada",
            None,
        )
        assert p.coletar_eventos() == [
            PagamentoEstornadoEvent(
                ordem_id=p.ordem_id, pagamento_id=p.id, estornado_em=DENTRO_DO_PRAZO
            )
        ]

    def test_so_cobranca_pendente_e_encerrada(self) -> None:
        with pytest.raises(TransicaoStatusInvalidaError, match="APROVADO"):
            aprovado().encerrar_cobranca(encerrado_em=AGORA, motivo="x")

    def test_falha_de_estorno_registra_evento_sem_mudar_status(self) -> None:
        p = aprovado()
        p.registrar_falha_de_estorno("payment too old to refund")
        assert p.status is StatusPagamento.APROVADO
        assert p.coletar_eventos() == [
            EstornoDePagamentoFalhouEvent(
                ordem_id=p.ordem_id,
                pagamento_id=p.id,
                motivo="payment too old to refund",
            )
        ]


def test_historico_guarda_cada_mudanca_uma_vez() -> None:
    p = pagamento()
    pendente = NotificacaoRecebida(
        recebida_em=AGORA, referencia_pagamento="1", status_provedor="pending"
    )
    reenviada = NotificacaoRecebida(
        recebida_em=DENTRO_DO_PRAZO, referencia_pagamento="1", status_provedor="pending"
    )
    aprovada = NotificacaoRecebida(
        recebida_em=DENTRO_DO_PRAZO,
        referencia_pagamento="1",
        status_provedor="approved",
    )
    assert p.registrar_notificacao(pendente) is True
    assert p.registrar_notificacao(reenviada) is False
    assert p.registrar_notificacao(aprovada) is True
    assert p.notificacoes == (pendente, aprovada)
