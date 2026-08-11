"""GK-manager background loop.

The goalkeeper is the only manager-aware player. On every game tick its HTTP
entrypoint calls ``record_state`` to stash the freshest game state (cheap). A
separate daemon thread wakes every ``MANAGER_INTERVAL_S`` seconds, runs a larger
"manager" LLM over that state (plus its own AgentCore Memory of the match) to
produce a StrategyDirective, and broadcasts it to the four outfield players over
the A2A strategy channel.

This keeps all deliberation (the big LLM + the A2A hops) off every player's
per-tick command path — the reactive tier only ever reads a cached directive.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Optional

from state import summarize_state
from directive import StrategyDirective, DEFAULT_DIRECTIVE
from strategy_channel import DirectiveBroadcaster, update_local_cache

logger = logging.getLogger("manager_loop")

MANAGER_INTERVAL_S = float(os.environ.get("MANAGER_INTERVAL_S", "6"))

# Freshest game state observed by the GK's per-tick entrypoint.
_state_lock = threading.Lock()
_latest_state: Optional[dict] = None
_latest_team_id: int = 0

_started = False


def record_state(game_state: dict, team_id: int) -> None:
    """Called from the GK entrypoint each tick — O(1), just stores a reference."""
    global _latest_state, _latest_team_id
    with _state_lock:
        _latest_state = game_state
        _latest_team_id = team_id


def _snapshot() -> tuple[Optional[dict], int]:
    with _state_lock:
        return _latest_state, _latest_team_id


MANAGER_SYSTEM_PROMPT = """You are the MANAGER of an EXTREMELY AGGRESSIVE 5v5 AI soccer team. \
You are the goalkeeper, but right now you are thinking as the coach for the WHOLE team.

You periodically read the current game state and your memory of how the match has gone, \
then issue ONE team-wide tactical directive that all five players will follow until your next order.

Your team's identity is relentless, high-pressing, high-line attacking football. Bias toward \
aggression, but adapt: if you are defending a lead late, or the opponent keeps scoring on the \
counter, you may temper risk or drop the line.

Output ONLY a JSON object (no prose) with exactly these keys:
{
  "stance": 0|1|2,                      // 0=Balanced, 1=Attack, 2=Defend (team default)
  "press_intensity": 0.0-1.0,           // how hard to hunt the ball carrier
  "line_height": 0.0-1.0,               // 0=deep block, 1=camp on opponent goal
  "mark_target_id": -1..4,              // opponent player id to prioritise marking, -1 for none
  "risk": "low"|"medium"|"high"|"all_out",
  "tempo": "slow"|"balanced"|"fast",
  "focus_side": "left"|"center"|"right",
  "notes": "<= 15 words of plain-language instruction to the players"
}
Return ONLY the JSON object."""


def _extract_json_object(text: str) -> Optional[dict]:
    """Pull the first balanced {...} object out of an LLM response."""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(text)):
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def start_manager(
    self_runtime_name: str,
    model_id: str = "us.anthropic.claude-sonnet-5",
    interval_s: float = MANAGER_INTERVAL_S,
) -> None:
    """Spawn the daemon thread that computes and broadcasts directives.

    The manager agent (and its boto3 clients) are created lazily inside the
    thread so importing this module never touches AWS — keeping local tests fast.
    """
    global _started
    if _started:
        return
    _started = True

    def _run():
        # Build the manager's own memory-backed reasoning agent lazily.
        try:
            from memory_agent_base import create_memory_agent
            manager_agent = create_memory_agent(
                MANAGER_SYSTEM_PROMPT, player_id=0, position_label="MANAGER", model_id=model_id
            )
        except Exception as e:
            logger.error("Manager agent init failed, disabling manager loop: %s", e)
            return

        broadcaster = DirectiveBroadcaster(self_runtime_name)
        version = 0
        current = DEFAULT_DIRECTIVE

        while True:
            time.sleep(interval_s)
            state, team_id = _snapshot()
            if state is None:
                continue
            try:
                summary = summarize_state(state, team_id, 0, "GK")
                response = manager_agent(
                    f"Current situation (you are team {team_id}):\n\n{summary}\n\n"
                    f"Your previous directive was: {current.to_json()}\n"
                    f"Issue the team's next directive as JSON."
                )
                data = _extract_json_object(str(response))
                if data is None:
                    logger.warning("Manager LLM returned no parseable directive; keeping v%d", version)
                    continue
                version += 1
                current = StrategyDirective.from_dict({**data, "version": version})
                update_local_cache(current)          # GK's own player follows its orders
                broadcaster.broadcast(current)        # push to the outfield four over A2A
            except Exception as e:
                logger.warning("Manager tick failed: %s", e)

    threading.Thread(target=_run, name="gk-manager-loop", daemon=True).start()
    logger.info("GK-manager loop started (interval=%.1fs, model=%s)", interval_s, model_id)
