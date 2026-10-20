# Native Harbor Agents

An explicit `native_external_v1` profile for Harbor's real `terminus-2`, `codex`
and `claude-code` agents. It does not wrap, rename, or replace the controlled
Inspect agent, and does not change any legacy launcher or frozen experiment.

## CLI

Run from the source/release root with its dependencies available. In the research
checkout, include `src` and the repository root on `PYTHONPATH`.

```sh
PYTHONPATH=src:. python -m integrations.native_agents prepare BUNDLE OUT \
  --agent codex --model openai/MODEL --agent-version CLI_VERSION --max-calls 100

PYTHONPATH=src:. python -m integrations.native_agents prepare BUNDLE OUT \
  --agent claude-code --model MODEL --agent-version CLI_VERSION --max-calls 100

PYTHONPATH=src:. python -m integrations.native_agents prepare BUNDLE OUT \
  --agent terminus-2 --model openai/MODEL --max-calls 100
```

Replace `MODEL`, `CLI_VERSION` and the illustrative allowance with explicit
approved selections. Codex/Claude require an exact semantic CLI version; neither
configuration nor the generated Dockerfile uses `@latest`. Terminus is pinned to
Harbor commit `cfc54c995e90cd438deb862189a58053b9d89fd3`. `NATIVE.json` records the
requested versions, not an attestation of installed binaries. Explicit `run`
checks the local Harbor distribution's archive metadata and records its version
in `STARTED.json`; this checks installation provenance, not file integrity. Harbor's native
setup checks the installed CLI and its trajectory retains agent version data.

Codex and Claude use explicit raw provider model IDs. One matching `openai/`
(Codex) or `anthropic/` (Claude) prefix is accepted and stripped exactly once;
other prefixes and nested slash IDs are rejected because Harbor's CLI mapping
cannot preserve them. `job.json` and `NATIVE.json.model` contain that raw ID;
`NATIVE.json.provider_model` is the exact relay allowlist value. Terminus keeps
its provider prefix for LiteLLM routing, with the raw wire ID recorded separately.
Older prepared profiles without this mapping must be prepared again.

`prepare` reads the existing bundle only. It performs no reference execution,
Docker build, provider call, credential lookup, or `.env` loading. It generates
`job.json`, `NATIVE.json` and a Harbor task. Job paths are relative and no host
source path, credential value or host environment is serialized in `job.json`.

Only the explicit command below can start Docker/SDK/model execution:

```sh
PYTHONPATH=src:. python -m integrations.native_agents run OUT \
  --credential-env MY_DEDICATED_PROVIDER_KEY
```

The named variable must already be supplied by the caller's secure environment.
No login files, keychains or `.env` are searched. `--api-base` may explicitly
select a compatible HTTPS native API origin or `/v1` base. Defaults are OpenAI's
native API for Codex/OpenAI Terminus and Anthropic's API for Claude/Anthropic
Terminus. A Chat Completions-only endpoint is not Codex/Claude compatibility.
The CLI returns nonzero for missing Docker, malformed configuration or failed
verification. A valid business failure has reward 0, not an infrastructure exit.

`run` resolves the selected Docker context's endpoint (or `DOCKER_HOST` when
`DOCKER_CONTEXT` is unset) without copying configuration or credentials. Only
local Unix sockets are supported; remote SSH/TCP/TLS contexts are rejected.
Both daemon preflight and Harbor worker use the same resolved `DOCKER_HOST`,
temporary isolated HOME and empty `DOCKER_CONFIG`. No host Docker config/auth
or context files are passed to the worker. Preflight must pass before the named
provider credential is read or `STARTED.json` is written.

Submission delegation uses precisely the same parser:

```sh
python run.py native prepare BUNDLE OUT --agent codex --model openai/MODEL --agent-version CLI_VERSION --max-calls 100
python run.py native run OUT --credential-env MY_DEDICATED_PROVIDER_KEY
```

The Python APIs are `prepare(bundle, output, *, agent, model, max_calls,
agent_version=None) -> dict`, `run(prepared, *, credential_env, api_base=None)
-> int`, and `__main__.main(argv=None) -> int`.

## Boundaries

- FinanceOps V6, FieldOps, SocialOps, LogisticsOps, ResearchOps, DocOps and
  FactoryOps bundle manifests are supported (`environment` or `env`, `task`,
  `public`). FieldOps extracts `environment/business/task` from the legacy
  export and uses its original checkpoint, never the surrounding export tree.
