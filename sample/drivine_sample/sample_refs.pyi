# Written by drivine: write it again when the models change, rather than editing it.
#   python -m drivine.stubs drivine_sample.fragment.anonymous_web_user drivine_sample.fragment.chunk_node drivine_sample.fragment.github_person drivine_sample.fragment.guide_user drivine_sample.fragment.issue drivine_sample.fragment.issue_core drivine_sample.fragment.issue_state_reason drivine_sample.fragment.oddly_stored drivine_sample.fragment.organization drivine_sample.fragment.person drivine_sample.fragment.registered_web_user drivine_sample.fragment.sample_article_node drivine_sample.fragment.sample_doc_node drivine_sample.fragment.sample_resolver_node drivine_sample.fragment.stamped_note drivine_sample.fragment.web_user drivine_sample.fragment.work_history drivine_sample.view.anonymous_guide_user drivine_sample.view.chunk_view drivine_sample.view.guide_user_with_optional_web_user drivine_sample.view.guide_user_with_polymorphic_web_user drivine_sample.view.guide_user_with_required_web_user drivine_sample.view.person_career drivine_sample.view.person_context drivine_sample.view.raised_and_assigned_issue drivine_sample.view.sample_full_text_view drivine_sample.view.sample_vector_view --out drivine_sample/sample_refs.py
from collections.abc import Callable
from typing import Any, Protocol, overload

from drivine import (
    BagRefs,
    ComparableRef,
    Condition,
    ListRef,
    NodeTests,
    StringRef,
    ValueRef,
    Where,
)
from drivine_sample.fragment.anonymous_web_user import AnonymousWebUser
from drivine_sample.fragment.chunk_node import ChunkNode
from drivine_sample.fragment.github_person import GithubPerson
from drivine_sample.fragment.guide_user import GuideUser
from drivine_sample.fragment.issue_core import IssueCore
from drivine_sample.fragment.issue_state_reason import IssueStateReason
from drivine_sample.fragment.oddly_stored import OddlyStored
from drivine_sample.fragment.organization import Organization
from drivine_sample.fragment.person import Person
from drivine_sample.fragment.registered_web_user import RegisteredWebUser
from drivine_sample.fragment.sample_article_node import SampleArticleNode
from drivine_sample.fragment.sample_doc_node import SampleDocNode
from drivine_sample.fragment.sample_resolver_node import SampleResolverNode
from drivine_sample.fragment.stamped_note import StampedNote
from drivine_sample.fragment.web_user import WebUser
from drivine_sample.view.anonymous_guide_user import AnonymousGuideUser
from drivine_sample.view.chunk_view import ChunkView
from drivine_sample.view.guide_user_with_optional_web_user import GuideUserWithOptionalWebUser
from drivine_sample.view.guide_user_with_polymorphic_web_user import GuideUserWithPolymorphicWebUser
from drivine_sample.view.guide_user_with_required_web_user import GuideUserWithRequiredWebUser
from drivine_sample.view.person_career import PersonCareer
from drivine_sample.view.person_context import PersonContext
from drivine_sample.view.raised_and_assigned_issue import RaisedAndAssignedIssue
from drivine_sample.view.sample_full_text_view import SampleFullTextView
from drivine_sample.view.sample_vector_view import SampleVectorView


class AnonymousWebUserRefs(NodeTests, Protocol):
    uuid: StringRef
    display_name: StringRef
    anonymous_token: StringRef


class AnonymousWebUserNodes(AnonymousWebUserRefs, Protocol):
    def any(self, conditions: Callable[[AnonymousWebUserNodes], Where], /) -> Condition: ...
    def none(self, conditions: Callable[[AnonymousWebUserNodes], Where], /) -> Condition: ...


class ChunkNodeRefs(NodeTests, Protocol):
    id: StringRef
    text: StringRef
    container_section_id: StringRef


class ChunkNodeNodes(ChunkNodeRefs, Protocol):
    def any(self, conditions: Callable[[ChunkNodeNodes], Where], /) -> Condition: ...
    def none(self, conditions: Callable[[ChunkNodeNodes], Where], /) -> Condition: ...


