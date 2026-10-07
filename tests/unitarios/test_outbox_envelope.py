"""Envelope da outbox e contrato das mensagens (RFC-004, secoes 5.2 e 5.3)."""

from __future__ import annotations

import json
from dataclasses import dataclass, fields
from datetime import timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from src.compartilhado.aplicacao.outbox import para_envelope
from src.compartilhado.dominio.events import IntegrationEvent
from src.compartilhado.infraestrutura.mensageria import contratos
from src.orcamento.dominio import events as eventos_orcamento
from src.orcamento.dominio.orcamento import CanalDecisao
from src.pagamento.dominio import events as eventos_pagamento
from src.pagamento.dominio.estados import StatusNoProvedor
from tests.factories import (
    AGORA,
    ATENDENTE_SUB,
    confirmar,
    orcamento,
    pagamento,
    situacao,
)

# JSON Schemas do catalogo (RFC-004, secao 5.3) copiados do repositorio da
# plataforma (contratos/, SHA em contratos/ORIGEM): uma mensagem que o Billing
# publica precisa validar no schema que os consumidores usam.
SCHEMAS = {
    caminho.name.removesuffix(".schema.json"): json.loads(caminho.read_text())
    for caminho in (contratos.diretorio() / "schemas").glob("*.schema.json")
}
COMANDOS_CONSUMIDOS = {
    "GerarOrcamento",
    "CancelarOrcamento",
    "SolicitarPagamento",
    "EstornarPagamento",
}
CATALOGO_DO_BILLING = set(SCHEMAS) - {"envelope"} - COMANDOS_CONSUMIDOS


def _envelope(
    evento: IntegrationEvent, *, causation_id: UUID | None = None
) -> dict[str, Any]:
    return para_envelope(
        evento, mensagem_id=uuid4(), causation_id=causation_id, ocorrido_em=AGORA
    )


def _classe_do_evento(tipo: str) -> type[IntegrationEvent]:
    return next(
        valor
        for modulo in (eventos_orcamento, eventos_pagamento)
        for valor in vars(modulo).values()
        if isinstance(valor, type) and valor.__name__ == f"{tipo}Event"
    )


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
    assert len(CATALOGO_DO_BILLING) == 13


@pytest.mark.parametrize("tipo", sorted(CATALOGO_DO_BILLING))
def test_campos_de_cada_evento_sao_os_do_contrato(tipo: str) -> None:
    campos = {campo.name for campo in fields(_classe_do_evento(tipo))}
    schema = SCHEMAS[tipo]
    assert campos == set(schema["properties"])
    assert set(schema["required"]) <= campos


def test_linha_do_orcamento_gerado_segue_o_contrato() -> None:
    campos = {campo.name for campo in fields(eventos_orcamento.LinhaOrcamentoGerado)}
    assert campos == set(SCHEMAS["OrcamentoGerado"]["$defs"]["linha"]["properties"])


def _violacoes(envelope: dict[str, Any]) -> list[str]:
    formatos = FormatChecker()
    validadores = (
        (Draft202012Validator(SCHEMAS["envelope"], format_checker=formatos), envelope),
        (
            Draft202012Validator(SCHEMAS[envelope["tipo"]], format_checker=formatos),
            envelope["dados"],
        ),
    )
    return [
        f"{'/'.join(map(str, erro.absolute_path))}: {erro.message}"
        for validador, instancia in validadores
        for erro in validador.iter_errors(instancia)
    ]


def _ultimo(eventos: list[IntegrationEvent]) -> IntegrationEvent:
    return eventos[-1]


def _exemplos_de_orcamento() -> dict[str, IntegrationEvent]:
    aprovado = orcamento()
    aprovado.aprovar(
        canal=CanalDecisao.ATENDENTE, agora=AGORA, decidido_por=ATENDENTE_SUB
    )
    recusado = orcamento()
    recusado.recusar(canal=CanalDecisao.LINK, agora=AGORA)
    expirado = orcamento()
    expirado.expirar(agora=AGORA + timedelta(hours=73))
    cancelado = orcamento()
    cancelado.cancelar(motivo="OS cancelada")
    return {
        "orcamento-gerado": _ultimo(orcamento().coletar_eventos()),
        "aprovado-pelo-atendente": _ultimo(aprovado.coletar_eventos()),
        "recusado-pelo-link": _ultimo(recusado.coletar_eventos()),
        "orcamento-expirado": _ultimo(expirado.coletar_eventos()),
        "orcamento-cancelado": _ultimo(cancelado.coletar_eventos()),
        "geracao-falhou": eventos_orcamento.GeracaoDeOrcamentoFalhouEvent(
            ordem_id=uuid4(),
            motivo="Itens inexistentes ou inativos na tabela de precos",
            codigos_invalidos=("SRV-NAO-EXISTE", "PEC-VELA"),
        ),
    }


