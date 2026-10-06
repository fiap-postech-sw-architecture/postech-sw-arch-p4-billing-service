"""Casos de uso da tabela de precos (CRUD do admin e validacao de itens)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.compartilhado.dominio.dinheiro import Dinheiro
from src.precos.aplicacao.dtos import Pagina, PrecoPecaDTO, PrecoServicoDTO
from src.precos.dominio.exceptions import PrecoNaoEncontradoError
from src.precos.dominio.preco import PrecoPeca, PrecoServico
from src.precos.dominio.validacao import codigos_invalidos

if TYPE_CHECKING:
    from collections.abc import Sequence
    from decimal import Decimal

    from src.compartilhado.aplicacao.unit_of_work import UnitOfWork
    from src.precos.dominio.repository import (
        PrecoPecaRepository,
        PrecoServicoRepository,
    )


class PrecosDeServicos:
    """Cadastro de precos de servicos (escrita restrita ao admin na API)."""

    def __init__(self, uow: UnitOfWork, repo: PrecoServicoRepository) -> None:
        self._uow = uow
        self._repo = repo

    def cadastrar(
        self, *, codigo: str, nome: str, descricao: str, preco: Decimal
    ) -> PrecoServicoDTO:
        novo = PrecoServico.cadastrar(
            codigo=codigo, nome=nome, descricao=descricao, preco=Dinheiro(preco)
        )
        self._uow.executar(lambda: self._repo.salvar(novo))
        return PrecoServicoDTO.de(novo)

    def listar(self, *, offset: int, limit: int) -> Pagina[PrecoServicoDTO]:
        itens = self._repo.listar(offset=offset, limit=limit)
        return Pagina(
            itens=[PrecoServicoDTO.de(p) for p in itens],
            total=self._repo.contar(),
            offset=offset,
            limit=limit,
        )

    def obter(self, codigo: str) -> PrecoServicoDTO:
        return PrecoServicoDTO.de(self._obter(codigo))

    def atualizar(
        self, codigo: str, *, nome: str, descricao: str, preco: Decimal, ativo: bool
    ) -> PrecoServicoDTO:
        def trabalho() -> PrecoServico:
            atual = self._obter(codigo)
            atual.atualizar(
                nome=nome, descricao=descricao, preco=Dinheiro(preco), ativo=ativo
            )
            self._repo.salvar(atual)
            return atual

        return PrecoServicoDTO.de(self._uow.executar(trabalho))

    def desativar(self, codigo: str) -> None:
        def trabalho() -> None:
            atual = self._obter(codigo)
            atual.desativar()
            self._repo.salvar(atual)

        self._uow.executar(trabalho)

    def _obter(self, codigo: str) -> PrecoServico:
        preco = self._repo.obter_por_codigo(codigo)
        if preco is None:
            msg = f"Servico {codigo} nao encontrado na tabela de precos"
            raise PrecoNaoEncontradoError(msg)
        return preco


class PrecosDePecas:
    """Cadastro de precos de pecas (escrita restrita ao admin na API)."""

    def __init__(self, uow: UnitOfWork, repo: PrecoPecaRepository) -> None:
        self._uow = uow
        self._repo = repo

    def cadastrar(self, *, sku: str, nome: str, preco: Decimal) -> PrecoPecaDTO:
        nova = PrecoPeca.cadastrar(sku=sku, nome=nome, preco=Dinheiro(preco))
        self._uow.executar(lambda: self._repo.salvar(nova))
        return PrecoPecaDTO.de(nova)

    def listar(self, *, offset: int, limit: int) -> Pagina[PrecoPecaDTO]:
        itens = self._repo.listar(offset=offset, limit=limit)
        return Pagina(
            itens=[PrecoPecaDTO.de(p) for p in itens],
            total=self._repo.contar(),
            offset=offset,
            limit=limit,
        )

    def obter(self, sku: str) -> PrecoPecaDTO:
        return PrecoPecaDTO.de(self._obter(sku))

    def atualizar(
        self, sku: str, *, nome: str, preco: Decimal, ativo: bool
    ) -> PrecoPecaDTO:
        def trabalho() -> PrecoPeca:
            atual = self._obter(sku)
            atual.atualizar(nome=nome, preco=Dinheiro(preco), ativo=ativo)
            self._repo.salvar(atual)
            return atual

        return PrecoPecaDTO.de(self._uow.executar(trabalho))

    def desativar(self, sku: str) -> None:
        def trabalho() -> None:
            atual = self._obter(sku)
            atual.desativar()
            self._repo.salvar(atual)

        self._uow.executar(trabalho)

    def _obter(self, sku: str) -> PrecoPeca:
        preco = self._repo.obter_por_codigo(sku)
        if preco is None:
            msg = f"Peca {sku} nao encontrada na tabela de precos"
            raise PrecoNaoEncontradoError(msg)
        return preco


class ValidarItens:
    """Valida codigos de servicos e skus de pecas (chamada sincrona da Execucao)."""

    def __init__(
        self, servicos: PrecoServicoRepository, pecas: PrecoPecaRepository
    ) -> None:
        self._servicos = servicos
        self._pecas = pecas

    def executar(self, *, servicos: Sequence[str], pecas: Sequence[str]) -> list[str]:
        """Devolve os codigos invalidos (inexistentes ou inativos); vazio = ok."""
        return codigos_invalidos(
            servicos_solicitados=servicos,
            pecas_solicitadas=pecas,
            servicos=self._servicos.obter_por_codigos(set(servicos)),
            pecas=self._pecas.obter_por_codigos(set(pecas)),
        )
