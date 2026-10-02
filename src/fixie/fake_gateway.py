"""In-memory AdoGateway backed by a JSON fixture, for tests that never touch Azure DevOps."""

import base64
import json
import threading
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from fixie.gateway import OPEN_STATES, AdoGateway
from fixie.model import (
    Attachment,
    Build,
    Comment,
    Commit,
    Identity,
    PrComment,
    PrThread,
    PullRequest,
    Ticket,
    TicketUpdate,
)


def _parse_dt(value: str, field_name: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError(f"{field_name} is not timezone-aware: {value!r}")
    return dt


def _parse_identity(d: dict) -> Identity:
    return Identity(d["display_name"], d["unique_name"])


def _parse_ticket(ticket_id: int, d: dict) -> Ticket:
    return Ticket(
        id=ticket_id,
        title=d["title"],
        work_item_type=d["work_item_type"],
        state=d["state"],
        tags=tuple(d["tags"]),
        description=d["description"],
        acceptance_criteria=d.get("acceptance_criteria", ""),
        repro_steps=d.get("repro_steps", ""),
        changed_date=_parse_dt(d["changed_date"], "changed_date"),
        priority=d.get("priority", 2),
    )


def _parse_update(d: dict) -> TicketUpdate:
    return TicketUpdate(
        id=d["id"],
        revised_by=_parse_identity(d["revised_by"]),
        revised_date=_parse_dt(d["revised_date"], "revised_date"),
        tags_added=tuple(d.get("tags_added", [])),
        tags_removed=tuple(d.get("tags_removed", [])),
        state_from=d.get("state_from"),
        state_to=d.get("state_to"),
        description_changed=d.get("description_changed", False),
    )


def _parse_comment(d: dict) -> Comment:
    return Comment(
        id=d["id"],
        text=d["text"],
        created_by=_parse_identity(d["created_by"]),
        created_date=_parse_dt(d["created_date"], "created_date"),
        modified_by=_parse_identity(d["modified_by"]),
        modified_date=_parse_dt(d["modified_date"], "modified_date"),
    )


def _parse_attachment(d: dict) -> Attachment:
    return Attachment(id=d["id"], name=d["name"], url=d["url"])


def _parse_pr(pr_id: int, d: dict) -> PullRequest:
    return PullRequest(
        id=pr_id,
        title=d["title"],
        description=d["description"],
        source_branch=d["source_branch"],
        target_branch=d["target_branch"],
        status=d["status"],
        is_draft=d["is_draft"],
        work_item_ids=tuple(d.get("work_item_ids", [])),
        created_date=_parse_dt(d["created_date"], "created_date"),
    )


def _parse_pr_comment(d: dict) -> PrComment:
    return PrComment(
        id=d["id"],
        author=_parse_identity(d["author"]),
        content=d["content"],
        published_date=_parse_dt(d["published_date"], "published_date"),
        last_updated_date=_parse_dt(d["last_updated_date"], "last_updated_date"),
        comment_type=d["comment_type"],
    )


def _parse_thread(d: dict) -> PrThread:
    return PrThread(
        id=d["id"],
        status=d.get("status"),
        file_path=d.get("file_path"),
        line=d.get("line"),
        comments=tuple(_parse_pr_comment(c) for c in d.get("comments", [])),
        published_date=_parse_dt(d["published_date"], "published_date"),
        last_updated_date=_parse_dt(d["last_updated_date"], "last_updated_date"),
    )


def _parse_build(d: dict) -> Build:
    return Build(
        id=d["id"],
        status=d["status"],
        result=d.get("result"),
        queue_time=_parse_dt(d["queue_time"], "queue_time"),
        finish_time=_parse_dt(d["finish_time"], "finish_time") if d.get("finish_time") else None,
        source_version=d["source_version"],
        requested_for=_parse_identity(d["requested_for"]),
    )


def _parse_commit(d: dict) -> Commit:
    return Commit(
        sha=d["sha"],
        author=_parse_identity(d["author"]),
        date=_parse_dt(d["date"], "date"),
        message=d["message"],
    )


class FakeGateway(AdoGateway):
    """In-memory AdoGateway loaded from a JSON fixture; records every write for assertions."""

    def __init__(self, fixture: Path, owner: Identity):
        data = json.loads(Path(fixture).read_text(encoding="utf-8"))
        self.now = _parse_dt(data["now"], "now")
        self.owner = owner
        self.writes: list[tuple[str, dict[str, object]]] = []
        self._lock = threading.RLock()

        self.tickets: dict[int, Ticket] = {}
        self.updates: dict[int, list[TicketUpdate]] = {}
        self.comments: dict[int, list[Comment]] = {}
        self.attachments: dict[int, list[Attachment]] = {}
        self.blobs: dict[str, bytes] = {}
        for ticket_id_str, ticket_data in data.get("tickets", {}).items():
            ticket_id = int(ticket_id_str)
            self.tickets[ticket_id] = _parse_ticket(ticket_id, ticket_data)
            self.updates[ticket_id] = [_parse_update(u) for u in ticket_data.get("updates", [])]
            self.comments[ticket_id] = [_parse_comment(c) for c in ticket_data.get("comments", [])]
            attachments = []
            for a in ticket_data.get("attachments", []):
                attachments.append(_parse_attachment(a))
                self.blobs[a["url"]] = base64.b64decode(a["content_b64"])
            self.attachments[ticket_id] = attachments

        self.pull_requests: dict[int, PullRequest] = {}
        self.threads: dict[int, list[PrThread]] = {}
        self.builds: dict[int, list[Build]] = {}
        for pr_id_str, pr_data in data.get("pull_requests", {}).items():
            pr_id = int(pr_id_str)
            self.pull_requests[pr_id] = _parse_pr(pr_id, pr_data)
            self.threads[pr_id] = [_parse_thread(t) for t in pr_data.get("threads", [])]
            self.builds[pr_id] = [_parse_build(b) for b in pr_data.get("builds", [])]

        self._attachment_seq = len(self.blobs)

        self.branches: dict[str, Commit] = {
            name: _parse_commit(b) for name, b in data.get("branches", {}).items()
        }

    def _tick(self) -> datetime:
        timestamp = self.now
        self.now = self.now + timedelta(seconds=1)
        return timestamp

    def _record(self, method: str, params: dict[str, object]) -> None:
        self.writes.append((method, params))

    def find_tagged_tickets(self, tag: str) -> list[int]:
        tag_lower = tag.lower()
        with self._lock:
            return [
                ticket.id
                for ticket in sorted(self.tickets.values(), key=lambda t: (t.priority, t.id))
                if ticket.state in OPEN_STATES and any(t.lower() == tag_lower for t in ticket.tags)
            ]

    def get_ticket(self, ticket_id: int) -> Ticket:
        with self._lock:
            return self.tickets[ticket_id]

    def get_updates(self, ticket_id: int) -> list[TicketUpdate]:
        with self._lock:
            return list(self.updates.get(ticket_id, []))

    def get_comments(self, ticket_id: int) -> list[Comment]:
        with self._lock:
            return list(self.comments.get(ticket_id, []))

    def get_attachments(self, ticket_id: int) -> list[Attachment]:
        with self._lock:
            return list(self.attachments.get(ticket_id, []))

    def get_pull_request(self, pr_id: int) -> PullRequest:
        with self._lock:
            return self.pull_requests[pr_id]

    def find_pull_requests(self, source_branch: str) -> list[PullRequest]:
        with self._lock:
            return sorted(
                (pr for pr in self.pull_requests.values() if pr.source_branch == source_branch),
                key=lambda pr: pr.id,
            )

    def get_threads(self, pr_id: int) -> list[PrThread]:
        with self._lock:
            return list(self.threads.get(pr_id, []))

    def get_builds(self, pr_id: int) -> list[Build]:
        with self._lock:
            return list(self.builds.get(pr_id, []))

    def get_branch_head(self, branch: str) -> Commit | None:
        with self._lock:
            return self.branches.get(branch)

    def add_comment(self, ticket_id: int, text: str) -> Comment:
        with self._lock:
            if ticket_id not in self.tickets:
                raise KeyError(ticket_id)
            new_id = max((c.id for cs in self.comments.values() for c in cs), default=0) + 1
            timestamp = self._tick()
            comment = Comment(
                id=new_id,
                text=text,
                created_by=self.owner,
                created_date=timestamp,
                modified_by=self.owner,
                modified_date=timestamp,
            )
            self.comments.setdefault(ticket_id, []).append(comment)
            self._record("add_comment", {"ticket_id": ticket_id, "text": text})
            return comment

    def remove_tag(self, ticket_id: int, tag: str) -> None:
        with self._lock:
            ticket = self.tickets[ticket_id]
            tag_lower = tag.lower()
            kept = tuple(t for t in ticket.tags if t.lower() != tag_lower)
            if kept != ticket.tags:
                self._tick()
                self.tickets[ticket_id] = replace(ticket, tags=kept)
            self._record("remove_tag", {"ticket_id": ticket_id, "tag": tag})

    def set_state(self, ticket_id: int, state: str) -> None:
        with self._lock:
            ticket = self.tickets[ticket_id]
            timestamp = self._tick()
            if ticket.state != state:
                self.updates.setdefault(ticket_id, []).append(
                    TicketUpdate(
                        id=len(self.updates.get(ticket_id, [])) + 1,
                        revised_by=self.owner,
                        revised_date=timestamp,
                        tags_added=(),
                        tags_removed=(),
                        state_from=ticket.state,
                        state_to=state,
                    )
                )
                self.tickets[ticket_id] = replace(ticket, state=state)
            self._record("set_state", {"ticket_id": ticket_id, "state": state})

    def reply_to_thread(self, pr_id: int, thread_id: int, text: str) -> PrComment:
        with self._lock:
            threads = self.threads.get(pr_id)
            if threads is None:
                raise KeyError(pr_id)
            for i, thread in enumerate(threads):
                if thread.id == thread_id:
                    timestamp = self._tick()
                    reply = PrComment(
                        id=len(thread.comments) + 1,
                        author=self.owner,
                        content=text,
                        published_date=timestamp,
                        last_updated_date=timestamp,
                        comment_type="text",
                    )
                    threads[i] = replace(thread, comments=thread.comments + (reply,), last_updated_date=timestamp)
                    self._record("reply_to_thread", {"pr_id": pr_id, "thread_id": thread_id, "text": text})
                    return reply
            raise KeyError(thread_id)

    def edit_comment(self, ticket_id: int, comment_id: int, text: str) -> Comment:
        with self._lock:
            if ticket_id not in self.tickets:
                raise KeyError(ticket_id)
            comments = self.comments.get(ticket_id, [])
            for i, comment in enumerate(comments):
                if comment.id == comment_id:
                    timestamp = self._tick()
                    edited = replace(comment, text=text, modified_by=self.owner, modified_date=timestamp)
                    comments[i] = edited
                    self._record("edit_comment", {"ticket_id": ticket_id, "comment_id": comment_id, "text": text})
                    return edited
            raise KeyError(comment_id)

    def create_pull_request(self, source_branch: str, target_branch: str, title: str, description: str) -> PullRequest:
        with self._lock:
            if any(pr.source_branch == source_branch and pr.status == "active" for pr in self.pull_requests.values()):
                raise ValueError(f"active pull request exists for {source_branch}")
            new_id = max(self.pull_requests.keys(), default=0) + 1
            timestamp = self._tick()
            pr = PullRequest(
                id=new_id,
                title=title,
                description=description,
                source_branch=source_branch,
                target_branch=target_branch,
                status="active",
                is_draft=False,
                work_item_ids=(),
                created_date=timestamp,
            )
            self.pull_requests[new_id] = pr
            self.threads[new_id] = []
            self.builds[new_id] = []
            self._record(
                "create_pull_request",
                {"source_branch": source_branch, "target_branch": target_branch, "title": title, "description": description},
            )
            return pr

    def update_pull_request(self, pr_id: int, title: str, description: str) -> PullRequest:
        with self._lock:
            if pr_id not in self.pull_requests:
                raise KeyError(pr_id)
            self._tick()
            updated = replace(self.pull_requests[pr_id], title=title, description=description)
            self.pull_requests[pr_id] = updated
            self._record("update_pull_request", {"pr_id": pr_id, "title": title, "description": description})
            return updated

    def link_work_item(self, pr_id: int, ticket_id: int) -> None:
        with self._lock:
            if pr_id not in self.pull_requests:
                raise KeyError(pr_id)
            if ticket_id not in self.tickets:
                raise KeyError(ticket_id)
            self._tick()
            pr = self.pull_requests[pr_id]
            if ticket_id not in pr.work_item_ids:
                self.pull_requests[pr_id] = replace(pr, work_item_ids=pr.work_item_ids + (ticket_id,))
            self._record("link_work_item", {"pr_id": pr_id, "ticket_id": ticket_id})

    def attach_file(self, ticket_id: int, name: str, content: bytes) -> Attachment:
        with self._lock:
            if ticket_id not in self.tickets:
                raise KeyError(ticket_id)
            self._tick()
            self._attachment_seq += 1
            new_id = f"att-{self._attachment_seq}"
            url = f"https://fake.ado/attachments/{new_id}"
            attachment = Attachment(id=new_id, name=name, url=url)
            self.attachments.setdefault(ticket_id, []).append(attachment)
            self.blobs[url] = content
            self._record("attach_file", {"ticket_id": ticket_id, "name": name, "content": content})
            return attachment
