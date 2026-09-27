"""Finish the AgentCore runtime once the account quota allows one agent.

VPC, EFS, the execution role, and the container image are recorded in
``runtimes/agentcore/deployed.json``. This script does not create a NAT
gateway. It creates the runtime only when Total Agents per Account is
above zero and no runtime named ``a2a_tracker`` exists yet.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import botocore.session


ROOT = Path(__file__).resolve().parents[1]
DEPLOYED = ROOT / "runtimes" / "agentcore" / "deployed.json"
RUNTIME_NAME = "a2a_tracker"
QUOTA_CODE = "L-F4575653"


def main() -> int:
    aws = shutil.which("aws")
    if aws is None:
        print("Stopped. The AWS CLI is not installed.", flush=True)
        print("Steps: runtimes/agentcore/README.md", flush=True)
        return 2
    identity = subprocess.run(
        [aws, "sts", "get-caller-identity"],
        check=False,
        capture_output=True,
        text=True,
    )
    if identity.returncode != 0:
        print("Stopped. aws sts get-caller-identity failed.", flush=True)
        print((identity.stderr or identity.stdout).strip(), flush=True)
        return 2
    print(identity.stdout.strip(), flush=True)
    if not DEPLOYED.exists():
        print(f"Missing {DEPLOYED}.", flush=True)
        return 2
    record = json.loads(DEPLOYED.read_text())
    region = record["region"]
    quota = subprocess.run(
        [
            aws,
            "service-quotas",
            "get-service-quota",
            "--service-code",
            "bedrock-agentcore",
            "--quota-code",
            QUOTA_CODE,
            "--region",
            region,
            "--query",
            "Quota.Value",
            "--output",
            "text",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    applied = quota.stdout.strip() if quota.returncode == 0 else "unknown"
    print(f"Total Agents per Account applied quota: {applied}", flush=True)
    if quota.returncode != 0 or applied in {"0", "0.0", "None", ""}:
        request = record.get("quota", {})
        print(
            "Stopped. This account cannot create an AgentCore runtime yet.",
            flush=True,
        )
        print(
            "A quota increase is already requested: "
            f"id={request.get('increaseRequestId')} "
            f"desired={request.get('requestedValue')} "
            f"status={request.get('status')}",
            flush=True,
        )
        print("No NAT gateway was created. The runtime was not created.", flush=True)
        return 3

    session = botocore.session.Session()
    client = session.create_client("bedrock-agentcore-control", region_name=region)
    existing = client.list_agent_runtimes().get("agentRuntimes", [])
    for runtime in existing:
        if runtime.get("agentRuntimeName") == RUNTIME_NAME:
            arn = runtime.get("agentRuntimeArn")
            print(f"Runtime already exists: {arn}", flush=True)
            record["agentRuntime"] = arn
            DEPLOYED.write_text(json.dumps(record, indent=2) + "\n")
            return 0

    def require_mmds(request=None, **_kwargs) -> None:
        body = getattr(request, "body", None)
        if not body:
            return
        payload = json.loads(body)
        payload["metadataConfiguration"] = {"requireMMDSV2": True}
        encoded = json.dumps(payload).encode()
        request.body = encoded
        request.headers["Content-Length"] = str(len(encoded))

    client.meta.events.register_first(
        "request-created.bedrock-agentcore-control",
        require_mmds,
    )
    created = client.create_agent_runtime(
        agentRuntimeName=RUNTIME_NAME,
        agentRuntimeArtifact={
            "containerConfiguration": {"containerUri": record["image"]}
        },
        roleArn=record["executionRoleArn"],
        networkConfiguration={
            "networkMode": "VPC",
            "networkModeConfig": {
                "securityGroups": [record["runtimeSecurityGroupId"]],
                "subnets": [record["subnetId"]],
            },
        },
        protocolConfiguration={"serverProtocol": "A2A"},
        environmentVariables={
            "A2A_TARGET": "agentcore",
            "A2A_HOST": "0.0.0.0",
            "A2A_PORT": "9000",
            "A2A_DB_PATH": "/mnt/efs/a2a_tasks.db",
        },
        filesystemConfigurations=[
            {
                "efsAccessPoint": {
                    "accessPointArn": record["accessPointArn"],
                    "mountPath": "/mnt/efs",
                }
            }
        ],
        description="A2A task tracker with EFS-backed SQLite",
    )
    arn = created["agentRuntimeArn"]
    print(f"Created runtime {arn}", flush=True)
    record["agentRuntime"] = arn
    DEPLOYED.write_text(json.dumps(record, indent=2) + "\n")
    print(
        "When status is READY, run: "
        "A2A_TARGET=agentcore "
        f"AWS_REGION={region} "
        f"AGENTCORE_RUNTIME_ARN={arn} "
        "uv run python scripts/prove_tracking.py",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
