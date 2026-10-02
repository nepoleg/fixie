"""AdoGateway backed by Azure DevOps through the azure-devops Python library."""

import html
import io
import re
import threading
from datetime import datetime

from azure.devops.connection import Connection
from azure.devops.exceptions import AzureDevOpsServiceError
from azure.devops.v7_1.git.models import Comment as GitComment
from azure.devops.v7_1.git.models import GitPullRequest, GitPullRequestSearchCriteria
from azure.devops.v7_1.work_item_tracking.models import CommentCreate, CommentUpdate, JsonPatchOperation, Wiql
from msrest.authentication import BasicAuthentication

from fixie.gateway import OPEN_STATES, TAGGED_TICKETS_WIQL, AdoGateway
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

_WORK_ITEM_LOCATION = "72c7ddf8-2cdc-4f60-90cd-ab71c14a399b"
_ATTACHMENT_ID_RE = re.compile(r"/_apis/wit/attachments/([0-9a-fA-F-]{36})(?:[/?#]|$)")
_TEXT_FIELDS = {
    "description": "System.Description",
    "acceptance_criteria": "Microsoft.VSTS.Common.AcceptanceCriteria",
    "repro_steps": "Microsoft.VSTS.TCM.ReproSteps",
}
_HEADS = "refs/heads/"
_BATCH = 200


def split_tags(value: str | None) -> tuple[str, ...]:
    """Split an ADO `System.Tags` value ("a; b") into trimmed, non-empty tags."""
    return tuple(t.strip() for t in (value or "").split(";") if t.strip())


def attachment_id(url: str) -> str | None:
    """Return the attachment GUID in an ADO attachment URL, or None if the URL is not one."""
    match = _ATTACHMENT_ID_RE.search(url)
    return match.group(1).lower() if match else None


def wiql_quote(value: str) -> str:
    """Escape a value for use inside a single-quoted WIQL string."""
    return value.replace("'", "''")


def _identity(ref) -> Identity:
    if ref is None:
        return Identity("", "")
    return Identity((ref.display_name or "").strip(), (ref.unique_name or "").strip())


def _dt(value) -> datetime:
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError(f"naive datetime from ADO: {value!r}")
    return dt


def _branch(ref_name: str | None) -> str:
    ref_name = ref_name or ""
    return ref_name[len(_HEADS):] if ref_name.startswith(_HEADS) else ref_name


def _not_found(exc: AzureDevOpsServiceError) -> bool:
    text = f"{exc.type_key or ''} {exc.message or ''}".lower()
    return "notfound" in text or "does not exist" in text or "doesnotexist" in text


def _ticket_from_json(data: dict) -> Ticket:
    fields = data["fields"]
    formats = data.get("multilineFieldsFormat") or {}
    markdown = frozenset(name for name, ref in _TEXT_FIELDS.items() if formats.get(ref, "").lower() == "markdown")
    texts = {}
    for name, ref in _TEXT_FIELDS.items():
        value = fields.get(ref) or ""
        texts[name] = html.unescape(value) if name in markdown else value
    return Ticket(
        id=data["id"],
        title=fields.get("System.Title", ""),
        work_item_type=fields.get("System.WorkItemType", ""),
        state=fields.get("System.State", ""),
        tags=split_tags(fields.get("System.Tags")),
        changed_date=_dt(fields["System.ChangedDate"]),
        priority=int(fields.get("Microsoft.VSTS.Common.Priority", 2)),
        markdown_fields=markdown,
        **texts,
    )


def _update(u) -> TicketUpdate | None:
    fields = u.fields or {}
    changed = fields.get("System.ChangedDate")
    if changed is None or changed.new_value is None:
        return None
    tags, state = fields.get("System.Tags"), fields.get("System.State")
    old_tags = set(t.lower() for t in split_tags(tags.old_value)) if tags else set()
    new_tags = split_tags(tags.new_value) if tags else ()
    new_lower = set(t.lower() for t in new_tags)
    return TicketUpdate(
        id=u.id,
        revised_by=_identity(u.revised_by),
        revised_date=_dt(changed.new_value),
        tags_added=tuple(t for t in new_tags if t.lower() not in old_tags),
        tags_removed=tuple(t for t in split_tags(tags.old_value) if t.lower() not in new_lower) if tags else (),
        state_from=state.old_value if state else None,
        state_to=state.new_value if state else None,
        description_changed="System.Description" in fields,
    )


