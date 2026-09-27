"""Prove the orchestration contract against the local A2A server.

1. Start a slow task and receive the task id before it finishes.
2. Read WORKING, then later COMPLETED, with GetTask.
3. Stream events and print a timestamp on each one.
4. Start two tasks together, then a third only after both complete.
5. Cancel a running task. It stays CANCELED.
6. Stop the server, read SQLite, start again, and GetTask the same ids.

The client speaks JSON-RPC 2.0 directly so the request and response are visible.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
from botocore.credentials import Credentials

from agentcore_http import (
    SESSION_HEADER,
    encode_rpc,
    invocation_base,
    new_session_id,
    sign_headers,
)


ROOT = Path(__file__).resolve().parents[1]
HOST = os.environ.get("A2A_HOST", "127.0.0.1")
PORT = os.environ.get("A2A_PORT", "8123")
BASE = f"http://{HOST}:{PORT}"
DB_PATH = ROOT / "data" / "a2a_tasks.db"
SLOW_SECONDS = 4
SHAPE_PORT = "9010"
GATEWAY = "http://127.0.0.1:8124"
GATEWAY_AGENT = "demo-tracker"


def rpc(method: str, params: dict, request_id: str | None = None) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": request_id or str(uuid.uuid4()),
        "method": method,
        "params": params,
    }


def user_message(text: str) -> dict:
    return {
        "messageId": str(uuid.uuid4()),
        "role": "ROLE_USER",
        "parts": [{"text": text}],
    }


def headers() -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "A2A-Version": "1.0",
    }


def as_task(payload: dict) -> dict:
    """Normalize SendMessage (result.task) and GetTask (result is the task)."""

    result = payload.get("result", payload)
    nested = result.get("task")
    if isinstance(nested, dict):
        return nested
    if "status" in result:
        return result
    return {}


def task_state(payload: dict) -> str:
    return as_task(payload).get("status", {}).get("state", "UNKNOWN")


def task_id_of(payload: dict) -> str:
    return as_task(payload)["id"]


def artifact_text(payload: dict, name: str) -> str:
    task = as_task(payload)
    texts: list[str] = []
    for artifact in task.get("artifacts", []):
        if artifact.get("name") != name:
            continue
        for part in artifact.get("parts", []):
            if "text" in part:
                texts.append(part["text"])
    return " | ".join(texts)


def result_text(payload: dict) -> str:
    return artifact_text(payload, "result")


def stamp(label: str) -> None:
    print(f"{time.strftime('%H:%M:%S')}  {label}", flush=True)


class Caller:
    """JSON-RPC over plain HTTP, or the same body signed for AgentCore."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        base: str,
        session_id: str | None = None,
        region: str | None = None,
        credentials: Credentials | None = None,
        agent_id: str | None = None,
    ) -> None:
        self.http = http
        self.base = base.rstrip("/")
        self.session_id = session_id
        self.region = region
        self.credentials = credentials
        self.agent_id = agent_id

    def _url(self, path: str) -> str:
        suffix = path if path.startswith("/") else f"/{path}"
        url = f"{self.base}{suffix}"
        if self.credentials is not None and "qualifier=" not in url:
            joiner = "&" if "?" in url else "?"
            url = f"{url}{joiner}qualifier=DEFAULT"
        return url

    def _headers(self, url: str, body: bytes | None) -> dict[str, str]:
        hdrs = headers()
        if body is None:
            hdrs.pop("Content-Type", None)
        if self.session_id:
            hdrs[SESSION_HEADER] = self.session_id
        if self.agent_id:
            hdrs["X-Agent-Id"] = self.agent_id
        if self.credentials is None or self.region is None:
            return hdrs
        return sign_headers(
            url=url,
            body=body or b"",
            headers=hdrs,
            region=self.region,
            credentials=self.credentials,
            method="GET" if body is None else "POST",
        )

    async def post_rpc(self, body: dict) -> httpx.Response:
        raw = encode_rpc(body)
        url = self._url("/")
        return await self.http.post(url, headers=self._headers(url, raw), content=raw)

    async def get(self, path: str) -> httpx.Response:
        url = self._url(path)
        return await self.http.get(url, headers=self._headers(url, None))

    def stream_rpc(self, body: dict):
        raw = encode_rpc(body)
        url = self._url("/")
        return self.http.stream(
            "POST",
            url,
            headers=self._headers(url, raw),
            content=raw,
        )


