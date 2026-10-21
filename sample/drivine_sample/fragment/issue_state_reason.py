from enum import StrEnum


class IssueStateReason(StrEnum):
    COMPLETED = "COMPLETED"
    NOT_PLANNED = "NOT_PLANNED"
    REOPENED = "REOPENED"
    UNKNOWN = "UNKNOWN"
