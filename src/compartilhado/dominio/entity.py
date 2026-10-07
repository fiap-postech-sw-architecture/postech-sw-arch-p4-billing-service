from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID, uuid4


@dataclass(eq=False)
class Entity:
    id: UUID = field(default_factory=uuid4)

    def __setattr__(self, name: str, value: object) -> None:
        # Guarda de imutabilidade da identidade: a primeira atribuicao de ``id``
        # (no __init__ do dataclass ou na reidratacao pelo repositorio) passa;
        # qualquer reatribuicao e rejeitada.
        if name == "id" and "id" in self.__dict__:
            msg = "Identidade da entidade nao pode ser alterada apos criacao"
            raise AttributeError(msg)
        super().__setattr__(name, value)

    def __post_init__(self) -> None:
        # Hook vazio: subclasses com invariantes proprias chamam
        # super().__post_init__().
        pass

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, type(self)):
            return NotImplemented
        return self.id == other.id

    def __hash__(self) -> int:
        return hash(self.id)
