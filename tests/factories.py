"""Fabricas dos agregados pelos construtores reais (invariantes valem nos testes)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from src.compartilhado.dominio.dinheiro import Dinheiro
from src.orcamento.dominio.orcamento import LinhaOrcamento, Orcamento, TipoItem
from src.pagamento.dominio.pagamento import Pagamento, ResultadoAprovacao

AGORA = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
LINK = "http://billing.teste/api/v1/publico/orcamentos/token"


def dinheiro(valor: str) -> Dinheiro:
    return Dinheiro(Decimal(valor))


def linha(
    *,
    tipo: TipoItem = TipoItem.SERVICO,
    codigo: str = "SRV-TROCA-OLEO",
    descricao: str = "Troca de oleo",
    quantidade: int = 1,
    preco: str = "120.00",
) -> LinhaOrcamento:
    return LinhaOrcamento(
        tipo=tipo,
        codigo=codigo,
        descricao=descricao,
        quantidade=quantidade,
        preco_unitario=dinheiro(preco),
    )


def linhas_padrao() -> list[LinhaOrcamento]:
    """Troca de oleo (120) + 4 x oleo 5W30 (45) + filtro (35) = 335.00."""
    return [
        linha(),
        linha(
            tipo=TipoItem.PECA,
            codigo="PEC-OLEO-5W30",
            descricao="Oleo de motor 5W30 (1 L)",
            quantidade=4,
            preco="45.00",
        ),
        linha(
            tipo=TipoItem.PECA,
            codigo="PEC-FILTRO-OLEO",
            descricao="Filtro de oleo",
            preco="35.00",
        ),
    ]


def orcamento(
    *,
    ordem_id: UUID | None = None,
    linhas: list[LinhaOrcamento] | None = None,
    criado_em: datetime = AGORA,
    validade: timedelta = timedelta(hours=72),
) -> Orcamento:
    """Orcamento PENDENTE recem-gerado (com o evento OrcamentoGerado pendente)."""
    return Orcamento.gerar(
        id=uuid4(),
        ordem_id=ordem_id or uuid4(),
        linhas=linhas or linhas_padrao(),
        criado_em=criado_em,
        valido_ate=criado_em + validade,
        link_decisao=LINK,
    )


def pagamento(
    *,
    valor: str = "335.00",
    ordem_id: UUID | None = None,
    orcamento_id: UUID | None = None,
    criado_em: datetime = AGORA,
    validade: timedelta = timedelta(minutes=60),
) -> Pagamento:
    """Pagamento PENDENTE recem-solicitado (com PagamentoSolicitado pendente)."""
    pagamento_id = uuid4()
    return Pagamento.solicitar(
        id=pagamento_id,
        ordem_id=ordem_id or uuid4(),
        orcamento_id=orcamento_id or uuid4(),
        valor=dinheiro(valor),
        provedor="simulado",
        referencia_preferencia=f"sim-pref-{pagamento_id}",
        checkout_url=f"http://billing.teste/simulador/checkout/{pagamento_id}",
        criado_em=criado_em,
        expira_em=criado_em + validade,
    )


def confirmar(p: Pagamento, *, referencia: str = "1", agora: datetime = AGORA) -> None:
    """Aprovacao do provedor pelo valor exato (o caminho feliz do webhook)."""
    resultado = p.aplicar_aprovacao(
        referencia_pagamento=referencia, valor_cobrado=p.valor, agora=agora
    )
    assert resultado is ResultadoAprovacao.CONFIRMADO
