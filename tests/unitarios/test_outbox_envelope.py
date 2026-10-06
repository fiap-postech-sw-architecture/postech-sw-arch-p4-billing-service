"""Envelope da outbox: formato e catalogo da RFC-004 (secoes 5.2 e 5.3)."""

from __future__ import annotations

import json
from dataclasses import dataclass, fields
from decimal import Decimal
from uuid import uuid4

import pytest

from src.compartilhado.aplicacao.outbox import para_envelope
from src.compartilhado.dominio.events import IntegrationEvent
from src.orcamento.dominio import events as eventos_orcamento
from src.orcamento.dominio.orcamento import CanalDecisao
from src.pagamento.dominio import events as eventos_pagamento
from tests.factories import AGORA, orcamento

CATALOGO_DO_BILLING = {
    "OrcamentoGerado",
    "GeracaoDeOrcamentoFalhou",
    "OrcamentoAprovado",
    "OrcamentoRecusado",
    "OrcamentoExpirado",
    "OrcamentoCancelado",
    "PagamentoSolicitado",
    "PagamentoConfirmado",
    "PagamentoRecusado",
    "PagamentoExpirado",
    "PagamentoCancelado",
    "PagamentoEstornado",
    "EstornoDePagamentoFalhou",
}


def test_classes_de_evento_cobrem_exatamente_o_catalogo() -> None:
    classes = [
        valor
        for modulo in (eventos_orcamento, eventos_pagamento)
        for valor in vars(modulo).values()
        if isinstance(valor, type)
        and issubclass(valor, IntegrationEvent)
        and valor is not IntegrationEvent
    ]
    assert {c.__name__.removesuffix("Event") for c in classes} == CATALOGO_DO_BILLING


# Campos de `dados` do catalogo da RFC-004 (secao 5.3), por mensagem.
DADOS_DO_CATALOGO = {
    "OrcamentoGerado": {
        "ordem_id",
        "orcamento_id",
        "linhas",
        "total",
        "moeda",
        "valido_ate",
        "link_decisao",
    },
    "GeracaoDeOrcamentoFalhou": {"ordem_id", "motivo", "codigos_invalidos"},
    "OrcamentoAprovado": {"ordem_id", "orcamento_id", "decidido_em", "canal"},
    "OrcamentoRecusado": {"ordem_id", "orcamento_id", "decidido_em", "canal"},
    "OrcamentoExpirado": {"ordem_id", "orcamento_id"},
    "OrcamentoCancelado": {"ordem_id", "orcamento_id"},
    "PagamentoSolicitado": {
        "ordem_id",
        "pagamento_id",
        "valor",
        "moeda",
        "checkout_url",
        "expira_em",
    },
    "PagamentoConfirmado": {
        "ordem_id",
        "pagamento_id",
        "valor",
        "moeda",
        "confirmado_em",
        "referencia_provedor",
    },
    "PagamentoRecusado": {"ordem_id", "pagamento_id", "motivo"},
    "PagamentoExpirado": {"ordem_id", "pagamento_id", "motivo"},
    "PagamentoCancelado": {"ordem_id", "pagamento_id", "cancelado_em"},
    "PagamentoEstornado": {"ordem_id", "pagamento_id", "estornado_em", "motivo"},
    "EstornoDePagamentoFalhou": {"ordem_id", "pagamento_id", "motivo"},
}
LINHA_DO_CATALOGO = {"codigo", "descricao", "quantidade", "preco_unitario", "subtotal"}


@pytest.mark.parametrize("tipo", sorted(DADOS_DO_CATALOGO))
def test_dados_de_cada_evento_sao_exatamente_os_do_catalogo(tipo: str) -> None:
    classe = next(
        valor
        for modulo in (eventos_orcamento, eventos_pagamento)
        for valor in vars(modulo).values()
        if isinstance(valor, type) and valor.__name__ == f"{tipo}Event"
    )
    campos = {campo.name for campo in fields(classe)} - {"ocorrido_em"}
    assert campos == DADOS_DO_CATALOGO[tipo]


