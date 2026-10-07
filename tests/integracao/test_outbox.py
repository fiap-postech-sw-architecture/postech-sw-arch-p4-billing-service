"""Outbox da mensageria na unidade de trabalho (ADR-036, ADR-043; RFC-004, 5.4).

Envelope com ``causation_id`` e ``ocorrido_em`` do relogio injetado, contexto
W3C capturado na gravacao, a transacao da mensagem (efeito, outbox e
``mensagens_processadas`` comitados juntos pelo consumidor) e ``aberto_por``
para os eventos que o registro emite depois, sem comando.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
from opentelemetry import trace

from src.compartilhado.infraestrutura.mensageria.contratos import (
    MensagemForaDoContratoError,
)
from src.compartilhado.infraestrutura.unit_of_work import (
    MensagemRecebida,
    MongoUnitOfWork,
    UnidadeDaMensagem,
    processar_mensagem,
)
from src.orcamento.dominio.events import GeracaoDeOrcamentoFalhouEvent
from src.orcamento.dominio.orcamento import CanalDecisao
from src.orcamento.infraestrutura.repository import MongoOrcamentoRepository
from tests.factories import AGORA, orcamento
from tests.integracao.apoio import RelogioFixo, eventos_do_outbox

if TYPE_CHECKING:
    from collections.abc import Callable

    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )
    from pymongo.database import Database

    Banco = Database[dict[str, Any]]

_tracer = trace.get_tracer("teste")


def _comando(tipo: str = "GerarOrcamento") -> MensagemRecebida:
    return MensagemRecebida(id=uuid4(), tipo=tipo, correlation_id=uuid4())


def _salvar_no_comando(
    banco: Banco,
    comando: MensagemRecebida,
    salvar: Callable[[UnidadeDaMensagem], None],
    relogio: RelogioFixo | None = None,
) -> None:
    """O que um handler faz: grava o efeito na transacao da mensagem."""

    def handler(uow: UnidadeDaMensagem) -> None:
        uow.executar(lambda: salvar(uow))

    processar_mensagem(banco, comando, handler, relogio=relogio or RelogioFixo())


def _traceparent(span: trace.Span) -> str:
    contexto = span.get_span_context()
    flags = int(contexto.trace_flags)
    return f"00-{contexto.trace_id:032x}-{contexto.span_id:016x}-{flags:02x}"


def _linha(banco: Banco, tipo: str) -> dict[str, Any]:
    linha = banco["outbox"].find_one({"tipo": tipo})
    assert linha is not None
    return linha


class TestDocumentoDaOutbox:
    def test_linha_pronta_para_o_relay(self, banco: Banco) -> None:
        relogio = RelogioFixo(AGORA + timedelta(minutes=3, microseconds=4567))
        uow = MongoUnitOfWork(banco, relogio=relogio)
        gerado = orcamento()
        uow.executar(lambda: MongoOrcamentoRepository(uow).salvar(gerado))

        linha = _linha(banco, "OrcamentoGerado")
        assert linha["exchange"] == "pytstop.eventos"
        assert linha["routing_key"] == "evento.billing.orcamento_gerado"
        assert linha["status"] == "pendente"
        assert linha["tentativas"] == 0
        # Datas do relogio injetado (BSON guarda milissegundos).
        assert linha["criado_em"] == linha["proxima_tentativa_em"]
        assert linha["criado_em"] == relogio.agora.replace(microsecond=4000)
        assert linha["envelope"]["ocorrido_em"] == "2026-10-06T12:03:00.004Z"
        # Sem comando e sem quem abriu o registro: sem causa nem trace.
        assert linha["envelope"]["causation_id"] is None
        assert "traceparent" not in linha

    def test_evento_fora_do_contrato_aborta_a_transacao(self, banco: Banco) -> None:
        uow = MongoUnitOfWork(banco)
        gerado = orcamento()
        fora = GeracaoDeOrcamentoFalhouEvent(
            ordem_id=uuid4(), motivo="x", codigos_invalidos=("com espaco",)
        )

        def trabalho() -> None:
            MongoOrcamentoRepository(uow).salvar(gerado)
            uow.registrar_evento(fora)

        with pytest.raises(MensagemForaDoContratoError, match="codigos_invalidos"):
            uow.executar(trabalho)
        assert banco["orcamentos"].count_documents({}) == 0
        assert eventos_do_outbox(banco) == []


class TestComandoEmProcessamento:
    def test_resposta_tem_o_comando_como_causa_e_o_trace_do_span(
        self, banco: Banco, spans: InMemorySpanExporter
    ) -> None:
        comando = _comando()
        gerado = orcamento()
        with _tracer.start_as_current_span("process GerarOrcamento") as span:
            _salvar_no_comando(
                banco, comando, lambda uow: MongoOrcamentoRepository(uow).salvar(gerado)
            )

        linha = _linha(banco, "OrcamentoGerado")
        assert linha["envelope"]["causation_id"] == str(comando.id)
        assert linha["traceparent"] == _traceparent(span)
        documento = banco["orcamentos"].find_one({"_id": gerado.id})
        assert documento is not None
        assert documento["aberto_por"] == {
            "mensagem_id": comando.id,
            "traceparent": _traceparent(span),
        }

    def test_registra_a_mensagem_na_transacao_do_efeito(self, banco: Banco) -> None:
        comando = _comando()
        relogio = RelogioFixo()
        _salvar_no_comando(
            banco,
            comando,
            lambda uow: MongoOrcamentoRepository(uow).salvar(orcamento()),
            relogio,
        )

        assert list(banco["mensagens_processadas"].find()) == [
            {
                "_id": comando.id,
                "tipo": "GerarOrcamento",
                "correlation_id": comando.correlation_id,
                "processada_em": relogio.agora,
            }
        ]

    @pytest.mark.parametrize(
        "falha",
        ["no-handler", "na-gravacao-final"],
    )
    def test_falha_depois_do_efeito_nao_grava_nada(
        self, banco: Banco, monkeypatch: pytest.MonkeyPatch, falha: str
    ) -> None:
        def defeito(*_args: object) -> None:
            raise RuntimeError("falha depois do efeito")

        def handler(uow: UnidadeDaMensagem) -> None:
            uow.executar(lambda: MongoOrcamentoRepository(uow).salvar(orcamento()))
            if falha == "no-handler":
                defeito()

        if falha == "na-gravacao-final":
            monkeypatch.setattr(
                UnidadeDaMensagem, "_gravar_mensagem_processada", defeito
            )

        with pytest.raises(RuntimeError, match="falha depois do efeito"):
            processar_mensagem(banco, _comando(), handler)

        assert banco["orcamentos"].count_documents({}) == 0
        assert eventos_do_outbox(banco) == []
        assert banco["mensagens_processadas"].count_documents({}) == 0

    def test_comando_sem_efeito_e_registrado(self, banco: Banco) -> None:
        comando = _comando("SolicitarPagamento")
        processar_mensagem(banco, comando, lambda _uow: None)
        assert banco["mensagens_processadas"].find_one({"_id": comando.id})

    def test_reentrega_nao_reescreve_o_registro_da_mensagem(self, banco: Banco) -> None:
        comando = _comando()
        relogio = RelogioFixo()
        processar_mensagem(banco, comando, lambda _uow: None, relogio=relogio)
        primeiro = list(banco["mensagens_processadas"].find())
        relogio.avancar(days=20)

        processar_mensagem(banco, comando, lambda _uow: None, relogio=relogio)

        # $setOnInsert: o prazo do TTL (30 dias) nao recomeca a cada reentrega.
        assert list(banco["mensagens_processadas"].find()) == primeiro

    def test_sem_comando_nada_vai_para_mensagens_processadas(
        self, banco: Banco
    ) -> None:
        uow = MongoUnitOfWork(banco)
        uow.executar(lambda: MongoOrcamentoRepository(uow).salvar(orcamento()))
        assert banco["mensagens_processadas"].count_documents({}) == 0


class TestTransacaoDaMensagem:
    def test_trabalho_que_falha_recomeca_a_transacao_com_retrato_novo(
        self, banco: Banco
    ) -> None:
        gerado = orcamento()
        api = MongoUnitOfWork(banco)
        api.executar(lambda: MongoOrcamentoRepository(api).salvar(gerado))
        vistos: list[str] = []

        def handler(uow: UnidadeDaMensagem) -> None:
            repo = MongoOrcamentoRepository(uow)

            def ler_e_falhar() -> None:
                atual = repo.obter_por_id(gerado.id)
                assert atual is not None
                vistos.append(atual.status.value)
                # Outra escrita comita no meio, como num conflito que o caso
                # de uso trata relendo.
                banco["orcamentos"].update_one(
                    {"_id": gerado.id}, {"$set": {"status": "CANCELADO"}}
                )
                raise LookupError("conflito")

            with pytest.raises(LookupError):
                uow.executar(ler_e_falhar)

            def reler() -> None:
                documento = banco["orcamentos"].find_one(
                    {"_id": gerado.id}, session=uow.sessao
                )
                assert documento is not None
                vistos.append(documento["status"])

            uow.executar(reler)

        processar_mensagem(banco, _comando(), handler)

        assert vistos == ["PENDENTE", "CANCELADO"]

    def test_falha_depois_de_um_trabalho_gravado_e_defeito_e_nada_grava(
        self, banco: Banco
    ) -> None:
        def handler(uow: UnidadeDaMensagem) -> None:
            uow.executar(lambda: MongoOrcamentoRepository(uow).salvar(orcamento()))

            def falha() -> None:
                raise LookupError("segundo trabalho")

            uow.executar(falha)

        with pytest.raises(RuntimeError, match="depois de outro gravado"):
            processar_mensagem(banco, _comando(), handler)

        assert banco["orcamentos"].count_documents({}) == 0
        assert banco["mensagens_processadas"].count_documents({}) == 0


class TestEventoSemComando:
    def test_causa_e_trace_de_quem_abriu_o_registro(
        self, banco: Banco, spans: InMemorySpanExporter
    ) -> None:
        comando = _comando()
        gerado = orcamento()
        with _tracer.start_as_current_span("process GerarOrcamento") as span:
            _salvar_no_comando(
                banco, comando, lambda uow: MongoOrcamentoRepository(uow).salvar(gerado)
            )

        # Decisao do cliente (API sem span), dias depois.
        uow = MongoUnitOfWork(banco)
        repo = MongoOrcamentoRepository(uow)

        def aprovar() -> None:
            atual = repo.obter_por_id(gerado.id)
            assert atual is not None
            atual.aprovar(canal=CanalDecisao.LINK, agora=AGORA + timedelta(hours=1))
            repo.salvar(atual)

        uow.executar(aprovar)

        linha = _linha(banco, "OrcamentoAprovado")
        assert linha["envelope"]["causation_id"] == str(comando.id)
        assert linha["traceparent"] == _traceparent(span)
        # Sem span corrente (API sem instrumentacao), nada para ligar.
        assert "retomado_por" not in linha
        documento = banco["orcamentos"].find_one({"_id": gerado.id})
        assert documento is not None
        assert documento["status"] == "APROVADO"
        # O $setOnInsert nao e reescrito pelas gravacoes seguintes.
        assert documento["aberto_por"]["mensagem_id"] == comando.id

    def test_quem_retomou_o_passo_fica_gravado_para_o_span_link(
        self, banco: Banco, spans: InMemorySpanExporter
    ) -> None:
        comando = _comando()
        gerado = orcamento()
        with _tracer.start_as_current_span("process GerarOrcamento") as saga:
            _salvar_no_comando(
                banco, comando, lambda uow: MongoOrcamentoRepository(uow).salvar(gerado)
            )
        uow = MongoUnitOfWork(banco)
        repo = MongoOrcamentoRepository(uow)

        def aprovar() -> None:
            atual = repo.obter_por_id(gerado.id)
            assert atual is not None
            atual.aprovar(canal=CanalDecisao.LINK, agora=AGORA + timedelta(hours=1))
            repo.salvar(atual)

        # A requisicao que retomou o passo tem trace proprio.
        with _tracer.start_as_current_span("POST decisao") as requisicao:
            uow.executar(aprovar)

        linha = _linha(banco, "OrcamentoAprovado")
        assert linha["traceparent"] == _traceparent(saga)
        assert linha["retomado_por"] == {"traceparent": _traceparent(requisicao)}

    def test_registro_criado_por_outro_comando_guarda_o_primeiro(
        self, banco: Banco
    ) -> None:
        primeiro, segundo = _comando(), _comando("CancelarOrcamento")
        gerado = orcamento()
        _salvar_no_comando(
            banco, primeiro, lambda uow: MongoOrcamentoRepository(uow).salvar(gerado)
        )

        def cancelar(uow: UnidadeDaMensagem) -> None:
            repo = MongoOrcamentoRepository(uow)
            atual = repo.obter_por_id(gerado.id)
            assert atual is not None
            atual.cancelar(motivo="cancelamento")
            repo.salvar(atual)

        _salvar_no_comando(banco, segundo, cancelar)

        linha = _linha(banco, "OrcamentoCancelado")
        assert linha["envelope"]["causation_id"] == str(segundo.id)
        documento = banco["orcamentos"].find_one({"_id": gerado.id})
        assert documento is not None
        assert documento["aberto_por"]["mensagem_id"] == primeiro.id


class TestIndices:
    def test_retencao_por_indice_ttl(self, banco: Banco) -> None:
        outbox = banco["outbox"].index_information()["entregue_em_1"]
        assert outbox["expireAfterSeconds"] == 7 * 24 * 3600
        assert outbox["partialFilterExpression"] == {"status": "entregue"}
        mortas = banco["outbox"].index_information()["morta_em_1"]
        assert mortas["expireAfterSeconds"] == 30 * 24 * 3600
        assert mortas["partialFilterExpression"] == {"status": "dead"}
        processadas = banco["mensagens_processadas"].index_information()
        assert processadas["processada_em_1"]["expireAfterSeconds"] == 30 * 24 * 3600

    def test_indice_do_claim_do_relay(self, banco: Banco) -> None:
        indice = banco["outbox"].index_information()[
            "status_1_proxima_tentativa_em_1__id_1"
        ]
        assert indice["key"] == [
            ("status", 1),
            ("proxima_tentativa_em", 1),
            ("_id", 1),
        ]
