"""Historico do provedor: idempotencia das notificacoes e dos estornos."""

from __future__ import annotations

from datetime import datetime

import pytest

from src.compartilhado.dominio.exceptions import ValorInvalidoError
from src.pagamento.dominio.historico import (
    EstornoAutomatico,
    HistoricoDoProvedor,
    NotificacaoRecebida,
)
from tests.factories import AGORA


def notificacao(referencia: str = "1", status: str = "approved") -> NotificacaoRecebida:
    return NotificacaoRecebida(
        recebida_em=AGORA, referencia_pagamento=referencia, status_provedor=status
    )


class TestNotificacoes:
    def test_cada_status_de_cada_tentativa_entra_uma_vez(self) -> None:
        historico = HistoricoDoProvedor()

        assert historico.registrar_notificacao(notificacao("1", "pending"))
        assert not historico.registrar_notificacao(notificacao("1", "pending"))
        assert historico.registrar_notificacao(notificacao("1", "approved"))
        assert historico.registrar_notificacao(notificacao("2", "pending"))

        assert [
            (n.referencia_pagamento, n.status_provedor) for n in historico.notificacoes
        ] == [
            ("1", "pending"),
            ("1", "approved"),
            ("2", "pending"),
        ]

    def test_repetida_nao_troca_o_registro_original(self) -> None:
        historico = HistoricoDoProvedor()
        primeira = notificacao()
        historico.registrar_notificacao(primeira)

        historico.registrar_notificacao(
            NotificacaoRecebida(
                recebida_em=AGORA.replace(hour=15),
                referencia_pagamento="1",
                status_provedor="approved",
            )
        )

        assert historico.notificacoes == (primeira,)

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
        with pytest.raises(ValorInvalidoError, match=r"timezone|vazio"):
            NotificacaoRecebida(**(base | dados))  # type: ignore[arg-type]


class TestEstornosAutomaticos:
    def test_achado_pela_referencia_da_tentativa(self) -> None:
        historico = HistoricoDoProvedor()
        estorno = EstornoAutomatico(referencia_pagamento="9", registrado_em=AGORA)

        assert historico.estorno_automatico("9") is None
        historico.registrar_estorno_automatico(estorno)

        assert historico.estorno_automatico("9") == estorno
        assert historico.estorno_automatico("10") is None
        assert historico.estornos_automaticos == (estorno,)

    @pytest.mark.parametrize(
        "dados",
        [
            pytest.param(
                {"registrado_em": datetime(2026, 10, 6, 12, 0)}, id="data-sem-timezone"
            ),
            pytest.param({"referencia_pagamento": " "}, id="referencia-vazia"),
        ],
    )
    def test_estorno_valido(self, dados: dict[str, object]) -> None:
        base: dict[str, object] = {
            "referencia_pagamento": "9",
            "registrado_em": AGORA,
        }
        with pytest.raises(ValorInvalidoError, match=r"timezone|vazio"):
            EstornoAutomatico(**(base | dados))  # type: ignore[arg-type]


def test_historico_reidratado_nao_compartilha_as_listas_de_origem() -> None:
    origem = [notificacao()]
    historico = HistoricoDoProvedor(origem)

    historico.registrar_notificacao(notificacao("2"))

    assert len(origem) == 1
    assert len(historico.notificacoes) == 2
