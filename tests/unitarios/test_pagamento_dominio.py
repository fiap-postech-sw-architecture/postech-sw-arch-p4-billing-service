from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from src.compartilhado.dominio.dinheiro import Dinheiro
from src.compartilhado.dominio.exceptions import TransicaoStatusInvalidaError
from src.pagamento.dominio.cobranca import (
    Cobranca,
    EstornoAutomatico,
    NotificacaoRecebida,
    SituacaoNoProvedor,
)
from src.pagamento.dominio.estados import (
    MotivoEstorno,
    PlanoDeCompensacao,
    ResultadoNotificacao,
    StatusNoProvedor,
    StatusPagamento,
)
from src.pagamento.dominio.events import (
    EstornoDePagamentoFalhouEvent,
    PagamentoCanceladoEvent,
    PagamentoConfirmadoEvent,
    PagamentoEstornadoEvent,
    PagamentoExpiradoEvent,
    PagamentoRecusadoEvent,
    PagamentoSolicitadoEvent,
)
from src.pagamento.dominio.pagamento import (
    MOTIVO_PRAZO_ESGOTADO,
    TAMANHO_MAXIMO_MOTIVO,
    Pagamento,
)
from tests.factories import AGORA, confirmar, dinheiro, pagamento, situacao

if TYPE_CHECKING:
    from collections.abc import Callable

DENTRO_DO_PRAZO = AGORA + timedelta(minutes=10)
DEPOIS_DO_PRAZO = AGORA + timedelta(minutes=61)


def aplicar(
    p: Pagamento,
    status: StatusNoProvedor,
    *,
    referencia: str = "1",
    max_recusas: int = 3,
    agora: datetime = DENTRO_DO_PRAZO,
    valor: Dinheiro | None = None,
    detalhe: str | None = None,
    bruto: str | None = None,
) -> ResultadoNotificacao:
    tentativa = situacao(
        p,
        referencia=referencia,
        status=status,
        valor=valor,
        detalhe=detalhe,
        bruto=bruto,
    )
    return p.aplicar_notificacao(tentativa, agora=agora, max_recusas=max_recusas)


def solicitado() -> Pagamento:
    p = pagamento()
    p.limpar_eventos()
    return p


def confirmado() -> Pagamento:
    p = solicitado()
    confirmar(p, referencia="123", agora=DENTRO_DO_PRAZO)
    p.limpar_eventos()
    return p


def recusado() -> Pagamento:
    p = solicitado()
    aplicar(p, StatusNoProvedor.RECUSADO, max_recusas=1, detalhe="cc_rejected")
    p.limpar_eventos()
    return p


def expirado() -> Pagamento:
    p = solicitado()
    p.expirar(agora=DEPOIS_DO_PRAZO)
    p.limpar_eventos()
    return p


def cancelado() -> Pagamento:
    p = solicitado()
    p.concluir_compensacao(agora=DENTRO_DO_PRAZO, motivo="cancelamento")
    p.limpar_eventos()
    return p


def estornado() -> Pagamento:
    p = confirmado()
    p.concluir_compensacao(agora=DEPOIS_DO_PRAZO, motivo="cancelamento")
    p.limpar_eventos()
    return p


ESTADOS: dict[StatusPagamento, Callable[[], Pagamento]] = {
    StatusPagamento.SOLICITADO: solicitado,
    StatusPagamento.CONFIRMADO: confirmado,
    StatusPagamento.RECUSADO: recusado,
    StatusPagamento.EXPIRADO: expirado,
    StatusPagamento.CANCELADO: cancelado,
    StatusPagamento.ESTORNADO: estornado,
}


