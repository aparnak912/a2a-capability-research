# AgentCore, after the AWS account exists

`A2A_TARGET=local` is the default. The agent code does not change. `A2A_TARGET=agentcore` is the same JSON-RPC server (`SendMessage`, `GetTask`, `CancelTask`, `SendStreamingMessage`) on the host and port AgentCore requires, plus `GET /ping`.

Do not paste access keys into chat or into git. This directory has no secrets.

`scripts/deploy_agentcore.py` creates the runtime only after the account quota is above zero. It does not create a NAT gateway. Resource ids for this account, when a deploy has been started, are in `runtimes/agentcore/deployed.json` (not committed).

## What is different

| | Local | AgentCore |
| --- | --- | --- |
| Flag | `A2A_TARGET=local` | `A2A_TARGET=agentcore` |
| Listen | `127.0.0.1:8123` | `0.0.0.0:9000` |
| Health | none | `GET /ping` returns `{"status":"Healthy"}` or `HealthyBusy` while work is in flight |
| SQLite | `data/a2a_tasks.db` | `/mnt/efs/a2a_tasks.db` |
| Client | plain HTTP | SigV4, service `bedrock-agentcore`, plus `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id` and `A2A-Version: 1.0` |

The session id is not the A2A task id. A later `GetTask` may use a new session id because the task row is on the EFS mount, not in the microVM. Session storage inside AgentCore is per session and expires, so it does not satisfy that check.

`/ping` omits `time_of_last_update`. A timestamp that changes on every ping keeps the session alive until the max lifetime.

## 1. Create the account and stop

Do this yourself. Stop when `aws sts get-caller-identity` prints an account id.

1. Create an AWS account: [Getting started with an AWS account](https://docs.aws.amazon.com/accounts/latest/reference/getting-started-step1.html). Use region `us-west-2` unless you choose another region that offers AgentCore.
2. In IAM, create a user for this experiment. Attach `BedrockAgentCoreFullAccess` for the POC. Deploy also needs permission for IAM, CloudFormation or CDK, S3, ECR, and logs. That managed policy is broad and is for this experiment only. Docs: [IAM permissions](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-permissions.html) and [Get started with the CLI](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-get-started-cli.html).
3. Create an access key for that user. On this Mac, install the [AWS CLI](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) and run `aws configure`. Set the region to `us-west-2`. Leave the secret key only in the AWS CLI credential file.
4. Check:

```bash
aws sts get-caller-identity
node --version
```

Node 20 or later is required for the AgentCore CLI. This machine already has Node 24. Install the CLI only when you are ready to deploy:

```bash
npm install -g @aws/agentcore
agentcore --version
```

Until step 4 works, do not deploy.

## 2. What would be billed

After you say to create resources: the AgentCore runtime, a VPC, and an EFS file system. A NAT gateway is the part that usually costs the most, including while idle. This agent does not call a model, so the VPC should have no NAT. The resources will be listed again before anything is created.

## 3. Package

The container contract is ARM64, `0.0.0.0:9000`, JSON-RPC at `/`, the agent card at `/.well-known/agent-card.json`, and `GET /ping`. Enable MMDSv2. AWS rejects runtimes without it as of June 30, 2026 (`metadataConfiguration.requireMMDSV2=true`).

```bash
docker build --platform linux/arm64 -f runtimes/agentcore/Dockerfile -t a2a-tracker .
```

Placeholders for the client live in `.env.example`: `A2A_TARGET`, `AWS_REGION`, `AGENTCORE_RUNTIME_ARN`, `A2A_DB_PATH`.

## 4. Deploy

On this account the VPC, subnet, security groups, EFS access point, execution role, and ARM64 image were created in `us-west-2`. There is no NAT gateway and no internet gateway. The AgentCore runtime was not created: Service Quotas shows **Total Agents per Account = 0** for a new account, so `CreateAgentRuntime` returns `maxAgents limit exceeded` even though no runtime exists. An increase to 1001 is pending (the API rejects any request that is not above the documented default of 1000). Request id `d64ecee1b1e943949d43af86a36ba376o2HnWl24`.

When that quota is above zero:

```bash
uv run python scripts/deploy_agentcore.py
```

That creates one runtime named `a2a_tracker`, protocol A2A, SigV4, MMDSv2, EFS mounted at `/mnt/efs`.

## 5. Deploy shape, for a fresh account

EFS is the shared disk. Docs: [File system configurations](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-filesystem-configurations.html).

- Network mode `VPC`. Subnets must overlap the EFS mount target Availability Zones.
- Mount path `/mnt/efs` (one directory under `/mnt`).
- Runtime env `A2A_DB_PATH=/mnt/efs/a2a_tasks.db` and `A2A_TARGET=agentcore`.
- Execution role: `elasticfilesystem:ClientMount` and `elasticfilesystem:ClientWrite`, conditioned on the access point ARN.
- Security groups: TCP 2049 from the runtime to the mount target, and the reverse inbound rule.
- Access point POSIX user must match the container user. This image runs as root, so uid/gid `0`.
- Protocol `A2A`, inbound auth SigV4 (`AWS_IAM`).
- `requireMMDSV2=true`.

Confirm the VPC has no NAT gateway before creating the runtime.

## 6. Prove it

```bash
export A2A_TARGET=agentcore
export AWS_REGION=us-west-2
export AGENTCORE_RUNTIME_ARN=arn:aws:bedrock-agentcore:us-west-2:ACCOUNT:runtime/NAME
uv run python scripts/prove_tracking.py
```

That signs every JSON-RPC call and sends `X-Amzn-Bedrock-AgentCore-Runtime-Session-Id`. After the checks, it calls `GetTask` again with a new session id. The task must still be `TASK_STATE_COMPLETED` because the row is on EFS.

Local proof, with no AWS account:

```bash
uv run python scripts/prove_tracking.py
```
