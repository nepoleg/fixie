"""Immutable domain types shared by the gateway, event collection and rendering."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

BOT_SIGN = "🤖"


class RunStatus(StrEnum):
    """Outcome of one agent run on a ticket, or fixie giving up on it."""

    DONE = "done"
    BLOCKED = "blocked"
    NEEDS_INFO = "needs_info"
    GAVE_UP = "gave_up"


class EventType(StrEnum):
    """Kind of activity that can trigger or feed an agent run."""

    TICKET_COMMENT = "ticket_comment"
    PR_COMMENT = "pr_comment"
    PR_PUSH = "pr_push"
    BUILD_FAILED = "build_failed"
    REOPENED = "reopened"
    STATE_CHANGE = "state_change"


@dataclass(frozen=True)
class Identity:
    display_name: str
    unique_name: str


@dataclass(frozen=True)
class Ticket:
    id: int
    title: str
    work_item_type: str
    state: str
    tags: tuple[str, ...]
    description: str
    acceptance_criteria: str
    repro_steps: str
    changed_date: datetime
    priority: int = 2
    markdown_fields: frozenset[str] = frozenset()


@dataclass(frozen=True)
class TicketUpdate:
    id: int
    revised_by: Identity
    revised_date: datetime
    tags_added: tuple[str, ...]
    tags_removed: tuple[str, ...]
    state_from: str | None
    state_to: str | None
    description_changed: bool = False


@dataclass(frozen=True)
class Comment:
    id: int
    text: str
    created_by: Identity
    created_date: datetime
    modified_by: Identity
    modified_date: datetime


@dataclass(frozen=True)
class Attachment:
    id: str
    name: str
    url: str


@dataclass(frozen=True)
class PullRequest:
    id: int
    title: str
    description: str
    source_branch: str
    target_branch: str
    status: str
    is_draft: bool
    work_item_ids: tuple[int, ...]
    created_date: datetime


@dataclass(frozen=True)
class PrComment:
    id: int
    author: Identity
    content: str
    published_date: datetime
    last_updated_date: datetime
    comment_type: str


@dataclass(frozen=True)
class PrThread:
    id: int
    status: str | None
    file_path: str | None
    line: int | None
    comments: tuple[PrComment, ...]
    published_date: datetime
    last_updated_date: datetime


@dataclass(frozen=True)
class Build:
    id: int
    status: str
    result: str | None
    queue_time: datetime
    finish_time: datetime | None
    source_version: str
    requested_for: Identity


@dataclass(frozen=True)
class Commit:
    sha: str
    author: Identity
    date: datetime
    message: str


@dataclass(frozen=True)
class Event:
    type: EventType
    author: Identity
    time: datetime
    content: str
    allowlisted: bool


@dataclass(frozen=True)
class FixieStatus:
    run_started: datetime | None = None
    last_finished_run_started: datetime | None = None
    result: RunStatus | None = None
    pr_id: int | None = None
    head: str | None = None
    state: str | None = None
    last_build_id: int | None = None
    attempts: int = 0


@dataclass(frozen=True)
class RunResult:
    status: RunStatus
    summary: str
    questions: tuple[str, ...] = ()


class Action(StrEnum):
    """What the orchestrator should do with a ticket this poll."""

    NEW = "new"
    SKIP = "skip"
    RESUME = "resume"
    REBUILD = "rebuild"
    RETRY = "retry"
    GIVE_UP = "give_up"
    CLEAN_UP = "clean_up"
    STOP = "stop"


@dataclass(frozen=True)
class TicketSnapshot:
    ticket: Ticket
    updates: tuple[TicketUpdate, ...]
    comments: tuple[Comment, ...]
    pull_request: PullRequest | None
    threads: tuple[PrThread, ...]
    builds: tuple[Build, ...]
    head: Commit | None


@dataclass(frozen=True)
class Classification:
    action: Action
    events: tuple[Event, ...] = ()