async def post(client: Caller, body: dict) -> dict:
    response = await client.post_rpc(body)
    data = response.json()
    if response.status_code >= 400 and "error" not in data:
        response.raise_for_status()
    if "error" in data:
        raise RuntimeError(json.dumps(data["error"], indent=2))
    return data


async def start_slow(client: Caller, label: str) -> tuple[str, float, dict]:
    body = rpc(
        "SendMessage",
        {
            "message": user_message(f"slow {SLOW_SECONDS} {label}"),
            "configuration": {"returnImmediately": True},
        },
    )
    started = time.perf_counter()
    data = await post(client, body)
    elapsed = time.perf_counter() - started
    return task_id_of(data), elapsed, data  # type: ignore[return-value]


async def get_task(client: Caller, task_id: str) -> dict:
    return await post(client, rpc("GetTask", {"id": task_id}))


async def wait_until_done(client: Caller, task_id: str) -> dict:
    deadline = time.perf_counter() + SLOW_SECONDS + 10
    last = ""
    while time.perf_counter() < deadline:
        data = await get_task(client, task_id)
        state = task_state(data)
        if state != last:
            stamp(f"GetTask {task_id[:8]} state={state}")
            last = state
        if state in {
            "TASK_STATE_COMPLETED",
            "TASK_STATE_FAILED",
            "TASK_STATE_CANCELED",
            "TASK_STATE_REJECTED",
        }:
            return data
        await asyncio.sleep(0.4)
    raise TimeoutError(f"task {task_id} did not finish")


async def prove_detached(client: Caller) -> None:
    print("\n== 1 and 2. Detached start, then status ==", flush=True)
    body = rpc(
        "SendMessage",
        {
            "message": user_message(f"slow {SLOW_SECONDS} detached"),
            "configuration": {"returnImmediately": True},
        },
    )
    print("request", json.dumps(body), flush=True)
    started = time.perf_counter()
    data = await post(client, body)
    elapsed = time.perf_counter() - started
    print("response", json.dumps(data), flush=True)
    task_id = task_id_of(data)
    state = task_state(data)
    stamp(f"received task id in {elapsed:.2f}s state={state} id={task_id}")
    if elapsed >= SLOW_SECONDS - 1:
        raise AssertionError(
            "SendMessage waited for the slow task instead of returning an id"
        )
    if state not in {"TASK_STATE_WORKING", "TASK_STATE_SUBMITTED"}:
        raise AssertionError(f"expected a running state, got {state}")

    running = await get_task(client, task_id)
    stamp(f"status while running: {task_state(running)}")
    if task_state(running) not in {"TASK_STATE_WORKING", "TASK_STATE_SUBMITTED"}:
        raise AssertionError(
            f"immediate status read was {task_state(running)}, expected WORKING"
        )

    finished = await wait_until_done(client, task_id)
    text = result_text(finished)
    stamp(f"final result: {text}")
    if "finished" not in text:
        raise AssertionError(f"missing final result text: {text!r}")
    later = await get_task(client, task_id)
    if task_state(later) != "TASK_STATE_COMPLETED":
        raise AssertionError("status lookup after completion did not stay COMPLETED")


