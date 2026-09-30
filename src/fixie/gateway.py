"""Abstract read/write interface to Azure DevOps; the fake and real gateways implement it."""

from abc import ABC, abstractmethod

from fixie.model import (
    Attachment,
    Build,
    Comment,
    Commit,
    PrThread,
    PullRequest,
    Ticket,
    TicketUpdate,
)

OPEN_STATES = ("New", "Ready For Design", "Designing", "Ready for Development", "Developing", "Code Review")

TAGGED_TICKETS_WIQL = """
SELECT [System.Id], [System.Title]
FROM WorkItems
WHERE [System.TeamProject] = '{project}'
AND [System.State] IN (
          {states}
      )
AND [System.Tags] CONTAINS '{tag}'
ORDER BY [Microsoft.VSTS.Common.Priority] ASC, [System.Id] ASC
"""


class AdoGateway(ABC):
    """Read/write access to Azure DevOps tickets, pull requests, threads and builds."""

    @abstractmethod
    def find_tagged_tickets(self, tag: str) -> list[int]:
        """Return ids of open tickets tagged with tag, case-insensitive, highest priority first, then by id."""

    @abstractmethod
    def get_ticket(self, ticket_id: int) -> Ticket:
        """Return the ticket; raises KeyError if unknown."""

    @abstractmethod
    def get_updates(self, ticket_id: int) -> list[TicketUpdate]:
        """Return the ticket's revision history, oldest first."""

    @abstractmethod
    def get_comments(self, ticket_id: int) -> list[Comment]:
        """Return the ticket's comments, oldest first."""

    @abstractmethod
    def get_attachments(self, ticket_id: int) -> list[Attachment]:
        """Return the ticket's attachments."""

    @abstractmethod
    def download_attachment(self, url: str) -> bytes:
        """Return the attachment's content; raises KeyError if unknown."""

    @abstractmethod
    def get_pull_request(self, pr_id: int) -> PullRequest:
        """Return the pull request; raises KeyError if unknown."""

    @abstractmethod
    def find_pull_requests(self, source_branch: str) -> list[PullRequest]:
        """Return pull requests with the given source branch, any status."""

    @abstractmethod
    def get_threads(self, pr_id: int) -> list[PrThread]:
        """Return the pull request's comment threads."""

    @abstractmethod
    def get_builds(self, pr_id: int) -> list[Build]:
        """Return the pull request's validation builds, oldest first."""

    @abstractmethod
    def get_branch_head(self, branch: str) -> Commit | None:
        """Return the branch's head commit, or None if the branch does not exist."""

    @abstractmethod
    def add_comment(self, ticket_id: int, text: str) -> Comment:
        """Add a comment to the ticket and return it."""

    @abstractmethod
    def remove_tag(self, ticket_id: int, tag: str) -> None:
        """Remove tag from the ticket, case-insensitive; no-op if absent, raises KeyError if the ticket is unknown."""

    @abstractmethod
    def edit_comment(self, ticket_id: int, comment_id: int, text: str) -> Comment:
        """Replace a comment's text and return it; raises KeyError if unknown."""

    @abstractmethod
    def create_pull_request(self, source_branch: str, target_branch: str, title: str, description: str) -> PullRequest:
        """Create a pull request and return it."""

    @abstractmethod
    def update_pull_request(self, pr_id: int, title: str, description: str) -> PullRequest:
        """Replace a pull request's title and description; raises KeyError if unknown."""

    @abstractmethod
    def link_work_item(self, pr_id: int, ticket_id: int) -> None:
        """Link a ticket to a pull request; no-op if already linked."""

    @abstractmethod
    def attach_file(self, ticket_id: int, name: str, content: bytes) -> Attachment:
        """Attach a file to the ticket and return the attachment."""
