# AI Team (Strands + Aggressive Manager) — GK-Coach with Memory + A2A Strategy

Five AI agents for a 5v5 soccer match, built on the **extremely-aggressive** personality and extended with two things:

1. **AgentCore Memory (STM)** on every agent — cross-tick recall, same as the memory team.
2. A **goalkeeper that also acts as team manager**, running a deliberative "coach" loop that
   issues a team-wide **StrategyDirective** to the four outfield players over an **A2A
   (Agent-to-Agent) message channel**.

## Two-tier cognitive architecture

The 2-second tick budget drives the design: all match intelligence that must respond *now*
stays cheap and local, and all slow deliberation runs off the critical path.

```
 Reactive tier (every tick, on the 2s critical path)
   engine --HTTP gameState--> [gk] [def] [mid] [fwd1] [fwd2]
     each: small LLM (Claude Haiku 4.5), memory-backed, with the cached
           directive injected into its prompt  ->  ONE command
     (no A2A hop, no manager LLM on this path)

 Deliberative tier (background thread in the GK runtime, every ~6s)
   [gk runtime]
     bg thread: manager LLM (Claude Sonnet 5) reads latest state + match memory
                -> StrategyDirective  --A2A message--> def / mid / fwd1 / fwd2
```

- **Reactive tier** — each outfield agent runs a small directive-guided LLM per tick. It reads
  the latest cached directive (in-process, zero latency) and folds it into the prompt. If the
  LLM output can't be parsed, a deterministic fallback **tuned by the same directive** runs
  instead, so the rule-based safety net follows the manager's plan too.
- **Deliberative tier** — the GK's per-tick HTTP handler stashes the freshest game state; a
  daemon thread periodically runs the larger manager model over that state (plus the manager's
  own AgentCore Memory) and broadcasts a fresh directive. The manager LLM and the A2A hops
  never touch a player's command path.

### The StrategyDirective (`directive.py`)

A compact, clamped, parameterized plan — small enough to ship on the wire and simple enough for
a small model to obey:

```json
{ "stance": 1, "press_intensity": 0.85, "line_height": 0.7,
  "mark_target_id": -1, "risk": "high", "tempo": "fast",
  "focus_side": "center", "notes": "...", "version": 3 }
```

It renders to a `## MANAGER'S ORDERS` prompt block and can re-tune a `FallbackConfig`
(`tune_config`) so both the LLM and the rule-based fallback follow the same orders.

## Models

| Role | Model (Bedrock cross-region inference profile) |
|---|---|
| Players (GK, DEF, MID, FWD1, FWD2) | `us.anthropic.claude-haiku-4-5` |
| Manager / strategy (GK background loop) | `us.anthropic.claude-sonnet-5` |

> Adjust these IDs to your account/region's available Anthropic inference profiles if needed.
> The account must have Bedrock model access enabled for Claude Haiku 4.5 and Sonnet 5.

## A2A transport — what's real, and the one adaptation

The dissemination uses **A2A protocol messages** (a2a-sdk `Message` + `DataPart`). The transport,
however, is an AgentCore agent-to-agent **runtime invocation**, not a live A2A JSON-RPC socket —
and that is a deliberate consequence of the deployment shape, not a shortcut:

- Every AgentCore runtime advertises exactly **one** protocol, and all five players must stay
  **HTTP-invocable by the game engine**. So a player runtime cannot also host a reachable A2A
  server, and the GK (being a player) can't host one either.
- The engine only hands game state to the **players**, so a standalone A2A "coach" runtime would
  be blind — the GK, which already receives state each tick, is the natural manager.

So the GK's manager loop discovers its teammates at runtime (`ListAgentRuntimes`, filtered by the
`TEAM_RUNTIME_PREFIX`) and pushes the A2A-typed directive to each via `InvokeAgentRuntime`. The
whole channel is isolated in `strategy_channel.py`; to move to a genuine A2A JSON-RPC socket,
deploy the manager as a separate `server_protocol: A2A` runtime (Strands `A2AServer`) and have
players pull — a self-contained swap of that one module.

