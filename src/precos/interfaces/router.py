"""Tabela de precos: CRUD do admin, leitura por usuario interno e validacao."""

from __future__ import annotations

import dataclasses
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, Query, status

from src.compartilhado.interfaces.autenticacao import (
    Papel,
    UsuarioAutenticado,
    exigir_papel,
)
from src.precos.aplicacao.use_cases import PrecosDePecas, PrecosDeServicos, ValidarItens
from src.precos.interfaces.dependencies import (
    obter_precos_de_pecas,
    obter_precos_de_servicos,
    obter_validar_itens,
)
from src.precos.interfaces.schemas import (
    AtualizarPecaRequest,
    AtualizarServicoRequest,
    CadastrarPecaRequest,
    CadastrarServicoRequest,
    PecaListaResponse,
    PecaResponse,
    ServicoListaResponse,
    ServicoResponse,
    ValidacaoRequest,
    ValidacaoResponse,
)

_log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/precos", tags=["precos"])


def _auditar(usuario: UsuarioAutenticado, acao: str, alvo: str) -> None:
    """Log de auditoria das escritas de preco (sub, acao, alvo; ADR-039)."""
    _log.info("audit_price_changed", ator=usuario.sub, acao=acao, alvo=alvo)


Admin = Annotated[UsuarioAutenticado, Depends(exigir_papel(Papel.ADMIN))]
UsuarioInterno = Annotated[
    UsuarioAutenticado, Depends(exigir_papel(Papel.ATENDENTE, Papel.MECANICO))
]
Servicos = Annotated[PrecosDeServicos, Depends(obter_precos_de_servicos)]
Pecas = Annotated[PrecosDePecas, Depends(obter_precos_de_pecas)]
Offset = Annotated[int, Query(ge=0)]
Limit = Annotated[int, Query(ge=1, le=100)]


@router.post(
    "/servicos",
    status_code=status.HTTP_201_CREATED,
    summary="Cadastra o preco de um servico (admin)",
    responses={409: {"description": "Codigo ja cadastrado."}},
)
def cadastrar_servico(
    body: CadastrarServicoRequest, usuario: Admin, servicos: Servicos
) -> ServicoResponse:
    dto = servicos.cadastrar(
        codigo=body.codigo, nome=body.nome, descricao=body.descricao, preco=body.preco
    )
    _auditar(usuario, "cadastrar_servico", dto.codigo)
    return ServicoResponse(**dataclasses.asdict(dto))


@router.get("/servicos", summary="Lista os precos de servicos (paginado)")
def listar_servicos(
    _usuario: UsuarioInterno, servicos: Servicos, offset: Offset = 0, limit: Limit = 20
) -> ServicoListaResponse:
    pagina = servicos.listar(offset=offset, limit=limit)
    return ServicoListaResponse(
        items=[ServicoResponse(**dataclasses.asdict(p)) for p in pagina.itens],
        total=pagina.total,
        offset=pagina.offset,
        limit=pagina.limit,
    )


@router.get(
    "/servicos/{codigo}",
    summary="Consulta o preco de um servico",
    responses={404: {"description": "Codigo nao cadastrado."}},
)
def obter_servico(
    codigo: str, _usuario: UsuarioInterno, servicos: Servicos
) -> ServicoResponse:
    return ServicoResponse(**dataclasses.asdict(servicos.obter(codigo)))


@router.put(
    "/servicos/{codigo}",
    summary="Atualiza nome, descricao, preco e situacao de um servico (admin)",
    responses={404: {"description": "Codigo nao cadastrado."}},
)
def atualizar_servico(
    codigo: str, body: AtualizarServicoRequest, usuario: Admin, servicos: Servicos
) -> ServicoResponse:
    dto = servicos.atualizar(
        codigo,
        nome=body.nome,
        descricao=body.descricao,
        preco=body.preco,
        ativo=body.ativo,
    )
    _auditar(usuario, "atualizar_servico", codigo)
    return ServicoResponse(**dataclasses.asdict(dto))


@router.delete(
    "/servicos/{codigo}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Desativa um servico (admin); orcamentos ja gerados nao mudam",
    responses={404: {"description": "Codigo nao cadastrado."}},
)
def desativar_servico(codigo: str, usuario: Admin, servicos: Servicos) -> None:
    servicos.desativar(codigo)
    _auditar(usuario, "desativar_servico", codigo)


@router.post(
    "/pecas",
    status_code=status.HTTP_201_CREATED,
    summary="Cadastra o preco de uma peca (admin)",
    responses={409: {"description": "SKU ja cadastrado."}},
)
def cadastrar_peca(
    body: CadastrarPecaRequest, usuario: Admin, pecas: Pecas
) -> PecaResponse:
    dto = pecas.cadastrar(sku=body.sku, nome=body.nome, preco=body.preco)
    _auditar(usuario, "cadastrar_peca", dto.sku)
    return PecaResponse(**dataclasses.asdict(dto))


@router.get("/pecas", summary="Lista os precos de pecas (paginado)")
def listar_pecas(
    _usuario: UsuarioInterno, pecas: Pecas, offset: Offset = 0, limit: Limit = 20
) -> PecaListaResponse:
    pagina = pecas.listar(offset=offset, limit=limit)
    return PecaListaResponse(
        items=[PecaResponse(**dataclasses.asdict(p)) for p in pagina.itens],
        total=pagina.total,
        offset=pagina.offset,
        limit=pagina.limit,
    )


@router.get(
    "/pecas/{sku}",
    summary="Consulta o preco de uma peca",
    responses={404: {"description": "SKU nao cadastrado."}},
)
def obter_peca(sku: str, _usuario: UsuarioInterno, pecas: Pecas) -> PecaResponse:
    return PecaResponse(**dataclasses.asdict(pecas.obter(sku)))


@router.put(
    "/pecas/{sku}",
    summary="Atualiza nome, preco e situacao de uma peca (admin)",
    responses={404: {"description": "SKU nao cadastrado."}},
)
def atualizar_peca(
    sku: str, body: AtualizarPecaRequest, usuario: Admin, pecas: Pecas
) -> PecaResponse:
    dto = pecas.atualizar(sku, nome=body.nome, preco=body.preco, ativo=body.ativo)
    _auditar(usuario, "atualizar_peca", sku)
    return PecaResponse(**dataclasses.asdict(dto))


@router.delete(
    "/pecas/{sku}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Desativa uma peca (admin); orcamentos ja gerados nao mudam",
    responses={404: {"description": "SKU nao cadastrado."}},
)
def desativar_peca(sku: str, usuario: Admin, pecas: Pecas) -> None:
    pecas.desativar(sku)
    _auditar(usuario, "desativar_peca", sku)


@router.post(
    "/validacao",
    summary="Valida codigos de servicos e skus de pecas (Execucao -> Billing)",
)
def validar_itens(
    body: ValidacaoRequest,
    _usuario: Annotated[UsuarioAutenticado, Depends(exigir_papel(Papel.MECANICO))],
    validar: Annotated[ValidarItens, Depends(obter_validar_itens)],
) -> ValidacaoResponse:
    """Devolve os codigos inexistentes ou inativos; lista vazia = todos validos."""
    return ValidacaoResponse(
        invalidos=validar.executar(servicos=body.servicos, pecas=body.pecas)
    )
