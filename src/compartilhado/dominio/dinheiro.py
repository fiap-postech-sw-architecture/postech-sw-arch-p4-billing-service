from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Final

from src.compartilhado.dominio.exceptions import ValorInvalidoError
from src.compartilhado.dominio.value_object import ValueObject

_DUAS_CASAS = Decimal("0.01")
_TAMANHO_CODIGO_MOEDA = 3
# Teto do ``dinheiro`` nos contratos de mensagem (RFC-004, secao 5.3): ate 10
# digitos inteiros e 2 casas. Acima disso o valor nao cabe no evento.
DIGITOS_INTEIROS: Final = 10
_TETO = Decimal(10) ** DIGITOS_INTEIROS
_ACIMA_DO_TETO = f"Valor monetario acima de {DIGITOS_INTEIROS} digitos inteiros"


@dataclass(frozen=True, slots=True)
class Dinheiro(ValueObject):
    """Value Object monetario com moeda e precisao de 2 casas decimais.

    Usa Decimal para aritmetica exata (nunca float). Impoe valores nao
    negativos, finitos, abaixo do teto de ``DIGITOS_INTEIROS`` digitos e codigo
    de moeda ISO 4217 com 3 letras maiusculas. Operacoes retornam novas
    instancias e exigem a mesma moeda nas parcelas; resultado acima do teto e
    ``ValorInvalidoError``, como qualquer valor invalido.
    """

    valor: Decimal
    moeda: str = "BRL"

    def __post_init__(self) -> None:
        if not isinstance(self.valor, Decimal):
            try:
                object.__setattr__(self, "valor", Decimal(str(self.valor)))
            except InvalidOperation as exc:
                msg = "valor monetario invalido"
                raise ValorInvalidoError(msg) from exc

        if not self.valor.is_finite():
            msg = "Valor monetario deve ser finito"
            raise ValorInvalidoError(msg)

        # Teto antes do quantize: um expoente gigante estouraria a precisao do
        # contexto (InvalidOperation) em vez de ser recusado como valor.
        if abs(self.valor) >= _TETO:
            raise ValorInvalidoError(_ACIMA_DO_TETO)
        quantizado = self.valor.quantize(_DUAS_CASAS, rounding=ROUND_HALF_UP)
        # Normaliza zero negativo (-0.00 -> 0.00) antes das validacoes.
        quantizado += Decimal(0)
        if quantizado >= _TETO:  # 9999999999.995 arredonda para o teto
            raise ValorInvalidoError(_ACIMA_DO_TETO)
        object.__setattr__(self, "valor", quantizado)

        if self.valor < 0:
            msg = f"Valor nao pode ser negativo: {self.valor}"
            raise ValorInvalidoError(msg)

        moeda_valida = (
            len(self.moeda) == _TAMANHO_CODIGO_MOEDA
            # isascii: isalpha/isupper aceitam letras acentuadas; ISO 4217 e A-Z.
            and self.moeda.isascii()
            and self.moeda.isalpha()
            and self.moeda.isupper()
        )
        if not moeda_valida:
            msg = f"Moeda deve ter 3 letras maiusculas: {self.moeda}"
            raise ValorInvalidoError(msg)

    def __add__(self, outro: Dinheiro) -> Dinheiro:
        if not isinstance(outro, Dinheiro):
            return NotImplemented
        if self.moeda != outro.moeda:
            msg = f"Moedas diferentes: {self.moeda} e {outro.moeda}"
            raise ValorInvalidoError(msg)
        return Dinheiro(valor=self.valor + outro.valor, moeda=self.moeda)

    def __mul__(self, fator: int) -> Dinheiro:
        if not isinstance(fator, int):
            return NotImplemented
        return Dinheiro(valor=self.valor * fator, moeda=self.moeda)

    def __rmul__(self, fator: int) -> Dinheiro:
        return self.__mul__(fator)
