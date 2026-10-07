from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from src.compartilhado.dominio.dinheiro import Dinheiro
from src.compartilhado.dominio.exceptions import (
    TransicaoStatusInvalidaError,
    ValorInvalidoError,
)
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
from tests.factories import (
    AGORA,
    ATENDENTE_SUB,
    LINK,
    dinheiro,
    linha,
    linhas_padrao,
    orcamento,
)

DENTRO_DO_PRAZO = AGORA + timedelta(hours=1)
DEPOIS_DO_PRAZO = AGORA + timedelta(hours=72, seconds=1)


def reconstituir(**campos: object) -> Orcamento:
    padrao: dict[str, object] = {
        "id": uuid4(),
        "ordem_id": uuid4(),
        "criado_em": AGORA,
        "linhas": tuple(linhas_padrao()),
        "valido_ate": AGORA + timedelta(hours=1),
        "status": StatusOrcamento.PENDENTE,
        "decisao": None,
        "motivo_cancelamento": None,
    }
    return Orcamento.reconstituir(**(padrao | campos))  # type: ignore[arg-type]


class TestLinhaOrcamento:
    def test_subtotal_e_preco_vezes_quantidade(self) -> None:
        assert linha(quantidade=4, preco="45.00").subtotal == dinheiro("180.00")

    @pytest.mark.parametrize(
        "quantidade",
        [0, -1, True, 1.5, 2.0, "2", 1001, 10**100],
        ids=[
            "zero",
            "negativa",
            "bool",
            "fracao",
            "float-inteiro",
            "texto",
            "1001",
            "gigante",
        ],
    )
    def test_quantidade_e_inteiro_de_1_a_1000(self, quantidade: object) -> None:
        with pytest.raises(ValorInvalidoError) as erro:
            linha(quantidade=quantidade)  # type: ignore[arg-type]
        # A mensagem vira motivo de GeracaoDeOrcamentoFalhou: nao ecoa o valor.
        assert str(erro.value) == "Quantidade deve ser um inteiro de 1 a 1000"

    def test_quantidade_no_limite(self) -> None:
        assert linha(quantidade=1000).quantidade == 1000

    @pytest.mark.parametrize("campo", ["codigo", "descricao"])
    @pytest.mark.parametrize("valor", ["", "   "], ids=["vazio", "espacos"])
    def test_codigo_e_descricao_obrigatorios(self, campo: str, valor: str) -> None:
        with pytest.raises(ValorInvalidoError, match="codigo e descricao"):
            linha(**{campo: valor})  # type: ignore[arg-type]

    @pytest.mark.parametrize(("campo", "tamanho"), [("codigo", 51), ("descricao", 256)])
    def test_codigo_e_descricao_com_teto_do_contrato(
        self, campo: str, tamanho: int
    ) -> None:
        with pytest.raises(ValorInvalidoError, match="caracteres"):
            linha(**{campo: "A" * tamanho})  # type: ignore[arg-type]
        assert linha(**{campo: "A" * (tamanho - 1)})  # type: ignore[arg-type]

    def test_tipo_fora_do_enum_e_invalido(self) -> None:
        with pytest.raises(ValorInvalidoError, match="servico ou peca"):
            LinhaOrcamento(
                tipo="x",  # type: ignore[arg-type]
                codigo="SRV-X",
                descricao="X",
                quantidade=1,
                preco_unitario=dinheiro("1.00"),
            )

    def test_subtotal_acima_do_teto_do_dinheiro(self) -> None:
        with pytest.raises(ValorInvalidoError, match="10 digitos"):
            _ = linha(quantidade=1000, preco="99999999.99").subtotal

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

    def test_reconstituir_nao_gera_evento(self) -> None:
        reidratado = reconstituir()
        assert reidratado.status is StatusOrcamento.PENDENTE
        assert reidratado.coletar_eventos() == []

    @pytest.mark.parametrize(
        ("status", "decisao", "motivo"),
        [
            pytest.param(
                StatusOrcamento.APROVADO, None, None, id="aprovado-sem-decisao"
            ),
            pytest.param(
                StatusOrcamento.RECUSADO, None, None, id="recusado-sem-decisao"
            ),
            pytest.param(
                StatusOrcamento.PENDENTE,
                Decisao(CanalDecisao.LINK, AGORA),
                None,
                id="pendente-com-decisao",
            ),
            pytest.param(
                StatusOrcamento.EXPIRADO,
                Decisao(CanalDecisao.LINK, AGORA),
                None,
                id="expirado-com-decisao",
            ),
            pytest.param(
                StatusOrcamento.CANCELADO, None, None, id="cancelado-sem-motivo"
            ),
            pytest.param(StatusOrcamento.PENDENTE, None, "x", id="pendente-com-motivo"),
        ],
    )
    def test_reidratacao_confere_status_x_campos(
        self, status: StatusOrcamento, decisao: Decisao | None, motivo: str | None
    ) -> None:
        with pytest.raises(ValorInvalidoError, match=r"incoerente|Motivo"):
            reconstituir(status=status, decisao=decisao, motivo_cancelamento=motivo)

    @pytest.mark.parametrize(
        ("status", "decisao", "motivo"),
        [
            pytest.param(
                StatusOrcamento.APROVADO,
                Decisao(CanalDecisao.LINK, AGORA),
                None,
                id="aprovado",
            ),
            pytest.param(
                StatusOrcamento.CANCELADO,
                Decisao(CanalDecisao.ATENDENTE, AGORA, ATENDENTE_SUB),
                "OS cancelada",
                id="cancelado-depois-de-aprovado",
            ),
            pytest.param(
                StatusOrcamento.CANCELADO, None, "OS cancelada", id="cancelado"
            ),
            pytest.param(StatusOrcamento.EXPIRADO, None, None, id="expirado"),
        ],
    )
    def test_reidratacao_coerente(
        self, status: StatusOrcamento, decisao: Decisao | None, motivo: str | None
    ) -> None:
        reidratado = reconstituir(
            status=status, decisao=decisao, motivo_cancelamento=motivo
        )
        assert (reidratado.status, reidratado.decisao) == (status, decisao)

    def test_lapide_reidratada_sem_motivo_e_invalida(self) -> None:
        with pytest.raises(ValorInvalidoError, match="Motivo"):
            reconstituir(
                linhas=(),
                valido_ate=None,
                status=StatusOrcamento.CANCELADO,
                motivo_cancelamento=None,
            )

    @pytest.mark.parametrize(
        ("linhas", "valido_ate"),
        [
            pytest.param((), AGORA + timedelta(hours=1), id="sem-linhas"),
            pytest.param(tuple(linhas_padrao()), None, id="sem-validade"),
        ],
    )
    def test_sem_linhas_ou_validade_so_como_lapide(
        self, linhas: tuple[LinhaOrcamento, ...], valido_ate: datetime | None
    ) -> None:
        with pytest.raises(ValorInvalidoError, match="lapide"):
            Orcamento(
                _ordem_id=uuid4(),
                _linhas=linhas,
                _criado_em=AGORA,
                _valido_ate=valido_ate,
            )

    def test_linhas_em_moedas_diferentes_sao_invalidas(self) -> None:
        dolar = LinhaOrcamento(
            tipo=TipoItem.PECA,
            codigo="PEC-X",
            descricao="Importada",
            quantidade=1,
            preco_unitario=Dinheiro(Decimal("1.00"), moeda="USD"),
        )
        with pytest.raises(ValorInvalidoError, match="mesma moeda"):
            orcamento(linhas=[linha(), dolar])

    def test_datas_sem_timezone_sao_invalidas(self) -> None:
        with pytest.raises(ValorInvalidoError, match="timezone"):
            orcamento(criado_em=datetime(2026, 10, 6, 12, 0))

    def test_validade_deve_ser_posterior_a_criacao(self) -> None:
        with pytest.raises(ValorInvalidoError, match="posterior"):
            orcamento(validade=timedelta(0))

    def test_validade_em_segundo_cheio_como_o_exp_do_link(self) -> None:
        with pytest.raises(ValorInvalidoError, match="segundo cheio"):
            Orcamento(
                _ordem_id=uuid4(),
                _linhas=tuple(linhas_padrao()),
                _criado_em=AGORA,
                _valido_ate=AGORA + timedelta(hours=1, milliseconds=1),
            )

    @pytest.mark.parametrize(
        ("criado_em", "valido_ate"),
        [
            pytest.param(datetime(2026, 10, 6, 12, 0), AGORA, id="so-criado-em-naive"),
            pytest.param(AGORA, datetime(2026, 10, 9, 12, 0), id="so-valido-ate-naive"),
        ],
    )
    def test_cada_data_exige_timezone(
        self, criado_em: datetime, valido_ate: datetime
    ) -> None:
        with pytest.raises(ValorInvalidoError, match="timezone"):
            Orcamento(
                _ordem_id=uuid4(),
                _linhas=tuple(linhas_padrao()),
                _criado_em=criado_em,
                _valido_ate=valido_ate,
            )


