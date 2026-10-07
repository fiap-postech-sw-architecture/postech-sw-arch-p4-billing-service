"""Logging JSON (structlog) com mascaramento de PII e segredos.

Reaproveitado do p3 @ 08dcffe (``src/compartilhado/infraestrutura/logging.py``);
acrescimo do Billing: o token do link publico de decisao do orcamento (no path)
e o do checkout simulado (``?token=``) sao credenciais na URL e saem
mascarados dos logs (inclusive access log).
"""

from __future__ import annotations

import logging
import os
import re
import sys
from typing import TYPE_CHECKING, Any

import structlog
from opentelemetry import trace

if TYPE_CHECKING:
    from collections.abc import MutableMapping
    from typing import TextIO

# git_sha/git_date sao injetadas em build args -> ENV pelas pipelines
# (Makefile + Dockerfiles). Lidas uma vez no import e adicionadas a todo
# log structlog via processor -- assim ficam visiveis mesmo apos
# `clear_contextvars()` que o SecurityHeadersMiddleware faz a cada
# request. `[:12]` casa com o curto exibido no banner de boot.
_GIT_SHA = os.environ.get("PYTSTOP_GIT_SHA", "unknown")[:12]
_GIT_DATE = os.environ.get("PYTSTOP_GIT_DATE", "unknown")