- The main build context has only the public skill documents, instruction and
  standard-library HTTP client. Business code, source backends, task initial
  state and private evaluator inputs belong exclusively to the business image.
  Reference/oracle/solution/runtime directories are not copied from bundles.
- Main joins an egress network for its native SDK/API and an internal business
  network. Business joins only the internal network; no ports or custom volumes
  are published. No repository, home, Docker socket or private state host mount
  is configured. Harbor manages its own normal agent/verifier log transport.
- Exact CLI packages are installed during the main image build. At runtime the
  profile drops capabilities and forbids privilege escalation. No personal
  agent configuration, hooks, memories or host auth files are injected.
- A native-protocol relay retains the real provider key in the launcher. The
  sanitized Harbor child gets only the corresponding `OPENAI_*` or `ANTHROPIC_*`
  route plus a random per-run relay token, not the provider key. The token may
  appear in native CLI temporary auth storage; it expires when the launcher
  stops. Relay requests are fixed to the selected HTTPS upstream and model,
  with no redirects, credentials in job JSON, schema conversion or retries.
  SSE bytes and safe retry/rate-limit response headers pass through unchanged.
  Anthropic Messages and token-count routes also allow exactly `?beta=true`,
  matching the [official SDK's beta endpoints](https://github.com/anthropics/anthropic-sdk-typescript/blob/main/src/resources/beta/messages/messages.ts)
  and [reported Claude Code request](https://github.com/anthropics/claude-code/issues/30926).
  Other query strings remain rejected. WebSocket transport is not supported or
  verified; only native HTTP/SSE routes are implemented.
  Open egress is deliberate and is not a network allowlist security guarantee.
- Every `/call` shares one process-wide lock and persisted admission counter.
  Unknown tool/schema rejections and malformed call envelopes cost one B;
  only the two exact declaration names are free. Tool discovery/schema access
  and local file work do not consume B. B is not a token or dollar limit.
- Entry is persisted before execution. Exceptions leave execution uncertain;
  no mutation retry, tool-log replay or automatic state-directory restart is
  supported. Losing a response does not roll back its already committed effect.
- At B exhaustion the service rejects further calls. The trusted supervisor
  polls the private status channel and cancels the native loop. There can be a
  short polling delay and already-sent model requests cannot be recalled.
- At native return, error, cancellation or a budget stop, the supervisor first
  requests private `service freeze`: admission closes before waiting for any
  in-flight business operation. Only then does agent teardown occur, followed
  by verifier seal/score. Repeated cancellation cannot skip this settlement.
  Freeze failure is an infrastructure error, never an inferred reward. Verification
  is disabled and automatic compose teardown is skipped on that path to retain
  the possibly unsettled business worker for manual inspection, not replay.
- The host verifier uses a private Unix socket via `service_exec`; the actor
  cannot invoke score/seal/reset over HTTP. Seal and execution share a lock.
  The live gateway is scored, avoiding fresh-state reconstruction. Results go
  into `business_private`, outside the agent log mount. Reward requires settled,
  sealed state and `inspect_agent.scoring.business_accepted()`; missing or invalid
  evaluation raises rather than silently producing 0.

Native terminal commands, compaction, model defaults and stopping behavior are
the selected agent's own. There is no controlled action JSON or double-final
confirmation. Public docs are not rewritten with controlled output/compaction
rules. Native scripts can issue additional decisions after an error; the service
does not claim to enforce a controlled batch's first-error stopping semantics.

## Offline Preparation Check

After constructing a small bundle, use `python run.py native prepare` as shown
above to export its initial state, public instructions, Docker configuration,
and Harbor job. Inspect `NATIVE.json` for the selected agent, model, budget, and
`prepared_not_executed` status. Preparation does not contact a model provider.

In the independent checkout, FinanceOps preparation for Terminus-2 passed with
the pinned Harbor installation on 2026-10-11. Native image builds, CLI installation,
and live agent execution remain to be checked separately.

Business image requirements are pinned in `export.py` and installed during a
Docker build. The generated `job.json` is for Harbor interoperability; use this module's `run`
to retain credential isolation and B supervision. Direct `harbor run -c job.json`
bypasses those launcher protections and is not the supported experiment command.