class GithubPersonRefs(NodeTests, Protocol):
    uuid: StringRef
    name: StringRef
    bio: StringRef
    github_id: StringRef


class GithubPersonNodes(GithubPersonRefs, Protocol):
    def any(self, conditions: Callable[[GithubPersonNodes], Where], /) -> Condition: ...
    def none(self, conditions: Callable[[GithubPersonNodes], Where], /) -> Condition: ...


class GuideUserRefs(NodeTests, Protocol):
    uuid: StringRef
    guide_progress: ComparableRef[int]


class GuideUserNodes(GuideUserRefs, Protocol):
    def any(self, conditions: Callable[[GuideUserNodes], Where], /) -> Condition: ...
    def none(self, conditions: Callable[[GuideUserNodes], Where], /) -> Condition: ...


class IssueCoreRefs(NodeTests, Protocol):
    uuid: StringRef
    id: ComparableRef[int]
    state: StringRef
    state_reason: ValueRef[IssueStateReason]
    title: StringRef
    body: StringRef
    locked: ValueRef[bool]


class IssueCoreNodes(IssueCoreRefs, Protocol):
    def any(self, conditions: Callable[[IssueCoreNodes], Where], /) -> Condition: ...
    def none(self, conditions: Callable[[IssueCoreNodes], Where], /) -> Condition: ...


class OddlyStoredRefs(NodeTests, Protocol):
    id: StringRef
    price: ComparableRef[float]
    rate: ComparableRef[float]
    greeting: StringRef
    # path: reached with field("path")


class OddlyStoredNodes(OddlyStoredRefs, Protocol):
    def any(self, conditions: Callable[[OddlyStoredNodes], Where], /) -> Condition: ...
    def none(self, conditions: Callable[[OddlyStoredNodes], Where], /) -> Condition: ...


class OrganizationRefs(NodeTests, Protocol):
    uuid: StringRef
    name: StringRef


class OrganizationNodes(OrganizationRefs, Protocol):
    def any(self, conditions: Callable[[OrganizationNodes], Where], /) -> Condition: ...
    def none(self, conditions: Callable[[OrganizationNodes], Where], /) -> Condition: ...


class PersonRefs(NodeTests, Protocol):
    uuid: StringRef
    name: StringRef
    bio: StringRef


class PersonNodes(PersonRefs, Protocol):
    def any(self, conditions: Callable[[PersonNodes], Where], /) -> Condition: ...
    def none(self, conditions: Callable[[PersonNodes], Where], /) -> Condition: ...


class RegisteredWebUserRefs(NodeTests, Protocol):
    uuid: StringRef
    display_name: StringRef
    email: StringRef


class RegisteredWebUserNodes(RegisteredWebUserRefs, Protocol):
    def any(self, conditions: Callable[[RegisteredWebUserNodes], Where], /) -> Condition: ...
    def none(self, conditions: Callable[[RegisteredWebUserNodes], Where], /) -> Condition: ...


class SampleArticleNodeRefs(NodeTests, Protocol):
    id: StringRef
    body: StringRef


class SampleArticleNodeNodes(SampleArticleNodeRefs, Protocol):
    def any(self, conditions: Callable[[SampleArticleNodeNodes], Where], /) -> Condition: ...
    def none(self, conditions: Callable[[SampleArticleNodeNodes], Where], /) -> Condition: ...


class SampleDocNodeRefs(NodeTests, Protocol):
    id: StringRef
    title: StringRef
    embedding: ListRef[float]


class SampleDocNodeNodes(SampleDocNodeRefs, Protocol):
    def any(self, conditions: Callable[[SampleDocNodeNodes], Where], /) -> Condition: ...
    def none(self, conditions: Callable[[SampleDocNodeNodes], Where], /) -> Condition: ...


class SampleResolverNodeRefs(NodeTests, Protocol):
    id: StringRef
    section_id: StringRef
    metadata: BagRefs


class SampleResolverNodeNodes(SampleResolverNodeRefs, Protocol):
    def any(self, conditions: Callable[[SampleResolverNodeNodes], Where], /) -> Condition: ...
    def none(self, conditions: Callable[[SampleResolverNodeNodes], Where], /) -> Condition: ...


