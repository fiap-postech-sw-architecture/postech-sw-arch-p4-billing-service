from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from src.compartilhado.dominio.dinheiro import Dinheiro
from src.compartilhado.dominio.exceptions import TransicaoStatusInvalidaError
from src.orcamento.dominio.events import (
    LinhaOrcamentoGerado,
    OrcamentoAprovadoEvent,
    OrcamentoCanceladoEvent,
    OrcamentoExpiradoEvent,
    OrcamentoGeradoEvent,
    OrcamentoRecusadoEvent,
)
from src.orcamento.dominio.exceptions import OrcamentoVencidoError
from src.orcamento.dominio.orcamento import (
    CanalDecisao,
    Decisao,
    LinhaOrcamento,
    Orcamento,
    StatusOrcamento,
    TipoItem,
)
from tests.factories import AGORA, LINK, dinheiro, linha, linhas_padrao, orcamento

DENTRO_DO_PRAZO = AGORA + timedelta(hours=1)
DEPOIS_DO_PRAZO = AGORA + timedelta(hours=72, seconds=1)


class TestLinhaOrcamento:
    def test_subtotal_e_preco_vezes_quantidade(self) -> None:
        assert linha(quantidade=4, preco="45.00").subtotal == dinheiro("180.00")

    @pytest.mark.parametrize("quantidade", [0, -1, True])
    def test_quantidade_deve_ser_inteiro_positivo(self, quantidade: int) -> None:
        with pytest.raises(ValueError, match="Quantidade"):
            linha(quantidade=quantidade)

    @pytest.mark.parametrize("campo", ["codigo", "descricao"])
    def test_codigo_e_descricao_obrigatorios(self, campo: str) -> None:
        with pytest.raises(ValueError, match="codigo e descricao"):
            linha(**{campo: ""})  # type: ignore[arg-type]

    def test_e_imutavel(self) -> None:
        with pytest.raises(FrozenInstanceError):
            linha().quantidade = 2  # type: ignore[misc]


class TestGeracao:
    def test_gerar_registra_orcamento_gerado_com_precos_congelados(self) -> None:
        ordem_id = uuid4()
        gerado = orcamento(ordem_id=ordem_id)

        assert gerado.status is StatusOrcamento.PENDENTE
        assert gerado.total == dinheiro("335.00")
        assert gerado.coletar_eventos() == [
            OrcamentoGeradoEvent(
                ordem_id=ordem_id,
                orcamento_id=gerado.id,
                linhas=(
                    LinhaOrcamentoGerado(
                        codigo="SRV-TROCA-OLEO",
                        descricao="Troca de oleo",
                        quantidade=1,
                        preco_unitario=Decimal("120.00"),
                        subtotal=Decimal("120.00"),
                    ),
                    LinhaOrcamentoGerado(
                        codigo="PEC-OLEO-5W30",
                        descricao="Oleo de motor 5W30 (1 L)",
                        quantidade=4,
                        preco_unitario=Decimal("45.00"),
                        subtotal=Decimal("180.00"),
                    ),
                    LinhaOrcamentoGerado(
                        codigo="PEC-FILTRO-OLEO",
                        descricao="Filtro de oleo",
                        quantidade=1,
                        preco_unitario=Decimal("35.00"),
                        subtotal=Decimal("35.00"),
                    ),
                ),
                total=Decimal("335.00"),
                moeda="BRL",
                valido_ate=AGORA + timedelta(hours=72),
                link_decisao=LINK,
            )
        ]

    def test_reconstituicao_pelo_construtor_nao_gera_evento(self) -> None:
        reidratado = Orcamento(
            _ordem_id=uuid4(),
            _linhas=tuple(linhas_padrao()),
            _criado_em=AGORA,
            _valido_ate=AGORA + timedelta(hours=1),
        )
        assert reidratado.coletar_eventos() == []

    def test_sem_linhas_e_invalido(self) -> None:
        with pytest.raises(ValueError, match="ao menos uma linha"):
            Orcamento(
                _ordem_id=uuid4(),
                _linhas=(),
                _criado_em=AGORA,
                _valido_ate=AGORA + timedelta(hours=1),
            )

    def test_linhas_em_moedas_diferentes_sao_invalidas(self) -> None:
        dolar = LinhaOrcamento(
            tipo=TipoItem.PECA,
            codigo="PEC-X",
            descricao="Importada",
            quantidade=1,
            preco_unitario=Dinheiro(Decimal("1.00"), moeda="USD"),
        )
        with pytest.raises(ValueError, match="mesma moeda"):
            orcamento(linhas=[linha(), dolar])

    def test_datas_sem_timezone_sao_invalidas(self) -> None:
        with pytest.raises(ValueError, match="timezone"):
            orcamento(criado_em=datetime(2026, 10, 6, 12, 0))

    def test_validade_deve_ser_posterior_a_criacao(self) -> None:
        with pytest.raises(ValueError, match="posterior"):
            orcamento(validade=timedelta(0))


