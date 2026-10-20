"""Contexto W3C da mensageria e provider do OpenTelemetry por processo."""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import MagicMock

import pytest
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc import trace_exporter
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider, export
from opentelemetry.sdk.trace.export import (
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.sdk.trace.sampling import ParentBased
from opentelemetry.trace import (
    NonRecordingSpan,
    SpanContext,
    SpanKind,
    Status,
    StatusCode,
    TraceFlags,
)
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from starlette.testclient import TestClient

from src.compartilhado.infraestrutura.mensageria import telemetria
from src.compartilhado.infraestrutura.mensageria.telemetria import (
    instrumentar_clientes,
)
from src.main import criar_app
from tests.integracao.apoio import configuracao, provedor_descartavel
from tests.spans import tudo_que_o_span_leva

TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
# Mesmo trace, com a flag de amostragem desligada.
TRACEPARENT_NAO_AMOSTRADO = TRACEPARENT.removesuffix("01") + "00"


def test_fora_de_span_nao_ha_contexto() -> None:
    assert telemetria.contexto_atual() == {}


def test_contexto_do_span_corrente_e_lido_de_volta(
    spans: InMemorySpanExporter,
) -> None:
    pai = telemetria.contexto_de({"traceparent": TRACEPARENT, "x-tentativa": 2})
    with telemetria.tracer.start_as_current_span("filho", context=pai) as span:
        portador = telemetria.contexto_atual()
    assert portador["traceparent"].startswith("00-4bf92f3577b34da6a3ce929d0e0e4736-")
    [terminado] = spans.get_finished_spans()
    assert terminado.parent is not None
    assert terminado.parent.span_id == 0x00F067AA0BA902B7
    assert span.get_span_context().trace_id == 0x4BF92F3577B34DA6A3CE929D0E0E4736


def test_exportacao_otlp_so_com_otel_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    exportadores: list[dict[str, Any]] = []

    def exportador(**opcoes: Any) -> InMemorySpanExporter:
        exportadores.append(opcoes)
        return InMemorySpanExporter()

    monkeypatch.setattr(trace_exporter, "OTLPSpanExporter", exportador)
    telemetria.criar_provedor({}, processo="relay").shutdown()
    assert exportadores == []
    telemetria.criar_provedor(
        {"OTEL_ENABLED": "true", "OTEL_EXPORTER_OTLP_ENDPOINT": "http://jaeger:4317"},
        processo="relay",
    ).shutdown()
    telemetria.criar_provedor(
        {"OTEL_ENABLED": "1", "OTEL_EXPORTER_OTLP_ENDPOINT": "https://otlp.teste"},
        processo="relay",
    ).shutdown()
    assert exportadores == [
        {"endpoint": "http://jaeger:4317", "insecure": True},
        {"endpoint": "https://otlp.teste", "insecure": False},
    ]


def test_recurso_identifica_servico_versao_e_processo() -> None:
    provedor = telemetria.criar_provedor(
        {"OTEL_SERVICE_NAME": "", "PYTSTOP_GIT_SHA": "0123456789abcdef"},
        processo="consumidor",
    )
    atributos = dict(provedor.resource.attributes)
    provedor.shutdown()
    assert atributos["service.name"] == "billing-service"
    assert atributos["service.version"] == "0123456789ab"
    assert atributos["pytstop.processo"] == "consumidor"


def test_configurar_instala_o_provider_global_e_a_instrumentacao(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    passos: list[object] = []
    monkeypatch.setattr(trace, "set_tracer_provider", passos.append)
    monkeypatch.setattr(
        telemetria, "instalar_propagador", lambda: passos.append("propagador")
    )
    monkeypatch.setattr(telemetria, "instrumentar_clientes", passos.append)
    provedor = telemetria.configurar_telemetria("relay")
    # Na ordem: o PyMongo e o httpx usam o provider que acabou de ser instalado.
    assert passos == [provedor, "propagador", provedor]
    provedor.shutdown()


def test_propagador_global_so_leva_traceparent_e_tracestate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """O padrao do SDK soma o ``baggage`` do chamador, que a API aceitaria e o
    httpx repassaria ao Mercado Pago."""
    instalados: list[object] = []
    monkeypatch.setattr(telemetria, "set_global_textmap", instalados.append)

    telemetria.instalar_propagador()

    [propagador] = instalados
    assert isinstance(propagador, TraceContextTextMapPropagator)
    assert propagador.fields == {"traceparent", "tracestate"}


def test_instrumenta_o_mongodb_sem_capturar_o_comando_e_o_httpx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``capture_statement`` poria o documento (com link, checkout_url e valor)
    no atributo do span."""
    chamadas: list[tuple[str, dict[str, Any]]] = []

    class Instrumentador:
        def __init__(self, nome: str) -> None:
            self.nome = nome

        def instrument(self, **opcoes: Any) -> None:
            chamadas.append((self.nome, opcoes))

    monkeypatch.setattr(telemetria, "PymongoInstrumentor", lambda: Instrumentador("db"))
    monkeypatch.setattr(
        telemetria, "HTTPXClientInstrumentor", lambda: Instrumentador("http")
    )
    provedor = telemetria.criar_provedor({}, processo="api")

    instrumentar_clientes(provedor)

    assert chamadas == [
        ("db", {"tracer_provider": provedor, "capture_statement": False}),
        (
            "http",
            {
                "tracer_provider": provedor,
                "request_hook": telemetria.trocar_url_do_cliente_pelo_template,
            },
        ),
    ]
    provedor.shutdown()


MERCADO_PAGO = "https://api.mercadopago.com"


def span_do_cliente(atributos: dict[str, Any]) -> trace.Span:
    """Span gravando, como o que a instrumentacao do httpx entrega ao hook."""
    return TracerProvider().get_tracer("teste").start_span("GET", attributes=atributos)


def lido(span: trace.Span) -> ReadableSpan:
    assert isinstance(span, ReadableSpan)
    return span


def atributos_do(span: trace.Span) -> dict[str, Any]:
    return dict(lido(span).attributes or {})


@pytest.mark.parametrize("atributo", ["http.url", "url.full"])
@pytest.mark.parametrize(
    ("caminho", "template"),
    [
        ("/checkout/preferences", "/checkout/preferences"),
        ("/checkout/preferences/1234567890-CANARY-ID", "/checkout/preferences/{id}"),
        ("/v1/payments/98765432101", "/v1/payments/{id}"),
        (
            "/v1/payments/search?external_reference=CANARY-ID&sort=a",
            "/v1/payments/search",
        ),
        ("/v1/payments/98765432101/refunds", "/v1/payments/{id}/refunds"),
        ("/v1/payments/12%2F34", "/v1/payments/{id}"),
        ("/v1/payments/98765432101#CANARY-FRAGMENTO", "/v1/payments/{id}"),
    ],
    ids=[
        "criar",
        "cancelar",
        "consultar",
        "buscar",
        "estornar",
        "id-com-barra-codificada",
        "fragmento",
    ],
)
def test_url_do_cliente_sai_com_o_template_da_rota(
    atributo: str, caminho: str, template: str
) -> None:
    """O id da preferencia compoe o link do checkout, o do pagamento e
    credencial (ADR-039) e a busca leva o id do pagamento na query."""
    span = span_do_cliente({"http.method": "GET", atributo: MERCADO_PAGO + caminho})

    telemetria.trocar_url_do_cliente_pelo_template(span, object())

    assert atributos_do(span) == {
        "http.method": "GET",
        atributo: MERCADO_PAGO + template,
    }
    assert not any("CANARY" in t for t in tudo_que_o_span_leva(lido(span)))


@pytest.mark.parametrize(
    "caminho",
    [
        "/v1/payments/98765432101/",
        "/v1/payments/98765432101/refunds/CANARY-ID",
        "/v1/chargebacks/CANARY-ID",
        "/checkout/preferences/CANARY-ID/itens",
        "",
    ],
)
def test_caminho_fora_da_lista_do_gateway_nao_deixa_o_id_no_span(caminho: str) -> None:
    """Uma rota nova do gateway pode trazer um id: sai um valor fixo ate ela
    entrar na lista."""
    span = span_do_cliente({"http.url": f"{MERCADO_PAGO}{caminho}?q=CANARY-QUERY"})

    telemetria.trocar_url_do_cliente_pelo_template(span, object())

    assert atributos_do(span) == {"http.url": f"{MERCADO_PAGO}/nao_roteada"}


@pytest.mark.parametrize("url", ["http://[/v1/payments/CANARY-ID", "http://a]b/x"])
def test_url_malformada_nao_levanta_e_nao_deixa_o_id(url: str) -> None:
    """A instrumentacao do httpx nao protege o hook: levantar derrubaria a
    chamada ao provedor."""
    span = span_do_cliente({"http.url": url})

    telemetria.trocar_url_do_cliente_pelo_template(span, object())

    assert atributos_do(span) == {"http.url": "nao_roteada"}


def test_hook_do_cliente_so_troca_a_url_que_existe_e_e_texto() -> None:
    sem_url = span_do_cliente({"http.method": "GET"})
    nao_texto = span_do_cliente({"http.url": 42})

    telemetria.trocar_url_do_cliente_pelo_template(sem_url, object())
    telemetria.trocar_url_do_cliente_pelo_template(nao_texto, object())

    assert atributos_do(sem_url) == {"http.method": "GET"}
    assert atributos_do(nao_texto) == {"http.url": 42}


def test_hook_do_cliente_ignora_span_que_nao_grava() -> None:
    """Span descartado pelo amostrador (cliente sem pai) e span de outro SDK."""

    class SpanDeOutroSdk(NonRecordingSpan):
        def is_recording(self) -> bool:
            return True

    contexto = SpanContext(1, 1, is_remote=False, trace_flags=TraceFlags(1))
    telemetria.trocar_url_do_cliente_pelo_template(NonRecordingSpan(contexto), object())
    telemetria.trocar_url_do_cliente_pelo_template(SpanDeOutroSdk(contexto), object())


class TestAmostrador:
    """O amostrador do processo: span de cliente sem pai nao vira trace."""

    def test_provider_usa_o_amostrador_de_raiz_sob_o_pai(self) -> None:
        provedor = telemetria.criar_provedor({}, processo="teste")
        assert isinstance(provedor.sampler, ParentBased)
        provedor.shutdown()

    def test_descricao_do_amostrador_diz_o_que_ele_faz(self) -> None:
        """O SDK a mostra ao depurar o provider."""
        descricao = telemetria.AmostradorDeRaiz().get_description()
        assert "descarta span de cliente sem pai" in descricao

    @pytest.fixture
    def tracer(self) -> Any:
        return telemetria.criar_provedor({}, processo="teste").get_tracer("teste")

    def test_cliente_sem_pai_e_descartado(self, tracer: Any) -> None:
        with tracer.start_as_current_span("find", kind=SpanKind.CLIENT) as span:
            assert not span.is_recording()
            assert not span.get_span_context().trace_flags.sampled

    @pytest.mark.parametrize(
        "tipo",
        [SpanKind.SERVER, SpanKind.PRODUCER, SpanKind.CONSUMER, SpanKind.INTERNAL],
    )
    def test_demais_raizes_sao_gravadas(self, tracer: Any, tipo: SpanKind) -> None:
        with tracer.start_as_current_span("trabalho", kind=tipo) as span:
            assert span.is_recording()
            assert span.get_span_context().trace_flags.sampled

    def test_cliente_dentro_de_um_trace_segue_o_pai(self, tracer: Any) -> None:
        with (
            tracer.start_as_current_span("GET /x", kind=SpanKind.SERVER),
            tracer.start_as_current_span("find", kind=SpanKind.CLIENT) as filho,
        ):
            assert filho.is_recording()

    def test_filho_de_um_cliente_descartado_tambem_e_descartado(
        self, tracer: Any
    ) -> None:
        with (
            tracer.start_as_current_span("find", kind=SpanKind.CLIENT),
            tracer.start_as_current_span("filho") as filho,
        ):
            assert not filho.is_recording()

    def test_pai_remoto_amostrado_manda_ate_no_cliente(self, tracer: Any) -> None:
        pai = telemetria.contexto_de({"traceparent": TRACEPARENT})
        with tracer.start_as_current_span(
            "find", context=pai, kind=SpanKind.CLIENT
        ) as span:
            assert span.is_recording()

    def test_pai_remoto_nao_amostrado_descarta_ate_o_server(self, tracer: Any) -> None:
        pai = telemetria.contexto_de({"traceparent": TRACEPARENT_NAO_AMOSTRADO})
        with tracer.start_as_current_span(
            "GET /x", context=pai, kind=SpanKind.SERVER
        ) as span:
            assert not span.is_recording()


def test_tracestate_acima_do_limite_w3c_e_descartado(
    spans: InMemorySpanExporter,
) -> None:
    # 32 membros de 16 caracteres: dentro do limite de membros, acima de 512.
    grande = ",".join(f"k{i:02d}=v{'x' * 11}" for i in range(32))
    assert len(grande) > 512
    curto = "pytstop=abc"
    for estado, esperado in ((grande, ""), (curto, curto)):
        pai = telemetria.contexto_de({"traceparent": TRACEPARENT, "tracestate": estado})
        with telemetria.tracer.start_as_current_span("filho", context=pai):
            portador = telemetria.contexto_atual()
        assert portador.get("tracestate", "") == esperado
        assert portador["traceparent"].startswith(
            "00-4bf92f3577b34da6a3ce929d0e0e4736-"
        )


def test_contexto_de_descarta_o_tracestate_acima_do_limite() -> None:
    """O teto vale na leitura (AMQP e documento da outbox), alem da saida."""
    grande = ",".join(f"k{i:02d}=v{'x' * 11}" for i in range(32))

    def estado(valor: str) -> int:
        contexto = telemetria.contexto_de(
            {"traceparent": TRACEPARENT, "tracestate": valor}
        )
        return len(trace.get_current_span(contexto).get_span_context().trace_state)

    assert estado(grande) == 0
    assert estado("pytstop=abc") == 1


def test_tracestate_longo_que_chega_por_fora_do_contexto_de_nao_sai(
    spans: InMemorySpanExporter,
) -> None:
    """A requisicao HTTP entra pela instrumentacao do FastAPI, que le o header sem
    passar por ``contexto_de``: o teto vale tambem na saida (outbox e AMQP)."""
    grande = ",".join(f"k{i:02d}=v{'x' * 11}" for i in range(32))
    pai = TraceContextTextMapPropagator().extract(
        {"traceparent": TRACEPARENT, "tracestate": grande}
    )
    with telemetria.tracer.start_as_current_span("filho", context=pai):
        portador = telemetria.contexto_atual()
    assert "tracestate" not in portador
    assert portador["traceparent"].startswith("00-4bf92f3577b34da6a3ce929d0e0e4736-")


def test_tracestate_de_exatamente_512_caracteres_e_mantido(
    spans: InMemorySpanExporter,
) -> None:
    # 16 membros de 31 ou 32 caracteres: exatamente o limite do W3C.
    exato = ",".join(f"k{i:02d}={'v' * (27 if i < 15 else 28)}" for i in range(16))
    assert len(exato) == 512
    pai = telemetria.contexto_de({"traceparent": TRACEPARENT, "tracestate": exato})
    with telemetria.tracer.start_as_current_span("filho", context=pai):
        assert telemetria.contexto_atual()["tracestate"] == exato


def test_api_nao_deixa_o_fastapi_montar_o_export_otlp_na_partida(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Com configuracao automatica ligada e endpoint OTLP no ambiente, o FastAPI
    tentaria somar exportadores http/protobuf na partida e, sem o extra
    ``fastapi[opentelemetry]``, avisaria que a configuracao falhou. O
    ``auto_configure`` desligado mantem o log de boot limpo."""
    monkeypatch.setenv("FASTAPI_OTEL_AUTO_CONFIGURE", "true")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:1")
    avisos: list[str] = []

    class Coletor(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            avisos.append(record.getMessage())

    logger = logging.getLogger("fastapi")
    coletor = Coletor(level=logging.WARNING)
    logger.addHandler(coletor)
    try:
        # Banco injetado: o lifespan nao abre conexao.
        with TestClient(
            criar_app(
                configuracao(), banco=MagicMock(), provedor=provedor_descartavel()
            )
        ):
            pass
    finally:
        logger.removeHandler(coletor)

    assert avisos == []


class Gravador(SpanExporter):
    """Exportador que guarda o texto de erro que o span levava NO MOMENTO de
    exportar (o de verdade serializa e manda ao Jaeger nesse instante; guardar o
    span e olhar depois enxergaria o que um processador posterior tirou)."""

    def __init__(self) -> None:
        self.vistos: list[tuple[str | None, list[str]]] = []

    def export(self, spans: Any) -> Any:
        for span in spans:
            mensagens = [
                str((evento.attributes or {}).get("exception.message"))
                for evento in span.events
            ]
            self.vistos.append((span.status.description, mensagens))
        return SpanExportResult.SUCCESS


def provedor_com(exportador: SpanExporter) -> TracerProvider:
    provedor = telemetria.criar_provedor({}, processo="teste")
    provedor.add_span_processor(SimpleSpanProcessor(exportador))
    return provedor


class TestSemTextoDeErro:
    """O texto de uma excecao e o da descricao do status levam o que o erro viu."""

    def test_excecao_perde_a_mensagem_e_o_stack_e_guarda_o_tipo(self) -> None:
        exportador = InMemorySpanExporter()
        tracer = provedor_com(exportador).get_tracer("teste")

        with (
            pytest.raises(ValueError, match="SEGREDO"),
            tracer.start_as_current_span("trabalho"),
        ):
            msg = "SEGREDO cpf 123.456.789-09"
            raise ValueError(msg)

        [span] = exportador.get_finished_spans()
        [evento] = span.events
        assert evento.name == "exception"
        atributos = dict(evento.attributes or {})
        assert set(atributos) == {"exception.type", "exception.escaped"}
        assert atributos["exception.type"] == "ValueError"
        assert span.status.status_code is StatusCode.ERROR
        assert span.status.description is None
        assert [t for t in tudo_que_o_span_leva(span) if "SEGREDO" in t] == []

    def test_descricao_do_status_sai_e_o_codigo_fica(self) -> None:
        exportador = InMemorySpanExporter()
        tracer = provedor_com(exportador).get_tracer("teste")

        with tracer.start_as_current_span("trabalho") as span:
            span.set_status(Status(StatusCode.ERROR, "Bearer APP_USR-SEGREDO"))

        [terminado] = exportador.get_finished_spans()
        assert terminado.status.status_code is StatusCode.ERROR
        assert terminado.status.description is None

    def test_o_que_nao_e_texto_de_erro_nao_e_tocado(self) -> None:
        exportador = InMemorySpanExporter()
        tracer = provedor_com(exportador).get_tracer("teste")

        with tracer.start_as_current_span(
            "trabalho", attributes={"messaging.message.id": "abc"}
        ) as span:
            span.add_event("tentativa", {"numero": 2})
            span.set_status(Status(StatusCode.OK))

        [terminado] = exportador.get_finished_spans()
        assert terminado.status.status_code is StatusCode.OK
        assert dict(terminado.attributes or {}) == {"messaging.message.id": "abc"}
        [evento] = terminado.events
        assert (evento.name, dict(evento.attributes or {})) == (
            "tentativa",
            {"numero": 2},
        )

    def test_so_o_evento_exception_perde_o_texto(self) -> None:
        exportador = InMemorySpanExporter()
        tracer = provedor_com(exportador).get_tracer("teste")

        with tracer.start_as_current_span("trabalho") as span:
            span.add_event("outro", {"exception.message": "fica"})
            span.record_exception(KeyError("sai"))

        [terminado] = exportador.get_finished_spans()
        outro, excecao = terminado.events
        assert dict(outro.attributes or {}) == {"exception.message": "fica"}
        assert "exception.message" not in (excecao.attributes or {})

    def test_o_exportador_do_provider_ja_recebe_o_span_sem_o_texto(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """O processador entra antes do exportador OTLP que o proprio
        ``criar_provedor`` soma: o que sai do processo ja vem sem o texto. O lote
        de producao exporta mais tarde e enxerga o mesmo objeto, entao o teste troca
        o ``BatchSpanProcessor`` por um sincrono, onde a ordem decide."""
        gravador = Gravador()
        monkeypatch.setattr(trace_exporter, "OTLPSpanExporter", lambda **_: gravador)
        monkeypatch.setattr(export, "BatchSpanProcessor", SimpleSpanProcessor)
        provedor = telemetria.criar_provedor({"OTEL_ENABLED": "true"}, processo="api")

        with (
            pytest.raises(ValueError, match="SEGREDO"),
            provedor.get_tracer("teste").start_as_current_span("trabalho"),
        ):
            msg = "SEGREDO"
            raise ValueError(msg)
        provedor.shutdown()

        assert gravador.vistos == [(None, ["None"])]

    def test_marcar_falha_leva_o_nome_da_classe_e_nao_o_texto(self) -> None:
        exportador = InMemorySpanExporter()
        tracer = provedor_com(exportador).get_tracer("teste")

        with tracer.start_as_current_span("trabalho") as span:
            telemetria.marcar_falha(span, ConnectionResetError("SEGREDO do pedido"))

        [terminado] = exportador.get_finished_spans()
        assert terminado.status.status_code is StatusCode.ERROR
        assert terminado.status.description is None
        assert dict(terminado.attributes or {}) == {
            "pytstop.falha": "ConnectionResetError"
        }


class ProvedorEspia(TracerProvider):
    """Provider que conta os ``shutdown``."""

    def __init__(self) -> None:
        super().__init__(shutdown_on_exit=False)
        self.encerrado = 0

    def shutdown(self) -> None:
        self.encerrado += 1
        super().shutdown()


class TestEncerramentoDaApi:
    """Fora do PID 1 (no host) o uvicorn re-levanta o SIGTERM depois do desligamento
    gracioso e o processo morre sem rodar o ``atexit`` do SDK: so o ``shutdown`` do
    lifespan descarrega os spans que ainda esperam no lote. Como PID 1 (o
    container) o ``atexit`` ja rodava."""

    def test_api_encerra_o_provider_que_ela_instalou(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        espia = ProvedorEspia()
        monkeypatch.setattr("src.main.configurar_telemetria", lambda _processo: espia)

        with TestClient(criar_app(configuracao(), banco=MagicMock())):
            assert espia.encerrado == 0

        assert espia.encerrado == 1

    def test_api_nao_encerra_o_provider_que_recebeu(self) -> None:
        """Quem injeta o provider e dono dele (o dos testes serve a toda a sessao)."""
        espia = ProvedorEspia()

        with TestClient(criar_app(configuracao(), banco=MagicMock(), provedor=espia)):
            pass

        assert espia.encerrado == 0
