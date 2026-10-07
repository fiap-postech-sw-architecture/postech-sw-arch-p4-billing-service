"""Processo ``prazos``: conciliacao, expiracao, laco, sinais e boot."""

from __future__ import annotations

import logging
import signal
import threading
from datetime import timedelta
from functools import partial
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
from opentelemetry.sdk.trace import TracerProvider
from prometheus_client import REGISTRY

from src import prazos
from src.banco import preparar_banco
from src.compartilhado.infraestrutura.mongo import BancoNaoPreparadoError
from src.compartilhado.infraestrutura.processo import instalar_sinais
from src.compartilhado.infraestrutura.unit_of_work import MongoUnitOfWork
from src.configuracao import ConfiguracaoDosPrazos
from src.orcamento.infraestrutura.repository import MongoOrcamentoRepository
from src.pagamento.aplicacao.ports import (
    GatewayPagamentoIndisponivelError,
    GatewayPagamentoRecusouError,
)
from src.pagamento.aplicacao.use_cases import (
    ConciliarPagamentos,
    ProcessarNotificacaoPagamento,
)
from src.pagamento.infraestrutura.gateway import criar_gateway_de_conciliacao
from src.pagamento.infraestrutura.mercadopago import MercadoPagoGateway
from src.pagamento.infraestrutura.repository import MongoPagamentoRepository
from src.prazos import ResultadoDoCiclo, executar_ciclo, rodar
from tests.factories import AGORA, orcamento, pagamento
from tests.integracao.apoio import (
    GatewayRoteirizado,
    MetricasEspia,
    RelogioFixo,
    eventos_do_outbox,
)

if TYPE_CHECKING:
    from pathlib import Path

    from pymongo import MongoClient
    from pymongo.database import Database

    from src.pagamento.dominio.pagamento import Pagamento

    Banco = Database[dict[str, Any]]


class EsperaEspia(threading.Event):
    """Evento que registra cada espera do laco sem dormir de verdade.

    A ``espera_final``-esima espera marca a parada, e o laco sai: o ciclo e o
    intervalo do laco aparecem em ``esperas`` sem depender do relogio.
    """

    def __init__(self, espera_final: int) -> None:
        super().__init__()
        self.esperas: list[float | None] = []
        self._espera_final = espera_final

    def wait(self, timeout: float | None = None) -> bool:
        self.esperas.append(timeout)
        if len(self.esperas) == self._espera_final:
            self.set()
        return self.is_set()


def salvar(banco: Banco, *pagamentos: Pagamento) -> None:
    uow = MongoUnitOfWork(banco)
    repo = MongoPagamentoRepository(uow)
    for p in pagamentos:
        uow.executar(partial(repo.salvar, p))


def status(banco: Banco, p: Pagamento) -> str:
    documento = banco["pagamentos"].find_one({"_id": p.id})
    assert documento is not None
    return str(documento["status"])


def ciclo(banco: Banco, gateway: GatewayRoteirizado | None, agora: Any) -> Any:
    return executar_ciclo(
        banco,
        gateway=gateway,
        metricas=MetricasEspia(),
        max_recusas=3,
        relogio=RelogioFixo(agora),
    )