If `a2a-sdk` is unavailable the envelope degrades to an equivalent message-shaped dict, so local
tests run without it.

> **Multi-instance caveat:** the received directive is cached in an in-process module global. If
> AgentCore runs multiple warm instances of a player, a push reaches one instance; others use the
> last directive they received (or the aggressive default until their first push). For strict
> consistency across instances, back the cache with a shared store (e.g. the AgentCore Memory
> resource) — the `strategy_channel` read/write points are the seam for that.

## Architecture / files

```
ai-team-strands-aggressive-manager/
├── ai-gk/     Goalkeeper + MANAGER (player 0)  — Haiku 4.5 + Memory; runs the Sonnet 5 coach loop
├── ai-def/    Defender  (player 1)             — Haiku 4.5 + Memory, directive-guided
├── ai-mid/    Midfielder(player 2)             — Haiku 4.5 + Memory, directive-guided
├── ai-fwd1/   Forward 1 (player 3)             — Haiku 4.5 + Memory, directive-guided
├── ai-fwd2/   Forward 2 (player 4)             — Haiku 4.5 + Memory, directive-guided
├── directive.py               StrategyDirective: schema, serialization, prompt block, config tuning
├── strategy_channel.py        A2A envelope + teammate discovery/broadcast + directive cache
├── manager_invoke_handler.py  Outfield entrypoint: A2A push handling + directive-guided LLM/fallback
├── manager_loop.py            GK background coach loop (state capture + manager LLM + broadcast)
├── memory_agent_base.py       AgentCore Memory (STM) agent factory
├── create_memory.py           One-time Memory resource creation
├── deploy-all.sh              Build + deploy (macOS/Linux) — memory, per-agent env, Memory+A2A IAM
├── deploy-all-windows.ps1     Windows deploy (see note in the file — prefer the .sh under WSL)
└── destroy-all.sh             Tear down the 5 runtimes
```

Shared match logic (`state.py`, `parsing.py`, `fallback.py`, `agent_base.py`) lives in the
repo-level `lib/` and is copied in at deploy time.

## Deploy

```bash
# From the repo root or this directory:
AWS_PROFILE=your-profile AWS_DEFAULT_REGION=us-east-1 ./deploy-all.sh          # all five
AWS_PROFILE=your-profile ./deploy-all.sh ai-gk                                 # one agent
```

The script: auto-creates (or reuses) the AgentCore Memory resource, deploys each runtime with the
right env, and attaches a combined **Memory + A2A** IAM policy to the execution roles.

Environment variables set per runtime:

| Var | Purpose |
|---|---|
| `MEMORY_ID` | AgentCore Memory resource (auto-created if unset) |
| `TEAM_RUNTIME_PREFIX` | Name prefix for teammate discovery (default `aggmgr_`) |
| `AGENT_RUNTIME_NAME` | This runtime's name (so the GK excludes itself when broadcasting) |
| `MANAGER_AUTOSTART` | `1` on the GK (starts the coach loop), `0` on outfielders |
| `MANAGER_INTERVAL_S` | Coach recompute cadence in seconds (default `6`) |

The GK's execution role additionally needs `bedrock-agentcore:ListAgentRuntimes` and
`InvokeAgentRuntime` (attached by the script). In restricted environments (e.g. workshop
participant roles) the IAM attach may be denied — attach the `AgentCoreMemoryAndA2AAccess`
policy manually if so, or memory writes and directive broadcasts will fail at runtime.

After deploy, copy the five agent ARNs (printed by AgentCore) into the game/player portal.

## Test locally (offline)

```bash
cd ai-gk && python test_local.py          # any position; add --llm to hit Bedrock
```

Offline tests mock AgentCore + Memory, disable the coach thread, and cover: state summary,
directive serialization/clamping/prompt-block/tuning, the A2A envelope→cache path, the
directive-tuned fallback, and command parsing.

## Tear down

```bash
./destroy-all.sh            # all five runtimes (Memory resource is left intact)
```
