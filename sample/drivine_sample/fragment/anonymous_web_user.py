from __future__ import annotations

from drivine_sample.fragment.web_user import WebUser


class AnonymousWebUser(WebUser, labels=["Anonymous"]):
    """An anonymous web user: a WebUser with the Anonymous label."""

    anonymous_token: str
