import os

from azure.devops.connection import Connection
from msrest.authentication import BasicAuthentication
from dotenv import load_dotenv
from data import TicketData, TicketCommentData, Role

BOT_SIGN = "🤖"

load_dotenv()

# Deine Daten
organization_url = 'https://dev.azure.com/<your-org>'
personal_access_token = os.environ["AZURE_TOKEN"]
project = 'EETS'

# Verbindung aufbauen
credentials = BasicAuthentication('', personal_access_token)
connection = Connection(base_url=organization_url, creds=credentials)
wit_client = connection.clients.get_work_item_tracking_client()


def build_messages(comments) -> list[TicketCommentData]:
    messages = []
    for comment in comments.comments:
        role = Role.ASSISTANT if comment.text.startswith(BOT_SIGN) else Role.USER
        messages.append(TicketCommentData(comment.created_date, comment.text, role))
    return messages

def load_ticket_data(ticket_id: int):
    # 1. Ticket laden
    work_item = wit_client.get_work_item(id=ticket_id, expand="all")
    title = work_item.fields["System.Title"]
    description = work_item.fields["System.Description"]

    # 2. Kommentare als Conversation History aufbauen
    raw_comments = wit_client.get_comments(project=project, work_item_id=ticket_id)
    comments = build_messages(raw_comments)
    return TicketData(ticket_id, title, description, comments)

# Suche ausführen
# WIQL Abfrage nach einem Label (Tag)
wiql_query = f"""
SELECT [System.Id], [System.Title]
FROM WorkItems 
WHERE [System.TeamProject] = '{project}'
AND [System.State] IN (
          'New',
          'Ready For Design',
          'Designing',
          'Ready for Development',
          'Developing',
          'Code Review'
      )
AND [System.Tags] CONTAINS 'aiMate' 
"""

def find_tickets() -> list[int]:
    return [ticket.id for ticket in wit_client.query_by_wiql(wiql={'query': wiql_query}).work_items]

tickets = find_tickets()
ticketDatas = []
for ticket_id in tickets:
    data = load_ticket_data(ticket_id)
    if data:
        ticketDatas.append(data)

print(f"Fond following comments for tickets {ticketDatas}")





