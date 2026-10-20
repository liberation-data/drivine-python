from __future__ import annotations

from typing import Annotated

from drivine import GraphView, Relationship, Root
from drivine_sample.fragment.guide_user import GuideUser
from drivine_sample.fragment.web_user import WebUser


class GuideUserWithRequiredWebUser(GraphView):
    """A required WebUser, typed without None: only the GuideUsers that have one."""

    core: Annotated[GuideUser, Root()]
    web_user: Annotated[WebUser, Relationship("IS_WEB_USER")]
