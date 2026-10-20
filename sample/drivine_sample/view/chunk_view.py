from __future__ import annotations

from typing import Annotated

from drivine import GraphView, Root
from drivine_sample.fragment.chunk_node import ChunkNode


class ChunkView(GraphView):
    chunk: Annotated[ChunkNode, Root()]