def _comment(c) -> Comment:
    markdown = (c.format or "").lower() == "markdown"
    text = c.rendered_text if markdown and c.rendered_text else c.text
    return Comment(
        id=c.id,
        text=text or "",
        created_by=_identity(c.created_by),
        created_date=_dt(c.created_date),
        modified_by=_identity(c.modified_by or c.created_by),
        modified_date=_dt(c.modified_date or c.created_date),
    )


def _pr_comment(c) -> PrComment:
    return PrComment(
        id=c.id,
        author=_identity(c.author),
        content=c.content or "",
        published_date=_dt(c.published_date),
        last_updated_date=_dt(c.last_updated_date or c.published_date),
        comment_type=c.comment_type or "text",
    )


def _thread(t) -> PrThread:
    ctx = t.thread_context
    position = ctx.right_file_start or ctx.left_file_start if ctx else None
    comments = tuple(_pr_comment(c) for c in (t.comments or []) if not c.is_deleted)
    return PrThread(
        id=t.id,
        status=t.status,
        file_path=ctx.file_path if ctx else None,
        line=position.line if position else None,
        comments=comments,
        published_date=_dt(t.published_date),
        last_updated_date=_dt(t.last_updated_date or t.published_date),
    )


def _build(b) -> Build:
    return Build(
        id=b.id,
        status=b.status or "",
        result=b.result,
        queue_time=_dt(b.queue_time),
        finish_time=_dt(b.finish_time) if b.finish_time else None,
        source_version=b.source_version or "",
        requested_for=_identity(b.requested_for),
    )


