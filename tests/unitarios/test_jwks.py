"""Validador do JWT RS256 pelo JWKS do OS Service (chave RSA gerada no teste)."""

from __future__ import annotations

import io
import json
import secrets
import threading
import time
import urllib.request
from typing import TYPE_CHECKING, Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from prometheus_client import REGISTRY

from src.compartilhado.infraestrutura.jwks import (
    JwksIndisponivelError,
    TokenExpiradoError,
    TokenInvalidoError,
    ValidadorDeTokenJWKS,
)
from tests.conftest import AUDIENCIA, EMISSOR, KID_TESTE, SUB_DO_TESTE

if TYPE_CHECKING:
    from collections.abc import Callable

URL = "http://os.teste/.well-known/jwks.json"


class Relogio:
    def __init__(self) -> None:
        self.agora = 1000.0

    def __call__(self) -> float:
        return self.agora


class JwksFalso:
    """``PyJWKClient.fetch_data`` controlavel: conta as buscas e falha sob demanda."""

    def __init__(self, jwks: dict[str, Any]) -> None:
        self.jwks = jwks
        self.buscas = 0
        self.erro: Exception | None = None
        self.corpo: object = None

    def __call__(self) -> object:
        # Instancia (nao funcao) no atributo da classe: chamada sem o cliente.
        self.buscas += 1
        if self.erro is not None:
            raise self.erro
        return self.corpo if self.corpo is not None else self.jwks


@pytest.fixture
def relogio() -> Relogio:
    return Relogio()


@pytest.fixture
def jwks_falso(monkeypatch: pytest.MonkeyPatch, jwks: dict[str, Any]) -> JwksFalso:
    falso = JwksFalso(jwks)
    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", falso)
    return falso


@pytest.fixture
def validador(jwks_falso: JwksFalso, relogio: Relogio) -> ValidadorDeTokenJWKS:
    return ValidadorDeTokenJWKS(URL, relogio=relogio)


def fora_do_ar() -> jwt.PyJWKClientConnectionError:
    return jwt.PyJWKClientConnectionError("Fail to fetch data from the url")


