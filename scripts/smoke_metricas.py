"""Smoke das metricas na imagem (``make smoke``): o ``/metrics`` do processo.

Roda dentro do container do processo (``python - <processo> <url>``, o script
vem da entrada padrao), depois de o smoke da mensageria passar um
``GerarOrcamento`` pelo consumidor e pelo relay. Confere duas coisas:

- as familias do catalogo (ADR-043) que sao do processo estao todas no
  ``/metrics`` com ao menos uma amostra (``FAMILIAS``): a imagem que perde uma
  familia nao passa;
- algumas series de rotulo fechado nascem com zero e as do fluxo que acabou de
  rodar valem 1 (``SERIES``).
"""

from __future__ import annotations

import re
import sys
import urllib.request

MP = "pytstop_mercadopago_requisicoes_total"
VALOR = "pytstop_pagamentos_valor_centavos_total"
PUBLICADAS = "pytstop_mensagens_publicadas_total"
CONSUMIDAS = "pytstop_mensagens_consumidas_total"
RECONEXOES = "pytstop_reconexoes_ao_broker_total"
DECISAO = "pytstop_orcamentos_tempo_decisao_segundos_count"

# Familias por processo (o nome sem os sufixos _total, _created, _bucket, _sum e
# _count). Sem o ``circuit_breaker{mercadopago}``: o disjuntor so existe com o
# gateway real, e o compose sobe o simulador.
OUTBOX = ("outbox_pendentes", "outbox_dead", "pytstop_outbox_mais_antiga_segundos")
PAGAMENTO = (
    "pytstop_mercadopago_requisicoes",
    "pytstop_pagamentos_estornados",
    "pytstop_pagamentos_valor_centavos",
    "pytstop_estornos_automaticos_recusados",
    "pytstop_cancelamentos_de_cobranca_recusados",
)
FAMILIAS = {
    "api": (
        *OUTBOX,
        *PAGAMENTO,
        "http_request_duration_seconds",
        "pytstop_circuit_breaker_aberto",
        "pytstop_jwks_falhas",
        "pytstop_orcamentos_tempo_decisao_segundos",
        "pytstop_webhook_assinatura_invalida",
    ),
    "relay": (
        *OUTBOX,
        "pytstop_mensagens_publicadas",
        "pytstop_reconexoes_ao_broker",
    ),
    "consumidor": (
        *PAGAMENTO,
        "pytstop_mensagens_consumidas",
        "pytstop_reconexoes_ao_broker",
    ),
    "prazos": (*PAGAMENTO, "pytstop_prazos_ultimo_ciclo_timestamp_seconds"),
}


def serie(nome: str, valor: str = "", **rotulos: str) -> str:
    """Prefixo da linha da raspagem (rotulos em ordem alfabetica, como o cliente
    do Prometheus os escreve); sem ``valor``, so a existencia da serie."""
    if rotulos:
        pares = ",".join(f'{k}="{v}"' for k, v in sorted(rotulos.items()))
        nome = f"{nome}{{{pares}}}"
    return f"{nome} {valor}"


SERIES = {
    "api": (
        serie(DECISAO, "0.0", decisao="aprovado"),
        serie(DECISAO, "0.0", decisao="recusado"),
        serie(VALOR, "0.0", estado="confirmado"),
        serie(VALOR, "0.0", estado="estornado"),
        serie(MP, "0.0", operacao="estornar", resultado="sucesso"),
        serie("pytstop_pagamentos_estornados_total", "0.0", motivo="compensacao"),
    ),
    "relay": (
        serie(PUBLICADAS, "1.0", tipo="OrcamentoGerado"),
        serie(PUBLICADAS, "0.0", tipo="PagamentoEstornado"),
        serie(RECONEXOES, componente="relay"),
    ),
    "consumidor": (
        serie(CONSUMIDAS, "1.0", tipo="GerarOrcamento", resultado="processada"),
        serie(CONSUMIDAS, "0.0", tipo="EstornarPagamento", resultado="dlq"),
        serie(CONSUMIDAS, "0.0", tipo="desconhecido", resultado="dlq"),
        serie(RECONEXOES, componente="consumidor"),
        serie(VALOR, "0.0", estado="estornado"),
    ),
    "prazos": (
        serie(VALOR, "0.0", estado="confirmado"),
        serie(MP, "0.0", operacao="buscar_pagamentos", resultado="sucesso"),
    ),
}

SUFIXOS = ("_total", "_created", "_bucket", "_sum", "_count")


def familia(linha: str) -> str:
    nome = re.match(r"[A-Za-z_:][A-Za-z0-9_:]*", linha)
    if nome is None:
        raise ValueError(linha)
    base = nome.group()
    for sufixo in SUFIXOS:
        base = base.removesuffix(sufixo)
    return base


if __name__ == "__main__":
    processo, url = sys.argv[1], sys.argv[2]
    with urllib.request.urlopen(url, timeout=5) as resposta:  # noqa: S310 - URL do compose
        linhas = resposta.read().decode().splitlines()

    presentes = {familia(linha) for linha in linhas if linha and linha[0] != "#"}
    sem_familia = [f for f in FAMILIAS[processo] if f not in presentes]
    sem_serie = [
        esperada
        for esperada in SERIES[processo]
        if not any(linha.startswith(esperada) for linha in linhas)
    ]
    if sem_familia or sem_serie:
        print(
            f"smoke: {processo} sem as familias {sem_familia} e as series {sem_serie}",
            file=sys.stderr,
        )
        sys.exit(1)
    print(
        f"metricas do {processo} ok ({len(FAMILIAS[processo])} familias, "
        f"{len(SERIES[processo])} series)"
    )