async def prove_stream(client: Caller) -> None:
    print("\n== 3. Live stream ==", flush=True)
    body = rpc(
        "SendStreamingMessage",
        {"message": user_message(f"slow {SLOW_SECONDS} stream")},
    )
    stamps: list[float] = []
    async with client.stream_rpc(body) as response:
        response.raise_for_status()
        async for line in response.aiter_lines():
            if not line.startswith("data:"):
                continue
            event = json.loads(line.removeprefix("data:").strip())
            now = time.perf_counter()
            stamps.append(now)
            stamp(f"stream {summarize_stream(event)}")
    if len(stamps) < 3:
        raise AssertionError(f"expected several stream events, saw {len(stamps)}")
    span = stamps[-1] - stamps[0]
    stamp(f"stream spanned {span:.2f}s across {len(stamps)} events")
    if span < 2:
        raise AssertionError("stream events arrived together instead of over time")


def summarize_stream(event: dict) -> str:
    result = event.get("result", event)
    if "task" in result:
        task = result["task"]
        return f"task state={task.get('status', {}).get('state')} id={task.get('id', '')[:8]}"
    if "statusUpdate" in result:
        status = result["statusUpdate"].get("status", {})
        return f"status={status.get('state')}"
    if "artifactUpdate" in result:
        artifact = result["artifactUpdate"].get("artifact", {})
        texts = [part.get("text", "") for part in artifact.get("parts", [])]
        return f"artifact {artifact.get('name')}: {' '.join(texts)}"
    if "error" in event:
        return f"error {event['error']}"
    return json.dumps(result)[:180]


async def prove_parallel(client: Caller) -> None:
    print("\n== 4. Two together, then a third ==", flush=True)
    wall = time.perf_counter()

    async def one(label: str) -> tuple[str, float]:
        task_id, elapsed, _data = await start_slow(client, label)
        stamp(f"started {label} in {elapsed:.2f}s id={task_id}")
        if elapsed >= 2:
            raise AssertionError(f"{label} did not return an id immediately")
        return task_id, elapsed

    (id_a, _), (id_b, _) = await asyncio.gather(one("alpha"), one("beta"))
    parallel_started = time.perf_counter() - wall
    stamp(f"both ids in hand after {parallel_started:.2f}s")
    if parallel_started >= SLOW_SECONDS:
        raise AssertionError("parallel starts waited for the slow work")

    done_a, done_b = await asyncio.gather(
        wait_until_done(client, id_a),
        wait_until_done(client, id_b),
    )
    stamp(f"alpha result: {result_text(done_a)}")
    stamp(f"beta result: {result_text(done_b)}")

    before_third = time.perf_counter()
    id_c, elapsed_c, _data = await start_slow(client, "gamma")
    stamp(f"started gamma in {elapsed_c:.2f}s id={id_c} after alpha and beta completed")
    if before_third - wall < SLOW_SECONDS - 1:
        raise AssertionError("gamma started before the first pair could have finished")
    done_c = await wait_until_done(client, id_c)
    stamp(f"gamma result: {result_text(done_c)}")
    if len({id_a, id_b, id_c}) != 3:
        raise AssertionError("expected three distinct task ids")


async def prove_cancel(client: Caller) -> str:
    print("\n== 5. Cancel a running task ==", flush=True)
    task_id, elapsed, _data = await start_slow(client, "cancel-me")
    stamp(f"started cancel-me in {elapsed:.2f}s id={task_id}")
    running = await get_task(client, task_id)
    if task_state(running) != "TASK_STATE_WORKING":
        raise AssertionError(f"expected WORKING before cancel, got {task_state(running)}")

    canceled = await post(client, rpc("CancelTask", {"id": task_id}))
    print("cancel response", json.dumps(canceled), flush=True)
    if task_state(canceled) != "TASK_STATE_CANCELED":
        raise AssertionError(f"cancel returned {task_state(canceled)}")

    await asyncio.sleep(SLOW_SECONDS + 1)
    later = await get_task(client, task_id)
    stamp(f"status after waiting: {task_state(later)}")
    if task_state(later) != "TASK_STATE_CANCELED":
        raise AssertionError("canceled task changed state after the wait")
    if "finished" in result_text(later):
        raise AssertionError("canceled task still produced a finished result")

    missing = await client.post_rpc(rpc("CancelTask", {"id": "does-not-exist"}))
    missing_body = missing.json()
    stamp(f"unknown task cancel error code={missing_body.get('error', {}).get('code')}")
    if "error" not in missing_body:
        raise AssertionError("cancel of an unknown id succeeded")
    return task_id