class TestCiclo:
    def test_expira_orcamentos_e_pagamentos_vencidos(self, banco: Banco) -> None:
        o = orcamento(validade=timedelta(hours=1))
        uow = MongoUnitOfWork(banco)
        uow.executar(lambda: MongoOrcamentoRepository(uow).salvar(o))
        salvar(banco, pagamento(validade=timedelta(minutes=10)))

        assert ciclo(banco, None, AGORA) == ResultadoDoCiclo(0, 0, 0)
        depois = AGORA + timedelta(hours=2)
        assert ciclo(banco, None, depois) == ResultadoDoCiclo(0, 1, 1)
        assert ciclo(banco, None, depois) == ResultadoDoCiclo(0, 0, 0)

        tipos = {e["tipo"] for e in eventos_do_outbox(banco)}
        assert {"OrcamentoExpirado", "PagamentoExpirado"} <= tipos

    def test_concilia_antes_de_expirar(self, banco: Banco) -> None:
        """Webhook perdido: o pagamento aprovado no prazo nao vence."""
        pago_sem_webhook = pagamento(validade=timedelta(minutes=10))
        abandonado = pagamento(validade=timedelta(minutes=10))
        salvar(banco, pago_sem_webhook, abandonado)
        gateway = GatewayRoteirizado()
        gateway.registrar_resultado(
            pagamento_id=pago_sem_webhook.id,
            valor=pago_sem_webhook.cobranca.valor,  # type: ignore[union-attr]
            aprovado=True,
        )

        resultado = ciclo(banco, gateway, AGORA + timedelta(hours=1))

        assert resultado == ResultadoDoCiclo(2, 0, 1)
        assert status(banco, pago_sem_webhook) == "CONFIRMADO"
        assert status(banco, abandonado) == "EXPIRADO"

    def test_lote_cheio_e_sinalizado(self) -> None:
        limite = prazos.LIMITE_POR_CICLO
        assert ResultadoDoCiclo(0, limite, 0).lote_cheio
        assert ResultadoDoCiclo(0, 0, limite).lote_cheio
        assert ResultadoDoCiclo(0, 0, 2, limite=2).lote_cheio
        assert not ResultadoDoCiclo(1, 2, 3).lote_cheio
        # Conciliacao cheia nao repete: consultar nao tira o pagamento da lista.
        assert not ResultadoDoCiclo(limite, 0, 0).lote_cheio

    def test_conciliacao_no_limite_espera_o_proximo_ciclo(self, banco: Banco) -> None:
        salvar(banco, pagamento(), pagamento())
        resultado = executar_ciclo(
            banco,
            gateway=GatewayRoteirizado(),
            metricas=MetricasEspia(),
            max_recusas=3,
            relogio=RelogioFixo(AGORA),
            limite=1,
        )
        assert resultado == ResultadoDoCiclo(1, 0, 0)
        assert not resultado.lote_cheio

    def test_expiracao_no_limite_repete_sem_esperar(self, banco: Banco) -> None:
        salvar(banco, pagamento(validade=timedelta(minutes=10)), pagamento())
        resultado = executar_ciclo(
            banco,
            gateway=None,
            metricas=MetricasEspia(),
            max_recusas=3,
            relogio=RelogioFixo(AGORA + timedelta(hours=2)),
            limite=1,
        )
        assert resultado == ResultadoDoCiclo(0, 0, 1)
        assert resultado.lote_cheio


class TestConciliacao:
    def processar(
        self, banco: Banco, gateway: GatewayRoteirizado, max_recusas: int = 3
    ) -> ConciliarPagamentos:
        uow = MongoUnitOfWork(banco)
        repo = MongoPagamentoRepository(uow)
        return ConciliarPagamentos(
            repo,
            gateway,
            ProcessarNotificacaoPagamento(
                uow, repo, gateway, MetricasEspia(), max_recusas, RelogioFixo()
            ),
        )

    def test_tentativas_do_provedor_passam_pelo_caso_de_uso_do_webhook(
        self, banco: Banco
    ) -> None:
        p = pagamento()
        salvar(banco, p)
        gateway = GatewayRoteirizado()
        for _ in range(2):
            gateway.registrar_resultado(
                pagamento_id=p.id,
                valor=p.cobranca.valor,  # type: ignore[union-attr]
                aprovado=False,
            )

        assert self.processar(banco, gateway, max_recusas=2).executar() == 1

        assert status(banco, p) == "RECUSADO"
        assert len(eventos_do_outbox(banco, "PagamentoRecusado")) == 1
        # Ja encerrado: nao e consultado de novo.
        assert self.processar(banco, gateway).executar() == 0

    def test_provedor_fora_pausa_o_ciclo(
        self, banco: Banco, caplog: pytest.LogCaptureFixture
    ) -> None:
        salvar(banco, pagamento(), pagamento())

        class ForaDoAr(GatewayRoteirizado):
            def __init__(self) -> None:
                super().__init__()
                self.buscas = 0

            def buscar_por_referencia_externa(self, referencia_externa: str) -> Any:
                self.buscas += 1
                raise GatewayPagamentoIndisponivelError

        gateway = ForaDoAr()
        with caplog.at_level(logging.WARNING):
            assert self.processar(banco, gateway).executar() == 0
        assert gateway.buscas == 1
        assert "payment_reconciliation_paused" in caplog.messages

    def test_recusa_do_provedor_na_busca_para_o_ciclo_com_um_log(
        self, banco: Banco, caplog: pytest.LogCaptureFixture
    ) -> None:
        salvar(banco, pagamento(), pagamento(), pagamento())

        class TokenRevogado(GatewayRoteirizado):
            def __init__(self) -> None:
                super().__init__()
                self.buscas = 0

            def buscar_por_referencia_externa(self, referencia_externa: str) -> Any:
                self.buscas += 1
                msg = "Mercado Pago respondeu 401: invalid access token"
                raise GatewayPagamentoRecusouError(msg)

        gateway = TokenRevogado()
        with caplog.at_level(logging.ERROR):
            assert self.processar(banco, gateway).executar() == 0
        assert gateway.buscas == 1
        assert caplog.messages.count("payment_reconciliation_refused") == 1

    def test_pagamento_com_defeito_nao_trava_os_demais(
        self, banco: Banco, caplog: pytest.LogCaptureFixture
    ) -> None:
        defeituoso, saudavel = pagamento(), pagamento()
        salvar(banco, defeituoso, saudavel)
        gateway = GatewayRoteirizado()
        gateway.registrar_resultado(
            pagamento_id=saudavel.id,
            valor=saudavel.cobranca.valor,  # type: ignore[union-attr]
            aprovado=True,
        )
        original = gateway.buscar_por_referencia_externa

        def buscar(referencia_externa: str) -> Any:
            if referencia_externa == str(defeituoso.id):
                msg = "resposta com defeito"
                raise RuntimeError(msg)
            return original(referencia_externa)

        gateway.buscar_por_referencia_externa = buscar  # type: ignore[method-assign]
        with caplog.at_level(logging.ERROR):
            assert self.processar(banco, gateway).executar() == 2
        assert "payment_reconciliation_failed" in caplog.messages
        assert status(banco, saudavel) == "CONFIRMADO"


