# Local A2A tracking experiment

This proves one piece of an orchestration gateway: start work on an agent, get an id immediately, check status later, stream events while connected, and run two tasks at once before starting a third.

Protocol: A2A **1.0** over **JSON-RPC 2.0**. Python SDK: `a2a-sdk` 1.1.5. No cloud account and no model.

## Run

```bash
uv sync
uv run python scripts/prove_tracking.py
```

That starts the server, runs the four checks, and stops the server. To leave the server up yourself:

```bash
uv run python -m a2a_server
uv run python -m gateway
```

The Agent Card is at `http://127.0.0.1:8123/.well-known/agent-card.json`. JSON-RPC calls are `POST /` with header `A2A-Version: 1.0`.

`A2A_TARGET=local` is the default. `A2A_TARGET=agentcore` listens on `0.0.0.0:9000` and adds `GET /ping`. Account setup and the deploy command are in `runtimes/agentcore/README.md`.

## What a caller sends

```json
{
  "jsonrpc": "2.0",
  "id": "req-1",
  "method": "SendMessage",
  "params": {
    "message": {
      "messageId": "m1",
      "role": "ROLE_USER",
      "parts": [{ "text": "slow 4 detached" }]
    },
    "configuration": { "returnImmediately": true }
  }
}
```

The response is a task id in `TASK_STATE_WORKING`. The slow command keeps running. A later call uses the same id:

```json
{
  "jsonrpc": "2.0",
  "id": "req-2",
  "method": "GetTask",
  "params": { "id": "the-task-id" }
}
```

`SendStreamingMessage` uses the same params and returns server-sent events.

Cancel a running task with `CancelTask` and the same id. The state becomes `TASK_STATE_CANCELED` and stays there.

```json
{
  "jsonrpc": "2.0",
  "id": "req-3",
  "method": "CancelTask",
  "params": { "id": "the-task-id" }
}
```

Tasks are stored in SQLite at `data/a2a_tasks.db` (`A2A_DB_PATH`). `GetTask` reads that file, so the id, state, and result are still there after the server process restarts.

Commands the agent understands: `slow 4 alpha`, `ask`, `add 2 3`, or any other text, which is echoed.

`ask` returns `TASK_STATE_INPUT_REQUIRED` and waits. A later `SendMessage` on the same task id carries the answer and the task completes.

The local gateway listens on `http://127.0.0.1:8124`. Callers send the same JSON-RPC body with header `X-Agent-Id: demo-tracker` and do not send a host, port, or ARN. The gateway looks up that id and forwards the call. `CreateTaskPushNotificationConfig` can point at `http://127.0.0.1:8124/callbacks`; a finished task is POSTed there. `SubscribeToTask` with `{"id": "<task id>"}` reattaches while the task is still running: the first event is the current task, and later ticks arrive as new events. Ticks that already happened stay on the task row and are read with `GetTask`.

## What was verified

`scripts/prove_tracking.py` passed on this machine:

- `SendMessage` with `returnImmediately: true` returned a task id in about 0.00s while a 4 second task was still `TASK_STATE_WORKING`.
- `GetTask` on that id returned `WORKING`, then `COMPLETED`, with the result text still available after completion.
- `SendStreamingMessage` delivered 7 events over 3.99 seconds, one tick at a time.
- Two tasks started together. Both ids came back in 0.01s. A third task started only after both were `COMPLETED`. Each task had its own id.

The task id is a row in SQLite. A second HTTP call can read it, and so can a new process after restart. On AgentCore the same row has to live on a shared disk (EFS), because a runtime session id is not the task id and the microVM disk is ephemeral.
