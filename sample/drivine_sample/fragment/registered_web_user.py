from __future__ import annotations

from drivine_sample.fragment.web_user import WebUser


class RegisteredWebUser(WebUser, labels=["Registered"]):
    """A registered web user: a WebUser with the Registered label."""

    email: str
