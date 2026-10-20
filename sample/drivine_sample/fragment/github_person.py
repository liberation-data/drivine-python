from __future__ import annotations

from typing import Annotated

from drivine import NodeFragment, NodeId


class GithubPerson(NodeFragment, labels=["Person", "Mapped", "GithubPerson"]):
    """A person with the labels of a GitHub user too: not a subclass of Person, as in drivine4j's sample."""

    uuid: Annotated[str, NodeId()]
    name: str
    bio: str | None = None
    github_id: str
