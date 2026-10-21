from __future__ import annotations

from typing import Annotated

from drivine import GraphView, Root
from drivine_sample.fragment.sample_doc_node import SampleDocNode


class SampleVectorView(GraphView):
    doc: Annotated[SampleDocNode, Root()]