class TestLapide:
    def test_lapide_nasce_cancelada_sem_linhas_e_responde(self) -> None:
        ordem_id = uuid4()
        tumulo = Orcamento.lapide(
            id=uuid4(), ordem_id=ordem_id, cancelado_em=AGORA, motivo="cancelamento"
        )
        assert (tumulo.status, tumulo.linhas, tumulo.valido_ate) == (
            StatusOrcamento.CANCELADO,
            (),
            None,
        )
        assert tumulo.total == dinheiro("0.00")
        assert tumulo.motivo_cancelamento == "cancelamento"
        assert tumulo.coletar_eventos() == [
            OrcamentoCanceladoEvent(ordem_id=ordem_id, orcamento_id=tumulo.id)
        ]
        assert not tumulo.vencido(DEPOIS_DO_PRAZO)
        assert tumulo.cancelar(motivo="de novo") is False
        assert tumulo.e_lapide

    def test_orcamento_gerado_nao_e_lapide_mesmo_cancelado(self) -> None:
        gerado = orcamento()
        gerado.cancelar(motivo="cancelamento")
        assert not gerado.e_lapide

    def test_lapide_exige_motivo(self) -> None:
        with pytest.raises(ValorInvalidoError, match="Motivo"):
            Orcamento.lapide(
                id=uuid4(), ordem_id=uuid4(), cancelado_em=AGORA, motivo=""
            )

    def test_lapide_nao_tem_orcamento_gerado(self) -> None:
        tumulo = Orcamento.lapide(
            id=uuid4(), ordem_id=uuid4(), cancelado_em=AGORA, motivo="x"
        )
        with pytest.raises(TransicaoStatusInvalidaError, match="lapide"):
            tumulo.desfecho_da_geracao(LINK)
        with pytest.raises(TransicaoStatusInvalidaError):
            tumulo.aprovar(canal=CanalDecisao.LINK, agora=AGORA)


