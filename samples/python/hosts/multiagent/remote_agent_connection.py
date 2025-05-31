from typing import Callable
import httpx
import os
from urllib.parse import urlparse
from a2a.client import A2AClient
from a2a.types import (
    AgentCard,
    Task,
    Message,
    MessageSendParams,
    TaskStatusUpdateEvent,
    TaskArtifactUpdateEvent,
    SendMessageRequest,
    SendStreamingMessageRequest,
    JSONRPCErrorResponse,
)
from oauthlib.oauth2 import BackendApplicationClient
from requests_oauthlib import OAuth2Session
from uuid import uuid4

TaskCallbackArg = Task | TaskStatusUpdateEvent | TaskArtifactUpdateEvent
TaskUpdateCallback = Callable[[TaskCallbackArg, AgentCard], Task]


class RemoteAgentConnections:
    """A class to hold the connections to the remote agents."""

    def __init__(self, client: httpx.AsyncClient, agent_card: AgentCard):
        self.agent_client = A2AClient(client, agent_card)
        self.card = agent_card
        self.pending_tasks = set()
        self.token = self.get_oauth_token(agent_card.url)

    def get_agent(self) -> AgentCard:
        return self.card

    async def send_message(
        self,
        request: MessageSendParams,
        task_callback: TaskUpdateCallback | None,
    ) -> Task | Message | None:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        if self.card.capabilities.streaming:
            task = None
            async for response in self.agent_client.send_message_streaming(
                SendStreamingMessageRequest(id=str(uuid4()), params=request),
                http_kwargs={ "headers":headers }
            ):
                if not response.root.result:
                    return response.root.error
                # In the case a message is returned, that is the end of the interaction.
                event = response.root.result
                if isinstance(event, Message):
                    return event

                # Otherwise we are in the Task + TaskUpdate cycle.
                if task_callback and event:
                    task = task_callback(event, self.card)
                if hasattr(event, 'final') and event.final:
                    break
            return task
        else:  # Non-streaming
            response = await self.agent_client.send_message(
                SendMessageRequest(id=str(uuid4()), params=request),
                http_kwargs={ "headers":headers }
            )
            if isinstance(response.root, JSONRPCErrorResponse):
                return response.root.error
            if isinstance(response.root.result, Message):
                return response.root.result

            if task_callback:
                task_callback(response.root.result, self.card)
            return response.root.result

    def get_oauth_token(self, agent: str) -> str | None:
        print ("Trying to obtain token via client credenitals grant")
        try:
            client_id = os.getenv("OAUTH_CLIENT_ID")
            client_secret = os.getenv("OAUTH_CLIENT_SECRET")

            if not client_id or not client_secret:
                print("Missing OAUTH_CLIENT_ID or OAUTH_CLIENT_SECRET in environment, skipping OAuth flow.")
                return None

            parsed = urlparse(agent)
            if not parsed.scheme or not parsed.netloc:
                return None
            base_url = f"{parsed.scheme}://{parsed.netloc}"
            discovery_url = f'{base_url}/.well-known/oauth-authorization-server'
            try:
                with httpx.Client(timeout=5.0) as client:
                    response = client.get(discovery_url)
                    response.raise_for_status()
                    metadata = response.json()
            except Exception as e:
                print(f"Failed to fetch metadata from {discovery_url}: {e}")
                return None

            token_url = metadata.get('token_endpoint')
            if not token_url:
                print("Missing 'token_endpoint' in server metadata.")
                return None

            client = BackendApplicationClient(client_id=client_id)
            oauth = OAuth2Session(client=client)

            try:
                token_response = oauth.fetch_token(
                    token_url=token_url,
                    client_id=client_id,
                    client_secret=client_secret
                )
                print ("Obtained OAuth token")
                return token_response.get('access_token')
            except Exception as e:
                print(f"Failed to fetch token: {e}")
                return None

        except Exception as e:
            print(f"Unexpected error in get_oauth_token_sync: {e}")
            return None

