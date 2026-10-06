"""Tabela de precos: precos comerciais de servicos e de pecas.

Evolucao do ``catalogo_servicos`` do p3 @ 08dcffe (``ServicoOferecido``): o
servico ganha codigo de negocio estavel e a peca passa a ter preco comercial
aqui, ligada pelo ``sku`` a quantidade fisica do estoque na Execucao.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.compartilhado.dominio.aggregate_root import AggregateRoot
from src.compartilhado.dominio.exceptions import ValorInvalidoError

if TYPE_CHECKING:
    from uuid import UUID

    from src.compartilhado.dominio.dinheiro import Dinheiro

# Codigo de negocio: maiusculas, digitos e hifens (ex.: SRV-TROCA-OLEO,
# PEC-OLEO-5W30). Mesmo formato para codigo de servico e sku de peca.
_FORMATO_CODIGO = re.compile(r"[A-Z0-9]+(?:-[A-Z0-9]+)*")
TAMANHO_MAXIMO_CODIGO = 50


def _validar_codigo(rotulo: str, codigo: str) -> None:
    if len(codigo) > TAMANHO_MAXIMO_CODIGO or not _FORMATO_CODIGO.fullmatch(codigo):
        msg = (
            f"{rotulo} invalido: use maiusculas, digitos e hifens "
            f"(ate {TAMANHO_MAXIMO_CODIGO} caracteres)"
        )
        raise ValorInvalidoError(msg)


def _validar_texto(rotulo: str, valor: str) -> None:
    if not valor.strip():
        msg = f"{rotulo} nao pode ser vazio"
        raise ValorInvalidoError(msg)


def _validar_preco(preco: Dinheiro) -> None:
    if preco.valor <= 0:
        msg = f"Preco deve ser maior que zero (recebido: {preco.valor})"
        raise ValorInvalidoError(msg)


@dataclass(eq=False, kw_only=True)
class PrecoServico(AggregateRoot):
    _codigo: str
    _nome: str
    _descricao: str
    _preco: Dinheiro
    _ativo: bool = True

    def __post_init__(self) -> None:
        super().__post_init__()
        _validar_codigo("Codigo do servico", self._codigo)
        self._aplicar(self._nome, self._descricao, self._preco)

    @classmethod
    def cadastrar(
        cls, *, codigo: str, nome: str, descricao: str, preco: Dinheiro
    ) -> PrecoServico:
        """Servico novo na tabela, ativo para os proximos orcamentos."""
        return cls(_codigo=codigo, _nome=nome, _descricao=descricao, _preco=preco)

    @classmethod
    def reconstituir(
        cls,
        *,
        id: UUID,  # noqa: A002 - mesmo nome do campo herdado de Entity
        codigo: str,
        nome: str,
        descricao: str,
        preco: Dinheiro,
        ativo: bool,
    ) -> PrecoServico:
        """Reidrata do armazenamento: as invariantes valem de novo."""
        return cls(
            id=id,
            _codigo=codigo,
            _nome=nome,
            _descricao=descricao,
            _preco=preco,
            _ativo=ativo,
        )

    @property
    def codigo(self) -> str:
        return self._codigo

    @property
    def nome(self) -> str:
        return self._nome

    @property
    def descricao(self) -> str:
        return self._descricao

    @property
    def preco(self) -> Dinheiro:
        return self._preco

    @property
    def ativo(self) -> bool:
        return self._ativo

    def atualizar(
        self, *, nome: str, descricao: str, preco: Dinheiro, ativo: bool
    ) -> None:
        """Novo preco vale para orcamentos futuros; os gerados ficam congelados."""
        self._aplicar(nome, descricao, preco)
        self._ativo = ativo

    def desativar(self) -> None:
        """Retira o servico de novos orcamentos. Idempotente."""
        self._ativo = False

    def _aplicar(self, nome: str, descricao: str, preco: Dinheiro) -> None:
        _validar_texto("Nome do servico", nome)
        _validar_texto("Descricao do servico", descricao)
        _validar_preco(preco)
        self._nome = nome
        self._descricao = descricao
        self._preco = preco


@dataclass(eq=False, kw_only=True)
class PrecoPeca(AggregateRoot):
    _sku: str
    _nome: str
    _preco: Dinheiro
    _ativo: bool = True

    def __post_init__(self) -> None:
        super().__post_init__()
        _validar_codigo("SKU da peca", self._sku)
        self._aplicar(self._nome, self._preco)

    @classmethod
    def cadastrar(cls, *, sku: str, nome: str, preco: Dinheiro) -> PrecoPeca:
        """Peca nova na tabela, ativa para os proximos orcamentos."""
        return cls(_sku=sku, _nome=nome, _preco=preco)

    @classmethod
    def reconstituir(
        cls,
        *,
        id: UUID,  # noqa: A002 - mesmo nome do campo herdado de Entity
        sku: str,
        nome: str,
        preco: Dinheiro,
        ativo: bool,
    ) -> PrecoPeca:
        """Reidrata do armazenamento: as invariantes valem de novo."""
        return cls(id=id, _sku=sku, _nome=nome, _preco=preco, _ativo=ativo)

    @property
    def sku(self) -> str:
        return self._sku

    @property
    def nome(self) -> str:
        return self._nome

    @property
    def preco(self) -> Dinheiro:
        return self._preco

    @property
    def ativo(self) -> bool:
        return self._ativo

    def atualizar(self, *, nome: str, preco: Dinheiro, ativo: bool) -> None:
        """Novo preco vale para orcamentos futuros; os gerados ficam congelados."""
        self._aplicar(nome, preco)
        self._ativo = ativo

    def desativar(self) -> None:
        """Retira a peca de novos orcamentos. Idempotente."""
        self._ativo = False

    def _aplicar(self, nome: str, preco: Dinheiro) -> None:
        _validar_texto("Nome da peca", nome)
        _validar_preco(preco)
        self._nome = nome
        self._preco = preco
