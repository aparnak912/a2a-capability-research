"""Agent Card for the local demo agent."""

from a2a.types.a2a_pb2 import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
)
from a2a.utils.constants import PROTOCOL_VERSION_1_0, TransportProtocol


def build_agent_card(base_url: str) -> AgentCard:
    """Describe the local agent and its JSON-RPC 1.0 endpoint."""

    root = base_url.rstrip("/") + "/"
    return AgentCard(
        name="demo-tracker",
        description=(
            "Local agent used to prove task ids, status checks, "
            "streaming, and parallel starts."
        ),
        version="0.1.0",
        supported_interfaces=[
            AgentInterface(
                protocol_binding=TransportProtocol.JSONRPC,
                protocol_version=PROTOCOL_VERSION_1_0,
                url=root,
            )
        ],
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        capabilities=AgentCapabilities(streaming=True),
        skills=[
            AgentSkill(
                id="slow",
                name="Slow task",
                description="Waits about N seconds, emitting one tick per second.",
                tags=["slow", "async"],
                examples=["slow 4 alpha"],
            ),
            AgentSkill(
                id="echo",
                name="Echo",
                description="Returns the input text.",
                tags=["echo"],
                examples=["hello"],
            ),
            AgentSkill(
                id="add",
                name="Add",
                description="Adds two integers.",
                tags=["calculator"],
                examples=["add 2 3"],
            ),
        ],
    )