class TestClaims:
    def test_token_valido_devolve_as_claims(
        self, validador: ValidadorDeTokenJWKS, emitir_token: Callable[..., str]
    ) -> None:
        claims = validador.validar(emitir_token("mecanico"))
        assert (claims["sub"], claims["papel"], claims["type"]) == (
            SUB_DO_TESTE,
            "mecanico",
            "access",
        )

    @pytest.mark.parametrize(
        ("deslocamento", "aceito"),
        [
            pytest.param(-5, True, id="expirado-dentro-do-leeway"),
            pytest.param(-15, False, id="expirado-alem-do-leeway"),
        ],
    )
    def test_leeway_de_10_segundos_no_exp(
        self,
        validador: ValidadorDeTokenJWKS,
        emitir_token: Callable[..., str],
        deslocamento: int,
        aceito: bool,
    ) -> None:
        token = emitir_token("admin", exp=int(time.time()) + deslocamento)
        if aceito:
            assert validador.validar(token)["papel"] == "admin"
        else:
            with pytest.raises(TokenExpiradoError):
                validador.validar(token)

    def test_iat_adiantado_alem_do_leeway_e_invalido(
        self, validador: ValidadorDeTokenJWKS, emitir_token: Callable[..., str]
    ) -> None:
        assert validador.validar(emitir_token("admin", iat=int(time.time()) + 5))
        with pytest.raises(TokenInvalidoError):
            validador.validar(emitir_token("admin", iat=int(time.time()) + 20))

    @pytest.mark.parametrize(
        "claims",
        [
            pytest.param({"exp": None}, id="sem-exp"),
            pytest.param({"sub": None}, id="sem-sub"),
            pytest.param({"iss": None}, id="sem-iss"),
            pytest.param({"aud": None}, id="sem-aud"),
            pytest.param({"iss": "outro-emissor"}, id="outro-emissor"),
            pytest.param({"aud": "outra-audiencia"}, id="outra-audiencia"),
        ],
    )
    def test_claims_obrigatorias_e_conferidas(
        self,
        validador: ValidadorDeTokenJWKS,
        emitir_token: Callable[..., str],
        claims: dict[str, Any],
    ) -> None:
        token = emitir_token("admin", **claims)
        with pytest.raises(TokenInvalidoError):
            validador.validar(token)

    def test_emissor_e_audiencia_sao_os_do_contrato_e_nao_vem_do_ambiente(
        self,
        validador: ValidadorDeTokenJWKS,
        emitir_token: Callable[..., str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # ADR-039: iss=pytstop-os-service e aud=pytstop. Variavel de ambiente
        # nao troca nenhum dos dois (nem abre o servico para token de outro emissor).
        monkeypatch.setenv("JWT_ISSUER", "outro-emissor")
        monkeypatch.setenv("JWT_AUDIENCE", "outra-audiencia")

        claims = validador.validar(emitir_token("admin"))

        assert (claims["iss"], claims["aud"]) == ("pytstop-os-service", "pytstop")
        with pytest.raises(TokenInvalidoError):
            validador.validar(
                emitir_token("admin", iss="outro-emissor", aud="outra-audiencia")
            )

    def test_assinatura_de_outra_chave_com_o_mesmo_kid(
        self, validador: ValidadorDeTokenJWKS, emitir_token: Callable[..., str]
    ) -> None:
        impostora = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        with pytest.raises(TokenInvalidoError):
            validador.validar(emitir_token("admin", chave=impostora))

    @pytest.mark.parametrize("algoritmo", ["HS256", "none"])
    def test_troca_de_algoritmo_nao_e_aceita(
        self, validador: ValidadorDeTokenJWKS, algoritmo: str
    ) -> None:
        claims = {"sub": "x", "papel": "admin", "iss": EMISSOR, "aud": AUDIENCIA}
        # "none" ignora a chave; HS256 assina com um segredo qualquer.
        token = jwt.encode(
            {**claims, "exp": int(time.time()) + 60, "type": "access"},
            secrets.token_hex(32) if algoritmo == "HS256" else "",
            algorithm=algoritmo,
            headers={"kid": KID_TESTE},
        )
        with pytest.raises(TokenInvalidoError):
            validador.validar(token)

    @pytest.mark.parametrize(
        "token", ["", "abc", "a.b.c", "eyJhbGciOiJSUzI1NiJ9.e30.c2ln"]
    )
    def test_token_malformado(
        self, validador: ValidadorDeTokenJWKS, token: str
    ) -> None:
        with pytest.raises(TokenInvalidoError):
            validador.validar(token)


class TestCacheDoJwks:
    def test_busca_com_timeout_de_2_segundos_e_guarda_10_minutos(
        self,
        monkeypatch: pytest.MonkeyPatch,
        jwks: dict[str, Any],
        emitir_token: Callable[..., str],
        relogio: Relogio,
    ) -> None:
        timeouts: list[object] = []

        class Resposta(io.BytesIO):
            def __enter__(self) -> Resposta:
                return self

            def __exit__(self, *_: object) -> None:
                return None

        def abrir(
            _self: object, _req: object, _data: object = None, timeout: object = None
        ) -> Resposta:
            timeouts.append(timeout)
            return Resposta(json.dumps(jwks).encode())

        monkeypatch.setattr(urllib.request.OpenerDirector, "open", abrir)
        validador = ValidadorDeTokenJWKS(URL, relogio=relogio)
        token = emitir_token("admin")
        validador.validar(token)
        relogio.agora += 599
        validador.validar(token)
        assert timeouts == [2]
        relogio.agora += 1
        validador.validar(token)
        assert timeouts == [2, 2]

    def test_kid_desconhecido_busca_de_novo_no_maximo_a_cada_30_s(
        self,
        validador: ValidadorDeTokenJWKS,
        jwks_falso: JwksFalso,
        emitir_token: Callable[..., str],
        chave_rsa: rsa.RSAPrivateKey,
        relogio: Relogio,
    ) -> None:
        validador.validar(emitir_token("admin"))
        desconhecido = jwt.encode(
            {"sub": "x", "iss": EMISSOR, "aud": AUDIENCIA, "exp": 9_999_999_999},
            chave_rsa,
            algorithm="RS256",
            headers={"kid": "outra-chave"},
        )
        for _ in range(3):
            with pytest.raises(TokenInvalidoError, match="kid"):
                validador.validar(desconhecido)
        assert jwks_falso.buscas == 1
        relogio.agora += 30
        with pytest.raises(TokenInvalidoError):
            validador.validar(desconhecido)
        assert jwks_falso.buscas == 2

    def test_sem_copia_e_com_o_jwks_fora_do_ar_e_indisponivel(
        self,
        validador: ValidadorDeTokenJWKS,
        jwks_falso: JwksFalso,
        emitir_token: Callable[..., str],
    ) -> None:
        jwks_falso.erro = fora_do_ar()
        antes = REGISTRY.get_sample_value("pytstop_jwks_falhas_total") or 0.0
        with pytest.raises(JwksIndisponivelError) as erro:
            validador.validar(emitir_token("admin"))
        assert erro.value.retry_after == 5
        assert REGISTRY.get_sample_value("pytstop_jwks_falhas_total") == antes + 1

    @pytest.mark.parametrize(
        "corpo",
        [
            pytest.param({"keys": []}, id="sem-chaves"),
            pytest.param({"nada": 1}, id="formato-desconhecido"),
        ],
    )
    def test_jwks_sem_chave_utilizavel_e_indisponivel_e_nao_401(
        self,
        validador: ValidadorDeTokenJWKS,
        jwks_falso: JwksFalso,
        emitir_token: Callable[..., str],
        corpo: object,
    ) -> None:
        jwks_falso.corpo = corpo
        with pytest.raises(JwksIndisponivelError):
            validador.validar(emitir_token("admin"))

    def test_corpo_que_nao_e_json_e_indisponivel(
        self,
        validador: ValidadorDeTokenJWKS,
        jwks_falso: JwksFalso,
        emitir_token: Callable[..., str],
    ) -> None:
        jwks_falso.erro = json.JSONDecodeError("Expecting value", "<html>", 0)
        with pytest.raises(JwksIndisponivelError):
            validador.validar(emitir_token("admin"))

    def test_copia_velha_vale_ate_1_hora_quando_a_renovacao_falha(
        self,
        validador: ValidadorDeTokenJWKS,
        jwks_falso: JwksFalso,
        emitir_token: Callable[..., str],
        relogio: Relogio,
    ) -> None:
        token = emitir_token("admin")
        validador.validar(token)
        jwks_falso.erro = fora_do_ar()
        relogio.agora += 3599
        assert validador.validar(token)["papel"] == "admin"  # stale-if-error
        relogio.agora += 10
        with pytest.raises(JwksIndisponivelError):
            validador.validar(token)

    def test_falha_memorizada_e_circuito_aberto_evitam_novas_buscas(
        self,
        validador: ValidadorDeTokenJWKS,
        jwks_falso: JwksFalso,
        emitir_token: Callable[..., str],
        relogio: Relogio,
    ) -> None:
        jwks_falso.erro = fora_do_ar()
        token = emitir_token("admin")
        for _ in range(3):
            with pytest.raises(JwksIndisponivelError):
                validador.validar(token)
            with pytest.raises(JwksIndisponivelError):
                validador.validar(token)  # dentro dos 5 s: nem tenta
            relogio.agora += 5
        assert jwks_falso.buscas == 3
        # 3 falhas seguidas: circuito aberto por 30 s, sem busca.
        with pytest.raises(JwksIndisponivelError) as erro:
            validador.validar(token)
        assert jwks_falso.buscas == 3
        assert erro.value.retry_after == 25
        relogio.agora += 25
        jwks_falso.erro = None
        assert validador.validar(token)["papel"] == "admin"  # prova fecha
        assert jwks_falso.buscas == 4


class TestJwksPendurado:
    def test_busca_pendurada_nao_prende_quem_tem_copia(
        self,
        monkeypatch: pytest.MonkeyPatch,
        jwks: dict[str, Any],
        emitir_token: Callable[..., str],
        relogio: Relogio,
    ) -> None:
        liberar = threading.Event()
        entrou = threading.Event()
        buscas: list[int] = []

        def buscar(_cliente: object) -> dict[str, Any]:
            buscas.append(1)
            if len(buscas) > 1:
                entrou.set()
                liberar.wait(timeout=10)
            return jwks

        monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", buscar)
        validador = ValidadorDeTokenJWKS(URL, relogio=relogio)
        token = emitir_token("admin")
        validador.validar(token)
        relogio.agora += 601  # copia velha: a proxima chamada renova
        pendurada = threading.Thread(target=validador.validar, args=(token,))
        pendurada.start()
        try:
            assert entrou.wait(timeout=5)
            inicio = time.monotonic()
            assert validador.validar(token)["papel"] == "admin"
            assert time.monotonic() - inicio < 0.5
        finally:
            liberar.set()
            pendurada.join(timeout=5)
        assert len(buscas) == 2

    @pytest.mark.parametrize("sucesso", [True, False], ids=["renovou", "falhou"])
    def test_quem_esperava_a_busca_do_outro_usa_o_resultado_dela(
        self,
        monkeypatch: pytest.MonkeyPatch,
        jwks: dict[str, Any],
        emitir_token: Callable[..., str],
        relogio: Relogio,
        sucesso: bool,
    ) -> None:
        """Sem copia (boot): B espera o lock da busca de A e nao busca de novo."""
        liberar = threading.Event()
        entrou = threading.Event()
        buscas: list[int] = []

        def buscar(_cliente: object) -> dict[str, Any]:
            buscas.append(1)
            entrou.set()
            liberar.wait(timeout=10)
            if not sucesso:
                raise fora_do_ar()
            return jwks

        monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", buscar)
        validador = ValidadorDeTokenJWKS(URL, relogio=relogio)
        token = emitir_token("admin")
        resultados: list[object] = []

        def validar() -> None:
            try:
                resultados.append(validador.validar(token)["papel"])
            except JwksIndisponivelError as exc:
                resultados.append(exc)

        primeira = threading.Thread(target=validar)
        primeira.start()
        assert entrou.wait(timeout=5)
        segunda = threading.Thread(target=validar)
        segunda.start()
        time.sleep(0.2)  # a segunda fica na fila do lock (timeout de 2 s)
        liberar.set()
        primeira.join(timeout=5)
        segunda.join(timeout=5)

        assert len(buscas) == 1
        if sucesso:
            assert resultados == ["admin", "admin"]
        else:
            assert all(isinstance(r, JwksIndisponivelError) for r in resultados)