def test_linha_do_orcamento_gerado_segue_o_catalogo() -> None:
    campos = {campo.name for campo in fields(eventos_orcamento.LinhaOrcamentoGerado)}
    assert campos == LINHA_DO_CATALOGO


def test_envelope_do_orcamento_gerado() -> None:
    gerado = orcamento()
    evento = gerado.coletar_eventos()[0]
    mensagem_id = uuid4()

    envelope = para_envelope(evento, mensagem_id=mensagem_id)

    assert envelope["id"] == str(mensagem_id)
    assert envelope["tipo"] == "OrcamentoGerado"
    assert envelope["versao"] == 1
    assert envelope["origem"] == "billing-service"
    assert envelope["correlation_id"] == str(gerado.ordem_id)
    assert envelope["causation_id"] is None
    assert envelope["ocorrido_em"].endswith("Z")
    assert envelope["dados"] == {
        "ordem_id": str(gerado.ordem_id),
        "orcamento_id": str(gerado.id),
        "linhas": [
            {
                "codigo": "SRV-TROCA-OLEO",
                "descricao": "Troca de oleo",
                "quantidade": 1,
                "preco_unitario": "120.00",
                "subtotal": "120.00",
            },
            {
                "codigo": "PEC-OLEO-5W30",
                "descricao": "Oleo de motor 5W30 (1 L)",
                "quantidade": 4,
                "preco_unitario": "45.00",
                "subtotal": "180.00",
            },
            {
                "codigo": "PEC-FILTRO-OLEO",
                "descricao": "Filtro de oleo",
                "quantidade": 1,
                "preco_unitario": "35.00",
                "subtotal": "35.00",
            },
        ],
        "total": "335.00",
        "moeda": "BRL",
        "valido_ate": "2026-10-09T12:00:00.000Z",
        "link_decisao": "http://billing.teste/api/v1/publico/orcamentos/token",
    }
    # Pronto para o relay: so tipos JSON, dinheiro como string.
    assert json.loads(json.dumps(envelope)) == envelope


def test_enum_vira_valor_e_tupla_vira_lista() -> None:
    evento = eventos_orcamento.OrcamentoAprovadoEvent(
        ordem_id=uuid4(),
        orcamento_id=uuid4(),
        decidido_em=AGORA,
        canal=CanalDecisao.ATENDENTE,
    )
    dados = para_envelope(evento, mensagem_id=uuid4())["dados"]
    assert dados["canal"] == "atendente"
    assert dados["decidido_em"] == "2026-10-06T12:00:00.000Z"

    falha = eventos_orcamento.GeracaoDeOrcamentoFalhouEvent(
        ordem_id=uuid4(), motivo="x", codigos_invalidos=("SRV-X",)
    )
    assert para_envelope(falha, mensagem_id=uuid4())["dados"]["codigos_invalidos"] == [
        "SRV-X"
    ]


def test_float_nunca_entra_no_envelope() -> None:
    @dataclass(frozen=True, slots=True, kw_only=True)
    class ValorEmFloatEvent(IntegrationEvent):
        valor: float

    with pytest.raises(TypeError, match="float"):
        para_envelope(
            ValorEmFloatEvent(ordem_id=uuid4(), valor=1.5), mensagem_id=uuid4()
        )


def test_decimal_preserva_a_escala() -> None:
    @dataclass(frozen=True, slots=True, kw_only=True)
    class ValorEvent(IntegrationEvent):
        valor: Decimal

    dados = para_envelope(
        ValorEvent(ordem_id=uuid4(), valor=Decimal("350.00")), mensagem_id=uuid4()
    )["dados"]
    assert dados["valor"] == "350.00"


def test_str_enum_sai_como_str_puro() -> None:
    evento = eventos_orcamento.OrcamentoRecusadoEvent(
        ordem_id=uuid4(),
        orcamento_id=uuid4(),
        decidido_em=AGORA,
        canal=CanalDecisao.LINK,
    )
    canal = para_envelope(evento, mensagem_id=uuid4())["dados"]["canal"]
    assert type(canal) is str
