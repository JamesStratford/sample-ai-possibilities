"""A2A strategy dissemination channel.

The GK-manager broadcasts a StrategyDirective to its four outfield teammates.
The directive is wrapped in an **A2A protocol message** (a2a-sdk ``Message`` with
a ``DataPart`` payload) so the exchange is A2A at the message level.

Transport note
--------------
On Bedrock AgentCore, each runtime advertises exactly one server protocol and all
five position agents must stay HTTP-invocable by the game engine, so a teammate
runtime cannot also host a reachable A2A JSON-RPC endpoint. We therefore carry the
A2A message as the payload of an agent-to-agent AgentCore runtime invocation
(``bedrock-agentcore:InvokeAgentRuntime``). Swap ``_deliver`` for a direct A2A
JSON-RPC client if the teammates are ever deployed as A2A runtimes.

Latency note
------------
Broadcasting happens only in the GK's background manager thread, never on a
player's per-tick command path. Outfield agents read the last directive from an
in-process cache (see ``get_cached_directive``), so reading it costs nothing.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Optional

from directive import StrategyDirective, DEFAULT_DIRECTIVE

logger = logging.getLogger("strategy_channel")

# Discriminator that marks an invocation as a strategy push rather than a game tick.
MESSAGE_TYPE = "STRATEGY_DIRECTIVE"

# Runtimes in this team share a name prefix so the manager can discover teammates
# without any ARN wiring. Keep in sync with the .bedrock_agentcore.yaml templates.
RUNTIME_PREFIX = os.environ.get("TEAM_RUNTIME_PREFIX", "aggmgr_")


# ---------------------------------------------------------------------------
# A2A message envelope (a2a-sdk types when available, dict fallback otherwise)
# ---------------------------------------------------------------------------

def _try_import_a2a():
    try:
        from a2a.types import Message, DataPart, Part, Role  # type: ignore
        return Message, DataPart, Part, Role
    except Exception:
        return None


def build_envelope(directive: StrategyDirective, sender: str = "gk-manager") -> dict:
    """Build the invocation payload carrying the directive as an A2A message.

    Returns a plain dict suitable for JSON-encoding into an AgentCore payload.
    When a2a-sdk is installed the embedded ``a2aMessage`` is a spec-compliant
    A2A Message; otherwise a minimal message-shaped dict is used.
    """
    data = {"directive": json.loads(directive.to_json())}
    a2a = _try_import_a2a()
    if a2a is not None:
        Message, DataPart, Part, Role = a2a
        msg = Message(
            role=Role.agent,
            parts=[Part(root=DataPart(data=data))],
            message_id=f"strategy-{directive.version}",
        )
        a2a_message = msg.model_dump(mode="json", exclude_none=True)
    else:
        a2a_message = {
            "role": "agent",
            "messageId": f"strategy-{directive.version}",
            "parts": [{"kind": "data", "data": data}],
        }
    return {"messageType": MESSAGE_TYPE, "sender": sender, "a2aMessage": a2a_message}


def directive_from_envelope(prompt_data: dict) -> Optional[StrategyDirective]:
    """Extract a StrategyDirective from an inbound envelope, or None if absent.

    Tolerates both the a2a-sdk message shape and the dict fallback shape.
    """
    if not isinstance(prompt_data, dict) or prompt_data.get("messageType") != MESSAGE_TYPE:
        return None
    msg = prompt_data.get("a2aMessage", {})
    for part in msg.get("parts", []):
        # a2a-sdk dumps DataPart as {"kind":"data","data":{...}} (or nested under "root")
        root = part.get("root", part)
        if root.get("kind") == "data" or "data" in root:
            data = root.get("data", {})
            directive = data.get("directive")
            if isinstance(directive, dict):
                return StrategyDirective.from_dict(directive)
    return None


# ---------------------------------------------------------------------------
# Outfield side — cache the latest directive (read on the hot path, lock-guarded)
# ---------------------------------------------------------------------------

_cache_lock = threading.Lock()
_cached: StrategyDirective = DEFAULT_DIRECTIVE


def receive_directive(prompt_data: dict) -> Optional[StrategyDirective]:
    """If ``prompt_data`` is a strategy push, cache it and return it; else None."""
    directive = directive_from_envelope(prompt_data)
    if directive is None:
        return None
    global _cached
    with _cache_lock:
        # Ignore stale broadcasts that arrive out of order.
        if directive.version >= _cached.version:
            _cached = directive
    return directive


def get_cached_directive() -> StrategyDirective:
    """Return the latest directive (or the aggressive default before first push)."""
    with _cache_lock:
        return _cached


def update_local_cache(directive: StrategyDirective) -> None:
    """Set the local cache directly (used by the GK so its own player, which is
    the manager, follows the orders it just issued)."""
    global _cached
    with _cache_lock:
        if directive.version >= _cached.version:
            _cached = directive


# ---------------------------------------------------------------------------
# GK-manager side — discover teammate runtimes and broadcast
# ---------------------------------------------------------------------------

class DirectiveBroadcaster:
    """Discovers teammate AgentCore runtimes by name prefix and pushes directives.

    Discovery results are cached; if discovery or a push fails it is logged and
    swallowed — a missed broadcast must never crash the manager loop.
    """

    def __init__(self, self_runtime_name: str, region: Optional[str] = None,
                 prefix: str = RUNTIME_PREFIX):
        self._self_name = self_runtime_name
        self._prefix = prefix
        self._region = region or os.environ.get("AWS_DEFAULT_REGION")
        self._teammate_arns: Optional[list[str]] = None
        self._control = None
        self._data = None

    def _clients(self):
        if self._control is None or self._data is None:
            import boto3
            self._control = boto3.client("bedrock-agentcore-control", region_name=self._region)
            self._data = boto3.client("bedrock-agentcore", region_name=self._region)
        return self._control, self._data

    def _discover(self) -> list[str]:
        """Return teammate runtime ARNs (all team runtimes except this one)."""
        if self._teammate_arns is not None:
            return self._teammate_arns
        control, _ = self._clients()
        arns: list[str] = []
        paginator_kwargs = {}
        while True:
            resp = control.list_agent_runtimes(**paginator_kwargs)
            for rt in resp.get("agentRuntimes", []):
                name = rt.get("agentRuntimeName", "")
                arn = rt.get("agentRuntimeArn") or rt.get("agentRuntimeId")
                if name.startswith(self._prefix) and name != self._self_name and arn:
                    arns.append(arn)
            token = resp.get("nextToken")
            if not token:
                break
            paginator_kwargs = {"nextToken": token}
        self._teammate_arns = arns
        logger.info("Discovered %d teammate runtimes for prefix %r", len(arns), self._prefix)
        return arns

    def broadcast(self, directive: StrategyDirective) -> int:
        """Push the directive to every teammate. Returns the number pushed OK."""
        try:
            arns = self._discover()
        except Exception as e:
            logger.warning("Teammate discovery failed: %s", e)
            return 0

        payload = json.dumps({"prompt": json.dumps(build_envelope(directive))}).encode("utf-8")
        _, data = self._clients()
        ok = 0
        for arn in arns:
            if self._deliver(data, arn, directive.version, payload):
                ok += 1
        logger.info("Broadcast directive v%d to %d/%d teammates", directive.version, ok, len(arns))
        return ok

    @staticmethod
    def _deliver(data_client, arn: str, version: int, payload: bytes) -> bool:
        try:
            # runtimeSessionId must be reasonably long; keep it stable per teammate
            # so pushes reuse a warm session.
            session_id = f"strategy-broadcast-session-{abs(hash(arn)) % (10 ** 12):012d}"
            data_client.invoke_agent_runtime(
                agentRuntimeArn=arn,
                runtimeSessionId=session_id,
                payload=payload,
                contentType="application/json",
                accept="application/json",
            )
            return True
        except Exception as e:  # never let one bad teammate break the loop
            logger.warning("Push to %s failed: %s", arn, e)
            return False
