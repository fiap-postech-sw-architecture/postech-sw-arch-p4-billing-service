"""Copia dos contratos de mensageria do platform (ADR-036; RFC-004, secao 5.5).

``contratos/`` guarda os schemas e exemplos das mensagens que o Billing produz e
consome, o ``asyncapi.yaml`` e, em ``contratos/rabbitmq/``, a topologia do
broker que o compose e os testes sobem, todos copiados do repositorio da
plataforma no commit gravado em ``contratos/ORIGEM``. A copia tem de ser
identica a origem: o teste baixa o tarball do platform nesse SHA (repositorio
publico) uma vez, com novas tentativas, e compara byte a byte. Todo exemplo do
platform tem de passar na validacao que o servico aplica.

Sem rede, o teste de checksum falha no CI e e pulado fora dele (com o motivo);
para rodar a suite offline de proposito: ``uv run pytest -m "not rede"``.
"""

from __future__ import annotations

import io
import json
import os
import re
import tarfile
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from src.compartilhado.infraestrutura.mensageria import contratos
from src.compartilhado.infraestrutura.mensageria.consumidor import NIVEIS_DE_RETRY
from src.consumidor import FILA
from src.main import criar_app
from tests.integracao.apoio import configuracao

RAIZ = contratos.diretorio()
ORIGEM = (RAIZ / "ORIGEM").read_text(encoding="utf-8").strip()
TOPOLOGIA = RAIZ / "rabbitmq"
TARBALL = (
    "https://codeload.github.com/fiap-postech-sw-architecture/"
    "postech-sw-arch-p4-platform/tar.gz/{sha}"
)
TENTATIVAS = 3
COPIADOS = sorted(
    caminho
    for caminho in RAIZ.rglob("*")
    if caminho.is_file() and caminho.name != "ORIGEM"
)
COMANDOS = {
    "GerarOrcamento",
    "CancelarOrcamento",
    "SolicitarPagamento",
    "EstornarPagamento",
}


def _no_platform(copia: Path) -> str:
    """Caminho do arquivo no platform: a topologia vem de k8s/ e do compose."""
    relativo = copia.relative_to(RAIZ).as_posix()
    if not relativo.startswith("rabbitmq/"):
        return f"contratos/{relativo}"
    if copia.name == "rabbitmq-admin.json":
        return "compose/rabbitmq-admin.json"
    return f"k8s/base/rabbitmq/{copia.name}"


def _platform(sha: str) -> dict[str, bytes]:
    """Arquivos do platform no ``sha``, pelo caminho a partir da raiz do repo.

    Repete erro de rede e resposta 429 ou 5xx (um reset do GitHub nao e
    divergencia da copia). Esgotadas as tentativas, falha no CI e pula fora
    dele.
    """
    falha: httpx.HTTPError | None = None
    for tentativa in range(TENTATIVAS):
        if tentativa:
            time.sleep(2**tentativa)
        try:
            resposta = httpx.get(TARBALL.format(sha=sha), timeout=30)
            resposta.raise_for_status()
        except httpx.HTTPError as exc:
            falha = exc
            continue
        with tarfile.open(fileobj=io.BytesIO(resposta.content), mode="r:gz") as tar:
            return {
                membro.name.split("/", 1)[1]: arquivo.read()
                for membro in tar.getmembers()
                if (arquivo := tar.extractfile(membro)) is not None
            }
    motivo = (
        f"sem acesso ao platform no SHA {sha} depois de {TENTATIVAS} tentativas "
        f"({type(falha).__name__}); offline, rode com -m 'not rede'"
    )
    if os.environ.get("CI"):
        pytest.fail(motivo, pytrace=False)
    pytest.skip(motivo)


def test_origem_e_um_sha_completo() -> None:
    assert len(ORIGEM) == 40
    assert int(ORIGEM, 16) >= 0