class TestDesfechos:
    def test_orcamento_gerado_e_republicado_igual(self) -> None:
        gerado = orcamento()
        [original] = gerado.coletar_eventos()
        assert gerado.desfecho_da_geracao(LINK) == original

    @pytest.mark.parametrize("estado", ["cancelado", "recusado", "expirado"])
    def test_encerrado_responde_cancelado(self, estado: str) -> None:
        gerado = orcamento()
        if estado == "cancelado":
            gerado.cancelar(motivo="x")
        elif estado == "recusado":
            gerado.recusar(canal=CanalDecisao.LINK, agora=DENTRO_DO_PRAZO)
        else:
            gerado.expirar(agora=DEPOIS_DO_PRAZO)
        assert gerado.desfecho_do_cancelamento() == OrcamentoCanceladoEvent(
            ordem_id=gerado.ordem_id, orcamento_id=gerado.id
        )

    @pytest.mark.parametrize("aprovar", [False, True], ids=["pendente", "aprovado"])
    def test_pendente_ou_aprovado_ainda_cancela(self, aprovar: bool) -> None:
        gerado = orcamento()
        if aprovar:
            gerado.aprovar(canal=CanalDecisao.LINK, agora=DENTRO_DO_PRAZO)
        with pytest.raises(TransicaoStatusInvalidaError, match="pode ser cancelado"):
            gerado.desfecho_do_cancelamento()