class TestLaco:
    def test_roda_ate_o_sinal_toca_o_heartbeat_e_marca_o_ciclo(
        self, tmp_path: Path
    ) -> None:
        parar = threading.Event()
        heartbeat = tmp_path / "heartbeat"
        ciclos: list[int] = []

        def um_ciclo() -> ResultadoDoCiclo:
            ciclos.append(1)
            if len(ciclos) == 3:
                parar.set()
            return ResultadoDoCiclo(0, 1, 0)

        antes = REGISTRY.get_sample_value(
            "pytstop_prazos_ultimo_ciclo_timestamp_seconds"
        )
        rodar(um_ciclo, intervalo=0.01, parar=parar, heartbeat=heartbeat)

        assert len(ciclos) == 3
        assert heartbeat.exists()
        depois = REGISTRY.get_sample_value(
            "pytstop_prazos_ultimo_ciclo_timestamp_seconds"
        )
        assert depois is not None
        assert depois > (antes or 0)

    def test_falha_no_ciclo_vai_para_o_log_e_o_laco_segue(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        parar = threading.Event()
        tentativas: list[int] = []

        def falhar() -> ResultadoDoCiclo:
            tentativas.append(1)
            if len(tentativas) == 2:
                parar.set()
            msg = "banco fora"
            raise RuntimeError(msg)

        with caplog.at_level(logging.ERROR):
            rodar(falhar, intervalo=0.01, parar=parar, heartbeat=tmp_path / "hb")
        assert len(tentativas) == 2
        assert caplog.messages.count("deadlines_cycle_failed") == 2
        assert (tmp_path / "hb").exists()

    def test_espera_o_intervalo_entre_os_ciclos(self, tmp_path: Path) -> None:
        parar = EsperaEspia(espera_final=3)
        ciclos: list[int] = []

        def um_ciclo() -> ResultadoDoCiclo:
            ciclos.append(1)
            if len(ciclos) == 10:  # rede de seguranca: laco ocupado nao trava a suite
                parar.set()
            return ResultadoDoCiclo(0, 0, 0)

        rodar(um_ciclo, intervalo=7.5, parar=parar, heartbeat=tmp_path / "hb")

        # Sem espera o laco martelaria o Mongo e o provedor: cada ciclo e
        # seguido de uma espera do intervalo configurado.
        assert (len(ciclos), parar.esperas) == (3, [7.5, 7.5, 7.5])

    def test_lote_cheio_repete_sem_esperar_o_intervalo(self, tmp_path: Path) -> None:
        parar = EsperaEspia(espera_final=1)
        resultados = [
            ResultadoDoCiclo(0, prazos.LIMITE_POR_CICLO, 0),
            ResultadoDoCiclo(0, 0, 0),
        ]
        ciclos: list[ResultadoDoCiclo] = []

        def ciclo() -> ResultadoDoCiclo:
            ciclos.append(resultados[min(len(ciclos), 1)])
            if len(ciclos) == 10:  # rede de seguranca: laco ocupado nao trava a suite
                parar.set()
            return ciclos[-1]

        rodar(ciclo, intervalo=3600, parar=parar, heartbeat=tmp_path / "hb")

        # O lote cheio vai direto ao proximo ciclo; so o ciclo vazio espera.
        assert (len(ciclos), parar.esperas) == (2, [3600])


def test_sigterm_encerra_o_laco() -> None:
    parar = threading.Event()
    anteriores = (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT))
    try:
        # Guarda: se instalar_sinais deixasse de tratar o sinal, o padrao do
        # processo mataria o proprio pytest (rc=-15) em vez de falhar o teste.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        instalar_sinais(parar)
        signal.raise_signal(signal.SIGTERM)
        assert parar.is_set()
        parar.clear()
        signal.raise_signal(signal.SIGINT)
        assert parar.is_set()
    finally:
        signal.signal(signal.SIGTERM, anteriores[0])
        signal.signal(signal.SIGINT, anteriores[1])