async def wait_for_card(client: Caller) -> None:
    deadline = time.perf_counter() + 15
    while time.perf_counter() < deadline:
        try:
            response = await client.get("/.well-known/agent-card.json")
            if response.status_code == 200:
                card = response.json()
                binding = card["supportedInterfaces"][0]
                stamp(
                    "agent card "
                    f"name={card['name']} "
                    f"binding={binding['protocolBinding']} "
                    f"version={binding['protocolVersion']}"
                )
                return
        except httpx.HTTPError:
            pass
        await asyncio.sleep(0.2)
    raise TimeoutError(f"agent card never came up at {client.base}")


def start_server(extra_env: dict[str, str] | None = None) -> subprocess.Popen[bytes]:
    env = os.environ.copy()
    env["A2A_TARGET"] = "local"
    env["A2A_HOST"] = HOST
    env["A2A_PORT"] = PORT
    env["A2A_DB_PATH"] = str(DB_PATH)
    if extra_env:
        env.update(extra_env)
    return subprocess.Popen(
        [sys.executable, "-m", "a2a_server"],
        cwd=ROOT,
        env=env,
    )


def start_gateway() -> subprocess.Popen[bytes]:
    env = os.environ.copy()
    env["A2A_UPSTREAM"] = BASE
    env["GATEWAY_HOST"] = "127.0.0.1"
    env["GATEWAY_PORT"] = "8124"
    return subprocess.Popen(
        [sys.executable, "-m", "gateway"],
        cwd=ROOT,
        env=env,
    )


def stop_server(server: subprocess.Popen[bytes]) -> None:
    server.terminate()
    try:
        server.wait(timeout=5)
    except subprocess.TimeoutExpired:
        server.kill()
        server.wait(timeout=5)


def reset_database() -> None:
    for suffix in ("", "-wal", "-shm"):
        path = Path(f"{DB_PATH}{suffix}")
        if path.exists():
            path.unlink()


def read_stored_states() -> dict[str, str]:
    connection = sqlite3.connect(DB_PATH)
    try:
        rows = connection.execute("select id, status from tasks").fetchall()
    finally:
        connection.close()
    states: dict[str, str] = {}
    for task_id, status_json in rows:
        status = json.loads(status_json)
        states[task_id] = status.get("state", "UNKNOWN")
    return states


async def prove_persisted(completed_id: str, canceled_id: str) -> None:
    print("\n== 6. SQLite still has the tasks after restart ==", flush=True)
    stored = read_stored_states()
    stamp(f"sqlite rows={len(stored)}")
    if stored.get(completed_id) != "TASK_STATE_COMPLETED":
        raise AssertionError(f"sqlite missing completed task: {stored.get(completed_id)}")
    if stored.get(canceled_id) != "TASK_STATE_CANCELED":
        raise AssertionError(f"sqlite missing canceled task: {stored.get(canceled_id)}")

    server = start_server()
    try:
        async with httpx.AsyncClient(timeout=30) as http:
            client = Caller(http, BASE)
            await wait_for_card(client)
            completed = await get_task(client, completed_id)
            canceled = await get_task(client, canceled_id)
            stamp(
                f"after restart completed={task_state(completed)} "
                f"canceled={task_state(canceled)} "
                f"result={result_text(completed)!r}"
            )
            if task_state(completed) != "TASK_STATE_COMPLETED":
                raise AssertionError("restart lost the completed task")
            if "finished" not in result_text(completed):
                raise AssertionError("restart lost the result text")
            if task_state(canceled) != "TASK_STATE_CANCELED":
                raise AssertionError("restart lost the canceled task")
    finally:
        stop_server(server)


def local_caller(http: httpx.AsyncClient, base: str = BASE) -> Caller:
    return Caller(http, base)