class TestDecisao:
    @pytest.mark.parametrize(
        ("canal", "decidido_por"),
        [(CanalDecisao.LINK, None), (CanalDecisao.ATENDENTE, ATENDENTE_SUB)],
        ids=["link", "atendente"],
    )
    def test_aprovar_registra_decisao_e_evento(
        self, canal: CanalDecisao, decidido_por: str | None
    ) -> None:
        gerado = orcamento()
        gerado.limpar_eventos()

        gerado.aprovar(canal=canal, agora=DENTRO_DO_PRAZO, decidido_por=decidido_por)

        assert gerado.status is StatusOrcamento.APROVADO
        assert gerado.decisao == Decisao(canal, DENTRO_DO_PRAZO, decidido_por)
        assert gerado.coletar_eventos() == [
            OrcamentoAprovadoEvent(
                ordem_id=gerado.ordem_id,
                orcamento_id=gerado.id,
                decidido_em=DENTRO_DO_PRAZO,
                canal=canal,
                decidido_por=decidido_por,
            )
        ]

    @pytest.mark.parametrize(
        ("canal", "decidido_por", "erro"),
        [
            pytest.param(CanalDecisao.ATENDENTE, None, "exige", id="atendente-sem-sub"),
            pytest.param(
                CanalDecisao.ATENDENTE, " ", "exige", id="atendente-sub-vazio"
            ),
            pytest.param(
                CanalDecisao.ATENDENTE, "atendente-1", "exige", id="sub-que-nao-e-uuid"
            ),
            pytest.param(
                CanalDecisao.ATENDENTE,
                ATENDENTE_SUB.upper(),
                "exige",
                id="uuid-fora-da-forma-canonica",
            ),
            pytest.param(CanalDecisao.LINK, ATENDENTE_SUB, "sem", id="link-com-sub"),
        ],
    )
    def test_decidido_por_so_e_com_o_atendente(
        self, canal: CanalDecisao, decidido_por: str | None, erro: str
    ) -> None:
        gerado = orcamento()
        with pytest.raises(ValorInvalidoError, match=f"{erro} decidido_por"):
            gerado.aprovar(
                canal=canal, agora=DENTRO_DO_PRAZO, decidido_por=decidido_por
            )
        assert gerado.status is StatusOrcamento.PENDENTE

    def test_decisao_exige_timezone(self) -> None:
        with pytest.raises(ValorInvalidoError, match="timezone"):
            Decisao(CanalDecisao.LINK, datetime(2026, 10, 6, 13, 0))

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
        assert gerado.valido_ate is not None
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
            gerado.recusar(
                canal=CanalDecisao.ATENDENTE,
                agora=DENTRO_DO_PRAZO,
                decidido_por=ATENDENTE_SUB,
            )

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
        assert gerado.valido_ate is not None
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
    @pytest.mark.parametrize("decidir", [False, True], ids=["pendente", "aprovado"])
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
        with pytest.raises(ValorInvalidoError, match="Motivo"):
            orcamento().cancelar(motivo=" ")


def test_tipos_de_item_seguem_o_catalogo() -> None:
    assert [t.value for t in TipoItem] == ["servico", "peca"]