class TestBoot:
    @pytest.fixture
    def ambiente(
        self, monkeypatch: pytest.MonkeyPatch, mongo_uri: str, tmp_path: Path
    ) -> dict[str, str]:
        variaveis = {
            "ENVIRONMENT": "test",
            "MP_MODE": "simulado",
            "MONGODB_URI": mongo_uri,
            "MONGODB_DB": f"teste_{uuid4().hex}",
            "PRAZOS_HEARTBEAT": str(tmp_path / "hb"),
        }
        for nome, valor in variaveis.items():
            monkeypatch.setenv(nome, valor)
        # O provider global do OpenTelemetry e o dos testes (fixture spans).
        monkeypatch.setattr(
            prazos, "configurar_telemetria", lambda _processo: TracerProvider()
        )
        return variaveis

    @pytest.mark.parametrize("modo", ["simulado", "mercadopago"])
    def test_main_sobe_com_a_configuracao_minima_e_para_no_sinal(
        self,
        ambiente: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
        cliente_mongo: MongoClient[dict[str, Any]],
        modo: str,
    ) -> None:
        monkeypatch.setenv("MP_MODE", modo)
        monkeypatch.setenv("MP_ACCESS_TOKEN", "TEST-token-de-teste")  # gitleaks:allow
        portas: list[int] = []
        monkeypatch.setattr(prazos, "start_http_server", portas.append)
        parar = threading.Event()
        parar.set()  # o laco sai na hora; o boot e o encerramento rodam inteiros
        preparar_banco(cliente_mongo[ambiente["MONGODB_DB"]])
        try:
            prazos.main(parar)
        finally:
            cliente_mongo.drop_database(ambiente["MONGODB_DB"])
        assert portas == [9100]

    def test_main_instala_os_sinais_quando_nao_recebe_o_evento(
        self,
        ambiente: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
        cliente_mongo: MongoClient[dict[str, Any]],
    ) -> None:
        monkeypatch.setattr(prazos, "start_http_server", lambda _porta: None)
        eventos: list[threading.Event] = []

        def instalar(parar: threading.Event) -> None:
            eventos.append(parar)
            parar.set()

        monkeypatch.setattr(prazos, "instalar_sinais", instalar)
        preparar_banco(cliente_mongo[ambiente["MONGODB_DB"]])
        try:
            prazos.main()
        finally:
            cliente_mongo.drop_database(ambiente["MONGODB_DB"])
        assert len(eventos) == 1

    def test_main_recusa_banco_sem_o_init(
        self,
        ambiente: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
        cliente_mongo: MongoClient[dict[str, Any]],
    ) -> None:
        monkeypatch.setattr(prazos, "start_http_server", lambda _porta: None)
        parar = threading.Event()
        parar.set()
        try:
            with pytest.raises(BancoNaoPreparadoError, match=r"src\.banco"):
                prazos.main(parar)
        finally:
            cliente_mongo.drop_database(ambiente["MONGODB_DB"])

    def test_gateway_so_com_o_mercado_pago_real(self) -> None:
        simulado = ConfiguracaoDosPrazos.do_ambiente(
            {"ENVIRONMENT": "test", "MP_MODE": "simulado"}
        )
        assert criar_gateway_de_conciliacao(simulado) is None
        real = ConfiguracaoDosPrazos.do_ambiente(
            {
                "ENVIRONMENT": "test",
                "MP_MODE": "mercadopago",
                "MP_ACCESS_TOKEN": "TEST-token-de-teste",  # gitleaks:allow
            }
        )
        gateway = criar_gateway_de_conciliacao(real)
        assert isinstance(gateway, MercadoPagoGateway)
        gateway.fechar()