@pytest.mark.rede
def test_copia_e_identica_a_do_platform_no_sha_de_origem() -> None:
    platform = _platform(ORIGEM)

    divergentes = [
        copia.relative_to(RAIZ).as_posix()
        for copia in COPIADOS
        if platform.get(_no_platform(copia)) != copia.read_bytes()
    ]

    assert {c.name for c in TOPOLOGIA.iterdir()} == {
        "definitions.json",
        "permissoes.json",
        "rabbitmq.conf",
        "criar-usuarios.sh",
        "enabled_plugins",
        "rabbitmq-admin.json",
    }
    assert divergentes == []


def test_cada_nivel_de_retry_do_consumidor_tem_fila_ttl_e_permissao() -> None:
    """O consumidor publica a copia em ``pytstop.retry`` com a chave do nivel:
    a topologia a leva a uma fila com esse TTL, que a devolve a fila de
    comandos, e o usuario ``billing`` pode usar a chave (e so ela)."""
    definicoes = json.loads((TOPOLOGIA / "definitions.json").read_text())
    permissoes = json.loads((TOPOLOGIA / "permissoes.json").read_text())
    filas = {fila["name"]: fila for fila in definicoes["queues"]}
    [escrita] = [
        p["write"]
        for p in permissoes["topic_permissions"]
        if (p["user"], p["exchange"]) == ("billing", "pytstop.retry")
    ]
    [politica] = [
        p for p in definicoes["policies"] if re.search(p["pattern"], f"{FILA}.retry.1s")
    ]

    for nivel in NIVEIS_DE_RETRY:
        nome = f"{FILA}.retry.{nivel}"
        assert filas[nome]["arguments"] == {
            "x-queue-type": "quorum",
            "x-message-ttl": int(nivel.removesuffix("s")) * 1000,
        }
        assert {
            "source": "pytstop.retry",
            "vhost": "/",
            "destination": nome,
            "destination_type": "queue",
            "routing_key": nome,
            "arguments": {},
        } in definicoes["bindings"]
        assert re.search(escrita, nome)
        # \z e nao $: o $ do PCRE aceitaria a chave seguida de quebra de linha.
        assert not re.search(escrita, f"{nome}\n")
    assert politica["definition"]["dead-letter-routing-key"] == FILA
    assert not re.search(escrita, FILA)


def test_politica_da_fila_de_comandos_limita_as_entregas() -> None:
    """Com prefetch 1 no consumidor, o limite de entregas da policy isola a
    mensagem que derruba a conexao a cada entrega (RFC-004, secao 5.1)."""
    definicoes = json.loads((TOPOLOGIA / "definitions.json").read_text())
    [politica] = [p for p in definicoes["policies"] if re.search(p["pattern"], FILA)]

    assert politica["definition"]["delivery-limit"] == 5
    assert politica["definition"]["dead-letter-exchange"] == "pytstop.dlx"


def test_copia_tem_o_envelope_e_os_tipos_do_billing() -> None:
    relativos = {c.relative_to(RAIZ).as_posix() for c in COPIADOS}
    schemas = {r for r in relativos if r.startswith("schemas/")}
    exemplos = {r for r in relativos if r.startswith("exemplos/")}
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


def test_sem_os_schemas_a_api_nao_sobe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CONTRATOS_DIR", str(tmp_path))
    # Os schemas ficam em cache: limpa para recarregar do diretorio novo.
    contratos._validadores.cache_clear()
    try:
        with pytest.raises(contratos.ContratosAusentesError, match="CONTRATOS_DIR"):
            criar_app(configuracao())
    finally:
        monkeypatch.delenv("CONTRATOS_DIR")
        contratos._validadores.cache_clear()
    assert "envelope" not in contratos.tipos_com_contrato()


def test_diretorio_dos_contratos_vem_da_variavel_na_imagem(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CONTRATOS_DIR", "/app/contratos")
    assert contratos.diretorio().as_posix() == "/app/contratos"
    monkeypatch.delenv("CONTRATOS_DIR")
    assert contratos.diretorio() == RAIZ