def _no_estado(estado: StatusOrcamento) -> Orcamento:
    gerado = orcamento()
    if estado is StatusOrcamento.APROVADO:
        gerado.aprovar(canal=CanalDecisao.LINK, agora=DENTRO_DO_PRAZO)
    elif estado is StatusOrcamento.RECUSADO:
        gerado.recusar(canal=CanalDecisao.LINK, agora=DENTRO_DO_PRAZO)
    elif estado is StatusOrcamento.EXPIRADO:
        gerado.expirar(agora=DEPOIS_DO_PRAZO)
    elif estado is StatusOrcamento.CANCELADO:
        gerado.cancelar(motivo="OS cancelada")
    gerado.limpar_eventos()
    return gerado


def _comando(gerado: Orcamento, comando: str) -> bool | None:
    """Executa o comando; so ``cancelar`` diz se mudou alguma coisa."""
    if comando == "aprovar":
        gerado.aprovar(canal=CanalDecisao.LINK, agora=DENTRO_DO_PRAZO)
    elif comando == "recusar":
        gerado.recusar(canal=CanalDecisao.LINK, agora=DENTRO_DO_PRAZO)
    elif comando == "expirar":
        gerado.expirar(agora=DEPOIS_DO_PRAZO)
    else:
        return gerado.cancelar(motivo="OS cancelada")
    return None


P, A, R, E, C = (
    StatusOrcamento.PENDENTE,
    StatusOrcamento.APROVADO,
    StatusOrcamento.RECUSADO,
    StatusOrcamento.EXPIRADO,
    StatusOrcamento.CANCELADO,
)
_TRANSICAO = TransicaoStatusInvalidaError
_VENCIDO = OrcamentoVencidoError

# (origem, comando) -> destino (com o evento do destino), excecao, ou None
# (cancelar o que ja esta encerrado: nada muda e nada sai).
MATRIZ_DO_ORCAMENTO: dict[tuple[StatusOrcamento, str], object] = {
    (P, "aprovar"): A,
    (P, "recusar"): R,
    (P, "expirar"): E,
    (P, "cancelar"): C,
    (A, "aprovar"): _TRANSICAO,
    (A, "recusar"): _TRANSICAO,
    (A, "expirar"): _TRANSICAO,
    (A, "cancelar"): C,
    (R, "aprovar"): _TRANSICAO,
    (R, "recusar"): _TRANSICAO,
    (R, "expirar"): _TRANSICAO,
    (R, "cancelar"): None,
    (E, "aprovar"): _VENCIDO,
    (E, "recusar"): _VENCIDO,
    (E, "expirar"): _TRANSICAO,
    (E, "cancelar"): None,
    (C, "aprovar"): _TRANSICAO,
    (C, "recusar"): _TRANSICAO,
    (C, "expirar"): _TRANSICAO,
    (C, "cancelar"): None,
}
_EVENTO_DO_DESTINO = {
    A: OrcamentoAprovadoEvent,
    R: OrcamentoRecusadoEvent,
    E: OrcamentoExpiradoEvent,
    C: OrcamentoCanceladoEvent,
}


@pytest.mark.parametrize(
    ("origem", "comando"),
    list(MATRIZ_DO_ORCAMENTO),
    ids=[
        f"{origem.value.lower()}-{comando}" for origem, comando in MATRIZ_DO_ORCAMENTO
    ],
)
def test_matriz_de_transicoes_do_orcamento(
    origem: StatusOrcamento, comando: str
) -> None:
    gerado = _no_estado(origem)
    esperado = MATRIZ_DO_ORCAMENTO[(origem, comando)]
    if isinstance(esperado, type):
        with pytest.raises(esperado):
            _comando(gerado, comando)
        assert gerado.status is origem
        assert gerado.coletar_eventos() == []
    elif esperado is None:
        assert _comando(gerado, comando) is False
        assert gerado.status is origem
        assert gerado.coletar_eventos() == []
    else:
        assert isinstance(esperado, StatusOrcamento)
        _comando(gerado, comando)
        assert gerado.status is esperado
        [evento] = gerado.coletar_eventos()
        assert isinstance(evento, _EVENTO_DO_DESTINO[esperado])
