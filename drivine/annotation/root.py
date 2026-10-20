from dataclasses import dataclass


@dataclass(frozen=True)
class Root:
    """
    Marks a view's root, the node fragment the query starts from: `person: Annotated[Person, Root()]`. A view has one.
    """
