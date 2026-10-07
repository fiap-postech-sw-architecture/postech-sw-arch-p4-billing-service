from __future__ import annotations

from dataclasses import FrozenInstanceError, dataclass, fields
from datetime import UTC
from decimal import Decimal
from uuid import uuid4

import pytest

from src.compartilhado.dominio.aggregate_root import AggregateRoot
from src.compartilhado.dominio.dinheiro import Dinheiro
from src.compartilhado.dominio.entity import Entity
from src.compartilhado.dominio.events import IntegrationEvent
from src.compartilhado.dominio.exceptions import (
    DomainException,
    EntidadeNaoEncontradaError,
    ValorInvalidoError,
)
from src.compartilhado.dominio.relogio import agora_utc


class TestDinheiro:
    def test_quantiza_em_duas_casas_com_arredondamento_comercial(self) -> None:
        assert Dinheiro(Decimal("10.005")).valor == Decimal("10.01")
        assert Dinheiro(Decimal("10.004")).valor == Decimal("10.00")

    def test_moeda_padrao_brl(self) -> None:
        assert Dinheiro(Decimal("1")).moeda == "BRL"

    def test_converte_valor_nao_decimal_pela_string(self) -> None:
        assert Dinheiro(10).valor == Decimal("10.00")  # type: ignore[arg-type]

    def test_valor_invalido_levanta_value_error(self) -> None:
        with pytest.raises(ValorInvalidoError, match="invalido"):
            Dinheiro("abc")  # type: ignore[arg-type]

    @pytest.mark.parametrize("valor", ["Infinity", "NaN"])
    def test_valor_nao_finito_e_rejeitado(self, valor: str) -> None:
        with pytest.raises(ValorInvalidoError, match="finito"):
            Dinheiro(Decimal(valor))

    @pytest.mark.parametrize(
        "valor",
        ["10000000000", "9999999999.995", "1E+30", "-1E+30"],
        ids=["11-digitos", "arredonda-para-o-teto", "expoente", "expoente-negativo"],
    )
    def test_acima_de_10_digitos_inteiros_e_rejeitado(self, valor: str) -> None:
        # Teto do dinheiro nos contratos de mensagem; o expoente gigante nao
        # pode escapar como decimal.InvalidOperation (viraria 500).
        with pytest.raises(ValorInvalidoError, match="10 digitos inteiros"):
            Dinheiro(Decimal(valor))

    def test_teto_exato_e_aceito(self) -> None:
        assert Dinheiro(Decimal("9999999999.99")).valor == Decimal("9999999999.99")

    def test_multiplicacao_que_estoura_o_teto_e_valor_invalido(self) -> None:
        with pytest.raises(ValorInvalidoError, match="10 digitos inteiros"):
            _ = Dinheiro(Decimal("99999999.99")) * 10**19

    def test_valor_negativo_e_rejeitado(self) -> None:
        with pytest.raises(ValorInvalidoError, match="negativo"):
            Dinheiro(Decimal("-0.01"))

    def test_zero_negativo_vira_zero(self) -> None:
        assert str(Dinheiro(Decimal("-0.001")).valor) == "0.00"

    @pytest.mark.parametrize(
        "moeda",
        ["brl", "BR", "BR1", "BRÁ"],
        ids=["minusculas", "duas-letras", "com-digito", "com-acento"],
    )
    def test_moeda_fora_do_iso_4217_e_rejeitada(self, moeda: str) -> None:
        with pytest.raises(ValorInvalidoError, match="maiusculas"):
            Dinheiro(Decimal("1"), moeda=moeda)

    def test_soma_e_multiplicacao_por_inteiro(self) -> None:
        assert Dinheiro(Decimal("10.50")) + Dinheiro(Decimal("0.25")) == Dinheiro(
            Decimal("10.75")
        )
        assert Dinheiro(Decimal("45.00")) * 4 == Dinheiro(Decimal("180.00"))
        assert 4 * Dinheiro(Decimal("45.00")) == Dinheiro(Decimal("180.00"))

    def test_soma_de_moedas_diferentes_e_rejeitada(self) -> None:
        with pytest.raises(ValorInvalidoError, match="Moedas diferentes"):
            _ = Dinheiro(Decimal("1")) + Dinheiro(Decimal("1"), moeda="USD")

    def test_operandos_de_outro_tipo_devolvem_not_implemented(self) -> None:
        d = Dinheiro(Decimal("1"))
        assert d.__add__(1) is NotImplemented  # type: ignore[operator]
        assert d.__mul__(1.5) is NotImplemented  # type: ignore[operator]

    def test_e_imutavel_e_comparado_por_valor(self) -> None:
        d = Dinheiro(Decimal("1"))
        with pytest.raises(FrozenInstanceError):
            d.valor = Decimal("2")  # type: ignore[misc]
        assert len({Dinheiro(Decimal("1")), Dinheiro(Decimal("1.00"))}) == 1


class TestEntidadeEAgregado:
    def test_identidade_nao_pode_ser_reatribuida(self) -> None:
        entidade = Entity()
        with pytest.raises(AttributeError, match="Identidade"):
            entidade.id = uuid4()

    def test_igualdade_e_hash_pela_identidade(self) -> None:
        identidade = uuid4()
        original, reidratada = Entity(id=identidade), Entity(id=identidade)
        assert original == reidratada
        assert hash(original) == hash(reidratada)
        assert original != Entity()
        assert original.__eq__(object()) is NotImplemented

    def test_agregado_coleta_e_limpa_eventos(self) -> None:
        @dataclass(frozen=True, slots=True, kw_only=True)
        class AlgoAconteceuEvent(IntegrationEvent):
            pass

        @dataclass(eq=False, kw_only=True)
        class Agregado(AggregateRoot):
            def acontecer(self, evento: IntegrationEvent) -> None:
                self._registrar_evento(evento)

        agregado = Agregado()
        evento = AlgoAconteceuEvent(ordem_id=uuid4())
        agregado.acontecer(evento)
        coletados = agregado.coletar_eventos()
        coletados.clear()  # copia: nao mexe no agregado
        assert agregado.coletar_eventos() == [evento]
        agregado.limpar_eventos()
        assert agregado.coletar_eventos() == []


class TestEventoDeIntegracao:
    def test_tipo_e_o_nome_da_classe_sem_sufixo(self) -> None:
        @dataclass(frozen=True, slots=True, kw_only=True)
        class PagamentoConfirmadoEvent(IntegrationEvent):
            pass

        assert PagamentoConfirmadoEvent(ordem_id=uuid4()).tipo == "PagamentoConfirmado"

    def test_campos_do_evento_sao_so_o_dados(self) -> None:
        # ocorrido_em e causation_id sao de quem grava a outbox, nao do evento.
        assert [campo.name for campo in fields(IntegrationEvent)] == ["ordem_id"]


class TestExcecoes:
    def test_mensagem_padrao_e_mensagem_propria(self) -> None:
        assert EntidadeNaoEncontradaError().mensagem == "Entidade nao encontrada"
        assert str(EntidadeNaoEncontradaError("sumiu")) == "sumiu"
        assert DomainException.codigo == "VIOLACAO_REGRA_NEGOCIO"


def test_agora_utc_tem_timezone_e_precisao_de_milissegundos() -> None:
    agora = agora_utc()
    assert agora.tzinfo is UTC
    assert agora.microsecond % 1000 == 0
