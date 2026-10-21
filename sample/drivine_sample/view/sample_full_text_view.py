from __future__ import annotations

from typing import Annotated

from drivine import GraphView, Root
from drivine_sample.fragment.sample_article_node import SampleArticleNode


class SampleFullTextView(GraphView):
    article: Annotated[SampleArticleNode, Root()]