class TestSolicitacao:
    def test_solicitar_registra_evento_com_checkout(self) -> None:
        ordem_id = uuid4()
        p = pagamento(ordem_id=ordem_id)
        assert p.cobranca is not None
        assert p.status is StatusPagamento.SOLICITADO
        assert p.coletar_eventos() == [
            PagamentoSolicitadoEvent(
                ordem_id=ordem_id,
                pagamento_id=p.id,
                valor=Decimal("335.00"),
                moeda="BRL",
                checkout_url=p.cobranca.checkout_url,
                expira_em=AGORA + timedelta(minutes=60),
            )
        ]

    def test_valor_zero_e_invalido(self) -> None:
        with pytest.raises(ValueError, match="maior que zero"):
            pagamento(valor="0.00")

    @pytest.mark.parametrize(
        "campo", ["provedor", "referencia_preferencia", "checkout_url"]
    )
    def test_dados_do_provedor_sao_obrigatorios(self, campo: str) -> None:
        dados: dict[str, object] = {
            "orcamento_id": uuid4(),
            "valor": dinheiro("1.00"),
            "provedor": "simulado",
            "referencia_preferencia": "pref",
            "checkout_url": "http://x",
            "expira_em": AGORA,
            campo: " ",
        }
        with pytest.raises(ValueError, match="vazio"):
            Cobranca(**dados)  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        ("criado_em", "validade"),
        [
            pytest.param(datetime(2026, 10, 6, 12, 0), None, id="criado-em-sem-tz"),
            pytest.param(AGORA, "naive", id="expira-em-sem-tz"),
        ],
    )
    def test_datas_com_timezone(
        self, criado_em: datetime, validade: str | None
    ) -> None:
        with pytest.raises(ValueError, match="timezone"):
            if validade is None:
                pagamento(criado_em=criado_em)
            else:
                Cobranca(
                    orcamento_id=uuid4(),
                    valor=dinheiro("1.00"),
                    provedor="simulado",
                    referencia_preferencia="pref",
                    checkout_url="http://x",
                    expira_em=datetime(2026, 10, 6, 13, 0),
                )

    def test_expiracao_deve_ser_posterior_a_criacao(self) -> None:
        with pytest.raises(ValueError, match="posterior"):
            pagamento(validade=timedelta(0))


class TestLapide:
    def test_lapide_nasce_cancelada_sem_cobranca_e_responde(self) -> None:
        ordem_id = uuid4()
        tumulo = Pagamento.lapide(
            id=uuid4(), ordem_id=ordem_id, cancelado_em=AGORA, motivo="cancelamento"
        )
        assert (tumulo.status, tumulo.cobranca, tumulo.motivo) == (
            StatusPagamento.CANCELADO,
            None,
            "cancelamento",
        )
        assert tumulo.coletar_eventos() == [
            PagamentoCanceladoEvent(
                ordem_id=ordem_id, pagamento_id=tumulo.id, cancelado_em=AGORA
            )
        ]
        assert tumulo.compensar() is PlanoDeCompensacao.RESPONDER_CANCELADO
        assert not tumulo.vencido(DEPOIS_DO_PRAZO)

    def test_sem_cobranca_so_como_lapide_cancelada(self) -> None:
        with pytest.raises(ValueError, match="lapide"):
            Pagamento(_ordem_id=uuid4(), _criado_em=AGORA)


