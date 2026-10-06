from __future__ import annotations

import pytest
from prometheus_client import REGISTRY

from src.compartilhado.infraestrutura.circuit_breaker import (
    CircuitBreaker,
    CircuitoAbertoError,
)


class FalhaDeRedeError(Exception):
    pass


class Relogio:
    def __init__(self) -> None:
        self.agora = 1000.0

    def __call__(self) -> float:
        return self.agora


def falhar() -> None:
    raise FalhaDeRedeError


def gauge(dependencia: str) -> float | None:
    return REGISTRY.get_sample_value(
        "pytstop_circuit_breaker_aberto", {"dependencia": dependencia}
    )


@pytest.fixture
def relogio() -> Relogio:
    return Relogio()


@pytest.fixture
def breaker(relogio: Relogio) -> CircuitBreaker:
    return CircuitBreaker(
        "teste-cb",
        falha=FalhaDeRedeError,
        limite_falhas=3,
        segundos_aberto=30,
        relogio=relogio,
    )


def test_abre_depois_do_limite_de_falhas_seguidas(breaker: CircuitBreaker) -> None:
    for _ in range(3):
        with pytest.raises(FalhaDeRedeError):
            breaker.chamar(falhar)
    assert breaker.aberto
    assert gauge("teste-cb") == 1

    chamadas: list[int] = []
    with pytest.raises(CircuitoAbertoError):
        breaker.chamar(lambda: chamadas.append(1))
    assert chamadas == []  # aberto: nem tenta


def test_sucesso_zera_a_contagem(breaker: CircuitBreaker) -> None:
    for _ in range(2):
        with pytest.raises(FalhaDeRedeError):
            breaker.chamar(falhar)
    assert breaker.chamar(lambda: "ok") == "ok"
    for _ in range(2):
        with pytest.raises(FalhaDeRedeError):
            breaker.chamar(falhar)
    assert not breaker.aberto


def test_meio_aberto_fecha_com_sucesso(
    breaker: CircuitBreaker, relogio: Relogio
) -> None:
    for _ in range(3):
        with pytest.raises(FalhaDeRedeError):
            breaker.chamar(falhar)
    relogio.agora += 30

    assert breaker.chamar(lambda: 42) == 42
    assert not breaker.aberto
    assert gauge("teste-cb") == 0


def test_meio_aberto_reabre_na_primeira_falha(
    breaker: CircuitBreaker, relogio: Relogio
) -> None:
    for _ in range(3):
        with pytest.raises(FalhaDeRedeError):
            breaker.chamar(falhar)
    relogio.agora += 31
    with pytest.raises(FalhaDeRedeError):
        breaker.chamar(falhar)
    assert breaker.aberto


def test_erro_que_nao_e_falha_da_dependencia_nao_conta(
    breaker: CircuitBreaker,
) -> None:
    def recusar() -> None:
        raise ValueError("4xx de negocio")

    for _ in range(5):
        with pytest.raises(ValueError, match="negocio"):
            breaker.chamar(recusar)
    assert not breaker.aberto