async def prove_checks(client: Caller) -> tuple[str, str]:
    await wait_for_card(client)
    await prove_detached(client)
    await prove_stream(client)
    await prove_parallel(client)
    completed_id, _elapsed, _data = await start_slow(client, "persist")
    done = await wait_until_done(client, completed_id)
    stamp(f"persist result: {result_text(done)}")
    canceled_id = await prove_cancel(client)
    return completed_id, canceled_id


def prove_sigv4_signs_session_header() -> None:
    """Sign a request with throwaway keys. This does not call AWS."""

    print("\n== 7. SigV4 covers the AgentCore session header ==", flush=True)
    session_id = new_session_id()
    body = encode_rpc(rpc("GetTask", {"id": "task-1"}))
    url = invocation_base("us-west-2", "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/demo")
    url = f"{url}?qualifier=DEFAULT"
    signed = sign_headers(
        url=url,
        body=body,
        headers={
            "Content-Type": "application/json",
            "A2A-Version": "1.0",
            SESSION_HEADER: session_id,
        },
        region="us-west-2",
        credentials=Credentials("AKIDEXAMPLE", "secret"),
    )
    authorization = signed.get("Authorization", "")
    signed_names = authorization.split("SignedHeaders=", 1)[-1].split(",", 1)[0]
    if not authorization.startswith("AWS4-HMAC-SHA256"):
        raise AssertionError("SigV4 Authorization header was not added")
    if "x-amzn-bedrock-agentcore-runtime-session-id" not in signed_names:
        raise AssertionError(f"session header was not signed: {signed_names}")
    if len(session_id) < 33:
        raise AssertionError("session id is shorter than AgentCore requires")
    stamp("signed GetTask without calling AWS")


async def prove_agentcore_shape() -> None:
    """Run the container contract on this machine: port, /ping, shared task id."""

    print("\n== 8. AgentCore listen shape, still on this machine ==", flush=True)
    db_path = ROOT / "data" / "agentcore_shape.db"
    for suffix in ("", "-wal", "-shm"):
        path = Path(f"{db_path}{suffix}")
        if path.exists():
            path.unlink()
    server = start_server(
        {
            "A2A_TARGET": "agentcore",
            "A2A_HOST": "127.0.0.1",
            "A2A_PORT": SHAPE_PORT,
            "A2A_DB_PATH": str(db_path),
        }
    )
    base = f"http://127.0.0.1:{SHAPE_PORT}"
    try:
        async with httpx.AsyncClient(timeout=30) as http:
            client = Caller(http, base, session_id=new_session_id())
            await wait_for_card(client)
            ping = await client.get("/ping")
            if ping.status_code != 200 or ping.json().get("status") != "Healthy":
                raise AssertionError(f"expected Healthy ping, got {ping.status_code} {ping.text}")
            if "time_of_last_update" in ping.json():
                raise AssertionError("ping must not send a fresh timestamp on every call")
            task_id, _elapsed, _data = await start_slow(client, "shape")
            busy = await client.get("/ping")
            if busy.json().get("status") != "HealthyBusy":
                raise AssertionError(f"expected HealthyBusy while working, got {busy.text}")
            first_session = client.session_id
            client.session_id = new_session_id()
            if client.session_id == first_session:
                raise AssertionError("second session id matched the first")
            seen = await get_task(client, task_id)
            stamp(
                f"second session GetTask state={task_state(seen)} "
                f"sessions differ"
            )
            if task_state(seen) not in {"TASK_STATE_WORKING", "TASK_STATE_COMPLETED"}:
                raise AssertionError("a new session id could not read the task")
            finished = await wait_until_done(client, task_id)
            if task_state(finished) != "TASK_STATE_COMPLETED":
                raise AssertionError("shape task did not complete")
            idle_status = ""
            idle_deadline = time.perf_counter() + 5
            while time.perf_counter() < idle_deadline:
                idle = await client.get("/ping")
                idle_status = idle.json().get("status", "")
                if idle_status == "Healthy":
                    break
                await asyncio.sleep(0.1)
            if idle_status != "Healthy":
                raise AssertionError(f"expected Healthy after the task, got {idle_status}")
    finally:
        stop_server(server)


