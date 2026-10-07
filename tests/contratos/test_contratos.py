"""Copia dos contratos de mensageria do platform (ADR-036; RFC-004, secao 5.5).

``contratos/`` guarda os schemas e exemplos das mensagens que o Billing produz e
consome e o ``asyncapi.yaml``, copiados do repositorio da plataforma no commit
gravado em ``contratos/ORIGEM``. A copia tem de ser identica a origem (o CI
baixa cada arquivo pelo raw do GitHub, o repositorio e publico), e todo exemplo
do platform tem de passar na validacao que o servico aplica.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import httpx
import pytest
import yaml

from src.compartilhado.infraestrutura.mensageria import contratos

RAIZ = contratos.diretorio()
ORIGEM = (RAIZ / "ORIGEM").read_text(encoding="utf-8").strip()
URL_DO_PLATFORM = (
    "https://raw.githubusercontent.com/fiap-postech-sw-architecture/"
    f"postech-sw-arch-p4-platform/{ORIGEM}/contratos"
)
COPIADOS = sorted(
    caminho.relative_to(RAIZ).as_posix()
    for caminho in RAIZ.rglob("*")
    if caminho.is_file() and caminho.name != "ORIGEM"
)
COMANDOS = {
    "GerarOrcamento",
    "CancelarOrcamento",
    "SolicitarPagamento",
    "EstornarPagamento",
}


def _sha256(conteudo: bytes) -> str:
    return hashlib.sha256(conteudo).hexdigest()


def test_origem_e_um_sha_completo() -> None:
    assert len(ORIGEM) == 40
    assert int(ORIGEM, 16) >= 0


def test_copia_bate_com_o_platform_no_sha_de_origem() -> None:
    divergentes = []
    with httpx.Client(timeout=15, follow_redirects=True) as cliente:
        for relativo in COPIADOS:
            resposta = cliente.get(f"{URL_DO_PLATFORM}/{relativo}")
            resposta.raise_for_status()
            local = (RAIZ / relativo).read_bytes()
            if _sha256(resposta.content) != _sha256(local):
                divergentes.append(relativo)
    assert divergentes == []


def test_copia_tem_o_envelope_e_os_tipos_do_billing() -> None:
    schemas = {r for r in COPIADOS if r.startswith("schemas/")}
    exemplos = {r for r in COPIADOS if r.startswith("exemplos/")}
    tipos = {r.removeprefix("schemas/").removesuffix(".schema.json") for r in schemas}
    assert "envelope" in tipos
    assert tipos - {"envelope"} == contratos.tipos_com_contrato()
    assert {f"exemplos/{t}.json" for t in contratos.tipos_com_contrato()} == exemplos
    assert contratos.tipos_com_contrato() >= COMANDOS


@pytest.mark.parametrize("tipo", sorted(contratos.tipos_com_contrato()))
def test_exemplo_do_platform_passa_na_validacao(tipo: str) -> None:
    exemplo: dict[str, Any] = json.loads(
        (RAIZ / "exemplos" / f"{tipo}.json").read_text(encoding="utf-8")
    )
    contratos.validar(exemplo)


def _produtores() -> dict[str, str]:
    """Tipo -> usuario AMQP que o publica (``userId`` das operacoes ``send``)."""
    documento = yaml.safe_load((RAIZ / "asyncapi.yaml").read_text(encoding="utf-8"))
    produtores = {}
    for operacao in documento["operations"].values():
        if operacao["action"] != "send":
            continue
        [mensagem] = operacao["messages"]
        tipo = mensagem["$ref"].rsplit("/", 1)[1]
        produtores[tipo] = operacao["bindings"]["amqp"]["userId"]
    return produtores


def test_asyncapi_diz_quem_publica_cada_tipo_do_billing() -> None:
    produtores = _produtores()
    eventos = contratos.tipos_com_contrato() - COMANDOS
    assert {produtores[tipo] for tipo in COMANDOS} == {"os"}
    assert {produtores[tipo] for tipo in eventos} == {"billing"}


@pytest.mark.parametrize("tipo", sorted(contratos.tipos_com_contrato() - COMANDOS))
def test_routing_key_do_evento_e_a_do_asyncapi(tipo: str) -> None:
    documento = yaml.safe_load((RAIZ / "asyncapi.yaml").read_text(encoding="utf-8"))
    assert contratos.routing_key_do_evento(tipo) in documento["channels"]


def _exemplo(tipo: str) -> dict[str, Any]:
    conteudo = (RAIZ / "exemplos" / f"{tipo}.json").read_text(encoding="utf-8")
    exemplo: dict[str, Any] = json.loads(conteudo)
    return exemplo


class TestValidacao:
    def test_campo_novo_nao_invalida_leitor_tolerante(self) -> None:
        exemplo = _exemplo("GerarOrcamento")
        exemplo["campo_novo"] = "x"
        exemplo["dados"]["outro_campo"] = 1
        contratos.validar(exemplo)

    def test_versao_desconhecida_e_recusada(self) -> None:
        exemplo = _exemplo("GerarOrcamento")
        exemplo["versao"] = 2
        with pytest.raises(contratos.MensagemForaDoContratoError, match="versao 2"):
            contratos.validar(exemplo)

    def test_tipo_sem_contrato_neste_servico_e_recusado(self) -> None:
        exemplo = _exemplo("GerarOrcamento")
        exemplo["tipo"] = "ReservarPecas"
        with pytest.raises(contratos.MensagemForaDoContratoError, match="sem contrato"):
            contratos.validar(exemplo)

    def test_envelope_incompleto_e_recusado(self) -> None:
        exemplo = _exemplo("CancelarOrcamento")
        del exemplo["correlation_id"]
        with pytest.raises(
            contratos.MensagemForaDoContratoError, match="envelope fora do contrato"
        ):
            contratos.validar(exemplo)

    def test_dados_fora_do_contrato_dizem_onde_sem_ecoar_o_valor(self) -> None:
        exemplo = _exemplo("GerarOrcamento")
        exemplo["dados"]["itens"][0]["quantidade"] = 987654
        with pytest.raises(contratos.MensagemForaDoContratoError) as erro:
            contratos.validar(exemplo)
        assert "$.itens[0].quantidade" in str(erro.value)
        assert "maximum" in str(erro.value)
        assert "987654" not in str(erro.value)

    def test_data_fora_do_formato_e_recusada(self) -> None:
        exemplo = _exemplo("OrcamentoExpirado")
        exemplo["ocorrido_em"] = "ontem as 10h"
        with pytest.raises(contratos.MensagemForaDoContratoError):
            contratos.validar(exemplo)


def test_diretorio_dos_contratos_vem_da_variavel_na_imagem(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CONTRATOS_DIR", "/app/contratos")
    assert contratos.diretorio().as_posix() == "/app/contratos"
    monkeypatch.delenv("CONTRATOS_DIR")
    assert contratos.diretorio() == RAIZ
