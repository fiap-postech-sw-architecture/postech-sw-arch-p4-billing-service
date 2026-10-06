"""Disjuntor (circuit breaker) para dependencias externas (RFC-004 §5)."""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

from prometheus_client import Gauge

if TYPE_CHECKING:
    from collections.abc import Callable

CIRCUITO_ABERTO = Gauge(
    "pytstop_circuit_breaker_aberto",
    "1 quando o disjuntor da dependencia esta aberto (recusando chamadas).",
    ["dependencia"],
)


class CircuitoAbertoError(Exception):
    """Chamada recusada sem tentar: o circuito da dependencia esta aberto."""


class CircuitBreaker:
    """Abre apos ``limite_falhas`` falhas seguidas e recusa chamadas por
    ``segundos_aberto``; depois deixa passar a proxima chamada (meio-aberto):
    sucesso fecha o circuito, falha reabre na hora.

    So excecoes do tipo ``falha`` contam (erro transitorio da dependencia);
    erro de negocio (4xx) passa sem mexer no estado.
    """

    def __init__(
        self,
        dependencia: str,
        *,
        falha: type[Exception],
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
        CIRCUITO_ABERTO.labels(dependencia=dependencia).set(0)

    @property
    def aberto(self) -> bool:
        with self._trava:
            return self._aberto_ate is not None and self._relogio() < self._aberto_ate

    def chamar[T](self, operacao: Callable[[], T]) -> T:
        if self.aberto:
            msg = f"Circuito aberto para {self._dependencia}"
            raise CircuitoAbertoError(msg)
        try:
            resultado = operacao()
        except self._falha:
            self._registrar_falha()
            raise
        self._registrar_sucesso()
        return resultado

    def _registrar_falha(self) -> None:
        with self._trava:
            self._falhas_seguidas += 1
            if self._falhas_seguidas >= self._limite_falhas:
                self._aberto_ate = self._relogio() + self._segundos_aberto
                CIRCUITO_ABERTO.labels(dependencia=self._dependencia).set(1)

    def _registrar_sucesso(self) -> None:
        with self._trava:
            self._falhas_seguidas = 0
            self._aberto_ate = None
            CIRCUITO_ABERTO.labels(dependencia=self._dependencia).set(0)
