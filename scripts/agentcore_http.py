"""Sign the same JSON-RPC body AgentCore expects on InvokeAgentRuntime."""

from __future__ import annotations

import json
import uuid
from urllib.parse import quote

from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials, ReadOnlyCredentials


SERVICE = "bedrock-agentcore"
SESSION_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"


def new_session_id() -> str:
    """AgentCore session ids must be at least 33 characters. This is not the A2A task id."""

    return str(uuid.uuid4())


def invocation_base(region: str, runtime_arn: str) -> str:
    """Root the runtime maps onto the container's `/`."""

    encoded = quote(runtime_arn, safe="")
    return (
        f"https://bedrock-agentcore.{region}.amazonaws.com"
        f"/runtimes/{encoded}/invocations/"
    )


def sign_headers(
    *,
    url: str,
    body: bytes,
    headers: dict[str, str],
    region: str,
    credentials: Credentials | ReadOnlyCredentials,
    method: str = "POST",
) -> dict[str, str]:
    request = AWSRequest(method=method, url=url, data=body, headers=dict(headers))
    SigV4Auth(credentials, SERVICE, region).add_auth(request)
    signed = dict(request.headers)
    if "Authorization" not in signed:
        raise RuntimeError("SigV4 did not add an Authorization header")
    return signed


def encode_rpc(body: dict) -> bytes:
    return json.dumps(body, separators=(",", ":")).encode()
