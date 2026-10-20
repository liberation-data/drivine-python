from dataclasses import dataclass


@dataclass(frozen=True)
class GraphProperty:
    """
    Stores a field under the given property name, rather than the one the fragment's naming gives it:
    `url_v2: Annotated[str, GraphProperty("urlV2")]`.
    """

    name: str
