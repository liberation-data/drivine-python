from __future__ import annotations

from typing import Annotated

from drivine import GraphView, Relationship, Root
from drivine_sample.fragment.guide_user import GuideUser
from drivine_sample.fragment.web_user import WebUser


class GuideUserWithPolymorphicWebUser(GraphView):
    """A WebUser loaded as the subclass its labels say."""

    core: Annotated[GuideUser, Root()]
    web_user: Annotated[WebUser | None, Relationship("IS_WEB_USER")] = None
