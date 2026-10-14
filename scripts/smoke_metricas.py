"""Smoke das metricas na imagem (``make smoke``): o ``/metrics`` do processo.

Roda dentro do container do processo (``python - <processo> <url>``, o script
vem da entrada padrao), depois de o smoke da mensageria passar um
``GerarOrcamento`` pelo consumidor e pelo relay. Confere que cada processo lista
as series do catalogo (ADR-043) que sao dele: as de rotulo fechado ja nascem com
zero no boot, e as do fluxo que acabou de rodar valem 1.
"""

from __future__ import annotations

import sys
import urllib.request

MP = "pytstop_mercadopago_requisicoes_total"
VALOR = "pytstop_pagamentos_valor_centavos_total"
PUBLICADAS = "pytstop_mensagens_publicadas_total"
CONSUMIDAS = "pytstop_mensagens_consumidas_total"
RECONEXOES = "pytstop_reconexoes_ao_broker_total"
DECISAO = "pytstop_orcamentos_tempo_decisao_segundos_count"


def serie(nome: str, valor: str = "", **rotulos: str) -> str:
    """Prefixo da linha da raspagem (rotulos em ordem alfabetica, como o cliente
    do Prometheus os escreve); sem ``valor``, so a existencia da serie."""
    if rotulos:
        pares = ",".join(f'{k}="{v}"' for k, v in sorted(rotulos.items()))
        nome = f"{nome}{{{pares}}}"
    return f"{nome} {valor}"


ESPERADAS = {
    "api": (
        serie("outbox_pendentes"),
        serie("outbox_dead"),
        serie("pytstop_outbox_mais_antiga_segundos"),
        serie(DECISAO, "0.0", decisao="aprovado"),
        serie(DECISAO, "0.0", decisao="recusado"),
        serie(VALOR, "0.0", estado="confirmado"),
        serie(VALOR, "0.0", estado="estornado"),
        serie(MP, "0.0", operacao="estornar", resultado="sucesso"),
        serie("pytstop_pagamentos_estornados_total", "0.0", motivo="compensacao"),
    ),
    "relay": (
        serie("outbox_pendentes"),
        serie("outbox_dead"),
        serie("pytstop_outbox_mais_antiga_segundos"),
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
        serie("pytstop_prazos_ultimo_ciclo_timestamp_seconds"),
        serie(VALOR, "0.0", estado="confirmado"),
        serie(MP, "0.0", operacao="buscar_pagamentos", resultado="sucesso"),
    ),
}

processo, url = sys.argv[1], sys.argv[2]
with urllib.request.urlopen(url, timeout=5) as resposta:  # noqa: S310 - URL do compose
    linhas = resposta.read().decode().splitlines()

faltam = [
    esperada
    for esperada in ESPERADAS[processo]
    if not any(linha.startswith(esperada) for linha in linhas)
]
if faltam:
    print(f"smoke: {processo} sem as series {faltam}", file=sys.stderr)
    sys.exit(1)
print(f"metricas do {processo} ok ({len(ESPERADAS[processo])} series)")
