"""Validacao do ``x-signature`` do webhook do Mercado Pago (borda HTTP).

Documentacao: https://www.mercadopago.com.br/developers/pt/docs/checkout-pro-preferences/payment-notifications
"""

from __future__ import annotations

import hashlib
import hmac


def assinatura_webhook_valida(
    *,
    segredo: str,
    x_signature: str | None,
    x_request_id: str | None,
    data_id: str | None,
) -> bool:
    """Confere o ``x-signature`` do webhook do Mercado Pago.

    Manifesto ``id:<data.id>;request-id:<x-request-id>;ts:<ts>;``, com o
    ``data.id`` (query string) em minusculas; parte ausente sai do manifesto,
    como manda a documentacao. Comparacao em tempo constante. Sem janela de
    tempo: repetir uma notificacao valida so dispara outra consulta ao
    provedor, e o processamento e idempotente.
    """
    if not segredo or not x_signature:
        return False
    partes = {
        chave.strip(): valor.strip()
        for chave, valor in (
            parte.split("=", 1) for parte in x_signature.split(",") if "=" in parte
        )
    }
    ts, v1 = partes.get("ts"), partes.get("v1")
    if not ts or not v1:
        return False
    manifesto = ""
    if data_id:
        manifesto += f"id:{data_id.lower()};"
    if x_request_id:
        manifesto += f"request-id:{x_request_id};"
    manifesto += f"ts:{ts};"
    esperado = hmac.new(
        segredo.encode(), manifesto.encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(esperado.encode(), v1.encode())
