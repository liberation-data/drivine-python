from dataclasses import dataclass


@dataclass(frozen=True)
class NullAsDefault:
    """
    Loads a stored null as the field's default: `status: Annotated[str, NullAsDefault()] = "active"`. A property the
    node doesn't have gets the default anyway. TypeScript's `@Default`.
    """
