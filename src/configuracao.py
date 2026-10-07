"""Configuracao lida do ambiente e validada no boot (falha rapido), por processo.

Cada processo le so o que usa: a API (``Configuracao``), o ``prazos``
(``ConfiguracaoDosPrazos``), o relay da outbox (``ConfiguracaoDoRelay``), o
consumidor dos comandos da saga (``ConfiguracaoDoConsumidor``) e o seed e a
preparacao do banco (``ConfiguracaoDoBanco``). API e consumidor dividem o que
os casos de uso dos comandos precisam (``ConfiguracaoDosComandos``: link de
decisao, prazos e provedor de pagamento).

Sem ``ENVIRONMENT`` o servico assume producao (falha fechada): enderecos e o
segredo do link precisam vir explicitos, e o segredo de demonstracao publico
no repo e recusado (mesma postura do ``validar_segredos_no_startup`` do p3).
Desenvolvimento e testes declaram ``ENVIRONMENT=development|test``; qualquer
outro valor e erro de configuracao. Fora de dev/test as URLs que saem para o
cliente ou levam credencial (``BILLING_PUBLIC_URL``, ``MP_API_URL``,
``MP_NOTIFICATION_URL``) exigem https. ``MP_MODE`` nao tem padrao, e o simulador
(que aprova pagamento sem provedor) so sobe em producao com
``SIMULADOR_PERMITIDO=true`` explicito (ADR-040).
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import unquote, urlsplit

if TYPE_CHECKING:
    from collections.abc import Mapping

_AMBIENTES_DE_DESENVOLVIMENTO = frozenset({"development", "test"})
_AMBIENTES = _AMBIENTES_DE_DESENVOLVIMENTO | {"production"}
_TAMANHO_MINIMO_SEGREDO = 32

# Valor de demonstracao (compose/testes); proibido fora de development/test.
SEGREDO_LINK_DEMO = "demo-link-orcamento-pytstop-nao-usar-em-producao"

_PADROES_DE_DESENVOLVIMENTO = {
    "MONGODB_URI": "mongodb://localhost:27017/?directConnection=true",
    "JWKS_URL": "http://localhost:8000/.well-known/jwks.json",
    "BILLING_PUBLIC_URL": "http://localhost:8002",
    "ORCAMENTO_LINK_SECRET": SEGREDO_LINK_DEMO,
}
# Caminho efemero do container, como o heartbeat do relay do p3. O nome e fixo
# de proposito (o healthcheck le o arquivo), e o /tmp e o tmpfs privado do
# container, onde so roda o usuario do servico: por isso as supressoes.
_HEARTBEAT_PADRAO = "/tmp/prazos-heartbeat"  # noqa: S108  # nosec B108  # NOSONAR
_HEARTBEAT_DO_RELAY = "/tmp/relay-heartbeat"  # noqa: S108  # nosec B108  # NOSONAR
_HEARTBEAT_DO_CONSUMIDOR = "/tmp/consumidor-heartbeat"  # noqa: S108  # nosec B108  # NOSONAR
# /metrics dos processos sem API (prazos, relay, consumidor): a mesma porta do
# OS e da Execucao e do exemplo de descoberta do platform. A API serve o dela na
# porta HTTP.
_METRICAS = 9100


class ModoMercadoPago(StrEnum):
    SIMULADO = "simulado"
    MERCADOPAGO = "mercadopago"


class _Ambiente:
    """``ENVIRONMENT`` validado e as variaveis lidas com os padroes de dev."""

    def __init__(self, env: Mapping[str, str] | None) -> None:
        self.env = os.environ if env is None else env
        valor = self.env.get("ENVIRONMENT", "production")
        self.nome = valor.strip().lower()
        if self.nome not in _AMBIENTES:
            opcoes = ", ".join(sorted(_AMBIENTES))
            msg = f"ENVIRONMENT invalido: {valor!r} (use {opcoes})"
            raise ValueError(msg)
        self.desenvolvimento = self.nome in _AMBIENTES_DE_DESENVOLVIMENTO

    def opcional(self, nome: str, padrao: str) -> str:
        """Variavel vazia vale como ausente (``VAR=`` no .env usa o padrao)."""
        return self.env.get(nome) or padrao

    def exigir(self, nome: str) -> str:
        """Obrigatoria fora de dev/test; em dev/test cai no padrao local."""
        padrao = _PADROES_DE_DESENVOLVIMENTO.get(nome, "")
        valor = self.env.get(nome) or (padrao if self.desenvolvimento else "")
        if not valor:
            msg = f"{nome} obrigatoria quando ENVIRONMENT={self.nome}"
            raise ValueError(msg)
        return valor

    def url(self, nome: str, valor: str, *, https: bool) -> str:
        """URL http(s) absoluta, sem usuario e senha; com ``https``, TLS
        obrigatorio fora de dev/test (o JWKS interno do cluster pode ser http)."""
        partes = urlsplit(valor)
        if partes.scheme not in {"http", "https"} or not partes.hostname:
            msg = f"{nome} deve ser uma URL http(s) absoluta"
            raise ValueError(msg)
        if "@" in partes.netloc:
            msg = f"{nome} nao pode ter usuario e senha na URL"
            raise ValueError(msg)
        if https and not self.desenvolvimento and partes.scheme != "https":
            msg = f"{nome} precisa de https com ENVIRONMENT={self.nome}"
            raise ValueError(msg)
        return valor

    def mp_api_url(self) -> str:
        return self.url(
            "MP_API_URL",
            self.opcional("MP_API_URL", "https://api.mercadopago.com"),
            https=True,
        )

    def positivo(self, nome: str, padrao: float) -> float:
        try:
            valor = float(self.opcional(nome, str(padrao)))
        except ValueError:
            msg = f"{nome} deve ser numerico"
            raise ValueError(msg) from None
        if not math.isfinite(valor) or valor <= 0:
            msg = f"{nome} deve ser um numero finito maior que zero"
            raise ValueError(msg)
        return valor

    def inteiro_positivo(self, nome: str, padrao: int) -> int:
        try:
            valor = int(self.opcional(nome, str(padrao)))
        except ValueError:
            msg = f"{nome} deve ser um inteiro"
            raise ValueError(msg) from None
        if valor < 1:
            msg = f"{nome} deve ser maior que zero"
            raise ValueError(msg)
        return valor

    def modo_mercadopago(self) -> ModoMercadoPago:
        valor = self.env.get("MP_MODE", "")
        if not valor:
            msg = "MP_MODE obrigatoria: simulado ou mercadopago"
            raise ValueError(msg)
        try:
            modo = ModoMercadoPago(valor.strip().lower())
        except ValueError:
            msg = f"MP_MODE invalido: {valor!r} (use simulado ou mercadopago)"
            raise ValueError(msg) from None
        permitido = self.env.get("SIMULADOR_PERMITIDO", "").strip().lower() == "true"
        if modo is ModoMercadoPago.SIMULADO and not (self.desenvolvimento or permitido):
            msg = (
                f"MP_MODE=simulado recusado com ENVIRONMENT={self.nome}: o simulador "
                "aprova pagamento sem provedor (SIMULADOR_PERMITIDO=true so em demo)"
            )
            raise ValueError(msg)
        return modo

    def mp_access_token(self, modo: ModoMercadoPago) -> str | None:
        token = self.env.get("MP_ACCESS_TOKEN") or None
        if modo is ModoMercadoPago.MERCADOPAGO and not token:
            msg = "MP_MODE=mercadopago exige MP_ACCESS_TOKEN"
            raise ValueError(msg)
        return token

    def rabbitmq(self) -> tuple[str, str]:
        """``RABBITMQ_URL`` com o usuario do servico, que vai no ``user_id`` de
        toda publicacao (o broker confere). Sem padrao: a senha nao fica no
        codigo, nem a de demonstracao."""
        url = self.env.get("RABBITMQ_URL") or ""
        partes = urlsplit(url)
        if partes.scheme not in {"amqp", "amqps"} or not partes.hostname:
            msg = "RABBITMQ_URL deve ser amqp(s)://<usuario>:<senha>@<host>:<porta>/"
            raise ValueError(msg)
        if not partes.username:
            msg = "RABBITMQ_URL precisa do usuario do servico (ex.: billing)"
            raise ValueError(msg)
        return url, unquote(partes.username)


@dataclass(frozen=True, slots=True)
class ConfiguracaoDoBanco:
    """O que o seed e a preparacao do banco (``python -m src.banco``) usam."""

    ambiente: str
    mongodb_uri: str = field(repr=False)
    mongodb_banco: str

    @classmethod
    def do_ambiente(cls, env: Mapping[str, str] | None = None) -> ConfiguracaoDoBanco:
        ambiente = _Ambiente(env)
        return cls(
            ambiente=ambiente.nome,
            mongodb_uri=ambiente.exigir("MONGODB_URI"),
            mongodb_banco=ambiente.opcional("MONGODB_DB", "billing"),
        )


@dataclass(frozen=True, slots=True)
class ConfiguracaoDosPrazos:
    """Processo ``prazos``: expiracao e, com o Mercado Pago real, conciliacao."""

    banco: ConfiguracaoDoBanco
    intervalo_segundos: float
    pagamento_max_recusas: int
    mp_modo: ModoMercadoPago
    mp_access_token: str | None = field(repr=False)
    mp_api_url: str
    mp_timeout_segundos: float
    heartbeat: Path
    porta_metricas: int

    @classmethod
    def do_ambiente(cls, env: Mapping[str, str] | None = None) -> ConfiguracaoDosPrazos:
        ambiente = _Ambiente(env)
        modo = ambiente.modo_mercadopago()
        return cls(
            banco=ConfiguracaoDoBanco.do_ambiente(ambiente.env),
            intervalo_segundos=ambiente.positivo("PRAZOS_INTERVALO_SEGUNDOS", 30),
            pagamento_max_recusas=ambiente.inteiro_positivo("PAGAMENTO_MAX_RECUSAS", 3),
            mp_modo=modo,
            mp_access_token=ambiente.mp_access_token(modo),
            mp_api_url=ambiente.mp_api_url(),
            mp_timeout_segundos=ambiente.positivo("MP_TIMEOUT_SEGUNDOS", 5),
            heartbeat=Path(ambiente.opcional("PRAZOS_HEARTBEAT", _HEARTBEAT_PADRAO)),
            porta_metricas=ambiente.inteiro_positivo("METRICS_PORT", _METRICAS),
        )


@dataclass(frozen=True, slots=True)
class ConfiguracaoDoRelay:
    """Processo ``relay``: outbox do MongoDB para o RabbitMQ."""

    banco: ConfiguracaoDoBanco
    rabbitmq_url: str = field(repr=False)
    rabbitmq_usuario: str
    heartbeat: Path
    porta_metricas: int

    @classmethod
    def do_ambiente(cls, env: Mapping[str, str] | None = None) -> ConfiguracaoDoRelay:
        ambiente = _Ambiente(env)
        url, usuario = ambiente.rabbitmq()
        return cls(
            banco=ConfiguracaoDoBanco.do_ambiente(ambiente.env),
            rabbitmq_url=url,
            rabbitmq_usuario=usuario,
            heartbeat=Path(ambiente.opcional("RELAY_HEARTBEAT", _HEARTBEAT_DO_RELAY)),
            porta_metricas=ambiente.inteiro_positivo("METRICS_PORT", _METRICAS),
        )


@dataclass(frozen=True, slots=True)
class ConfiguracaoDosComandos:
    """O que os casos de uso dos comandos usam: link de decisao, prazos e
    provedor de pagamento (API e consumidor leem igual)."""

    url_publica: str
    link_segredo: str = field(repr=False)
    orcamento_validade: timedelta
    pagamento_validade: timedelta
    mp_modo: ModoMercadoPago
    mp_access_token: str | None = field(repr=False)
    mp_api_url: str
    mp_notification_url: str
    mp_timeout_segundos: float

    @classmethod
    def do_ambiente(
        cls, env: Mapping[str, str] | None = None
    ) -> ConfiguracaoDosComandos:
        return cls._de(_Ambiente(env))

    @classmethod
    def _de(cls, ambiente: _Ambiente) -> ConfiguracaoDosComandos:
        segredo = ambiente.exigir("ORCAMENTO_LINK_SECRET")
        if not ambiente.desenvolvimento:
            _validar_segredo_de_producao(segredo)
        modo = ambiente.modo_mercadopago()
        url_publica = ambiente.url(
            "BILLING_PUBLIC_URL", ambiente.exigir("BILLING_PUBLIC_URL"), https=True
        ).rstrip("/")
        return cls(
            url_publica=url_publica,
            link_segredo=segredo,
            orcamento_validade=timedelta(
                hours=ambiente.positivo("ORCAMENTO_VALIDADE_HORAS", 72)
            ),
            pagamento_validade=timedelta(
                minutes=ambiente.positivo("PAGAMENTO_VALIDADE_MINUTOS", 60)
            ),
            mp_modo=modo,
            mp_access_token=ambiente.mp_access_token(modo),
            mp_api_url=ambiente.mp_api_url(),
            mp_notification_url=ambiente.url(
                "MP_NOTIFICATION_URL",
                ambiente.opcional(
                    "MP_NOTIFICATION_URL", f"{url_publica}/api/v1/webhooks/mercadopago"
                ),
                https=True,
            ),
            mp_timeout_segundos=ambiente.positivo("MP_TIMEOUT_SEGUNDOS", 5),
        )


@dataclass(frozen=True, slots=True)
class ConfiguracaoDoConsumidor:
    """Processo ``consumidor``: comandos da saga em ``billing.comandos``."""

    banco: ConfiguracaoDoBanco
    rabbitmq_url: str = field(repr=False)
    rabbitmq_usuario: str
    heartbeat: Path
    porta_metricas: int
    comandos: ConfiguracaoDosComandos

    @classmethod
    def do_ambiente(
        cls, env: Mapping[str, str] | None = None
    ) -> ConfiguracaoDoConsumidor:
        ambiente = _Ambiente(env)
        url, usuario = ambiente.rabbitmq()
        return cls(
            banco=ConfiguracaoDoBanco.do_ambiente(ambiente.env),
            rabbitmq_url=url,
            rabbitmq_usuario=usuario,
            heartbeat=Path(
                ambiente.opcional("CONSUMIDOR_HEARTBEAT", _HEARTBEAT_DO_CONSUMIDOR)
            ),
            porta_metricas=ambiente.inteiro_positivo("METRICS_PORT", _METRICAS),
            comandos=ConfiguracaoDosComandos._de(ambiente),
        )


@dataclass(frozen=True, slots=True)
class Configuracao:
    """Configuracao da API."""

    ambiente: str
    mongodb_uri: str = field(repr=False)
    mongodb_banco: str
    jwks_url: str
    url_publica: str
    link_segredo: str = field(repr=False)
    orcamento_validade: timedelta
    pagamento_validade: timedelta
    pagamento_max_recusas: int
    mp_modo: ModoMercadoPago
    mp_access_token: str | None = field(repr=False)
    mp_webhook_secret: str | None = field(repr=False)
    mp_api_url: str
    mp_notification_url: str
    mp_timeout_segundos: float

    @classmethod
    def do_ambiente(cls, env: Mapping[str, str] | None = None) -> Configuracao:
        ambiente = _Ambiente(env)
        comandos = ConfiguracaoDosComandos._de(ambiente)
        webhook_secret = ambiente.env.get("MP_WEBHOOK_SECRET") or None
        if comandos.mp_modo is ModoMercadoPago.MERCADOPAGO and not webhook_secret:
            msg = "MP_MODE=mercadopago exige MP_ACCESS_TOKEN e MP_WEBHOOK_SECRET"
            raise ValueError(msg)
        banco = ConfiguracaoDoBanco.do_ambiente(ambiente.env)
        return cls(
            ambiente=ambiente.nome,
            mongodb_uri=banco.mongodb_uri,
            mongodb_banco=banco.mongodb_banco,
            jwks_url=ambiente.url("JWKS_URL", ambiente.exigir("JWKS_URL"), https=False),
            url_publica=comandos.url_publica,
            link_segredo=comandos.link_segredo,
            orcamento_validade=comandos.orcamento_validade,
            pagamento_validade=comandos.pagamento_validade,
            pagamento_max_recusas=ambiente.inteiro_positivo("PAGAMENTO_MAX_RECUSAS", 3),
            mp_modo=comandos.mp_modo,
            mp_access_token=comandos.mp_access_token,
            mp_webhook_secret=webhook_secret,
            mp_api_url=comandos.mp_api_url,
            mp_notification_url=comandos.mp_notification_url,
            mp_timeout_segundos=comandos.mp_timeout_segundos,
        )

    @property
    def comandos(self) -> ConfiguracaoDosComandos:
        """A parte que o consumidor tambem le (fabrica do provedor)."""
        return ConfiguracaoDosComandos(
            url_publica=self.url_publica,
            link_segredo=self.link_segredo,
            orcamento_validade=self.orcamento_validade,
            pagamento_validade=self.pagamento_validade,
            mp_modo=self.mp_modo,
            mp_access_token=self.mp_access_token,
            mp_api_url=self.mp_api_url,
            mp_notification_url=self.mp_notification_url,
            mp_timeout_segundos=self.mp_timeout_segundos,
        )


def _validar_segredo_de_producao(segredo: str) -> None:
    if segredo == SEGREDO_LINK_DEMO:
        msg = "ORCAMENTO_LINK_SECRET usa o valor de demonstracao publico no repo"
        raise ValueError(msg)
    if len(segredo.encode()) < _TAMANHO_MINIMO_SEGREDO:
        msg = (
            f"ORCAMENTO_LINK_SECRET precisa de >= {_TAMANHO_MINIMO_SEGREDO} bytes "
            "(ex.: openssl rand -hex 32)"
        )
        raise ValueError(msg)
