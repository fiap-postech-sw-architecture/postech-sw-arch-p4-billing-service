# syntax=docker/dockerfile:1.7
# Adaptado do p3 @ 08dcffe. Builder e runtime com o mesmo Python (3.14) e o
# mesmo Debian (trixie): o venv copiado do builder aponta para o interpretador
# em /usr/local/bin e usa a glibc do runtime. O uv do builder acompanha a
# versao que gerou o uv.lock.
FROM ghcr.io/astral-sh/uv:0.11-python3.14-trixie-slim AS builder

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencias primeiro (cache de camada enquanto o lock nao muda); --frozen
# falha se o uv.lock estiver defasado; --no-dev deixa testes e linters de fora.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# --no-editable: o projeto entra no site-packages do venv ja compilado em
# bytecode; o runtime leva so o venv, sem a arvore de codigo-fonte.
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

FROM python:3.14-slim-trixie AS runtime

ARG GIT_SHA=unknown
ARG GIT_DATE=unknown

LABEL org.opencontainers.image.title="pytstop-billing-service" \
      org.opencontainers.image.source="https://github.com/fiap-postech-sw-architecture/postech-sw-arch-p4-billing-service" \
      org.opencontainers.image.description="Billing Service do PytStop: precos, orcamento e pagamento." \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.created="${GIT_DATE}"

# Pacotes do SO atualizados a cada build: a tag movel python:3.14-slim-trixie
# fica semanas sem rebuild e o trivy acusa CVE do Debian que ja tem correcao.
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Usuario de sistema sem shell de login; UID/GID numericos (1001) porque o
# runAsNonRoot do Kubernetes so verifica UID numerico.
RUN groupadd -r -g 1001 pytstop \
    && useradd -r -u 1001 -g pytstop -s /usr/sbin/nologin pytstop

# Sem pip no runtime: o app roda pelo venv do uv e nunca instala nada; o pip da
# base traz pacotes vendorizados que o trivy acusa sem correcao possivel aqui.
RUN python -m pip uninstall -y pip

WORKDIR /app
# Venv, contratos e entrypoint ficam do root (so leitura para o processo): o
# usuario 1001 executa, mas nao altera o proprio codigo. Os contratos (JSON
# Schema das mensagens, copiados do platform) validam a outbox e o consumo.
COPY --from=builder /app/.venv /app/.venv
COPY contratos ./contratos
COPY --chmod=755 entrypoint.sh ./

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTSTOP_GIT_SHA="${GIT_SHA}" \
    PYTSTOP_GIT_DATE="${GIT_DATE}" \
    CONTRATOS_DIR=/app/contratos

# Imagem slim sem curl: probe em Python na readiness (banco no ar e preparado).
# Vale para o processo `api`; o compose da ao `prazos` o heartbeat, e o
# Kubernetes usa as proprias probes.
HEALTHCHECK --interval=30s --timeout=4s --start-period=20s --start-interval=2s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/saude/pronto', timeout=3).status==200 else 1)"]

USER 1001:1001
EXPOSE 8000
ENTRYPOINT ["./entrypoint.sh"]
CMD ["api"]
