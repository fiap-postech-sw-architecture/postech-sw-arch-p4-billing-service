"""Fixtures compartilhadas: chave RSA de teste, JWKS, emissor de JWT e spans."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

if TYPE_CHECKING:
    from collections.abc import Callable

KID_TESTE = "chave-de-teste"
# O OS emite o id do usuario (UUID) no sub.
SUB_DO_TESTE = "5f0c7c5e-1b9e-4c3e-9a4e-2d8f6b1a7c11"
EMISSOR = "pytstop-os-service"
AUDIENCIA = "pytstop"


@pytest.fixture(scope="session")
def chave_rsa() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="session")
def jwks(chave_rsa: rsa.RSAPrivateKey) -> dict[str, Any]:
    publica = RSAAlgorithm.to_jwk(chave_rsa.public_key(), as_dict=True)
    return {"keys": [{**publica, "kid": KID_TESTE, "use": "sig", "alg": "RS256"}]}


@pytest.fixture
def jwks_publicado(
    monkeypatch: pytest.MonkeyPatch, jwks: dict[str, Any]
) -> dict[str, Any]:
    """Faz o PyJWKClient "baixar" o JWKS de teste em vez de ir a rede."""
    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", lambda _self: jwks)
    return jwks


@pytest.fixture
def emitir_token(chave_rsa: rsa.RSAPrivateKey) -> Callable[..., str]:
    def emitir(papel: str | None = "admin", *, chave: Any = None, **claims: Any) -> str:
        agora = int(time.time())
        payload: dict[str, Any] = {
            "sub": SUB_DO_TESTE,
            "iss": EMISSOR,
            "aud": AUDIENCIA,
            "iat": agora,
            "exp": agora + 900,
            "jti": str(uuid4()),
            "type": "access",
        }
        if papel is not None:
            payload["papel"] = papel
        payload.update(claims)
        payload = {k: v for k, v in payload.items() if v is not None}
        return jwt.encode(
            payload,
            chave or chave_rsa,
            algorithm="RS256",
            headers={"kid": KID_TESTE},
        )

    return emitir


@pytest.fixture(scope="session")
def _exportador_de_spans() -> InMemorySpanExporter:
    # O provider global do OpenTelemetry so e instalado uma vez por processo.
    exportador = InMemorySpanExporter()
    provedor = TracerProvider()
    provedor.add_span_processor(SimpleSpanProcessor(exportador))
    trace.set_tracer_provider(provedor)
    return exportador


@pytest.fixture
def spans(_exportador_de_spans: InMemorySpanExporter) -> InMemorySpanExporter:
    """Spans terminados durante o teste (provider do SDK em memoria)."""
    _exportador_de_spans.clear()
    return _exportador_de_spans
