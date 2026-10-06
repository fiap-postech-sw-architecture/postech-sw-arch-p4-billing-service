from __future__ import annotations

import pytest

from src.precos.dominio.preco import PrecoPeca, PrecoServico
from src.precos.dominio.validacao import codigos_invalidos
from tests.factories import dinheiro


def servico(codigo: str = "SRV-TROCA-OLEO", *, ativo: bool = True) -> PrecoServico:
    return PrecoServico(
        _codigo=codigo,
        _nome="Troca de oleo",
        _descricao="Troca do oleo do motor",
        _preco=dinheiro("120.00"),
        _ativo=ativo,
    )


def peca(sku: str = "PEC-OLEO-5W30", *, ativo: bool = True) -> PrecoPeca:
    return PrecoPeca(
        _sku=sku, _nome="Oleo 5W30", _preco=dinheiro("45.00"), _ativo=ativo
    )


class TestPrecoServico:
    def test_criacao_valida(self) -> None:
        preco = servico()
        assert (preco.codigo, preco.nome, preco.preco, preco.ativo) == (
            "SRV-TROCA-OLEO",
            "Troca de oleo",
            dinheiro("120.00"),
            True,
        )
        assert preco.descricao == "Troca do oleo do motor"

    @pytest.mark.parametrize(
        "codigo", ["", "srv-troca", "SRV TROCA", "SRV--X", "-SRV", "S" * 51]
    )
    def test_codigo_fora_do_formato_e_rejeitado(self, codigo: str) -> None:
        with pytest.raises(ValueError, match="Codigo do servico invalido"):
            servico(codigo)

    @pytest.mark.parametrize("campo", ["_nome", "_descricao"])
    def test_texto_vazio_e_rejeitado(self, campo: str) -> None:
        dados = {
            "_codigo": "SRV-X",
            "_nome": "Nome",
            "_descricao": "Descricao",
            "_preco": dinheiro("1.00"),
            campo: "   ",
        }
        with pytest.raises(ValueError, match="nao pode ser vazio"):
            PrecoServico(**dados)  # type: ignore[arg-type]

    def test_preco_zero_e_rejeitado(self) -> None:
        with pytest.raises(ValueError, match="maior que zero"):
            PrecoServico(
                _codigo="SRV-X", _nome="N", _descricao="D", _preco=dinheiro("0")
            )

    def test_atualizar_reaplica_invariantes_e_reativa(self) -> None:
        preco = servico(ativo=False)
        preco.atualizar(
            nome="Troca", descricao="Nova", preco=dinheiro("130.00"), ativo=True
        )
        assert (preco.nome, preco.descricao, preco.preco, preco.ativo) == (
            "Troca",
            "Nova",
            dinheiro("130.00"),
            True,
        )
        with pytest.raises(ValueError, match="maior que zero"):
            preco.atualizar(nome="T", descricao="D", preco=dinheiro("0"), ativo=True)

    def test_desativar_e_idempotente(self) -> None:
        preco = servico()
        preco.desativar()
        preco.desativar()
        assert preco.ativo is False


class TestPrecoPeca:
    def test_criacao_valida_e_atualizacao(self) -> None:
        preco = peca()
        assert (preco.sku, preco.nome, preco.preco) == (
            "PEC-OLEO-5W30",
            "Oleo 5W30",
            dinheiro("45.00"),
        )
        preco.atualizar(nome="Oleo", preco=dinheiro("50.00"), ativo=False)
        assert (preco.nome, preco.preco, preco.ativo) == (
            "Oleo",
            dinheiro("50.00"),
            False,
        )
        preco.desativar()
        assert preco.ativo is False

    def test_sku_invalido_e_rejeitado(self) -> None:
        with pytest.raises(ValueError, match="SKU da peca invalido"):
            peca("pec oleo")

    def test_nome_vazio_e_rejeitado(self) -> None:
        with pytest.raises(ValueError, match="Nome da peca"):
            PrecoPeca(_sku="PEC-X", _nome="", _preco=dinheiro("1.00"))


class TestValidacaoDeItens:
    def test_todos_validos_devolve_lista_vazia(self) -> None:
        assert (
            codigos_invalidos(
                servicos_solicitados=["SRV-TROCA-OLEO"],
                pecas_solicitadas=["PEC-OLEO-5W30"],
                servicos={"SRV-TROCA-OLEO": servico()},
                pecas={"PEC-OLEO-5W30": peca()},
            )
            == []
        )

    def test_inexistentes_e_inativos_na_ordem_pedida_sem_repeticao(self) -> None:
        invalidos = codigos_invalidos(
            servicos_solicitados=["SRV-NAO-EXISTE", "SRV-INATIVO", "SRV-NAO-EXISTE"],
            pecas_solicitadas=["PEC-OLEO-5W30", "PEC-INATIVA", "PEC-NAO-EXISTE"],
            servicos={"SRV-INATIVO": servico("SRV-INATIVO", ativo=False)},
            pecas={
                "PEC-OLEO-5W30": peca(),
                "PEC-INATIVA": peca("PEC-INATIVA", ativo=False),
            },
        )
        assert invalidos == [
            "SRV-NAO-EXISTE",
            "SRV-INATIVO",
            "PEC-INATIVA",
            "PEC-NAO-EXISTE",
        ]

    def test_codigo_de_servico_nao_vale_como_peca(self) -> None:
        assert codigos_invalidos(
            servicos_solicitados=[],
            pecas_solicitadas=["SRV-TROCA-OLEO"],
            servicos={"SRV-TROCA-OLEO": servico()},
            pecas={},
        ) == ["SRV-TROCA-OLEO"]
