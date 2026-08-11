"""
AI Soccer Goalkeeper + MANAGER (Aggressive Manager team) — controls player 0.

Two jobs in one runtime:
  1. Reactive tier: plays as an aggressive sweeper-keeper each tick, guided by
     the current team directive (memory-backed LLM, directive injected).
  2. Deliberative tier: a background thread runs a larger manager LLM every few
     seconds and broadcasts a StrategyDirective to the four outfield players
     over the A2A strategy channel. See manager_loop.py.
"""

import os, sys; sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib")); sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "lib"))
from _bootstrap import setup_lib_path; setup_lib_path(__file__)

# team-level shared modules live one level above src/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import json
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from memory_agent_base import create_memory_agent
from fallback import GK_CONFIG

from manager_invoke_handler import create_manager_invoke_handler
from manager_loop import start_manager, record_state
from strategy_channel import MESSAGE_TYPE

app = BedrockAgentCoreApp()

MY_PLAYER_ID = 0
POSITION_LABEL = "GK"

# Runtime name — must match the name in .bedrock_agentcore.yaml.template and
# share the TEAM_RUNTIME_PREFIX so the manager can discover its teammates.
SELF_RUNTIME_NAME = os.environ.get("AGENT_RUNTIME_NAME", "aggmgr_gk_agent")

SYSTEM_PROMPT = f"""You are an EXTREMELY AGGRESSIVE AI soccer goalkeeper controlling ONLY player {MY_PLAYER_ID} (the Goalkeeper) in a 5v5 match. You are ALSO the team manager, but for THIS response you only control your own player. You receive game state each tick and must return commands for YOUR player only.

You have MEMORY of previous ticks. Use recalled history to anticipate opponent shot patterns and remember which opponents are the most dangerous shooters.

You will also be given the MANAGER'S ORDERS (which you yourself issued). Follow them.

## Your Role — Aggressive Sweeper-Keeper
- You are NOT a traditional goalkeeper. You play as a sweeper-keeper who pushes far up the pitch.
- When your team has the ball, MOVE_TO the halfway line or beyond to act as an extra attacker.
- When you have the ball near your own goal, use GK_DISTRIBUTE with KICK to launch it forward.
- When you have the ball in midfield or beyond, PASS aggressively to forwards or SHOOT.
- SHOOT if you find yourself within ~35 units of the opponent's goal.
- Only retreat to your goal line when the ball is in your defensive third AND an opponent has it.
- Use INTERCEPT aggressively and PRESS_BALL hard when an opponent has the ball in your half.

## Available Commands (commandType → parameters)

ONE-SHOT:
- MOVE_TO: target_x (float), target_y (float), sprint (bool)
- PASS: target_player_id (int), type ("GROUND"|"AERIAL"|"THROUGH") — only if you have ball
- SHOOT: aim_location ("TL"|"TR"|"BL"|"BR"|"CENTER"), power (0.0-1.0) — only if you have ball
- SLIDE_TACKLE: target_player_id (int), sprint (bool), distance (float)
- GK_DISTRIBUTE: target_player_id (int), method ("THROW"|"KICK") — use KICK for long balls forward

MAINTAINED:
- PRESS_BALL: intensity (0.0-1.0)
- INTERCEPT: aggressive (bool) — ALWAYS set to true
- FOLLOW_PLAYER: target_player_id (int), target_team ("HOME"|"AWAY"), distance (float)

TACTICAL:
- SET_STANCE: stance (0=Balanced, 1=Attack, 2=Defend)
- CLEAR_OVERRIDE: {{}}
- RESET: {{}}

## Field
- Coordinates: x roughly -55 to +55, y roughly -35 to +35
- Team 0 (HOME) defends -x, attacks toward +x. Team 1 (AWAY) defends +x, attacks toward -x.

## Response
Return ONLY a JSON array with exactly ONE command for player {MY_PLAYER_ID}.
Example: [{{"commandType":"GK_DISTRIBUTE","playerId":{MY_PLAYER_ID},"parameters":{{"target_player_id":3,"method":"KICK"}},"duration":0}}]
Return ONLY the JSON array, no text before or after."""


agent = create_memory_agent(SYSTEM_PROMPT, MY_PLAYER_ID, POSITION_LABEL, model_id="us.anthropic.claude-haiku-4-5")

# The GK entrypoint plays player 0 AND feeds the manager loop the freshest state.
_play = create_manager_invoke_handler(app, agent, MY_PLAYER_ID, POSITION_LABEL, GK_CONFIG, register=False)


@app.entrypoint
async def invoke(payload, context):
    """Wrap the player handler so every real game tick also records state for the manager."""
    try:
        prompt = payload.get("prompt", "{}")
        prompt_data = json.loads(prompt) if isinstance(prompt, str) else prompt
        if isinstance(prompt_data, dict) and prompt_data.get("messageType") != MESSAGE_TYPE:
            record_state(prompt_data.get("gameState", {}), prompt_data.get("teamId", 0))
    except Exception:
        pass
    async for chunk in _play(payload, context):
        yield chunk


# Start the deliberative manager thread (gated so local tests stay offline).
if os.environ.get("MANAGER_AUTOSTART", "1") == "1":
    start_manager(SELF_RUNTIME_NAME)

if __name__ == "__main__":
    app.run()
