"""Disjuntor (circuit breaker) para dependencias externas (ADR-038)."""

from __future__ import annotations

import math
import threading
import time
from typing import TYPE_CHECKING

import structlog
from prometheus_client import Gauge

if TYPE_CHECKING:
    from collections.abc import Callable

_log = structlog.get_logger(__name__)

CIRCUITO_ABERTO = Gauge(
    "pytstop_circuit_breaker_aberto",
    "1 quando o disjuntor da dependencia esta aberto (recusando chamadas).",
    ["dependencia"],
)


class CircuitoAbertoError(Exception):
    """Chamada recusada sem tentar: o circuito da dependencia esta aberto."""


class CircuitBreaker:
    """Disjuntor de uma dependencia, compartilhado entre requests.

    Fechado: tudo passa; ``limite_falhas`` falhas seguidas abrem o circuito
    por ``segundos_aberto``. Vencido o prazo, UMA chamada de prova passa
    (meio-aberto) e as demais seguem barradas: sucesso fecha, falha reabre. A
    prova empurra o prazo ao ser liberada, entao uma prova cujo resultado se
    perdeu nao trava o circuito: outra sai no proximo prazo. O gauge fica em
    1 do momento em que abre ate fechar de novo.

    Em ``chamar`` so excecoes do tipo ``falha`` contam (erro transitorio da
    dependencia); erro de negocio (4xx) passa sem mexer no estado.
    """

    def __init__(
        self,
        dependencia: str,
        *,
        falha: type[Exception] = Exception,
        limite_falhas: int = 5,
        segundos_aberto: float = 30.0,
        relogio: Callable[[], float] = time.monotonic,
    ) -> None:
        self._dependencia = dependencia
        self._falha = falha
        self._limite_falhas = limite_falhas
        self._segundos_aberto = segundos_aberto
        self._relogio = relogio
        self._trava = threading.Lock()
        self._falhas_seguidas = 0
        self._aberto_ate: float | None = None
        self._gauge = CIRCUITO_ABERTO.labels(dependencia=dependencia)
        self._gauge.set(0)

    @property
    def aberto(self) -> bool:
        """Aberto ou meio-aberto (ainda sem a prova que fecha)."""
        with self._trava:
            return self._aberto_ate is not None

    def barrado(self) -> bool:
        """Aberto e dentro do prazo, sem efeito colateral (nao libera a prova):
        decide sem custo se vale tentar, por exemplo antes de tomar um lock."""
        with self._trava:
            return self._aberto_ate is not None and self._relogio() < self._aberto_ate

    def segundos_para_nova_tentativa(self) -> int:
        """Segundos (para cima) ate a proxima prova; 0 se fechado."""
        with self._trava:
            if self._aberto_ate is None:
                return 0
            return max(0, math.ceil(self._aberto_ate - self._relogio()))

    def permitir(self) -> bool:
        """Fechado: sim. Aberto: so a prova, uma por prazo vencido."""
        with self._trava:
            if self._aberto_ate is None:
                return True
            agora = self._relogio()
            if agora < self._aberto_ate:
                return False
            self._aberto_ate = agora + self._segundos_aberto
            return True

    def registrar_sucesso(self) -> None:
        with self._trava:
            if self._aberto_ate is not None:
                _log.info("circuit_breaker_closed", dependencia=self._dependencia)
                self._gauge.set(0)
            self._falhas_seguidas = 0
            self._aberto_ate = None

    def registrar_falha(self) -> None:
        with self._trava:
            self._falhas_seguidas += 1
            # Com o circuito aberto so a prova chega aqui: falha nela reabre.
            if self._aberto_ate is None and self._falhas_seguidas < self._limite_falhas:
                return
            if self._aberto_ate is None:
                _log.warning(
                    "circuit_breaker_opened",
                    dependencia=self._dependencia,
                    falhas=self._falhas_seguidas,
                )
                self._gauge.set(1)
            self._aberto_ate = self._relogio() + self._segundos_aberto

    def chamar[T](self, operacao: Callable[[], T]) -> T:
        if not self.permitir():
            msg = f"Circuito aberto para {self._dependencia}"
            raise CircuitoAbertoError(msg)
        try:
            resultado = operacao()
        except self._falha:
            self.registrar_falha()
            raise
        self.registrar_sucesso()
        return resultado
