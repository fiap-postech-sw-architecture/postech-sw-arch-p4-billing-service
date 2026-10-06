from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

# Espelha o formato validado no dominio (maiusculas, digitos e hifens).
_PADRAO_CODIGO = r"^[A-Z0-9]+(?:-[A-Z0-9]+)*$"
_TAMANHO_CODIGO = 50

Codigo = Annotated[str, Field(pattern=_PADRAO_CODIGO, max_length=_TAMANHO_CODIGO)]
Preco = Annotated[Decimal, Field(gt=0, max_digits=10, decimal_places=2)]
Nome = Annotated[str, Field(min_length=1, max_length=120)]
Descricao = Annotated[str, Field(min_length=1, max_length=2000)]
# Na validacao o codigo nao segue o padrao: codigo malformado e so mais um
# invalido na resposta, nao um 422 para a Execucao.
CodigoConsultado = Annotated[str, Field(min_length=1, max_length=_TAMANHO_CODIGO)]


class CadastrarServicoRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    codigo: Codigo = Field(examples=["SRV-TROCA-OLEO"])
    nome: Nome = Field(examples=["Troca de oleo"])
    descricao: Descricao
    preco: Preco = Field(examples=["120.00"])


class AtualizarServicoRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nome: Nome
    descricao: Descricao
    preco: Preco
    ativo: bool


class ServicoResponse(BaseModel):
    codigo: str
    nome: str
    descricao: str
    preco: Decimal
    moeda: str
    ativo: bool


class ServicoListaResponse(BaseModel):
    items: list[ServicoResponse]
    total: int
    offset: int
    limit: int


class CadastrarPecaRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sku: Codigo = Field(examples=["PEC-OLEO-5W30"])
    nome: Nome = Field(examples=["Oleo de motor 5W30 (1 L)"])
    preco: Preco = Field(examples=["45.00"])


class AtualizarPecaRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nome: Nome
    preco: Preco
    ativo: bool


class PecaResponse(BaseModel):
    sku: str
    nome: str
    preco: Decimal
    moeda: str
    ativo: bool


class PecaListaResponse(BaseModel):
    items: list[PecaResponse]
    total: int
    offset: int
    limit: int


class ValidacaoRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    servicos: list[CodigoConsultado] = Field(default_factory=list, max_length=200)
    pecas: list[CodigoConsultado] = Field(default_factory=list, max_length=200)


class ValidacaoResponse(BaseModel):
    invalidos: list[str]
