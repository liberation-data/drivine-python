# Written by scripts/unasync.py from tests/integration/test_landing_page_sample.py:
# change that, and run the script again.
"""
The landing page's Python sample (drivine's website/src/pages/index.astro), as written, so the site can't drift from
the API. Only WorkHistory and Organization, which the sample leaves out, are added here.
"""

from typing import Annotated

from drivine.sync import (
    GraphView,
    NodeFragment,
    NodeId,
    PersistenceManager,
    Relationship,
    RelationshipFragment,
    Root,
    StatelessGraphObjectManager,
    TargetNode,
    refs,
)


class Person(NodeFragment, labels=["Person"]):
    uuid: Annotated[str, NodeId()]
    name: str
    bio: str | None = None


class Organization(NodeFragment, labels=["Organization"]):
    uuid: Annotated[str, NodeId()]
    name: str


class WorkHistory(RelationshipFragment):
    role: str
    organization: Annotated[Organization, TargetNode()]


class PersonCareer(GraphView):
    person: Annotated[Person, Root()]
    employment_history: Annotated[list[WorkHistory], Relationship("WORKS_FOR")] = []


def test_loads_everyone_whose_bio_mentions_leading(pm: PersistenceManager):
    gom = StatelessGraphObjectManager(pm)
    studio = Organization(uuid="landing-org", name="Studio")
    gom.save_all(
        [
            PersonCareer(
                person=Person(uuid="landing-lead", name="Lead", bio="Design Lead"),
                employment_history=[WorkHistory(role="Lead", organization=studio)],
            ),
            PersonCareer(
                person=Person(uuid="landing-dev", name="Dev", bio="Developer"),
                employment_history=[WorkHistory(role="Dev", organization=studio)],
            ),
            PersonCareer(person=Person(uuid="landing-head", name="Head", bio="leading the platform team")),
        ]
    )

    # Everyone whose bio mentions leading
    q = refs(PersonCareer)
    leads = gom.load_all(PersonCareer, where=q.person.bio.contains_ignore_case("lead"))

    assert sorted(it.person.name for it in leads) == ["Head", "Lead"]
    lead = next(it for it in leads if it.person.name == "Lead")
    assert [(it.role, it.organization.name) for it in lead.employment_history] == [("Lead", "Studio")]
