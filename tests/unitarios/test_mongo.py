"""Opcoes do cliente MongoDB: limites de tempo e de conexoes (sem servidor)."""

from __future__ import annotations

from bson import UuidRepresentation

from src.compartilhado.infraestrutura.mongo import criar_cliente


def test_cliente_fala_uuid_padrao_em_utc_e_tem_limites_explicitos() -> None:
    # Porta sem servidor: o cliente e preguicoso, so as opcoes importam aqui.
    cliente = criar_cliente("mongodb://127.0.0.1:1/?directConnection=true")
    try:
        opcoes = cliente.options
        # Operacao presa em mongod travado cai em 10 s (CSOT), nao esgota o
        # threadpool; sem primario a API falha em 5 s, e nao nos 30 s do padrao.
        # Folga de conexoes sobre as 40 threads da API; o padrao do driver e 100.
        assert (
            opcoes.timeout,
            opcoes.server_selection_timeout,
            opcoes.pool_options.max_pool_size,
        ) == (10, 5, 50)
        assert opcoes.codec_options.uuid_representation == UuidRepresentation.STANDARD
        assert opcoes.codec_options.tz_aware
    finally:
        cliente.close()
