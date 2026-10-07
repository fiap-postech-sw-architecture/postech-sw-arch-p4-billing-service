"""Fabricas dos agregados pelos construtores reais (invariantes valem nos testes)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from src.compartilhado.dominio.dinheiro import Dinheiro
from src.orcamento.dominio.orcamento import LinhaOrcamento, Orcamento, TipoItem
from src.pagamento.dominio.cobranca import Cobranca, SituacaoNoProvedor
from src.pagamento.dominio.estados import ResultadoNotificacao, StatusNoProvedor
from src.pagamento.dominio.pagamento import Pagamento

AGORA = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
LINK = "http://billing.teste/api/v1/publico/orcamentos/token"
# sub (id do usuario no OS) do atendente que decide em nome do cliente.
ATENDENTE_SUB = "8a6f2b4c-3d1e-4f5a-9b7c-0d2e4f6a8b1c"


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
    valido_ate = criado_em + validade
    if valido_ate.microsecond:  # segundo cheio, como o GerarOrcamento grava
        valido_ate = valido_ate.replace(microsecond=0) + timedelta(seconds=1)
    return Orcamento.gerar(
        id=uuid4(),
        ordem_id=ordem_id or uuid4(),
        linhas=linhas or linhas_padrao(),
        criado_em=criado_em,
        valido_ate=valido_ate,
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
    """Pagamento SOLICITADO recem-criado (com PagamentoSolicitado pendente)."""
    pagamento_id = uuid4()
    return Pagamento.solicitar(
        id=pagamento_id,
        ordem_id=ordem_id or uuid4(),
        cobranca=Cobranca(
            orcamento_id=orcamento_id or uuid4(),
            valor=dinheiro(valor),
            provedor="simulado",
            referencia_preferencia=f"sim-pref-{pagamento_id}",
            checkout_url=f"http://billing.teste/simulador/checkout/{pagamento_id}",
            expira_em=criado_em + validade,
        ),
        criado_em=criado_em,
    )


def situacao(
    p: Pagamento | None = None,
    *,
    referencia: str = "1",
    status: StatusNoProvedor = StatusNoProvedor.APROVADO,
    bruto: str | None = None,
    valor: Dinheiro | None = None,
    detalhe: str | None = None,
) -> SituacaoNoProvedor:
    """Tentativa como a consulta ao provedor devolve; valor padrao = o cobrado."""
    brutos = {
        StatusNoProvedor.APROVADO: "approved",
        StatusNoProvedor.RECUSADO: "rejected",
        StatusNoProvedor.ESTORNADO: "refunded",
        StatusNoProvedor.EM_ANDAMENTO: "in_process",
    }
    cobranca = p.cobranca if p else None
    return SituacaoNoProvedor(
        referencia=referencia,
        referencia_externa=str(p.id) if p else None,
        status=status,
        status_provedor=bruto or brutos[status],
        detalhe=detalhe,
        valor=valor or (cobranca.valor if cobranca else None),
    )


def confirmar(p: Pagamento, *, referencia: str = "1", agora: datetime = AGORA) -> None:
    """Aprovacao do provedor pelo valor exato (o caminho feliz do webhook)."""
    resultado = p.aplicar_notificacao(
        situacao(p, referencia=referencia), agora=agora, max_recusas=3
    )
    assert resultado is ResultadoNotificacao.CONFIRMADO
