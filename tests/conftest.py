"""Fixtures compartilhadas: chave RSA de teste, JWKS e emissor de JWT."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

if TYPE_CHECKING:
    from collections.abc import Callable

# Colima (macOS): o testcontainers usa o SDK do Docker, que le DOCKER_HOST e
# nao os contexts do CLI; o Ryuk precisa do socket visto de dentro da VM.
# Inocuo no CI (Linux com /var/run/docker.sock) e com Docker Desktop.
_SOCKET_COLIMA = Path.home() / ".colima" / "default" / "docker.sock"
if "DOCKER_HOST" not in os.environ and _SOCKET_COLIMA.exists():
    os.environ["DOCKER_HOST"] = f"unix://{_SOCKET_COLIMA}"
    os.environ.setdefault(
        "TESTCONTAINERS_DOCKER_SOCKET_OVERRIDE", "/var/run/docker.sock"
    )

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
