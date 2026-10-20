from __future__ import annotations

from typing import Annotated

from drivine import FullTextIndex, NodeFragment, NodeId


class SampleArticleNode(NodeFragment, labels=["SampleArticle"]):
    """A fragment with a full-text index, which load_matching searches."""

    id: Annotated[str, NodeId()]
    body: Annotated[str, FullTextIndex()]