def prove_listen_defaults() -> None:
    code = (
        "import os\n"
        "os.environ.pop('A2A_HOST', None)\n"
        "os.environ.pop('A2A_PORT', None)\n"
        "os.environ.pop('A2A_DB_PATH', None)\n"
        "os.environ['A2A_TARGET'] = 'agentcore'\n"
        "from a2a_server.config import database_path, listen_host, listen_port\n"
        "assert listen_host() == '0.0.0.0', listen_host()\n"
        "assert listen_port() == 9000, listen_port()\n"
        "assert str(database_path()).endswith('/mnt/efs/a2a_tasks.db'), database_path()\n"
        "os.environ['A2A_TARGET'] = 'local'\n"
        "assert listen_host() == '127.0.0.1'\n"
        "assert listen_port() == 8123\n"
        "assert str(database_path()).endswith('data/a2a_tasks.db')\n"
    )
    probe = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        raise AssertionError(probe.stderr or probe.stdout)


async def wait_for_gateway(http: httpx.AsyncClient) -> None:
    deadline = time.perf_counter() + 15
    while time.perf_counter() < deadline:
        try:
            response = await http.get(f"{GATEWAY}/callbacks")
            if response.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        await asyncio.sleep(0.2)
    raise TimeoutError(f"gateway never came up at {GATEWAY}")


async def read_sse(client: Caller, body: dict) -> list[dict]:
    events: list[dict] = []
    async with client.stream_rpc(body) as response:
        response.raise_for_status()
        async for line in response.aiter_lines():
            if not line.startswith("data:"):
                continue
            events.append(json.loads(line.removeprefix("data:").strip()))
    return events


async def prove_ask_and_push(client: Caller, http: httpx.AsyncClient) -> None:
    asked = await post(client, rpc("SendMessage", {"message": user_message("ask")}))
    if task_state(asked) != "TASK_STATE_INPUT_REQUIRED":
        raise AssertionError(f"ask returned {task_state(asked)}")
    task_id = task_id_of(asked)
    context_id = as_task(asked).get("contextId")
    stamp(f"ask paused id={task_id}")

    await post(
        client,
        rpc(
            "CreateTaskPushNotificationConfig",
            {
                "taskId": task_id,
                "url": f"{GATEWAY}/callbacks",
                "token": "local-demo",
            },
        ),
    )
    answer = user_message("blue")
    answer["taskId"] = task_id
    if context_id:
        answer["contextId"] = context_id
    done = await post(client, rpc("SendMessage", {"message": answer}))
    stamp(f"resumed state={task_state(done)} result={result_text(done)!r}")
    if task_state(done) != "TASK_STATE_COMPLETED":
        raise AssertionError(f"resume returned {task_state(done)}")
    if "blue" not in result_text(done):
        raise AssertionError("resume lost the answer text")

    deadline = time.perf_counter() + 5
    blob = ""
    while time.perf_counter() < deadline:
        listed = await http.get(f"{GATEWAY}/callbacks")
        blob = listed.text
        if task_id in blob and "TASK_STATE_COMPLETED" in blob:
            stamp(f"push delivered for {task_id[:8]}")
            return
        await asyncio.sleep(0.2)
    raise AssertionError(f"push callback missing the completed task: {blob}")