class StampedNoteRefs(NodeTests, Protocol):
    id: StringRef
    text: StringRef
    stamp: StringRef


class StampedNoteNodes(StampedNoteRefs, Protocol):
    def any(self, conditions: Callable[[StampedNoteNodes], Where], /) -> Condition: ...
    def none(self, conditions: Callable[[StampedNoteNodes], Where], /) -> Condition: ...


class WebUserRefs(NodeTests, Protocol):
    uuid: StringRef
    display_name: StringRef


class WebUserNodes(WebUserRefs, Protocol):
    def any(self, conditions: Callable[[WebUserNodes], Where], /) -> Condition: ...
    def none(self, conditions: Callable[[WebUserNodes], Where], /) -> Condition: ...


class WorkHistoryRefs(NodeTests, Protocol):
    start_date: StringRef
    role: StringRef
    tags: ValueRef[Any]
    target: OrganizationNodes


class WorkHistoryNodes(WorkHistoryRefs, Protocol):
    def any(self, conditions: Callable[[WorkHistoryNodes], Where], /) -> Condition: ...
    def none(self, conditions: Callable[[WorkHistoryNodes], Where], /) -> Condition: ...


class AnonymousGuideUserRefs(NodeTests, Protocol):
    core: GuideUserNodes
    web_user: AnonymousWebUserNodes


class AnonymousGuideUserNodes(AnonymousGuideUserRefs, Protocol):
    def any(self, conditions: Callable[[AnonymousGuideUserNodes], Where], /) -> Condition: ...
    def none(self, conditions: Callable[[AnonymousGuideUserNodes], Where], /) -> Condition: ...


class ChunkViewRefs(NodeTests, Protocol):
    chunk: ChunkNodeNodes


class ChunkViewNodes(ChunkViewRefs, Protocol):
    def any(self, conditions: Callable[[ChunkViewNodes], Where], /) -> Condition: ...
    def none(self, conditions: Callable[[ChunkViewNodes], Where], /) -> Condition: ...


class GuideUserWithOptionalWebUserRefs(NodeTests, Protocol):
    core: GuideUserNodes
    web_user: WebUserNodes


class GuideUserWithOptionalWebUserNodes(GuideUserWithOptionalWebUserRefs, Protocol):
    def any(self, conditions: Callable[[GuideUserWithOptionalWebUserNodes], Where], /) -> Condition: ...
    def none(self, conditions: Callable[[GuideUserWithOptionalWebUserNodes], Where], /) -> Condition: ...


class GuideUserWithPolymorphicWebUserRefs(NodeTests, Protocol):
    core: GuideUserNodes
    web_user: WebUserNodes


class GuideUserWithPolymorphicWebUserNodes(GuideUserWithPolymorphicWebUserRefs, Protocol):
    def any(self, conditions: Callable[[GuideUserWithPolymorphicWebUserNodes], Where], /) -> Condition: ...
    def none(self, conditions: Callable[[GuideUserWithPolymorphicWebUserNodes], Where], /) -> Condition: ...


class GuideUserWithRequiredWebUserRefs(NodeTests, Protocol):
    core: GuideUserNodes
    web_user: WebUserNodes


class GuideUserWithRequiredWebUserNodes(GuideUserWithRequiredWebUserRefs, Protocol):
    def any(self, conditions: Callable[[GuideUserWithRequiredWebUserNodes], Where], /) -> Condition: ...
    def none(self, conditions: Callable[[GuideUserWithRequiredWebUserNodes], Where], /) -> Condition: ...


class PersonCareerRefs(NodeTests, Protocol):
    person: PersonNodes
    employment_history: WorkHistoryNodes


class PersonCareerNodes(PersonCareerRefs, Protocol):
    def any(self, conditions: Callable[[PersonCareerNodes], Where], /) -> Condition: ...
    def none(self, conditions: Callable[[PersonCareerNodes], Where], /) -> Condition: ...


class PersonContextRefs(NodeTests, Protocol):
    person: PersonNodes
    works_for: OrganizationNodes


