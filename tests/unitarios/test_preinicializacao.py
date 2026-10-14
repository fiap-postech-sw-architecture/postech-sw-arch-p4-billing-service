"""Toda serie de rotulo fechado nasce com zero no boot do processo (ADR-043).

Sem a serie em zero, o ``increase()`` do Prometheus perde o primeiro evento dela
e, no volume de uma demonstracao, o painel mostra zero. O registro de metricas e
global do processo, e outros testes ja incrementaram as series: cada caso sobe um
interpretador novo, importa o que o processo importa e le o ``REGISTRY`` antes
de qualquer evento.
"""

from __future__ import annotations

import json
import subprocess
import sys
from itertools import product
from pathlib import Path

import pytest

from src.compartilhado.infraestrutura.mensageria import contratos

RAIZ = Path(__file__).resolve().parents[2]
RESULTADOS = ("processada", "duplicada", "ignorada", "retry", "dlq")
OPERACOES_MP = (
    "criar_cobranca",
    "cancelar_cobranca",
    "consultar_pagamento",
    "buscar_pagamentos",
    "estornar",
)
RESULTADOS_MP = (
    "sucesso",
    "recusado",
    "nao_encontrado",
    "falha_transitoria",
    "resposta_invalida",
    "circuito_aberto",
    "status_desconhecido",
)
BALDES_DA_DECISAO = (
    "60.0",
    "300.0",
    "900.0",
    "3600.0",
    "14400.0",
    "43200.0",
    "86400.0",
    "172800.0",
    "259200.0",
    "+Inf",
)

Amostras = dict[tuple[str, tuple[tuple[str, str], ...]], float]

LEITURA = """
import json
from prometheus_client import REGISTRY
amostras = [[a.name, a.labels, a.value] for f in REGISTRY.collect() for a in f.samples]
print("AMOSTRAS=" + json.dumps(amostras))
"""