async def prove_subscribe(client: Caller) -> None:
    task_id, _elapsed, _data = await start_slow(client, "subscribe")
    deadline = time.perf_counter() + 10
    stored = ""
    while time.perf_counter() < deadline:
        stored = artifact_text(await get_task(client, task_id), "progress")
        if "tick 1" in stored:
            break
        await asyncio.sleep(0.2)
    if "tick 1" not in stored:
        raise AssertionError(f"tick 1 was not stored before subscribe: {stored!r}")

    events = await read_sse(client, rpc("SubscribeToTask", {"id": task_id}))
    if not events:
        raise AssertionError("SubscribeToTask produced no events")
    first = events[0].get("result", events[0])
    if "task" not in first:
        raise AssertionError(f"first subscribe event was not the current task: {events[0]}")
    later = json.dumps(events[1:])
    if not any(f"tick {n}" in later for n in (2, 3, 4)):
        raise AssertionError(f"subscribe did not deliver a later tick: {later}")
    final = artifact_text(await get_task(client, task_id), "progress")
    stamp(f"subscribe events={len(events)} stored={final!r}")
    if "tick 1" not in final:
        raise AssertionError("GetTask lost tick 1 after subscribe")


async def prove_gateway_lifecycle(http: httpx.AsyncClient) -> None:
    print("\n== 9. agent_id, pause, push, reconnect ==", flush=True)
    gateway = start_gateway()
    try:
        await wait_for_gateway(http)
        unknown = await http.post(
            f"{GATEWAY}/",
            headers={**headers(), "X-Agent-Id": "no-such-agent"},
            json=rpc("GetTask", {"id": "missing"}),
        )
        if unknown.status_code != 404:
            raise AssertionError(f"unknown agent_id returned {unknown.status_code}")
        client = Caller(http, GATEWAY, agent_id=GATEWAY_AGENT)
        await prove_ask_and_push(client, http)
        await prove_subscribe(client)
    finally:
        stop_server(gateway)


async def prove_local() -> None:
    prove_listen_defaults()
    reset_database()
    server = start_server()
    completed_id = ""
    canceled_id = ""
    try:
        async with httpx.AsyncClient(timeout=30) as http:
            client = local_caller(http)
            completed_id, canceled_id = await prove_checks(client)
            await prove_gateway_lifecycle(http)
            ping = await http.get(f"{BASE}/ping")
            if ping.status_code != 404:
                raise AssertionError("local target should not expose /ping")
    finally:
        stop_server(server)
    await prove_persisted(completed_id, canceled_id)
    prove_sigv4_signs_session_header()
    await prove_agentcore_shape()


async def prove_remote_agentcore() -> None:
    region = os.environ.get("AWS_REGION", "us-west-2")
    runtime_arn = os.environ.get("AGENTCORE_RUNTIME_ARN", "").strip()
    if not runtime_arn:
        raise SystemExit(
            "A2A_TARGET=agentcore needs AGENTCORE_RUNTIME_ARN. "
            "Deploy has not been run. See runtimes/agentcore/README.md."
        )
    from botocore.session import Session

    resolved = Session().get_credentials()
    if resolved is None:
        raise SystemExit("AWS credentials are not configured. Run aws configure, then retry.")
    credentials = Credentials(
        resolved.access_key,
        resolved.secret_key,
        resolved.token,
    )
    base = invocation_base(region, runtime_arn)
    print(f"\nAgentCore runtime {runtime_arn} in {region}", flush=True)
    async with httpx.AsyncClient(timeout=60) as http:
        client = Caller(
            http,
            base,
            session_id=new_session_id(),
            region=region,
            credentials=credentials,
        )
        completed_id, _canceled_id = await prove_checks(client)
        client.session_id = new_session_id()
        stamp(f"second session id={client.session_id}")
        again = await get_task(client, completed_id)
        stamp(f"second session GetTask state={task_state(again)} result={result_text(again)!r}")
        if task_state(again) != "TASK_STATE_COMPLETED":
            raise AssertionError("GetTask from a second session did not see the finished task")
        if "finished" not in result_text(again):
            raise AssertionError("second session lost the result text")


async def main() -> None:
    mode = os.environ.get("A2A_TARGET", "local").strip().lower()
    if mode == "local":
        await prove_local()
    elif mode == "agentcore":
        await prove_remote_agentcore()
    else:
        raise SystemExit("A2A_TARGET must be local or agentcore")
    print("\nAll checks passed.", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
