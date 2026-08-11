"""Invoke handler for outfield players on the manager team.

Differs from the shared lib's create_invoke_handler in two ways:

  1. It first checks whether the invocation is an A2A strategy push from the
     GK-manager (``strategy_channel.receive_directive``). If so it caches the
     directive and returns an ack instead of a game command.
  2. On a normal game tick it injects the cached StrategyDirective into the
     player's LLM prompt (the fast, directive-guided reactive tier) and tunes
     the deterministic fallback to follow the same plan.

The directive is read from an in-process cache, so honouring the manager's plan
adds no latency to the per-tick command path.
"""

import json
from strands import Agent

from parsing import parse_commands
from state import summarize_state
from fallback import FallbackConfig, build_fallback, build_last_resort

from strategy_channel import receive_directive, get_cached_directive


def create_manager_invoke_handler(
    app,
    agent: Agent,
    my_player_id: int,
    position_label: str,
    fallback_cfg: FallbackConfig,
    register: bool = True,
):
    """Build the directive-guided handler for a player.

    When ``register`` is True (outfield players) the handler is registered as the
    app's @entrypoint and returned. When False (the GK, which wraps it to also
    feed the manager loop) the undecorated coroutine function is returned so the
    caller can register its own wrapping entrypoint.
    """
    log = app.logger
    last_resort = build_last_resort(fallback_cfg, my_player_id)

    def _fallback(game_state, team_id, pid, directive):
        """Rule-based commands using a directive-tuned copy of the config."""
        tuned = directive.tune_config(fallback_cfg)
        return build_fallback(tuned)(game_state, team_id, pid)

    async def invoke(payload, context):
        try:
            prompt = payload.get("prompt", "{}")
            prompt_data = json.loads(prompt) if isinstance(prompt, str) else prompt

            # --- A2A strategy push from the GK-manager? ---
            directive = receive_directive(prompt_data)
            if directive is not None:
                log.info(f"{position_label} received strategy directive v{directive.version} "
                         f"(stance={directive.stance}, risk={directive.risk})")
                yield json.dumps({"status": "ok", "acceptedVersion": directive.version})
                return

            game_state = prompt_data.get("gameState", {})
            team_id = prompt_data.get("teamId", 0)

            my_players = prompt_data.get("myPlayers", [my_player_id])
            effective_pid = my_players[0] if my_players else my_player_id

            directive = get_cached_directive()

            state_summary = summarize_state(game_state, team_id, effective_pid, position_label)
            prompt_text = f"{directive.as_prompt_block()}\n\n{state_summary}"
            log.info(f"{position_label} agent invoked (team {team_id}, player {effective_pid}, "
                     f"directive v{directive.version})")

            response = agent(prompt_text)
            commands = parse_commands(str(response), team_id, effective_pid)

            if commands:
                log.info(f"LLM returned {len(commands)} commands: "
                         f"{[c.get('commandType') for c in commands]}")
                yield json.dumps(commands)
            else:
                log.warn(f"LLM parse failed, using directive-tuned fallback. Response: {str(response)[:200]}")
                commands = _fallback(game_state, team_id, effective_pid, directive)
                yield json.dumps(commands)

        except Exception as e:
            log.error(f"{position_label} agent error: {e}")
            try:
                prompt_data = json.loads(payload.get("prompt", "{}"))
                team_id = prompt_data.get("teamId", 0)
                my_players = prompt_data.get("myPlayers", [my_player_id])
                effective_pid = my_players[0] if my_players else my_player_id
                commands = _fallback(prompt_data.get("gameState", {}), team_id,
                                     effective_pid, get_cached_directive())
                yield json.dumps(commands)
            except Exception:
                cmd = dict(last_resort)
                cmd["teamId"] = 0
                yield json.dumps([cmd])

    if register:
        app.entrypoint(invoke)
    return invoke
