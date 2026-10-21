from __future__ import annotations

from typing import Annotated

from drivine import GraphView, Relationship, Root
from drivine_sample.fragment.guide_user import GuideUser
from drivine_sample.fragment.web_user import WebUser


class GuideUserWithOptionalWebUser(GraphView):
    """An optional WebUser: every GuideUser, with or without one."""

    core: Annotated[GuideUser, Root()]
    web_user: Annotated[WebUser | None, Relationship("IS_WEB_USER")] = None