class TestDecisao:
    @pytest.mark.parametrize("canal", list(CanalDecisao))
    def test_aprovar_registra_decisao_e_evento(self, canal: CanalDecisao) -> None:
        gerado = orcamento()
        gerado.limpar_eventos()

        gerado.aprovar(canal=canal, agora=DENTRO_DO_PRAZO)

        assert gerado.status is StatusOrcamento.APROVADO
        assert gerado.decisao == Decisao(canal=canal, decidido_em=DENTRO_DO_PRAZO)
        assert gerado.coletar_eventos() == [
            OrcamentoAprovadoEvent(
                ordem_id=gerado.ordem_id,
                orcamento_id=gerado.id,
                decidido_em=DENTRO_DO_PRAZO,
                canal=canal,
            )
        ]

    def test_recusar_registra_decisao_e_evento(self) -> None:
        gerado = orcamento()
        gerado.limpar_eventos()

        gerado.recusar(canal=CanalDecisao.LINK, agora=DENTRO_DO_PRAZO)

        assert gerado.status is StatusOrcamento.RECUSADO
        assert gerado.coletar_eventos() == [
            OrcamentoRecusadoEvent(
                ordem_id=gerado.ordem_id,
                orcamento_id=gerado.id,
                decidido_em=DENTRO_DO_PRAZO,
                canal=CanalDecisao.LINK,
            )
        ]

    def test_decidir_no_limite_do_prazo_ainda_vale(self) -> None:
        gerado = orcamento()
        gerado.aprovar(canal=CanalDecisao.LINK, agora=gerado.valido_ate)
        assert gerado.status is StatusOrcamento.APROVADO

    def test_decidir_depois_do_prazo_e_vencido_mesmo_sem_job(self) -> None:
        gerado = orcamento()
        gerado.limpar_eventos()
        with pytest.raises(OrcamentoVencidoError):
            gerado.aprovar(canal=CanalDecisao.LINK, agora=DEPOIS_DO_PRAZO)
        assert gerado.status is StatusOrcamento.PENDENTE
        assert gerado.coletar_eventos() == []

    def test_decidir_orcamento_expirado_e_vencido(self) -> None:
        gerado = orcamento()
        gerado.expirar(agora=DEPOIS_DO_PRAZO)
        with pytest.raises(OrcamentoVencidoError):
            gerado.recusar(canal=CanalDecisao.ATENDENTE, agora=DENTRO_DO_PRAZO)

    def test_segunda_decisao_e_transicao_invalida(self) -> None:
        gerado = orcamento()
        gerado.aprovar(canal=CanalDecisao.LINK, agora=DENTRO_DO_PRAZO)
        gerado.limpar_eventos()
        with pytest.raises(TransicaoStatusInvalidaError, match="APROVADO"):
            gerado.recusar(canal=CanalDecisao.LINK, agora=DENTRO_DO_PRAZO)
        assert gerado.coletar_eventos() == []


class TestExpiracao:
    def test_vencido_so_quando_pendente_e_fora_do_prazo(self) -> None:
        gerado = orcamento()
        assert not gerado.vencido(gerado.valido_ate)
        assert gerado.vencido(DEPOIS_DO_PRAZO)
        gerado.aprovar(canal=CanalDecisao.LINK, agora=DENTRO_DO_PRAZO)
        assert not gerado.vencido(DEPOIS_DO_PRAZO)

    def test_expirar_registra_evento(self) -> None:
        gerado = orcamento()
        gerado.limpar_eventos()
        gerado.expirar(agora=DEPOIS_DO_PRAZO)
        assert gerado.status is StatusOrcamento.EXPIRADO
        assert gerado.coletar_eventos() == [
            OrcamentoExpiradoEvent(ordem_id=gerado.ordem_id, orcamento_id=gerado.id)
        ]

    def test_expirar_dentro_do_prazo_e_rejeitado(self) -> None:
        with pytest.raises(TransicaoStatusInvalidaError, match="prazo"):
            orcamento().expirar(agora=DENTRO_DO_PRAZO)


class TestCancelamento:
    @pytest.mark.parametrize("decidir", [False, True])
    def test_cancela_pendente_ou_aprovado(self, decidir: bool) -> None:
        gerado = orcamento()
        if decidir:
            gerado.aprovar(canal=CanalDecisao.LINK, agora=DENTRO_DO_PRAZO)
        gerado.limpar_eventos()

        assert gerado.cancelar(motivo="Reserva de pecas falhou") is True

        assert gerado.status is StatusOrcamento.CANCELADO
        assert gerado.motivo_cancelamento == "Reserva de pecas falhou"
        assert gerado.coletar_eventos() == [
            OrcamentoCanceladoEvent(ordem_id=gerado.ordem_id, orcamento_id=gerado.id)
        ]

    def test_cancelar_de_novo_nao_faz_nada(self) -> None:
        gerado = orcamento()
        gerado.cancelar(motivo="x")
        gerado.limpar_eventos()
        assert gerado.cancelar(motivo="y") is False
        assert gerado.motivo_cancelamento == "x"
        assert gerado.coletar_eventos() == []

    @pytest.mark.parametrize("encerramento", ["recusar", "expirar"])
    def test_encerrado_sem_decisao_valida_nao_cancela_nem_falha(
        self, encerramento: str
    ) -> None:
        gerado = orcamento()
        if encerramento == "recusar":
            gerado.recusar(canal=CanalDecisao.LINK, agora=DENTRO_DO_PRAZO)
        else:
            gerado.expirar(agora=DEPOIS_DO_PRAZO)
        status = gerado.status
        gerado.limpar_eventos()

        assert gerado.cancelar(motivo="x") is False

        assert gerado.status is status
        assert gerado.motivo_cancelamento is None
        assert gerado.coletar_eventos() == []

    def test_motivo_obrigatorio(self) -> None:
        with pytest.raises(ValueError, match="Motivo"):
            orcamento().cancelar(motivo=" ")


def test_tipos_de_item_seguem_o_catalogo() -> None:
    assert [t.value for t in TipoItem] == ["servico", "peca"]
