"""Comandos da saga pelo consumidor, com MongoDB real e o provedor simulado.

Cada resposta leva como ``causation_id`` o id do comando respondido, inclusive
o desfecho republicado para o comando repetido (mesmo id ou id novo com a
mesma chave de negocio); o evento sem comando (decisao do cliente, webhook,
prazo) leva o id do comando que abriu o fluxo (RFC-004, secoes 4.5 e 5.4).
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import pytest
from opentelemetry import trace
from opentelemetry.trace import SpanKind
from prometheus_client import REGISTRY

from src.compartilhado.infraestrutura.mensageria.consumidor import (
    ConsumidorDeComandos,
)
from src.compartilhado.infraestrutura.mensageria.relay import RelayDaOutbox
from src.compartilhado.infraestrutura.unit_of_work import MongoUnitOfWork
from src.consumidor import FILA, criar_handlers
from src.orcamento.aplicacao.use_cases import DecidirOrcamento
from src.orcamento.infraestrutura.repository import MongoOrcamentoRepository
from src.pagamento.aplicacao.ports import (
    GatewayPagamentoIndisponivelError,
    GatewayPagamentoRecusouError,
)
from src.pagamento.aplicacao.use_cases import (
    ProcessarNotificacaoPagamento,
    SimularResultadoPagamento,
)
from src.pagamento.infraestrutura.repository import MongoPagamentoRepository
from src.prazos import executar_ciclo
from src.seed import semear
from tests.factories import ATENDENTE_SUB, dinheiro
from tests.integracao.apoio import (
    LINK,
    CanalDeTeste,
    GatewayRoteirizado,
    MetricasEspia,
    PublicadorFalso,
    RelogioFixo,
    comando,
    configuracao,
    entregar,
    eventos_do_outbox,
    token_do_checkout,
)

if TYPE_CHECKING:
    from opentelemetry.sdk.trace import ReadableSpan
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )
    from pymongo.database import Database

    Banco = Database[dict[str, Any]]

_tracer = trace.get_tracer("teste")

ITENS = [
    {"tipo": "servico", "codigo": "SRV-TROCA-OLEO", "quantidade": 1},
    {"tipo": "peca", "codigo": "PEC-OLEO-5W30", "quantidade": 4},
]


@pytest.fixture
def gateway() -> GatewayRoteirizado:
    return GatewayRoteirizado()


@pytest.fixture
def canal() -> CanalDeTeste:
    return CanalDeTeste()


@pytest.fixture
def consumidor(
    banco: Banco, gateway: GatewayRoteirizado, relogio: RelogioFixo
) -> ConsumidorDeComandos:
    semear(banco)
    handlers = criar_handlers(configuracao().comandos, gateway=gateway, relogio=relogio)
    return ConsumidorDeComandos(
        banco, handlers, fila=FILA, usuario="billing", relogio=relogio
    )


def gerar(ordem_id: UUID, **opcoes: Any) -> dict[str, Any]:
    itens = opcoes.pop("itens", ITENS)
    return comando(
        "GerarOrcamento", {"ordem_id": str(ordem_id), "itens": itens}, **opcoes
    )


def cancelar(ordem_id: UUID, **opcoes: Any) -> dict[str, Any]:
    return comando(
        "CancelarOrcamento",
        {"ordem_id": str(ordem_id), "motivo": "cancelamento"},
        **opcoes,
    )


def solicitar(ordem_id: UUID, orcamento_id: UUID, **opcoes: Any) -> dict[str, Any]:
    dados = {"ordem_id": str(ordem_id), "orcamento_id": str(orcamento_id)}
    return comando("SolicitarPagamento", dados, **opcoes)


def estornar(ordem_id: UUID, **opcoes: Any) -> dict[str, Any]:
    dados = {"ordem_id": str(ordem_id), "motivo": "cancelamento"}
    return comando("EstornarPagamento", dados, **opcoes)


def respostas(banco: Banco) -> list[tuple[str, str | None]]:
    """``(tipo, causation_id)`` de cada evento, na ordem da outbox."""
    return [(e["tipo"], e["causation_id"]) for e in eventos_do_outbox(banco)]


def _orcamento_id(banco: Banco, ordem_id: UUID) -> UUID:
    documento = banco["orcamentos"].find_one({"ordem_id": ordem_id})
    assert documento is not None
    orcamento_id: UUID = documento["_id"]
    return orcamento_id


def _pagamento(banco: Banco, ordem_id: UUID) -> dict[str, Any]:
    documento = banco["pagamentos"].find_one({"ordem_id": ordem_id})
    assert documento is not None
    return documento


def aprovar_pelo_atendente(banco: Banco, relogio: RelogioFixo, ordem_id: UUID) -> None:
    # Decisao na API: sem comando e sem span.
    uow = MongoUnitOfWork(banco, relogio=relogio)
    DecidirOrcamento(uow, MongoOrcamentoRepository(uow), LINK, relogio).por_atendente(
        _orcamento_id(banco, ordem_id), aprovar=True, decidido_por=ATENDENTE_SUB
    )


def _processar(
    banco: Banco, gateway: GatewayRoteirizado, relogio: RelogioFixo, max_recusas: int
) -> ProcessarNotificacaoPagamento:
    uow = MongoUnitOfWork(banco, relogio=relogio)
    return ProcessarNotificacaoPagamento(
        uow,
        MongoPagamentoRepository(uow),
        gateway,
        MetricasEspia(),
        max_recusas,
        relogio,
    )


def pagar_no_checkout(
    banco: Banco,
    gateway: GatewayRoteirizado,
    relogio: RelogioFixo,
    ordem_id: UUID,
    *,
    aprovar: bool = True,
    max_recusas: int = 3,
) -> None:
    pagamento = _pagamento(banco, ordem_id)
    SimularResultadoPagamento(
        gateway,
        MongoPagamentoRepository(MongoUnitOfWork(banco)),
        _processar(banco, gateway, relogio, max_recusas),
        relogio,
    ).executar(
        pagamento["_id"],
        token=token_do_checkout(pagamento["checkout_url"]),
        aprovar=aprovar,
    )


class TestGerarOrcamento:
    def test_responde_orcamento_gerado_com_o_comando_como_causa(
        self, banco: Banco, consumidor: ConsumidorDeComandos, canal: CanalDeTeste
    ) -> None:
        ordem_id = uuid4()
        mensagem = gerar(ordem_id)

        assert entregar(consumidor, canal, mensagem) == "processada"

        assert canal.confirmadas == [1]
        assert respostas(banco) == [("OrcamentoGerado", mensagem["id"])]
        assert banco["mensagens_processadas"].find_one({"_id": UUID(mensagem["id"])})

    def test_mesmo_id_de_novo_republica_sem_outro_orcamento(
        self, banco: Banco, consumidor: ConsumidorDeComandos, canal: CanalDeTeste
    ) -> None:
        mensagem = gerar(uuid4())
        entregar(consumidor, canal, mensagem)

        assert entregar(consumidor, canal, mensagem) == "duplicada"

        assert banco["orcamentos"].count_documents({}) == 1
        assert respostas(banco) == [("OrcamentoGerado", mensagem["id"])] * 2

    def test_reenvio_com_id_novo_responde_com_a_causa_do_reenvio(
        self, banco: Banco, consumidor: ConsumidorDeComandos, canal: CanalDeTeste
    ) -> None:
        ordem_id = uuid4()
        original, reenvio = gerar(ordem_id), gerar(ordem_id)
        entregar(consumidor, canal, original)

        assert entregar(consumidor, canal, reenvio) == "processada"

        assert respostas(banco) == [
            ("OrcamentoGerado", original["id"]),
            ("OrcamentoGerado", reenvio["id"]),
        ]
        documento = banco["orcamentos"].find_one({"ordem_id": ordem_id})
        assert documento is not None
        assert documento["aberto_por"]["mensagem_id"] == UUID(original["id"])

    def test_codigo_sem_preco_responde_a_falha_a_cada_repeticao(
        self, banco: Banco, consumidor: ConsumidorDeComandos, canal: CanalDeTeste
    ) -> None:
        ordem_id = uuid4()
        itens = [{"tipo": "servico", "codigo": "SRV-INEXISTENTE", "quantidade": 1}]
        primeiro, reenvio = gerar(ordem_id, itens=itens), gerar(ordem_id, itens=itens)

        entregar(consumidor, canal, primeiro)
        entregar(consumidor, canal, reenvio)

        assert respostas(banco) == [
            ("GeracaoDeOrcamentoFalhou", primeiro["id"]),
            ("GeracaoDeOrcamentoFalhou", reenvio["id"]),
        ]
        assert banco["orcamentos"].count_documents({}) == 0


class TestCancelarOrcamento:
    def test_compensacao_antes_do_original_grava_lapide_e_descarta_o_original(
        self,
        banco: Banco,
        consumidor: ConsumidorDeComandos,
        canal: CanalDeTeste,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        ordem_id = uuid4()
        compensacao, atrasado = cancelar(ordem_id), gerar(ordem_id)

        assert entregar(consumidor, canal, compensacao) == "processada"
        with caplog.at_level(logging.INFO):
            assert entregar(consumidor, canal, atrasado) == "ignorada"

        [ignorado] = [r for r in caplog.records if r.getMessage() == "command_ignored"]
        assert (ignorado.__dict__["comando"], ignorado.__dict__["motivo"]) == (
            "GerarOrcamento",
            "LAPIDE",
        )
        assert respostas(banco) == [("OrcamentoCancelado", compensacao["id"])]
        [lapide] = banco["orcamentos"].find()
        assert (lapide["status"], lapide["linhas"]) == ("CANCELADO", [])
        # O original descartado tambem fica registrado (sem efeito, com ack).
        assert canal.confirmadas == [1, 2]
        assert banco["mensagens_processadas"].count_documents({}) == 2

    def test_cancela_o_gerado_e_republica_no_reenvio(
        self, banco: Banco, consumidor: ConsumidorDeComandos, canal: CanalDeTeste
    ) -> None:
        ordem_id = uuid4()
        geracao = gerar(ordem_id)
        entregar(consumidor, canal, geracao)
        primeira, reenvio = cancelar(ordem_id), cancelar(ordem_id)

        entregar(consumidor, canal, primeira)
        entregar(consumidor, canal, reenvio)

        assert respostas(banco) == [
            ("OrcamentoGerado", geracao["id"]),
            ("OrcamentoCancelado", primeira["id"]),
            ("OrcamentoCancelado", reenvio["id"]),
        ]

    def test_orcamento_de_outra_ordem_vai_para_a_dlq(
        self, banco: Banco, consumidor: ConsumidorDeComandos, canal: CanalDeTeste
    ) -> None:
        ordem_id = uuid4()
        entregar(consumidor, canal, gerar(ordem_id))
        errado = comando(
            "CancelarOrcamento",
            {
                "ordem_id": str(ordem_id),
                "orcamento_id": str(uuid4()),
                "motivo": "cancelamento",
            },
        )

        assert entregar(consumidor, canal, errado) == "dlq"
        assert canal.rejeitadas == [2]
        assert [tipo for tipo, _ in respostas(banco)] == ["OrcamentoGerado"]


class TestTraducaoDosComandos:
    """Cada campo do ``dados`` chega ao caso de uso, com valores que mudam o
    resultado (a quantidade fixa em 1 ou o motivo constante passavam)."""

    def test_gerar_leva_itens_quantidades_e_validade_ao_orcamento(
        self, banco: Banco, consumidor: ConsumidorDeComandos, canal: CanalDeTeste
    ) -> None:
        entregar(consumidor, canal, gerar(uuid4()))

        [evento] = eventos_do_outbox(banco, "OrcamentoGerado")
        linhas = {
            linha["codigo"]: (linha["quantidade"], linha["subtotal"])
            for linha in evento["dados"]["linhas"]
        }
        assert linhas == {
            "SRV-TROCA-OLEO": (1, "120.00"),
            "PEC-OLEO-5W30": (4, "180.00"),
        }
        assert evento["dados"]["total"] == "300.00"
        # ORCAMENTO_VALIDADE_HORAS (72) a partir do relogio do consumidor.
        assert evento["dados"]["valido_ate"] == "2026-10-09T12:00:00.000Z"

    def test_cancelar_grava_o_motivo_do_comando(
        self, banco: Banco, consumidor: ConsumidorDeComandos, canal: CanalDeTeste
    ) -> None:
        ordem_id = uuid4()
        entregar(consumidor, canal, gerar(ordem_id))
        dados = {"ordem_id": str(ordem_id), "motivo": "orcamento_expirado"}

        entregar(consumidor, canal, comando("CancelarOrcamento", dados))

        documento = banco["orcamentos"].find_one({"ordem_id": ordem_id})
        assert documento is not None
        assert documento["motivo_cancelamento"] == "orcamento_expirado"

    def test_estornar_grava_o_motivo_do_comando(
        self,
        banco: Banco,
        consumidor: ConsumidorDeComandos,
        canal: CanalDeTeste,
        relogio: RelogioFixo,
    ) -> None:
        ordem_id = uuid4()
        entregar(consumidor, canal, gerar(ordem_id))
        aprovar_pelo_atendente(banco, relogio, ordem_id)
        entregar(consumidor, canal, solicitar(ordem_id, _orcamento_id(banco, ordem_id)))
        dados = {"ordem_id": str(ordem_id), "motivo": "pagamento_expirado"}

        entregar(consumidor, canal, comando("EstornarPagamento", dados))

        assert _pagamento(banco, ordem_id)["motivo"] == "pagamento_expirado"

    def test_estornar_pagamento_de_outra_ordem_vai_para_a_dlq(
        self,
        banco: Banco,
        consumidor: ConsumidorDeComandos,
        canal: CanalDeTeste,
        relogio: RelogioFixo,
    ) -> None:
        ordem_id = uuid4()
        entregar(consumidor, canal, gerar(ordem_id))
        aprovar_pelo_atendente(banco, relogio, ordem_id)
        entregar(consumidor, canal, solicitar(ordem_id, _orcamento_id(banco, ordem_id)))
        antes = len(eventos_do_outbox(banco))
        errado = comando(
            "EstornarPagamento",
            {
                "ordem_id": str(ordem_id),
                "pagamento_id": str(uuid4()),
                "motivo": "cancelamento",
            },
        )

        assert entregar(consumidor, canal, errado) == "dlq"

        assert len(eventos_do_outbox(banco)) == antes
        assert _pagamento(banco, ordem_id)["status"] == "SOLICITADO"

    def test_estorno_pelo_comando_conta_na_metrica_de_estornos(
        self,
        banco: Banco,
        consumidor: ConsumidorDeComandos,
        canal: CanalDeTeste,
        gateway: GatewayRoteirizado,
        relogio: RelogioFixo,
    ) -> None:
        ordem_id = uuid4()
        entregar(consumidor, canal, gerar(ordem_id))
        aprovar_pelo_atendente(banco, relogio, ordem_id)
        entregar(consumidor, canal, solicitar(ordem_id, _orcamento_id(banco, ordem_id)))
        pagar_no_checkout(banco, gateway, relogio, ordem_id)
        antes = _estornos("compensacao")

        entregar(consumidor, canal, estornar(ordem_id))

        assert _estornos("compensacao") == antes + 1

    def test_dados_do_comando_nao_vao_para_o_log_nem_para_o_span(
        self,
        consumidor: ConsumidorDeComandos,
        canal: CanalDeTeste,
        caplog: pytest.LogCaptureFixture,
        spans: InMemorySpanExporter,
    ) -> None:
        with caplog.at_level(logging.INFO):
            entregar(consumidor, canal, gerar(uuid4()))

        logs = "\n".join(
            str(r.__dict__) for r in caplog.records if r.name.startswith("src.")
        )
        atributos = "\n".join(
            f"{dict(s.attributes or {})} {s.status.description}"
            for s in spans.get_finished_spans()
        )
        assert "command_consumed" in logs  # o log existe; o dados e que nao
        for codigo in ("SRV-TROCA-OLEO", "PEC-OLEO-5W30"):
            assert codigo not in logs
            assert codigo not in atributos


def _estornos(motivo: str) -> float:
    valor = REGISTRY.get_sample_value(
        "pytstop_pagamentos_estornados_total", {"motivo": motivo}
    )
    return valor or 0.0


class TestConcorrencia:
    """Dois consumidores com a mesma mensagem (ou a mesma ordem) ao mesmo tempo:
    o indice unico por ordem decide dentro da transacao e os dois dao ack."""

    def _em_paralelo(
        self,
        banco: Banco,
        gateway: GatewayRoteirizado,
        relogio: RelogioFixo,
        envelopes: list[dict[str, Any]],
    ) -> list[tuple[str, CanalDeTeste]]:
        handlers = criar_handlers(
            configuracao().comandos, gateway=gateway, relogio=relogio
        )
        largada = threading.Barrier(len(envelopes))
        canais = [CanalDeTeste() for _ in envelopes]
        resultados: list[str] = ["" for _ in envelopes]

        def trabalhar(indice: int) -> None:
            consumidor = ConsumidorDeComandos(
                banco, handlers, fila=FILA, usuario="billing", relogio=relogio
            )
            largada.wait(timeout=10)
            try:
                resultados[indice] = entregar(
                    consumidor, canais[indice], envelopes[indice]
                )
            finally:
                consumidor.fechar()

        threads = [
            threading.Thread(target=trabalhar, args=(i,)) for i in range(len(envelopes))
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        return list(zip(resultados, canais, strict=True))

    @pytest.mark.parametrize("repeticao", range(5))
    def test_mesmo_gerar_em_dois_consumidores_gera_um_orcamento(
        self,
        banco: Banco,
        consumidor: ConsumidorDeComandos,
        gateway: GatewayRoteirizado,
        relogio: RelogioFixo,
        repeticao: int,
    ) -> None:
        mensagem = gerar(uuid4())

        resultados = self._em_paralelo(banco, gateway, relogio, [mensagem, mensagem])

        assert {r for r, _ in resultados} <= {"processada", "duplicada"}
        assert [c.confirmadas for _, c in resultados] == [[1], [1]]
        assert banco["orcamentos"].count_documents({}) == 1
        assert banco["mensagens_processadas"].count_documents({}) == 1

    @pytest.mark.parametrize("repeticao", range(5))
    def test_ids_novos_da_mesma_ordem_respondem_cada_um_com_a_propria_causa(
        self,
        banco: Banco,
        consumidor: ConsumidorDeComandos,
        gateway: GatewayRoteirizado,
        relogio: RelogioFixo,
        repeticao: int,
    ) -> None:
        ordem_id = uuid4()
        original, reenvio = gerar(ordem_id), gerar(ordem_id)

        resultados = self._em_paralelo(banco, gateway, relogio, [original, reenvio])

        assert [r for r, _ in resultados] == ["processada", "processada"]
        assert banco["orcamentos"].count_documents({}) == 1
        assert {causa for _, causa in respostas(banco)} == {
            original["id"],
            reenvio["id"],
        }
        assert len(respostas(banco)) == 2

    @pytest.mark.parametrize("repeticao", range(5))
    def test_mesmo_solicitar_em_dois_consumidores_grava_um_pagamento(
        self,
        banco: Banco,
        consumidor: ConsumidorDeComandos,
        canal: CanalDeTeste,
        gateway: GatewayRoteirizado,
        relogio: RelogioFixo,
        repeticao: int,
    ) -> None:
        ordem_id = uuid4()
        entregar(consumidor, canal, gerar(ordem_id))
        aprovar_pelo_atendente(banco, relogio, ordem_id)
        pedido = solicitar(ordem_id, _orcamento_id(banco, ordem_id))

        resultados = self._em_paralelo(banco, gateway, relogio, [pedido, pedido])

        assert {r for r, _ in resultados} <= {"processada", "duplicada"}
        assert [c.confirmadas for _, c in resultados] == [[1], [1]]
        assert banco["pagamentos"].count_documents({"ordem_id": ordem_id}) == 1


class TestEventosDoOrcamentoSemComando:
    def test_decisao_do_cliente_tem_como_causa_o_gerar_orcamento(
        self,
        banco: Banco,
        consumidor: ConsumidorDeComandos,
        canal: CanalDeTeste,
        relogio: RelogioFixo,
    ) -> None:
        ordem_id = uuid4()
        geracao = gerar(ordem_id)
        entregar(consumidor, canal, geracao)

        aprovar_pelo_atendente(banco, relogio, ordem_id)

        assert respostas(banco)[-1] == ("OrcamentoAprovado", geracao["id"])

    def test_expiracao_tem_como_causa_o_gerar_orcamento(
        self,
        banco: Banco,
        consumidor: ConsumidorDeComandos,
        canal: CanalDeTeste,
        relogio: RelogioFixo,
    ) -> None:
        ordem_id = uuid4()
        geracao = gerar(ordem_id)
        entregar(consumidor, canal, geracao)
        relogio.avancar(hours=73)

        executar_ciclo(
            banco,
            gateway=None,
            metricas=MetricasEspia(),
            max_recusas=3,
            relogio=relogio,
        )

        assert respostas(banco)[-1] == ("OrcamentoExpirado", geracao["id"])


class TestTraceDosPassosRetomados:
    """O evento de um passo retomado por pessoa ou prazo sai no trace da saga,
    com span link para o trace de quem o retomou (ADR-043)."""

    def _publicar(self, banco: Banco, relogio: RelogioFixo) -> None:
        RelayDaOutbox(
            banco, PublicadorFalso(), usuario="billing", relogio=relogio
        ).entregar_pendentes(10)

    def _spans_da_saga(
        self, spans: InMemorySpanExporter, evento: str
    ) -> tuple[ReadableSpan, ReadableSpan]:
        terminados = spans.get_finished_spans()
        consumo = next(s for s in terminados if s.name == "process GerarOrcamento")
        publicacao = next(s for s in terminados if s.name == f"publish {evento}")
        return consumo, publicacao

    def test_decisao_pela_api_sai_na_saga_com_link_para_a_requisicao(
        self,
        banco: Banco,
        consumidor: ConsumidorDeComandos,
        canal: CanalDeTeste,
        relogio: RelogioFixo,
        spans: InMemorySpanExporter,
    ) -> None:
        ordem_id = uuid4()
        entregar(consumidor, canal, gerar(ordem_id))
        # A instrumentacao do FastAPI abre o span da requisicao (aqui, a mao).
        with _tracer.start_as_current_span(
            "POST /api/v1/orcamentos/{id}/decisao", kind=SpanKind.SERVER
        ) as requisicao:
            aprovar_pelo_atendente(banco, relogio, ordem_id)

        self._publicar(banco, relogio)

        consumo, publicacao = self._spans_da_saga(spans, "OrcamentoAprovado")
        assert publicacao.context.trace_id == consumo.context.trace_id
        [link] = publicacao.links
        assert link.context.trace_id == requisicao.get_span_context().trace_id

    def test_expiracao_pelo_prazos_sai_na_saga_com_link_para_o_ciclo(
        self,
        banco: Banco,
        consumidor: ConsumidorDeComandos,
        canal: CanalDeTeste,
        relogio: RelogioFixo,
        spans: InMemorySpanExporter,
    ) -> None:
        entregar(consumidor, canal, gerar(uuid4()))
        relogio.avancar(hours=73)

        executar_ciclo(
            banco,
            gateway=None,
            metricas=MetricasEspia(),
            max_recusas=3,
            relogio=relogio,
        )
        self._publicar(banco, relogio)

        consumo, publicacao = self._spans_da_saga(spans, "OrcamentoExpirado")
        [ciclo] = [s for s in spans.get_finished_spans() if s.name == "prazos"]
        assert publicacao.context.trace_id == consumo.context.trace_id
        assert ciclo.context.trace_id != consumo.context.trace_id
        [link] = publicacao.links
        assert link.context.trace_id == ciclo.context.trace_id


class TestSolicitarPagamento:
    def _orcamento_aprovado(
        self,
        banco: Banco,
        consumidor: ConsumidorDeComandos,
        canal: CanalDeTeste,
        relogio: RelogioFixo,
    ) -> tuple[UUID, UUID]:
        ordem_id = uuid4()
        entregar(consumidor, canal, gerar(ordem_id))
        aprovar_pelo_atendente(banco, relogio, ordem_id)
        return ordem_id, _orcamento_id(banco, ordem_id)

    def test_responde_pagamento_solicitado_e_republica_no_reenvio(
        self,
        banco: Banco,
        consumidor: ConsumidorDeComandos,
        canal: CanalDeTeste,
        gateway: GatewayRoteirizado,
        relogio: RelogioFixo,
    ) -> None:
        ordem_id, orcamento_id = self._orcamento_aprovado(
            banco, consumidor, canal, relogio
        )
        original, reenvio = (
            solicitar(ordem_id, orcamento_id),
            solicitar(ordem_id, orcamento_id),
        )

        assert entregar(consumidor, canal, original) == "processada"
        assert entregar(consumidor, canal, reenvio) == "processada"
        assert entregar(consumidor, canal, reenvio) == "duplicada"

        assert respostas(banco)[-3:] == [
            ("PagamentoSolicitado", original["id"]),
            ("PagamentoSolicitado", reenvio["id"]),
            ("PagamentoSolicitado", reenvio["id"]),
        ]
        assert len(gateway.cobrancas) == 1
        assert _pagamento(banco, ordem_id)["aberto_por"]["mensagem_id"] == UUID(
            original["id"]
        )

    def test_orcamento_nao_aprovado_e_ignorado_sem_cobranca_nem_resposta(
        self,
        banco: Banco,
        consumidor: ConsumidorDeComandos,
        canal: CanalDeTeste,
        gateway: GatewayRoteirizado,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        ordem_id = uuid4()
        entregar(consumidor, canal, gerar(ordem_id))
        mensagem = solicitar(ordem_id, _orcamento_id(banco, ordem_id))

        # Descompasso de estado: ack e log com o codigo, nunca a DLQ.
        with caplog.at_level(logging.INFO):
            assert entregar(consumidor, canal, mensagem) == "ignorada"

        assert (canal.confirmadas, canal.rejeitadas) == ([1, 2], [])
        assert gateway.cobrancas == []
        assert [tipo for tipo, _ in respostas(banco)] == ["OrcamentoGerado"]
        assert banco["mensagens_processadas"].find_one({"_id": UUID(mensagem["id"])})
        [ignorado] = [r for r in caplog.records if r.getMessage() == "command_ignored"]
        assert (ignorado.__dict__["comando"], ignorado.__dict__["motivo"]) == (
            "SolicitarPagamento",
            "ORCAMENTO_NAO_APROVADO",
        )

    def test_orcamento_de_outra_ordem_vai_para_a_dlq_sem_cobranca(
        self,
        banco: Banco,
        consumidor: ConsumidorDeComandos,
        canal: CanalDeTeste,
        gateway: GatewayRoteirizado,
        relogio: RelogioFixo,
    ) -> None:
        ordem_id, outra = uuid4(), uuid4()
        entregar(consumidor, canal, gerar(outra))
        aprovar_pelo_atendente(banco, relogio, outra)
        mensagem = solicitar(ordem_id, _orcamento_id(banco, outra))

        # Falha permanente sem evento de falha no contrato: DLQ, com alerta.
        assert entregar(consumidor, canal, mensagem) == "dlq"

        assert canal.rejeitadas == [2]
        assert gateway.cobrancas == []
        assert (
            banco["mensagens_processadas"].find_one({"_id": UUID(mensagem["id"])})
            is None
        )

    def test_provedor_fora_vai_para_a_fila_de_retry(
        self,
        banco: Banco,
        consumidor: ConsumidorDeComandos,
        canal: CanalDeTeste,
        gateway: GatewayRoteirizado,
        relogio: RelogioFixo,
    ) -> None:
        ordem_id, orcamento_id = self._orcamento_aprovado(
            banco, consumidor, canal, relogio
        )
        gateway.erro_na_cobranca = GatewayPagamentoIndisponivelError()

        assert entregar(consumidor, canal, solicitar(ordem_id, orcamento_id)) == "retry"

        [(exchange, routing_key, _, copia)] = canal.publicadas
        assert (exchange, routing_key) == ("pytstop.retry", "billing.comandos.retry.1s")
        assert copia.headers["x-tentativa"] == 1
        assert banco["pagamentos"].count_documents({}) == 0

    def test_pagamento_e_recusa_tem_como_causa_o_solicitar_pagamento(
        self,
        banco: Banco,
        consumidor: ConsumidorDeComandos,
        canal: CanalDeTeste,
        gateway: GatewayRoteirizado,
        relogio: RelogioFixo,
    ) -> None:
        aprovado, orcamento_aprovado = self._orcamento_aprovado(
            banco, consumidor, canal, relogio
        )
        recusado, orcamento_recusado = self._orcamento_aprovado(
            banco, consumidor, canal, relogio
        )
        pedido_aprovado = solicitar(aprovado, orcamento_aprovado)
        pedido_recusado = solicitar(recusado, orcamento_recusado)
        entregar(consumidor, canal, pedido_aprovado)
        entregar(consumidor, canal, pedido_recusado)

        pagar_no_checkout(banco, gateway, relogio, aprovado)
        pagar_no_checkout(
            banco, gateway, relogio, recusado, aprovar=False, max_recusas=1
        )

        assert respostas(banco)[-2:] == [
            ("PagamentoConfirmado", pedido_aprovado["id"]),
            ("PagamentoRecusado", pedido_recusado["id"]),
        ]

    def test_expiracao_tem_como_causa_o_solicitar_pagamento(
        self,
        banco: Banco,
        consumidor: ConsumidorDeComandos,
        canal: CanalDeTeste,
        relogio: RelogioFixo,
    ) -> None:
        ordem_id, orcamento_id = self._orcamento_aprovado(
            banco, consumidor, canal, relogio
        )
        pedido = solicitar(ordem_id, orcamento_id)
        entregar(consumidor, canal, pedido)
        relogio.avancar(minutes=61)

        executar_ciclo(
            banco,
            gateway=None,
            metricas=MetricasEspia(),
            max_recusas=3,
            relogio=relogio,
        )

        assert respostas(banco)[-1] == ("PagamentoExpirado", pedido["id"])


class TestEstornarPagamento:
    def _pagamento_solicitado(
        self,
        banco: Banco,
        consumidor: ConsumidorDeComandos,
        canal: CanalDeTeste,
        relogio: RelogioFixo,
    ) -> tuple[UUID, dict[str, Any]]:
        ordem_id = uuid4()
        entregar(consumidor, canal, gerar(ordem_id))
        aprovar_pelo_atendente(banco, relogio, ordem_id)
        pedido = solicitar(ordem_id, _orcamento_id(banco, ordem_id))
        entregar(consumidor, canal, pedido)
        return ordem_id, pedido

    def test_compensacao_antes_do_pedido_grava_lapide_e_descarta_o_pedido(
        self,
        banco: Banco,
        consumidor: ConsumidorDeComandos,
        canal: CanalDeTeste,
        gateway: GatewayRoteirizado,
        relogio: RelogioFixo,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        ordem_id = uuid4()
        entregar(consumidor, canal, gerar(ordem_id))
        aprovar_pelo_atendente(banco, relogio, ordem_id)
        compensacao = estornar(ordem_id)

        assert entregar(consumidor, canal, compensacao) == "processada"
        atrasado = solicitar(ordem_id, _orcamento_id(banco, ordem_id))
        with caplog.at_level(logging.INFO):
            assert entregar(consumidor, canal, atrasado) == "ignorada"

        [ignorado] = [r for r in caplog.records if r.getMessage() == "command_ignored"]
        assert (ignorado.__dict__["comando"], ignorado.__dict__["motivo"]) == (
            "SolicitarPagamento",
            "LAPIDE",
        )
        assert respostas(banco)[-1] == ("PagamentoCancelado", compensacao["id"])
        assert gateway.cobrancas == []
        assert _pagamento(banco, ordem_id)["status"] == "CANCELADO"
        # Descartado sem abrir transacao, o pedido fica registrado mesmo assim.
        assert banco["mensagens_processadas"].find_one({"_id": UUID(atrasado["id"])})

    def test_cobranca_aberta_e_cancelada_e_republicada_no_reenvio(
        self,
        banco: Banco,
        consumidor: ConsumidorDeComandos,
        canal: CanalDeTeste,
        relogio: RelogioFixo,
    ) -> None:
        ordem_id, _ = self._pagamento_solicitado(banco, consumidor, canal, relogio)
        primeira, reenvio = estornar(ordem_id), estornar(ordem_id)

        entregar(consumidor, canal, primeira)
        entregar(consumidor, canal, reenvio)

        assert respostas(banco)[-2:] == [
            ("PagamentoCancelado", primeira["id"]),
            ("PagamentoCancelado", reenvio["id"]),
        ]

    def test_confirmado_e_estornado_e_republicado_no_reenvio(
        self,
        banco: Banco,
        consumidor: ConsumidorDeComandos,
        canal: CanalDeTeste,
        gateway: GatewayRoteirizado,
        relogio: RelogioFixo,
    ) -> None:
        ordem_id, _ = self._pagamento_solicitado(banco, consumidor, canal, relogio)
        pagar_no_checkout(banco, gateway, relogio, ordem_id)
        primeira, reenvio = estornar(ordem_id), estornar(ordem_id)

        entregar(consumidor, canal, primeira)
        entregar(consumidor, canal, reenvio)

        estornados = eventos_do_outbox(banco, "PagamentoEstornado")
        assert [(e["causation_id"], e["dados"]["motivo"]) for e in estornados] == [
            (primeira["id"], "compensacao"),
            (reenvio["id"], "compensacao"),
        ]
        assert len(gateway.estornos) == 1

    def test_estorno_automatico_e_resposta_ao_comando_tem_causas_diferentes(
        self,
        banco: Banco,
        consumidor: ConsumidorDeComandos,
        canal: CanalDeTeste,
        gateway: GatewayRoteirizado,
        relogio: RelogioFixo,
    ) -> None:
        ordem_id, pedido = self._pagamento_solicitado(banco, consumidor, canal, relogio)
        relogio.avancar(minutes=61)
        executar_ciclo(
            banco,
            gateway=None,
            metricas=MetricasEspia(),
            max_recusas=3,
            relogio=relogio,
        )
        pagamento = _pagamento(banco, ordem_id)
        # O cliente paga no checkout ainda aberto no provedor: aprovacao tardia.
        referencia = gateway.registrar_resultado(
            pagamento_id=pagamento["_id"], valor=dinheiro("300.00"), aprovado=True
        )
        _processar(banco, gateway, relogio, 3).executar(referencia)
        compensacao = estornar(ordem_id)

        entregar(consumidor, canal, compensacao)

        estornados = eventos_do_outbox(banco, "PagamentoEstornado")
        assert [(e["causation_id"], e["dados"]["motivo"]) for e in estornados] == [
            (pedido["id"], "pagamento_apos_encerramento"),
            (compensacao["id"], "pagamento_apos_encerramento"),
        ]

    def test_estorno_recusado_responde_a_falha(
        self,
        banco: Banco,
        consumidor: ConsumidorDeComandos,
        canal: CanalDeTeste,
        gateway: GatewayRoteirizado,
        relogio: RelogioFixo,
    ) -> None:
        ordem_id, _ = self._pagamento_solicitado(banco, consumidor, canal, relogio)
        pagar_no_checkout(banco, gateway, relogio, ordem_id)
        gateway.erro_no_estorno = GatewayPagamentoRecusouError("Saldo insuficiente")
        compensacao = estornar(ordem_id)

        assert entregar(consumidor, canal, compensacao) == "processada"

        assert respostas(banco)[-1] == ("EstornoDePagamentoFalhou", compensacao["id"])
        assert _pagamento(banco, ordem_id)["status"] == "CONFIRMADO"
