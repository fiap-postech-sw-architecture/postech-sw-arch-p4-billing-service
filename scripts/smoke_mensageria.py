"""Smoke da mensageria na imagem (``make smoke``): comando do OS vira evento.

Roda dentro de um container da stack (``python -`` no ``relay``), com o usuario
``os`` do broker: publica um ``GerarOrcamento`` em ``pytstop.comandos`` e espera
o ``OrcamentoGerado`` em ``os.eventos``. Prova a imagem inteira de ponta a ponta:
contratos em ``/app/contratos``, consumidor, transacao no MongoDB, outbox,
relay e as permissoes do broker.
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid

import pika

URL = os.environ["SMOKE_OS_URL"]
PRAZO_SEGUNDOS = 30

ordem_id = str(uuid.uuid4())
comando = {
    "id": str(uuid.uuid4()),
    "tipo": "GerarOrcamento",
    "versao": 1,
    "origem": "os-service",
    "correlation_id": ordem_id,
    "causation_id": None,
    "ocorrido_em": "2026-10-06T12:00:00Z",
    "dados": {
        "ordem_id": ordem_id,
        "itens": [{"tipo": "servico", "codigo": "SRV-TROCA-OLEO", "quantidade": 1}],
    },
}

with pika.BlockingConnection(pika.URLParameters(URL)) as conexao:
    canal = conexao.channel()
    canal.confirm_delivery()
    canal.basic_publish(
        "pytstop.comandos",
        "comando.billing.gerar_orcamento",
        json.dumps(comando).encode(),
        pika.BasicProperties(
            message_id=comando["id"],
            correlation_id=ordem_id,
            type="GerarOrcamento",
            user_id="os",
            content_type="application/json",
            delivery_mode=2,
        ),
        mandatory=True,
    )
    limite = time.monotonic() + PRAZO_SEGUNDOS
    while time.monotonic() < limite:
        metodo, propriedades, corpo = canal.basic_get("os.eventos", auto_ack=True)
        if metodo is None:
            time.sleep(0.2)
            continue
        evento = json.loads(corpo)
        if evento["causation_id"] == comando["id"]:
            print(f"mensageria ok: {evento['tipo']} de {propriedades.user_id}")
            sys.exit(0 if evento["tipo"] == "OrcamentoGerado" else 1)
print("mensageria falhou: nenhuma resposta em os.eventos", file=sys.stderr)
sys.exit(1)