class RealAdoGateway(AdoGateway):
    """AdoGateway that talks to Azure DevOps with a PAT; safe to call from several threads."""

    def __init__(self, org_url: str, project: str, repo: str, token: str):
        connection = Connection(base_url=org_url, creds=BasicAuthentication("", token))
        self._wit = connection.clients_v7_1.get_work_item_tracking_client()
        self._git = connection.clients_v7_1.get_git_client()
        self._build = connection.clients_v7_1.get_build_client()
        self.project = project
        self.repo = repo
        self._repo_ids: tuple[str, str] | None = None
        # ponytail: one lock serializes every ADO call (msrest sessions are not thread-safe); per-thread clients if it ever bottlenecks.
        self._lock = threading.RLock()

    def _work_item_json(self, ticket_id: int, expand: str | None = None) -> dict:
        query = {"$expand": expand} if expand else {}
        try:
            response = self._wit._send(
                http_method="GET",
                location_id=_WORK_ITEM_LOCATION,
                version="7.1",
                route_values={"project": self.project, "id": ticket_id},
                query_parameters=query,
            )
        except AzureDevOpsServiceError as exc:
            if _not_found(exc):
                raise KeyError(ticket_id) from None
            raise
        return response.json()

    def _repository_ids(self) -> tuple[str, str]:
        if self._repo_ids is None:
            repo = self._git.get_repository(self.repo, project=self.project)
            self._repo_ids = (repo.project.id, repo.id)
        return self._repo_ids

    def _work_item_ids(self, pr_id: int) -> tuple[int, ...]:
        refs = self._git.get_pull_request_work_item_refs(self.repo, pr_id, project=self.project) or []
        return tuple(sorted(int(r.id) for r in refs))

    def _pr(self, pr) -> PullRequest:
        return PullRequest(
            id=pr.pull_request_id,
            title=pr.title or "",
            description=pr.description or "",
            source_branch=_branch(pr.source_ref_name),
            target_branch=_branch(pr.target_ref_name),
            status=pr.status or "",
            is_draft=bool(pr.is_draft),
            work_item_ids=self._work_item_ids(pr.pull_request_id),
            created_date=_dt(pr.creation_date),
        )

    def find_tagged_tickets(self, tag: str) -> list[int]:
        query = TAGGED_TICKETS_WIQL.format(
            project=wiql_quote(self.project),
            states=", ".join(f"'{wiql_quote(s)}'" for s in OPEN_STATES),
            tag=wiql_quote(tag),
        )
        with self._lock:
            ids = [w.id for w in self._wit.query_by_wiql(Wiql(query=query)).work_items or []]
            tags: dict[int, tuple[str, ...]] = {}
            for i in range(0, len(ids), _BATCH):
                for item in self._wit.get_work_items(ids[i:i + _BATCH], project=self.project, fields=["System.Tags"]):
                    tags[item.id] = split_tags((item.fields or {}).get("System.Tags"))
        return [i for i in ids if any(t.lower() == tag.lower() for t in tags.get(i, ()))]

    def get_ticket(self, ticket_id: int) -> Ticket:
        with self._lock:
            return _ticket_from_json(self._work_item_json(ticket_id))

    def get_updates(self, ticket_id: int) -> list[TicketUpdate]:
        updates = []
        with self._lock:
            skip = 0
            while True:
                page = self._wit.get_updates(ticket_id, project=self.project, top=_BATCH, skip=skip)
                updates.extend(page)
                if len(page) < _BATCH:
                    break
                skip += _BATCH
        return [u for u in map(_update, updates) if u is not None]

    def get_comments(self, ticket_id: int) -> list[Comment]:
        comments, token = [], None
        with self._lock:
            while True:
                page = self._wit.get_comments(
                    self.project, ticket_id, top=_BATCH, continuation_token=token,
                    include_deleted=False, expand="renderedText", order="asc",
                )
                comments.extend(page.comments or [])
                token = page.continuation_token
                if not token:
                    break
        return [_comment(c) for c in comments if not c.is_deleted]

    def get_attachments(self, ticket_id: int) -> list[Attachment]:
        with self._lock:
            data = self._work_item_json(ticket_id, expand="relations")
        attachments = []
        for rel in data.get("relations") or []:
            if rel.get("rel") != "AttachedFile":
                continue
            att_id = attachment_id(rel["url"])
            if att_id is not None:
                name = (rel.get("attributes") or {}).get("name") or att_id
                attachments.append(Attachment(id=att_id, name=name, url=rel["url"]))
        return attachments

    def get_pull_request(self, pr_id: int) -> PullRequest:
        with self._lock:
            try:
                pr = self._git.get_pull_request_by_id(pr_id, project=self.project)
            except AzureDevOpsServiceError as exc:
                if _not_found(exc):
                    raise KeyError(pr_id) from None
                raise
            return self._pr(pr)

    def find_pull_requests(self, source_branch: str) -> list[PullRequest]:
        criteria = GitPullRequestSearchCriteria(source_ref_name=_HEADS + source_branch, status="all")
        with self._lock:
            prs = self._git.get_pull_requests(self.repo, criteria, project=self.project) or []
            return sorted((self._pr(pr) for pr in prs), key=lambda p: p.id)

    def get_threads(self, pr_id: int) -> list[PrThread]:
        with self._lock:
            threads = self._git.get_threads(self.repo, pr_id, project=self.project) or []
        return [_thread(t) for t in threads if not t.is_deleted]

    def get_builds(self, pr_id: int) -> list[Build]:
        with self._lock:
            builds = self._build.get_builds(
                self.project, branch_name=f"refs/pull/{pr_id}/merge", query_order="queueTimeAscending",
            ) or []
        return sorted((_build(b) for b in builds), key=lambda b: (b.queue_time, b.id))

    def get_branch_head(self, branch: str) -> Commit | None:
        with self._lock:
            refs = self._git.get_refs(self.repo, project=self.project, filter=f"heads/{branch}") or []
            ref = next((r for r in refs if r.name == _HEADS + branch), None)
            if ref is None:
                return None
            commit = self._git.get_commit(ref.object_id, self.repo, project=self.project)
        push = commit.push
        if push is not None and push.pushed_by is not None and push.date is not None:
            author, date = _identity(push.pushed_by), push.date
        else:
            person = commit.committer or commit.author
            author, date = Identity(person.name or "", (person.email or "").strip()), person.date
        return Commit(sha=commit.commit_id, author=author, date=_dt(date), message=commit.comment or "")

    def add_comment(self, ticket_id: int, text: str) -> Comment:
        with self._lock:
            return _comment(self._wit.add_comment(CommentCreate(text=text), self.project, ticket_id))

    def remove_tag(self, ticket_id: int, tag: str) -> None:
        with self._lock:
            data = self._work_item_json(ticket_id)
            tags = split_tags(data["fields"].get("System.Tags"))
            kept = [t for t in tags if t.lower() != tag.lower()]
            if len(kept) == len(tags):
                return
            self._wit.update_work_item(
                [
                    JsonPatchOperation(op="test", path="/rev", value=data["rev"]),
                    JsonPatchOperation(op="replace", path="/fields/System.Tags", value="; ".join(kept)),
                ],
                ticket_id,
                project=self.project,
            )

    def set_state(self, ticket_id: int, state: str) -> None:
        with self._lock:
            try:
                self._wit.update_work_item(
                    [JsonPatchOperation(op="replace", path="/fields/System.State", value=state)],
                    ticket_id,
                    project=self.project,
                )
            except AzureDevOpsServiceError as exc:
                if _not_found(exc):
                    raise KeyError(ticket_id) from None
                raise

    def reply_to_thread(self, pr_id: int, thread_id: int, text: str) -> PrComment:
        with self._lock:
            try:
                created = self._git.create_comment(
                    GitComment(content=text, comment_type="text", parent_comment_id=1),
                    self.repo, pr_id, thread_id, project=self.project,
                )
            except AzureDevOpsServiceError as exc:
                if _not_found(exc):
                    raise KeyError(thread_id) from None
                raise
        return _pr_comment(created)

    def edit_comment(self, ticket_id: int, comment_id: int, text: str) -> Comment:
        with self._lock:
            try:
                updated = self._wit.update_comment(CommentUpdate(text=text), self.project, ticket_id, comment_id)
            except AzureDevOpsServiceError as exc:
                if _not_found(exc):
                    raise KeyError(comment_id) from None
                raise
            return _comment(updated)

    def create_pull_request(self, source_branch: str, target_branch: str, title: str, description: str) -> PullRequest:
        pr = GitPullRequest(
            source_ref_name=_HEADS + source_branch,
            target_ref_name=_HEADS + target_branch,
            title=title,
            description=description,
        )
        with self._lock:
            return self._pr(self._git.create_pull_request(pr, self.repo, project=self.project))

    def update_pull_request(self, pr_id: int, title: str, description: str) -> PullRequest:
        with self._lock:
            try:
                pr = self._git.update_pull_request(
                    GitPullRequest(title=title, description=description), self.repo, pr_id, project=self.project
                )
            except AzureDevOpsServiceError as exc:
                if _not_found(exc):
                    raise KeyError(pr_id) from None
                raise
            return self._pr(pr)

    def link_work_item(self, pr_id: int, ticket_id: int) -> None:
        with self._lock:
            if ticket_id in self._work_item_ids(pr_id):
                return
            project_id, repo_id = self._repository_ids()
            artifact = f"vstfs:///Git/PullRequestId/{project_id}%2F{repo_id}%2F{pr_id}"
            self._wit.update_work_item(
                [
                    JsonPatchOperation(
                        op="add",
                        path="/relations/-",
                        value={"rel": "ArtifactLink", "url": artifact, "attributes": {"name": "Pull Request"}},
                    )
                ],
                ticket_id,
                project=self.project,
            )

    def attach_file(self, ticket_id: int, name: str, content: bytes) -> Attachment:
        with self._lock:
            ref = self._wit.create_attachment(io.BytesIO(content), project=self.project, file_name=name)
            self._wit.update_work_item(
                [
                    JsonPatchOperation(
                        op="add",
                        path="/relations/-",
                        value={"rel": "AttachedFile", "url": ref.url, "attributes": {"name": name}},
                    )
                ],
                ticket_id,
                project=self.project,
            )
        return Attachment(id=str(ref.id).lower(), name=name, url=ref.url)
