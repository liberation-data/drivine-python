from __future__ import annotations

from typing import Annotated

from drivine import GraphView, Relationship, Root
from drivine_sample.fragment.anonymous_web_user import AnonymousWebUser
from drivine_sample.fragment.guide_user import GuideUser


class AnonymousGuideUser(GraphView):
    """A required anonymous WebUser: only the GuideUsers whose web user carries the Anonymous label."""

    core: Annotated[GuideUser, Root()]
    web_user: Annotated[AnonymousWebUser, Relationship("IS_WEB_USER")]