def adicionar_versao_imagem(
    _logger: object,
    _method_name: str,
    event_dict: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    """Injeta git_sha/git_date em todo evento (sem sobrescrever explicit)."""
    event_dict.setdefault("git_sha", _GIT_SHA)
    event_dict.setdefault("git_date", _GIT_DATE)
    return event_dict


def adicionar_trace(
    _logger: object,
    _method_name: str,
    event_dict: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    """``trace_id``/``span_id`` do span corrente (relay e consumidor): do log no
    Loki ao trace no Jaeger (ADR-043). Fora de span, nada."""
    contexto = trace.get_current_span().get_span_context()
    if contexto.is_valid:
        event_dict.setdefault("trace_id", format(contexto.trace_id, "032x"))
        event_dict.setdefault("span_id", format(contexto.span_id, "016x"))
    return event_dict


_CPF_PATTERN = re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b")
_CNPJ_PATTERN = re.compile(r"\b\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}\b")
# O scrubber roda sobre o event_dict inteiro, tracebacks inclusos, sem limite
# de tamanho: cada trecho do e-mail tem teto (local-part de ate 64 caracteres,
# rotulos de ate 63, ate 10 rotulos, TLD de ate 24), entao a tentativa em cada
# posicao e curta e o custo cresce linear com a entrada. Com `+` sem teto, um
# texto como `a.a.a....` de 80 KB levava segundos (quadratico).
_EMAIL_PATTERN = re.compile(
    r"\b[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63}){0,8}"
    r"\.[A-Za-z]{2,24}\b"
)

# Telefone BR: duas formas estruturais, escolhidas para nao gerar falso-positivo
# em precos (`1500.00`), ids (`12345`), anos (`2026`), portas (`8000`) e CEPs
# (`12345-678`) -- nenhum deles tem o split `\d{4,5}-\d{4}` nem prefixo `+55`:
#   1. DDD (com/sem parenteses) + separador OPCIONAL + bloco local com hifen
#      4-4/5-4 -- cobre `(11)99999-0000` e `1199999-0000` alem dos formatados.
#      Colateral aceito (direcao LGPD-safe): ids numericos hifenizados com
#      shape 6+4 (`123456-7890`) tambem sao mascarados -- nenhum log site
#      atual emite esse formato (ordens usam UUID).
#   2. `+55` seguido de 10-11 digitos corridos -- cobre `+5511999990000` (o
#      prefixo de pais e estrutura suficiente; nada legitimo em log tem essa
#      forma). `+55 11999990000` (com espaco) e `11999990000` (sem nada) tem
#      11 digitos corridos com shape de CPF e caem no _CPF_PATTERN acima
#      antes desta regex; campos NOMEADOS telefone/celular/contato sao
#      mascarados pela denylist abaixo.
# O numero nao pode vir logo depois de um digito hexadecimal: os ids do servico
# (`ordem_id`, `correlation_id`, `request_id`, `sub`) sao UUID, e o v4 traz entre
# os grupos trechos `dd-dddd-dddd` (`732ffc02-3465-4237-...`) que o split 4-4
# casaria, sempre com o DDD colado ao fim de um grupo hexadecimal (minusculo ou
# maiusculo). Sem a guarda, cerca de 1,4% dos UUID saiam mascarados do log. Quem
# comeca em `(` ou `+` e telefone mesmo colado a uma palavra (`fone(11)99999-0000`),
# e depois de hifen ou de letra fora de a-f (`tel-11 99999-0000`) tambem.
_TELEFONE_PATTERN = re.compile(
    r"(?:(?<![0-9A-Fa-f])|(?=[(+]))"  # nao colado a digito hexadecimal (UUID)
    r"(?:"
    r"(?:\+55[\s.-]?)?"  # codigo do pais opcional
    r"(?:\(\d{2}\)|\d{2})"  # DDD com ou sem parenteses
    r"[\s.-]?"  # separador opcional entre DDD e numero
    r"9?\d{4}-\d{4}"  # 8 ou 9 digitos com hifen 4-4/5-4
    r"|"
    r"\+55[\s.-]?\d{10,11}"  # +55 com numero corrido (sem hifen local)
    r")"
    r"(?!\d)"  # nao seguido de digito (evita capturar parte de numero maior)
)

# Denylist de chaves: quando o NOME do campo indica segredo ou PII, o valor
# inteiro e mascarado -- independente de casar regex. Credenciais casam por
# trecho do nome (``mp_access_token``, ``x_signature``, ``webhook_secret``), e
# ``checkout_url``/``link_decisao`` carregam o token que paga ou decide; PII
# sem estrutura detectavel (telefone sem formatacao, contato em texto livre)
# casa pelo nome exato.
_TRECHOS_SENSIVEIS = (
    "token",
    "secret",
    "senha",
    "password",
    "signature",
    "authorization",
    "api_key",
    "checkout_url",
    "link_decisao",
)
_CHAVES_PII = frozenset({"telefone", "celular", "phone", "contato"})

_MASCARA = "***"

# Tokens de acesso na URL: o do link de decisao (``/publico/orcamentos/<token>``)
# decide o orcamento e o do checkout simulado (``?token=<token>``) paga a
# cobranca, entao nenhum pode ficar legivel no access log.
_TOKEN_NA_URL_PATTERN = re.compile(r"(/publico/orcamentos/|[?&]token=)[^/\s?&#\"']+")

# Loggers que o uvicorn configura com handler proprio + `propagate=False`.
# `configurar_logging` os religa ao root para passarem pelo scrubber.
_LOGGERS_UVICORN = ("uvicorn", "uvicorn.error", "uvicorn.access")

# Cap on recursion depth when scrubbing nested structures. Guards against
# pathological or cyclic structured log payloads without sacrificing coverage
# of realistic nesting (events usually nest 2-3 levels deep at most).
_MAX_SCRUB_DEPTH = 6


def _mask_cpf(match: re.Match[str]) -> str:
    raw = match.group().replace(".", "").replace("-", "")
    return f"***.***.{raw[6:9]}-**"


def _mask_cnpj(match: re.Match[str]) -> str:
    # Digitos do CNPJ NN.NNN.NNN/NNNN-NN: o terceiro grupo e raw[5:8].
    raw = match.group().replace(".", "").replace("/", "").replace("-", "")
    return f"**.***.{raw[5:8]}/****-**"


def _mask_email(match: re.Match[str]) -> str:
    email = match.group()
    local, domain = email.split("@", 1)
    masked_local = local[0] + "***" if local else "***"
    return f"{masked_local}@{domain}"


def _mask_string(value: str) -> str:
    value = _TOKEN_NA_URL_PATTERN.sub(rf"\g<1>{_MASCARA}", value)
    value = _CPF_PATTERN.sub(_mask_cpf, value)
    value = _CNPJ_PATTERN.sub(_mask_cnpj, value)
    value = _EMAIL_PATTERN.sub(_mask_email, value)
    return _TELEFONE_PATTERN.sub(_MASCARA, value)


def _chave_sensivel(key: Any) -> bool:  # noqa: ANN401  # chaves podem nao ser str
    if not isinstance(key, str):
        return False
    nome = key.lower()
    return nome in _CHAVES_PII or any(trecho in nome for trecho in _TRECHOS_SENSIVEIS)


def _scrub_value(value: Any, depth: int) -> Any:  # noqa: ANN401
    # Recursivamente normaliza qualquer estrutura JSON-like; o tipo de entrada
    # nao e conhecivel a priori, dai o Any.
    if depth >= _MAX_SCRUB_DEPTH:
        return value
    if isinstance(value, str):
        return _mask_string(value)
    if isinstance(value, dict):
        # Mascara o valor inteiro quando a chave esta na denylist; senao desce.
        return {
            k: (_MASCARA if _chave_sensivel(k) else _scrub_value(v, depth + 1))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_scrub_value(item, depth + 1) for item in value]
    if isinstance(value, tuple):
        return tuple(_scrub_value(item, depth + 1) for item in value)
    if isinstance(value, (set, frozenset)):
        limpo = {_scrub_value(item, depth + 1) for item in value}
        return limpo if isinstance(value, set) else frozenset(limpo)
    return value


def scrub_pii(
    _logger: Any,  # noqa: ANN401  # structlog bound logger; nao inspecionado
    _method_name: str,
    event_dict: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    """Structlog processor que mascara PII e segredos em todo o event_dict.

    Mascara CPF, CNPJ, email e telefone BR formatado por regex de VALOR; e mascara
    o valor inteiro quando o NOME do campo indica segredo (``_TRECHOS_SENSIVEIS``:
    token, secret, signature, ``checkout_url``...) ou PII (``_CHAVES_PII``).
    Percorre recursivamente strings, dicts, listas e
    tuplas ate `_MAX_SCRUB_DEPTH` para pegar PII em payloads estruturados. Aplicado
    automaticamente pelo pipeline de logging (inclusive na chave `exception` do
    traceback, que `format_exc_info` monta ANTES deste processor) para impedir
    vazamento de PII em logs -- structlog e stdlib (LGPD).
    """
    for key, value in event_dict.items():
        event_dict[key] = _MASCARA if _chave_sensivel(key) else _scrub_value(value, 0)
    return event_dict


_MAX_ERRO_LEN = 200


def redigir_pii_erro(erro: str) -> str:
    """Remove PII (CPF, CNPJ, e-mail, telefone) de strings de erro.

    Usada no handler de ``ValorInvalidoError`` (422): a mensagem de uma invariante pode
    ecoar o valor recebido e vai para o cliente, fora do alcance do scrubber de
    log (``scrub_pii``), que so atua no pipeline do log.

    Trunca o resultado em ``_MAX_ERRO_LEN`` caracteres para que uma mensagem de
    excecao longa nao volte inteira na resposta.
    """
    redacted = _mask_string(erro)
    if len(redacted) > _MAX_ERRO_LEN:
        redacted = redacted[:_MAX_ERRO_LEN] + "…"
    return redacted


# Cadeia COMPARTILHADA entre logs structlog e logs stdlib estrangeiros (uvicorn,
# bibliotecas, handler 500). ORDEM CRITICA: `format_exc_info` monta a
# chave `exception` a partir do `exc_info` e DEVE vir ANTES de `scrub_pii`, senao
# o traceback (com possivel PII no repr da excecao) escapa do mascaramento.
# `StackInfoRenderer` (stack_info -> string) tambem precede o scrub, que entao
# mascara ambas as strings. Nenhum renderer final aqui: o `ProcessorFormatter`
# (abaixo) renderiza para JSON tanto os logs structlog quanto os stdlib.
def _cadeia_compartilhada() -> list[Any]:
    return [
        structlog.contextvars.merge_contextvars,
        adicionar_versao_imagem,
        adicionar_trace,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        # Campos ``extra=`` dos logs stdlib (camada de aplicacao, sem
        # structlog) viram chaves do JSON -- antes do scrub, que os mascara.
        structlog.stdlib.ExtraAdder(),
        scrub_pii,
    ]


def configurar_logging(stream: TextIO | None = None) -> None:
    """Configura structlog + roteia o logging stdlib pelo mesmo scrubber de PII.

    JSON output, timestamps ISO e scrub automatico de PII/segredos. O
    ``ProcessorFormatter`` instalado no root logger faz com que TODO log stdlib
    (handler 500 em ``error_handler.py``, access/error logs do uvicorn, logs
    de bibliotecas e da camada de aplicacao) passe pela
    ``_cadeia_compartilhada`` -- inclusive ``scrub_pii`` -- via ``foreign_pre_chain``,
    sem reprocessar os logs ja-structlog (estes pulam o pre-chain). Assim nenhum
    traceback cru com PII escapa do pipeline do structlog.

    ``stream`` permite direcionar a saida (default ``sys.stdout``); usado em testes
    para capturar o output renderizado.
    """
    compartilhada = _cadeia_compartilhada()
    _configurar_structlog(compartilhada)
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(_formatador_json(compartilhada))
    root = logging.getLogger()
    # Substitui handlers existentes (ex.: o de uma chamada anterior) para
    # garantir que o scrubber seja o unico caminho de saida -- idempotente em
    # warm restarts/testes e sem handler cru remanescente.
    root.handlers = [handler]
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)
    # O pika loga em WARNING, com o comeco do corpo, a mensagem que o broker
    # devolve (mandatory), e em INFO cada passo da conexao: so os erros dele
    # interessam, e a reconexao ja tem log proprio.
    logging.getLogger("pika").setLevel(logging.ERROR)
    _religar_loggers_do_uvicorn()


def _configurar_structlog(compartilhada: list[Any]) -> None:
    structlog.configure(
        processors=[
            *compartilhada,
            # Handoff para o ProcessorFormatter do root handler (nao renderiza aqui).
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def _formatador_json(compartilhada: list[Any]) -> logging.Formatter:
    return structlog.stdlib.ProcessorFormatter(
        # Logs estrangeiros (stdlib) passam por esta cadeia ANTES da renderizacao;
        # logs ja-structlog ja a percorreram e a pulam (sem duplo processamento).
        foreign_pre_chain=compartilhada,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
    )


def _religar_loggers_do_uvicorn() -> None:
    # uvicorn (lancado por CLI no container) instala os PROPRIOS handlers nos
    # loggers `uvicorn`/`uvicorn.access` com `propagate=False` -- seus logs (inclui
    # access logs, que podem trazer PII em path/query) NAO chegariam ao handler de
    # scrub do root. A fabrica da API (``criar_app``) chama ``configurar_logging``
    # depois de o uvicorn montar seus loggers e antes da primeira linha do
    # servidor ("Started server process" ja sai em JSON): aqui removemos os
    # handlers crus e religamos ``propagate=True`` para que tudo flua pelo
    # ProcessorFormatter do root (scrubado, JSON unico). Idempotente.
    for nome in _LOGGERS_UVICORN:
        uvlog = logging.getLogger(nome)
        uvlog.handlers = []
        uvlog.propagate = True