class TestAprovacao:
    def test_aprovacao_pelo_valor_exato_confirma(self) -> None:
        p = solicitado()
        resultado = aplicar(p, StatusNoProvedor.APROVADO, referencia="123")
        assert resultado is ResultadoNotificacao.CONFIRMADO
        assert (p.status, p.referencia_pagamento, p.confirmado_em) == (
            StatusPagamento.CONFIRMADO,
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

    def test_mesma_aprovacao_de_novo_nao_muda_nada(self) -> None:
        p = confirmado()
        resultado = aplicar(p, StatusNoProvedor.APROVADO, referencia="123")
        assert resultado is ResultadoNotificacao.SEM_MUDANCA
        assert p.confirmado_em == DENTRO_DO_PRAZO
        assert p.coletar_eventos() == []

    def test_aprovacao_do_pagamento_ja_estornado_nao_estorna_de_novo(self) -> None:
        # Logo depois do estorno o provedor ainda pode responder "approved".
        p = estornado()
        resultado = aplicar(p, StatusNoProvedor.APROVADO, referencia="123")
        assert resultado is ResultadoNotificacao.SEM_MUDANCA
        assert p.status is StatusPagamento.ESTORNADO

    @pytest.mark.parametrize(
        "valor",
        [
            pytest.param(dinheiro("334.99"), id="centavo-a-menos"),
            pytest.param(dinheiro("335.01"), id="centavo-a-mais"),
            pytest.param(Dinheiro(Decimal("335.00"), moeda="USD"), id="outra-moeda"),
        ],
    )
    def test_valor_ou_moeda_diferentes_pedem_estorno(self, valor: Dinheiro) -> None:
        p = solicitado()
        resultado = aplicar(p, StatusNoProvedor.APROVADO, valor=valor)
        assert resultado is ResultadoNotificacao.ESTORNO_AUTOMATICO
        assert p.status is StatusPagamento.SOLICITADO
        assert p.coletar_eventos() == []

    def test_aprovacao_sem_valor_pede_estorno(self) -> None:
        p = solicitado()
        sem_valor = SituacaoNoProvedor(
            referencia="1",
            referencia_externa=str(p.id),
            status=StatusNoProvedor.APROVADO,
            status_provedor="approved",
            detalhe=None,
            valor=None,
        )
        resultado = p.aplicar_notificacao(sem_valor, agora=AGORA, max_recusas=3)
        assert resultado is ResultadoNotificacao.ESTORNO_AUTOMATICO

    @pytest.mark.parametrize(
        "estado",
        [
            pytest.param(StatusPagamento.CONFIRMADO, id="pago-por-outra-tentativa"),
            pytest.param(StatusPagamento.RECUSADO, id="recusado"),
            pytest.param(StatusPagamento.EXPIRADO, id="expirado"),
            pytest.param(StatusPagamento.CANCELADO, id="cancelado"),
            pytest.param(StatusPagamento.ESTORNADO, id="estornado"),
        ],
    )
    def test_aprovacao_de_cobranca_encerrada_pede_estorno(
        self, estado: StatusPagamento
    ) -> None:
        p = ESTADOS[estado]()
        resultado = aplicar(p, StatusNoProvedor.APROVADO, referencia="outra-tentativa")
        assert resultado is ResultadoNotificacao.ESTORNO_AUTOMATICO
        assert p.status is estado
        assert p.coletar_eventos() == []

    def test_aprovacao_depois_do_prazo_ainda_vale_se_solicitado(self) -> None:
        # O dinheiro entrou: a aprovacao do provedor vence o nosso prazo.
        p = solicitado()
        resultado = aplicar(p, StatusNoProvedor.APROVADO, agora=DEPOIS_DO_PRAZO)
        assert resultado is ResultadoNotificacao.CONFIRMADO


class TestRecusas:
    def test_recusas_contam_ate_o_maximo(self) -> None:
        p = solicitado()
        for referencia in ("r1", "r2"):
            resultado = aplicar(p, StatusNoProvedor.RECUSADO, referencia=referencia)
            assert resultado is ResultadoNotificacao.RECUSA_CONTADA
        assert (p.status, p.recusas) == (StatusPagamento.SOLICITADO, 2)
        assert p.coletar_eventos() == []

        resultado = aplicar(
            p, StatusNoProvedor.RECUSADO, referencia="r3", detalhe="cc_rejected_other"
        )

        assert resultado is ResultadoNotificacao.RECUSADO
        assert (p.status, p.recusas, p.motivo, p.encerrado_em) == (
            StatusPagamento.RECUSADO,
            3,
            "cc_rejected_other",
            DENTRO_DO_PRAZO,
        )
        assert p.coletar_eventos() == [
            PagamentoRecusadoEvent(
                ordem_id=p.ordem_id, pagamento_id=p.id, motivo="cc_rejected_other"
            )
        ]

    def test_a_mesma_tentativa_recusada_conta_uma_vez(self) -> None:
        p = solicitado()
        aplicar(p, StatusNoProvedor.RECUSADO, referencia="r1")
        assert (
            aplicar(p, StatusNoProvedor.RECUSADO, referencia="r1")
            is ResultadoNotificacao.SEM_MUDANCA
        )
        assert p.recusas == 1

    def test_maximo_de_uma_recusa(self) -> None:
        p = solicitado()
        resultado = aplicar(p, StatusNoProvedor.RECUSADO, max_recusas=1)
        assert resultado is ResultadoNotificacao.RECUSADO
        assert p.motivo == "rejected"

    def test_recusa_depois_de_confirmado_so_entra_no_historico(self) -> None:
        p = confirmado()
        resultado = aplicar(p, StatusNoProvedor.RECUSADO, referencia="r9")
        assert resultado is ResultadoNotificacao.REGISTRADA
        assert (p.status, p.recusas) == (StatusPagamento.CONFIRMADO, 0)

    def test_motivo_longo_do_provedor_e_cortado(self) -> None:
        p = solicitado()
        aplicar(p, StatusNoProvedor.RECUSADO, max_recusas=1, detalhe="x" * 600)
        assert p.motivo == "x" * TAMANHO_MAXIMO_MOTIVO


class TestOutrasSituacoes:
    @pytest.mark.parametrize(
        "status",
        [StatusNoProvedor.EM_ANDAMENTO, StatusNoProvedor.ESTORNADO],
        ids=["em-andamento", "estornado-no-provedor"],
    )
    def test_so_entram_no_historico_uma_vez(self, status: StatusNoProvedor) -> None:
        p = solicitado()
        assert aplicar(p, status) is ResultadoNotificacao.REGISTRADA
        assert aplicar(p, status) is ResultadoNotificacao.SEM_MUDANCA
        assert p.status is StatusPagamento.SOLICITADO
        assert len(p.notificacoes) == 1


class TestEstornoAutomatico:
    @pytest.mark.parametrize(
        "estado",
        [StatusPagamento.RECUSADO, StatusPagamento.EXPIRADO, StatusPagamento.CANCELADO],
        ids=["recusado", "expirado", "cancelado"],
    )
    def test_cobranca_encerrada_termina_estornada(
        self, estado: StatusPagamento
    ) -> None:
        p = ESTADOS[estado]()
        assert p.registrar_estorno_automatico("tardia", agora=DEPOIS_DO_PRAZO)
        assert (p.status, p.estornado_em, p.motivo_estorno) == (
            StatusPagamento.ESTORNADO,
            DEPOIS_DO_PRAZO,
            MotivoEstorno.PAGAMENTO_APOS_ENCERRAMENTO,
        )
        assert p.estornos_automaticos == (
            EstornoAutomatico(
                referencia_pagamento="tardia", registrado_em=DEPOIS_DO_PRAZO
            ),
        )
        assert p.coletar_eventos() == [
            PagamentoEstornadoEvent(
                ordem_id=p.ordem_id,
                pagamento_id=p.id,
                estornado_em=DEPOIS_DO_PRAZO,
                motivo=MotivoEstorno.PAGAMENTO_APOS_ENCERRAMENTO,
            )
        ]
        # A mesma tentativa nao e estornada de novo.
        assert not p.registrar_estorno_automatico("tardia", agora=DEPOIS_DO_PRAZO)
        assert (
            aplicar(p, StatusNoProvedor.APROVADO, referencia="tardia")
            is ResultadoNotificacao.REGISTRADA
        )

    @pytest.mark.parametrize(
        "estado",
        [StatusPagamento.SOLICITADO, StatusPagamento.CONFIRMADO],
        ids=["valor-divergente", "pagamento-em-dobro"],
    )
    def test_cobranca_aberta_ou_paga_nao_muda_de_status(
        self, estado: StatusPagamento
    ) -> None:
        p = ESTADOS[estado]()
        p.registrar_estorno_automatico("outra", agora=DENTRO_DO_PRAZO)
        assert p.status is estado
        [evento] = p.coletar_eventos()
        assert isinstance(evento, PagamentoEstornadoEvent)
        assert evento.motivo is MotivoEstorno.PAGAMENTO_APOS_ENCERRAMENTO

    def test_recusa_do_provedor_fica_marcada_sem_evento(self) -> None:
        p = cancelado()
        p.registrar_estorno_automatico(
            "tardia", agora=DEPOIS_DO_PRAZO, falha="payment too old"
        )
        assert p.status is StatusPagamento.CANCELADO
        assert p.estornos_automaticos[0].falha == "payment too old"
        assert p.coletar_eventos() == []


class TestCompensacao:
    @pytest.mark.parametrize(
        ("estado", "plano"),
        [
            (StatusPagamento.SOLICITADO, PlanoDeCompensacao.CANCELAR_COBRANCA),
            (StatusPagamento.CONFIRMADO, PlanoDeCompensacao.ESTORNAR_NO_PROVEDOR),
            (StatusPagamento.RECUSADO, PlanoDeCompensacao.RESPONDER_CANCELADO),
            (StatusPagamento.EXPIRADO, PlanoDeCompensacao.RESPONDER_CANCELADO),
            (StatusPagamento.CANCELADO, PlanoDeCompensacao.RESPONDER_CANCELADO),
            (StatusPagamento.ESTORNADO, PlanoDeCompensacao.RESPONDER_ESTORNADO),
        ],
        ids=lambda valor: str(valor).lower(),
    )
    def test_plano_por_estado(
        self, estado: StatusPagamento, plano: PlanoDeCompensacao
    ) -> None:
        p = ESTADOS[estado]()
        assert p.compensar() is plano
        assert p.status is estado

    def test_solicitado_vira_cancelado(self) -> None:
        p = solicitado()
        p.concluir_compensacao(agora=DENTRO_DO_PRAZO, motivo="cancelamento")
        assert (p.status, p.encerrado_em, p.motivo) == (
            StatusPagamento.CANCELADO,
            DENTRO_DO_PRAZO,
            "cancelamento",
        )
        assert p.coletar_eventos() == [
            PagamentoCanceladoEvent(
                ordem_id=p.ordem_id, pagamento_id=p.id, cancelado_em=DENTRO_DO_PRAZO
            )
        ]

    def test_confirmado_vira_estornado(self) -> None:
        p = confirmado()
        p.concluir_compensacao(agora=DEPOIS_DO_PRAZO, motivo="reserva_falhou")
        assert (p.status, p.estornado_em, p.motivo_estorno, p.motivo) == (
            StatusPagamento.ESTORNADO,
            DEPOIS_DO_PRAZO,
            MotivoEstorno.COMPENSACAO,
            "reserva_falhou",
        )
        assert p.coletar_eventos() == [
            PagamentoEstornadoEvent(
                ordem_id=p.ordem_id,
                pagamento_id=p.id,
                estornado_em=DEPOIS_DO_PRAZO,
                motivo=MotivoEstorno.COMPENSACAO,
            )
        ]

    @pytest.mark.parametrize(
        "estado",
        [
            StatusPagamento.RECUSADO,
            StatusPagamento.EXPIRADO,
            StatusPagamento.CANCELADO,
            StatusPagamento.ESTORNADO,
        ],
        ids=lambda valor: str(valor).lower(),
    )
    def test_encerrado_nao_conclui_compensacao_de_novo(
        self, estado: StatusPagamento
    ) -> None:
        p = ESTADOS[estado]()
        with pytest.raises(TransicaoStatusInvalidaError):
            p.concluir_compensacao(agora=DEPOIS_DO_PRAZO, motivo="x")
        assert p.status is estado
        assert p.coletar_eventos() == []

    @pytest.mark.parametrize(
        ("estado", "evento"),
        [
            (StatusPagamento.RECUSADO, PagamentoCanceladoEvent),
            (StatusPagamento.EXPIRADO, PagamentoCanceladoEvent),
            (StatusPagamento.CANCELADO, PagamentoCanceladoEvent),
            (StatusPagamento.ESTORNADO, PagamentoEstornadoEvent),
        ],
        ids=lambda valor: str(getattr(valor, "__name__", valor)).lower(),
    )
    def test_desfecho_registrado(self, estado: StatusPagamento, evento: type) -> None:
        p = ESTADOS[estado]()
        assert isinstance(p.desfecho_da_compensacao(), evento)

    @pytest.mark.parametrize(
        "estado", [StatusPagamento.SOLICITADO, StatusPagamento.CONFIRMADO]
    )
    def test_sem_desfecho_ainda(self, estado: StatusPagamento) -> None:
        with pytest.raises(TransicaoStatusInvalidaError, match="desfecho"):
            ESTADOS[estado]().desfecho_da_compensacao()

    def test_cancelado_responde_com_o_instante_do_encerramento(self) -> None:
        p = expirado()
        assert p.desfecho_da_compensacao() == PagamentoCanceladoEvent(
            ordem_id=p.ordem_id, pagamento_id=p.id, cancelado_em=DEPOIS_DO_PRAZO
        )

    def test_motivo_obrigatorio(self) -> None:
        with pytest.raises(ValueError, match="Motivo"):
            solicitado().concluir_compensacao(agora=AGORA, motivo=" ")

    def test_falha_de_estorno_registra_evento_sem_mudar_status(self) -> None:
        p = confirmado()
        p.registrar_falha_de_estorno("payment too old to refund " + "x" * 600)
        assert p.status is StatusPagamento.CONFIRMADO
        [evento] = p.coletar_eventos()
        assert isinstance(evento, EstornoDePagamentoFalhouEvent)
        assert len(evento.motivo) == TAMANHO_MAXIMO_MOTIVO

    def test_falha_de_estorno_so_com_pagamento_confirmado(self) -> None:
        with pytest.raises(TransicaoStatusInvalidaError):
            solicitado().registrar_falha_de_estorno("x")


class TestExpiracao:
    def test_expirar_so_solicitado_e_vencido(self) -> None:
        p = solicitado()
        assert not p.vencido(AGORA + timedelta(minutes=60))
        with pytest.raises(TransicaoStatusInvalidaError, match="prazo"):
            p.expirar(agora=DENTRO_DO_PRAZO)

        p.expirar(agora=DEPOIS_DO_PRAZO)

        assert (p.status, p.motivo, p.encerrado_em) == (
            StatusPagamento.EXPIRADO,
            MOTIVO_PRAZO_ESGOTADO,
            DEPOIS_DO_PRAZO,
        )
        assert p.coletar_eventos() == [
            PagamentoExpiradoEvent(
                ordem_id=p.ordem_id, pagamento_id=p.id, motivo=MOTIVO_PRAZO_ESGOTADO
            )
        ]

    @pytest.mark.parametrize(
        "estado",
        [s for s in StatusPagamento if s is not StatusPagamento.SOLICITADO],
        ids=lambda valor: str(valor).lower(),
    )
    def test_so_solicitado_vence(self, estado: StatusPagamento) -> None:
        p = ESTADOS[estado]()
        assert not p.vencido(DEPOIS_DO_PRAZO + timedelta(days=1))
        with pytest.raises(TransicaoStatusInvalidaError):
            p.expirar(agora=DEPOIS_DO_PRAZO + timedelta(days=1))


class TestReidratacao:
    @pytest.mark.parametrize(
        ("status", "faltando"),
        [
            (StatusPagamento.CONFIRMADO, "referencia e confirmado_em"),
            (StatusPagamento.ESTORNADO, "estornado_em e motivo_estorno"),
            (StatusPagamento.RECUSADO, "encerrado_em e motivo"),
            (StatusPagamento.EXPIRADO, "encerrado_em e motivo"),
        ],
        ids=lambda valor: str(valor).lower().replace(" ", "-"),
    )
    def test_status_exige_os_campos_dele(
        self, status: StatusPagamento, faltando: str
    ) -> None:
        cobranca = pagamento().cobranca
        with pytest.raises(ValueError, match="dados incompletos"):
            Pagamento(
                _ordem_id=uuid4(), _criado_em=AGORA, _cobranca=cobranca, _status=status
            )

    def test_datas_dos_eventos_com_timezone(self) -> None:
        cobranca = pagamento().cobranca
        with pytest.raises(ValueError, match="timezone"):
            Pagamento(
                _ordem_id=uuid4(),
                _criado_em=AGORA,
                _cobranca=cobranca,
                _status=StatusPagamento.CONFIRMADO,
                _referencia_pagamento="1",
                _confirmado_em=datetime(2026, 10, 6, 12, 5),
            )

    @pytest.mark.parametrize("recusas", [-1, True], ids=["negativa", "bool"])
    def test_recusas_inteiro_nao_negativo(self, recusas: int) -> None:
        with pytest.raises(ValueError, match="recusas"):
            Pagamento(
                _ordem_id=uuid4(),
                _criado_em=AGORA,
                _cobranca=pagamento().cobranca,
                _recusas=recusas,
            )


class TestHistorico:
    def test_guarda_cada_status_de_cada_tentativa_uma_vez(self) -> None:
        p = solicitado()
        aplicar(p, StatusNoProvedor.EM_ANDAMENTO, referencia="1", bruto="pending")
        aplicar(p, StatusNoProvedor.EM_ANDAMENTO, referencia="1", bruto="pending")
        aplicar(p, StatusNoProvedor.APROVADO, referencia="1")
        assert [
            (n.referencia_pagamento, n.status_provedor) for n in p.notificacoes
        ] == [
            ("1", "pending"),
            ("1", "approved"),
        ]

    @pytest.mark.parametrize(
        "dados",
        [
            pytest.param(
                {"recebida_em": datetime(2026, 10, 6, 12, 0)}, id="data-sem-timezone"
            ),
            pytest.param({"referencia_pagamento": " "}, id="referencia-vazia"),
            pytest.param({"status_provedor": ""}, id="status-vazio"),
        ],
    )
    def test_notificacao_valida(self, dados: dict[str, object]) -> None:
        base: dict[str, object] = {
            "recebida_em": AGORA,
            "referencia_pagamento": "1",
            "status_provedor": "approved",
        }
        with pytest.raises(ValueError, match=r"timezone|vazio"):
            NotificacaoRecebida(**(base | dados))  # type: ignore[arg-type]
