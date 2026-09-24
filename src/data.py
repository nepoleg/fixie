from dataclasses import dataclass
from datetime import datetime
from enum import Enum

class Role(Enum):
    ASSISTANT = 0
    USER = 1

@dataclass
class TicketCommentData:
    datetime: datetime
    text: str
    role: Role

@dataclass
class TicketData:
    ticket_id: int
    title: str
    description: str
    comments: list[TicketCommentData]