def _exemplos_de_pagamento() -> dict[str, IntegrationEvent]:
    # Motivos vindos do provedor maiores que o maxLength do contrato (500).
    recusado = pagamento()
    recusado.aplicar_notificacao(
        situacao(recusado, status=StatusNoProvedor.RECUSADO, detalhe="x" * 600),
        agora=AGORA,
        max_recusas=1,
    )
    confirmado = pagamento()
    confirmar(confirmado)
    expirado = pagamento()
    expirado.expirar(agora=AGORA + timedelta(minutes=61))
    cancelado = pagamento()
    cancelado.concluir_compensacao(agora=AGORA, motivo="OS cancelada")
    estornado = pagamento()
    confirmar(estornado)
    estornado.concluir_compensacao(agora=AGORA, motivo="OS cancelada")
    falha_de_estorno = pagamento()
    confirmar(falha_de_estorno)
    falha_de_estorno.registrar_falha_de_estorno("y" * 600)
    estorno_automatico = pagamento()
    estorno_automatico.expirar(agora=AGORA + timedelta(minutes=61))
    estorno_automatico.registrar_estorno_automatico("9", agora=AGORA)
    return {
        "pagamento-solicitado": _ultimo(pagamento().coletar_eventos()),
        "recusado-com-motivo-longo": _ultimo(recusado.coletar_eventos()),
        "pagamento-confirmado": _ultimo(confirmado.coletar_eventos()),
        "pagamento-expirado": _ultimo(expirado.coletar_eventos()),
        "pagamento-cancelado": _ultimo(cancelado.coletar_eventos()),
        "estornado-na-compensacao": _ultimo(estornado.coletar_eventos()),
        "falha-de-estorno-com-motivo-longo": _ultimo(
            falha_de_estorno.coletar_eventos()
        ),
        "estorno-automatico": _ultimo(estorno_automatico.coletar_eventos()),
    }


EXEMPLOS = {**_exemplos_de_orcamento(), **_exemplos_de_pagamento()}


def test_formatos_do_contrato_sao_checados() -> None:
    # Sem o validador instalado o FormatChecker aceita qualquer date-time.
    formatos = FormatChecker()
    assert {"date-time", "uuid"} <= set(formatos.checkers)
    assert not formatos.conforms("ontem", "date-time")


def test_os_exemplos_cobrem_todo_o_catalogo() -> None:
    assert {evento.tipo for evento in EXEMPLOS.values()} == CATALOGO_DO_BILLING


@pytest.mark.parametrize("exemplo", list(EXEMPLOS))
def test_cada_evento_valida_no_contrato_da_plataforma(exemplo: str) -> None:
    envelope = _envelope(EXEMPLOS[exemplo], causation_id=uuid4())
    assert _violacoes(envelope) == []


def test_ocorrido_em_vem_do_relogio_de_quem_grava() -> None:
    evento = eventos_orcamento.OrcamentoExpiradoEvent(
        ordem_id=uuid4(), orcamento_id=uuid4()
    )
    envelope = para_envelope(
        evento,
        mensagem_id=uuid4(),
        causation_id=None,
        ocorrido_em=AGORA + timedelta(seconds=5, microseconds=123456),
    )
    assert envelope["ocorrido_em"] == "2026-10-06T12:00:05.123Z"
    assert envelope["causation_id"] is None


def test_envelope_do_orcamento_gerado() -> None:
    gerado = orcamento()
    evento = gerado.coletar_eventos()[0]
    mensagem_id = uuid4()
    comando_id = uuid4()

    envelope = para_envelope(
        evento, mensagem_id=mensagem_id, causation_id=comando_id, ocorrido_em=AGORA
    )

    assert envelope["id"] == str(mensagem_id)
    assert envelope["tipo"] == "OrcamentoGerado"
    assert envelope["versao"] == 1
    assert envelope["origem"] == "billing-service"
    assert envelope["correlation_id"] == str(gerado.ordem_id)
    assert envelope["causation_id"] == str(comando_id)
    assert envelope["ocorrido_em"] == "2026-10-06T12:00:00.000Z"
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
        decidido_por=ATENDENTE_SUB,
    )
    dados = _envelope(evento)["dados"]
    assert dados["canal"] == "atendente"
    assert dados["decidido_em"] == "2026-10-06T12:00:00.000Z"
    assert dados["decidido_por"] == ATENDENTE_SUB

    falha = eventos_orcamento.GeracaoDeOrcamentoFalhouEvent(
        ordem_id=uuid4(), motivo="x", codigos_invalidos=("SRV-X",)
    )
    assert _envelope(falha)["dados"]["codigos_invalidos"] == ["SRV-X"]


def test_float_nunca_entra_no_envelope() -> None:
    @dataclass(frozen=True, slots=True, kw_only=True)
    class ValorEmFloatEvent(IntegrationEvent):
        valor: float

    with pytest.raises(TypeError, match="float"):
        _envelope(ValorEmFloatEvent(ordem_id=uuid4(), valor=1.5))


def test_decimal_preserva_a_escala() -> None:
    @dataclass(frozen=True, slots=True, kw_only=True)
    class ValorEvent(IntegrationEvent):
        valor: Decimal

    dados = _envelope(ValorEvent(ordem_id=uuid4(), valor=Decimal("350.00")))["dados"]
    assert dados["valor"] == "350.00"


def test_str_enum_sai_como_str_puro() -> None:
    evento = eventos_orcamento.OrcamentoRecusadoEvent(
        ordem_id=uuid4(),
        orcamento_id=uuid4(),
        decidido_em=AGORA,
        canal=CanalDecisao.LINK,
    )
    canal = _envelope(evento)["dados"]["canal"]
    assert type(canal) is str


def test_campo_opcional_sem_valor_fica_fora_de_dados() -> None:
    """``decidido_por`` so existe com canal=atendente (RFC-004, secao 5.3)."""
    evento = eventos_orcamento.OrcamentoAprovadoEvent(
        ordem_id=uuid4(),
        orcamento_id=uuid4(),
        decidido_em=AGORA,
        canal=CanalDecisao.LINK,
    )
    assert "decidido_por" not in _envelope(evento)["dados"]