class PersonContextNodes(PersonContextRefs, Protocol):
    def any(self, conditions: Callable[[PersonContextNodes], Where], /) -> Condition: ...
    def none(self, conditions: Callable[[PersonContextNodes], Where], /) -> Condition: ...


class RaisedAndAssignedIssueRefs(NodeTests, Protocol):
    issue: IssueCoreNodes
    assigned_to: PersonNodes
    raised_by: PersonContextNodes


class RaisedAndAssignedIssueNodes(RaisedAndAssignedIssueRefs, Protocol):
    def any(self, conditions: Callable[[RaisedAndAssignedIssueNodes], Where], /) -> Condition: ...
    def none(self, conditions: Callable[[RaisedAndAssignedIssueNodes], Where], /) -> Condition: ...


class SampleFullTextViewRefs(NodeTests, Protocol):
    article: SampleArticleNodeNodes


class SampleFullTextViewNodes(SampleFullTextViewRefs, Protocol):
    def any(self, conditions: Callable[[SampleFullTextViewNodes], Where], /) -> Condition: ...
    def none(self, conditions: Callable[[SampleFullTextViewNodes], Where], /) -> Condition: ...


class SampleVectorViewRefs(NodeTests, Protocol):
    doc: SampleDocNodeNodes


class SampleVectorViewNodes(SampleVectorViewRefs, Protocol):
    def any(self, conditions: Callable[[SampleVectorViewNodes], Where], /) -> Condition: ...
    def none(self, conditions: Callable[[SampleVectorViewNodes], Where], /) -> Condition: ...


@overload
def refs(type_: type[AnonymousWebUser], /) -> AnonymousWebUserRefs: ...


@overload
def refs(type_: type[RegisteredWebUser], /) -> RegisteredWebUserRefs: ...


@overload
def refs(type_: type[ChunkNode], /) -> ChunkNodeRefs: ...


@overload
def refs(type_: type[GithubPerson], /) -> GithubPersonRefs: ...


@overload
def refs(type_: type[GuideUser], /) -> GuideUserRefs: ...


@overload
def refs(type_: type[IssueCore], /) -> IssueCoreRefs: ...


@overload
def refs(type_: type[OddlyStored], /) -> OddlyStoredRefs: ...


@overload
def refs(type_: type[Organization], /) -> OrganizationRefs: ...


@overload
def refs(type_: type[Person], /) -> PersonRefs: ...


@overload
def refs(type_: type[SampleArticleNode], /) -> SampleArticleNodeRefs: ...


@overload
def refs(type_: type[SampleDocNode], /) -> SampleDocNodeRefs: ...


@overload
def refs(type_: type[SampleResolverNode], /) -> SampleResolverNodeRefs: ...


@overload
def refs(type_: type[StampedNote], /) -> StampedNoteRefs: ...


@overload
def refs(type_: type[WebUser], /) -> WebUserRefs: ...


@overload
def refs(type_: type[AnonymousGuideUser], /) -> AnonymousGuideUserRefs: ...


@overload
def refs(type_: type[ChunkView], /) -> ChunkViewRefs: ...


@overload
def refs(type_: type[GuideUserWithOptionalWebUser], /) -> GuideUserWithOptionalWebUserRefs: ...


@overload
def refs(type_: type[GuideUserWithPolymorphicWebUser], /) -> GuideUserWithPolymorphicWebUserRefs: ...


@overload
def refs(type_: type[GuideUserWithRequiredWebUser], /) -> GuideUserWithRequiredWebUserRefs: ...


@overload
def refs(type_: type[PersonCareer], /) -> PersonCareerRefs: ...


@overload
def refs(type_: type[PersonContext], /) -> PersonContextRefs: ...


@overload
def refs(type_: type[RaisedAndAssignedIssue], /) -> RaisedAndAssignedIssueRefs: ...


@overload
def refs(type_: type[SampleFullTextView], /) -> SampleFullTextViewRefs: ...


@overload
def refs(type_: type[SampleVectorView], /) -> SampleVectorViewRefs: ...


@overload
def refs(type_: type[object], /) -> Any: ...