def boot(codigo: str) -> Amostras:
    """Roda ``codigo`` num interpretador novo e devolve as amostras do REGISTRY."""
    resultado = subprocess.run(  # noqa: S603 - o interpretador de teste e o codigo do repositorio
        [sys.executable, "-I", "-c", codigo + LEITURA],
        cwd=RAIZ,
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    [linha] = [
        linha
        for linha in resultado.stdout.splitlines()
        if linha.startswith("AMOSTRAS=")
    ]
    return {
        (nome, tuple(sorted(rotulos.items()))): valor
        for nome, rotulos, valor in json.loads(linha.removeprefix("AMOSTRAS="))
    }


def serie(nome: str, **rotulos: str) -> tuple[str, tuple[tuple[str, str], ...]]:
    return nome, tuple(sorted(rotulos.items()))


def series_de(
    amostras: Amostras, nome: str
) -> dict[tuple[tuple[str, str], ...], float]:
    return {rotulos: valor for (n, rotulos), valor in amostras.items() if n == nome}


def assert_zeradas(amostras: Amostras, nome: str, *combinacoes: dict[str, str]) -> None:
    """Existem exatamente estas series de ``nome``, todas em zero."""
    esperadas = {tuple(sorted(c.items())): 0.0 for c in combinacoes}
    assert series_de(amostras, nome) == esperadas


@pytest.fixture(scope="module")
def boot_da_api() -> Amostras:
    return boot("import src.main\n")


@pytest.fixture(scope="module")
def boot_do_prazos() -> Amostras:
    return boot("import src.prazos\n")


class TestApiEPrazos:
    @pytest.mark.parametrize("processo", ["boot_da_api", "boot_do_prazos"])
    def test_mercado_pago_nasce_com_as_35_combinacoes_em_zero(
        self, processo: str, request: pytest.FixtureRequest
    ) -> None:
        amostras = request.getfixturevalue(processo)

        assert_zeradas(
            amostras,
            "pytstop_mercadopago_requisicoes_total",
            *(
                {"operacao": o, "resultado": r}
                for o, r in product(OPERACOES_MP, RESULTADOS_MP)
            ),
        )
        assert len(series_de(amostras, "pytstop_mercadopago_requisicoes_total")) == 35

    @pytest.mark.parametrize("processo", ["boot_da_api", "boot_do_prazos"])
    def test_valor_e_estornos_do_pagamento_nascem_em_zero(
        self, processo: str, request: pytest.FixtureRequest
    ) -> None:
        amostras = request.getfixturevalue(processo)

        assert_zeradas(
            amostras,
            "pytstop_pagamentos_valor_centavos_total",
            {"estado": "confirmado"},
            {"estado": "estornado"},
        )
        assert_zeradas(
            amostras,
            "pytstop_pagamentos_estornados_total",
            {"motivo": "compensacao"},
            {"motivo": "pagamento_apos_encerramento"},
        )

    def test_tempo_ate_a_decisao_nasce_em_zero_nas_duas_decisoes(
        self, boot_da_api: Amostras
    ) -> None:
        nome = "pytstop_orcamentos_tempo_decisao_segundos"
        for decisao in ("aprovado", "recusado"):
            assert boot_da_api[serie(f"{nome}_count", decisao=decisao)] == 0.0
            assert boot_da_api[serie(f"{nome}_sum", decisao=decisao)] == 0.0
            for le in BALDES_DA_DECISAO:
                esperada = serie(f"{nome}_bucket", decisao=decisao, le=le)
                assert boot_da_api[esperada] == 0.0
        decisoes = {
            dict(rotulos)["decisao"]
            for rotulos in series_de(boot_da_api, f"{nome}_count")
        }
        assert decisoes == {"aprovado", "recusado"}

    def test_so_a_api_conhece_a_decisao_do_orcamento(
        self, boot_do_prazos: Amostras
    ) -> None:
        assert not [n for n, _ in boot_do_prazos if n.startswith("pytstop_orcamentos_")]


class TestRelay:
    def test_publicadas_por_tipo_e_reconexoes_do_relay_nascem_em_zero(self) -> None:
        amostras = boot(
            """
import tempfile
import threading
from pathlib import Path
from unittest.mock import MagicMock

from src.compartilhado.infraestrutura.mensageria.amqp import manter_conectado
from src.compartilhado.infraestrutura.mensageria.relay import RelayDaOutbox

RelayDaOutbox(MagicMock(), MagicMock(), usuario="billing")
parar = threading.Event()
parar.set()
manter_conectado(
    MagicMock(),
    lambda: None,
    parar=parar,
    heartbeat=Path(tempfile.mkdtemp()) / "hb",
    processo="relay",
)
"""
        )

        # Os 13 eventos do contrato (o que tem schema menos os 4 comandos).
        eventos = sorted(contratos.eventos_publicados())
        assert len(eventos) == 13
        assert_zeradas(
            amostras,
            "pytstop_mensagens_publicadas_total",
            *({"tipo": tipo} for tipo in eventos),
        )
        # So o componente do proprio processo.
        assert_zeradas(
            amostras, "pytstop_reconexoes_ao_broker_total", {"componente": "relay"}
        )


class TestConsumidor:
    def test_consumidas_por_tipo_e_resultado_e_reconexoes_nascem_em_zero(self) -> None:
        amostras = boot(
            """
import tempfile
import threading
from pathlib import Path
from unittest.mock import MagicMock

from src.compartilhado.infraestrutura.mensageria.amqp import manter_conectado
from src.compartilhado.infraestrutura.mensageria.consumidor import (
    ConsumidorDeComandos,
)
from src.configuracao import ConfiguracaoDosComandos
from src.consumidor import criar_handlers
from src.pagamento.infraestrutura.gateway import criar_gateway

config = ConfiguracaoDosComandos.do_ambiente(
    {"ENVIRONMENT": "development", "MP_MODE": "simulado"}
)
handlers = criar_handlers(config, gateway=criar_gateway(config))
ConsumidorDeComandos(
    MagicMock(), handlers, fila="billing.comandos", usuario="billing"
)
parar = threading.Event()
parar.set()
manter_conectado(
    MagicMock(),
    lambda: None,
    parar=parar,
    heartbeat=Path(tempfile.mkdtemp()) / "hb",
    processo="consumidor",
)
"""
        )

        assert_zeradas(
            amostras,
            "pytstop_mensagens_consumidas_total",
            *(
                {"tipo": tipo, "resultado": resultado}
                for tipo, resultado in product(sorted(contratos.COMANDOS), RESULTADOS)
            ),
            # A mensagem sem tipo conhecido so pode ir para a DLQ.
            {"tipo": "desconhecido", "resultado": "dlq"},
        )
        assert_zeradas(
            amostras, "pytstop_reconexoes_ao_broker_total", {"componente": "consumidor"}
        )
