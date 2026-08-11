---
name: football-env
description: Reference for the 5v5 agentic-football game environment — observation (game state) schema, the full action/command space ("the levers"), field geometry, player roles, and the parse/validation rules the shared lib enforces. Use when writing, tuning, or debugging any AI soccer position agent in agentic-football-sample-agents.
---

# Agentic Football — Environment & Action Space

A 5v5 soccer simulation. Each **position** is a separate AgentCore agent that controls exactly **one** player and returns commands for that player only, once per game tick. Five agents (`ai-gk`, `ai-def`, `ai-mid`, `ai-fwd1`, `ai-fwd2`) form one team. Shared logic lives in `agentic-football-sample-agents/lib/` and is reused across every team variant (`ai-team-strands-balanced`, `-aggressive`, `-defensive`, `-memory`, `-gateway`).

Per-agent code: `<team>/<position>/src/main.py` (system prompt + wiring). Shared code: `lib/state.py`, `lib/parsing.py`, `lib/fallback.py`, `lib/agent_base.py`, `lib/_bootstrap.py`.

## Teams, players, geometry

- **Players:** ids `0–4` per team. `0`=GK, `1`=DEF, `2`=MID, `3`=FWD1, `4`=FWD2.
- **Teams:** `teamId 0` = HOME (`teamCode "home"`), `teamId 1` = AWAY (`teamCode "away"`).
- **Field:** `x ∈ [-55, 55]`, `y ∈ [-35, 35]`.
  - HOME defends `-x`, attacks toward `+x`. Own goal `x=-55`, opponent goal `x=+55`.
  - AWAY defends `+x`, attacks toward `-x`. Own goal `x=+55`, opponent goal `x=-55`.
  - `get_goal_positions(team_id)` → `(my_goal_x, opp_goal_x)`.

## Observation (input payload)

The agent's `@app.entrypoint invoke(payload, context)` receives:

```jsonc
{
  "prompt": {            // may be a JSON string or an object
    "gameState": { ... },
    "teamId": 0,         // which team this agent plays for
    "myPlayers": [2]     // player ids this agent controls; first is used
  }
}
```

`gameState` schema (from `lib/test_helpers.py::GAME_STATE`):

```jsonc
{
  "tick": 150,
  "gameTime": 120.5,             // seconds
  "playMode": "OPEN_PLAY",       // e.g. OPEN_PLAY, kickoff/throw-in/etc.
  "modeTeamId": null,            // team the play mode belongs to, or null
  "score": { "home": 1, "away": 0 },
  "ball": {
    "position": { "x": 15.3, "y": -5.2, "z": 0 },
    "velocity": { "x": 0, "y": 0, "z": 0 },
    "isFree": false,
    "possessionAgentId": "agentId_3",   // holder, or null if loose
    "rotation": {}, "angularVelocity": {}
  },
  "players": [
    {
      "agentId": "agentId_3",   // "..._<idx>"; idx 0-4
      "teamCode": "home",       // "home" | "away"
      "position": { "x": 14, "y": -5 },
      "velocity": { "x": 2, "y": 0 },
      "orientation": 0,
      "stamina": 0.65,          // 0.0–1.0
      "currentAction": 5,       // engine action enum
      "lastAction": "DribbleTo",
      "speed": 1.5,
      "isSprinting": true
    }
    // ... 10 total: 5 home then 5 away
  ],
  "teamChat": []
}
```

**Format-agnostic parsing** (`lib/state.py`): the lib accepts both a "new" and an "old" server format. Never read raw keys directly — use the helpers:
- New: `agentId` (`"agentId_2"`), `teamCode` (`"home"`/`"away"`), `ball.possessionAgentId`.
- Old: `playerId` (int), `teamId` (int), `ball.possessionPlayerId` (int).
- Helpers: `_player_idx(p)`, `_is_my_team(p, team_id)`, `_possession_idx(ball)`, `get_possession_info(ball, players, team_id)`, `dist(a, b)`.

Agents don't get raw JSON — `summarize_state(game_state, team_id, my_player_id, position_label)` renders a compact **text** summary (time/score/playmode, ball pos + holder, own goal x, your player line with stamina/distBall/hasBall, teammates, opponents with distToMyGoal/distToMe). That string is the LLM prompt. FWD/MID summaries add `distOppGoal`.

## Action space (the levers)

Return a **JSON array with exactly ONE command** for your player. Command shape:

```jsonc
{ "commandType": "PASS", "playerId": 2, "teamId": 0,
  "parameters": { ... }, "duration": 0 }
```

`playerId`/`teamId` are force-overwritten by the lib (see parsing), so the levers you actually control are **`commandType`**, **`parameters`**, and **`duration`**.

`duration`: `0` = one-shot (fire this tick). `N` > 0 = maintained for ~N ticks. TACTICAL commands use `0`.

### Command catalog

Valid types (`lib/parsing.py::VALID_COMMANDS`) — anything else is silently dropped:

**ONE-SHOT** (`duration: 0`)
| commandType | parameters | notes |
|---|---|---|
| `MOVE_TO` | `target_x` float, `target_y` float, `sprint` bool | coords clamped to field bounds |
| `PASS` | `target_player_id` int, `type` `"GROUND"\|"AERIAL"\|"THROUGH"` | only if you hold the ball |
| `SHOOT` | `aim_location` `"TL"\|"TR"\|"BL"\|"BR"\|"CENTER"`, `power` 0.0–1.0 | only if you hold the ball |
| `SLIDE_TACKLE` | `target_player_id` int, `sprint` bool, `distance` float | risky; `target_player_id: -1` = ball carrier |
| `GK_DISTRIBUTE` | `target_player_id` int, `method` `"THROW"\|"KICK"` | GK only |

**MAINTAINED** (`duration: N` ticks)
| commandType | parameters | notes |
|---|---|---|
| `PRESS_BALL` | `intensity` 0.0–1.0 | pressure the ball carrier |
| `MARK` | `target_player_id` int, `tightness` `"LOOSE"\|"TIGHT"` | man-mark an opponent |
| `INTERCEPT` | `aggressive` bool | predict & cut out the ball |
| `FOLLOW_PLAYER` | `target_player_id` int, `target_team` `"HOME"\|"AWAY"`, `distance` float | shadow a player |

**TACTICAL** (`duration: 0`)
| commandType | parameters | notes |
|---|---|---|
| `SET_STANCE` | `stance` `0`=Balanced, `1`=Attack, `2`=Defend | |
| `CLEAR_OVERRIDE` | `{}` | hand this player back to the engine's default AI |
| `RESET` | `{}` | clear all overrides for the team |

### Validation & normalization (`lib/parsing.py::parse_commands`)

The LLM's text response is parsed then normalized. What the engine actually receives is post-processed:
1. First `[...]` JSON array in the text is extracted (falls back to whole-text JSON, or a single `{commandType:...}` object).
2. Every command gets `teamId` and `playerId` **forced** to this agent's team/player — you cannot command other players or teams.
3. Unknown `commandType` → dropped.
4. `MOVE_TO`: `target_x` clamped `[-55,55]`, `target_y` clamped `[-35,35]`.
5. `PASS`/`MARK`/`FOLLOW_PLAYER`/`GK_DISTRIBUTE`/`SLIDE_TACKLE` with missing `target_player_id` get a default injected (PASS/GK→forward 3 or 4; SLIDE_TACKLE→`-1` ball carrier; MARK/FOLLOW→`0`).
6. Empty/invalid parse → `[]`, which triggers the fallback chain.

## Control flow & fallbacks (`lib/agent_base.py`)

Three layers, best to worst:
1. **LLM** → `parse_commands`. If ≥1 valid command, use it.
2. **Rule-based fallback** `build_fallback(cfg)(game_state, team_id, player_id)` — deterministic per-position behavior.
3. **Last-resort** single command from the position's `FallbackConfig` (used only if both above throw).

## Per-position config

Model per position (balanced team): GK=`nova-micro`, DEF=`nova-lite`, MID=`nova-pro`, FWD1/FWD2=`nova-micro`. Set in each `main.py` via `create_agent(SYSTEM_PROMPT, model_id=...)`.

`FallbackConfig` (`lib/fallback.py`) is the main tuning surface for rule-based behavior. Key levers:
- `possession_action`: `GK_DISTRIBUTE` | `PASS` | `SHOOT_OR_PASS` | `SHOOT_OR_ADVANCE`.
- Default positioning: `default_x_factor`, `default_x_ref` (`my_goal`|`opp_goal`|`ball_x`), `default_y` (fixed | `track_ball` | `track_ball_30`).
- Pressing: `press_distance`, `press_intensity`, `press_duration`.
- Shooting: `shoot_threshold` (dist to opp goal), `shoot_aim`, `shoot_power`.
- Forward runs: `advance_x_factor`/`advance_y`/`advance_sprint`, `support_x_factor`/`support_y`/`support_sprint`.
- DEF marking: `mark_threshold` (>0 enables), `mark_tightness`.
- `pass_exclude_ids` (e.g. DEF excludes GK id `0`), `default_stance`, `last_resort_*`.

Prebuilt configs: `GK_CONFIG`, `DEF_CONFIG`, `MID_CONFIG`, `FWD1_CONFIG`, `FWD2_CONFIG`. Team personalities (aggressive/defensive/balanced) differ mainly in system prompts + these config values.

Default stances by role: GK/DEF=`2` (Defend), MID=`0` (Balanced), FWD=`1` (Attack).

## Where to change what

| Goal | Edit |
|---|---|
| LLM tactical instructions / tone | `<team>/<pos>/src/main.py` `SYSTEM_PROMPT` |
| Model per position | `main.py` `create_agent(..., model_id=...)` |
| Deterministic fallback behavior | `lib/fallback.py` (`*_CONFIG` or `build_fallback`) |
| Add/rename a command or change validation | `lib/parsing.py` `VALID_COMMANDS` + `_tag_commands` |
| What the LLM sees each tick | `lib/state.py` `summarize_state` |
| Sample state for local tests | `lib/test_helpers.py` `GAME_STATE` |

## Testing

`<team>/<pos>/test_local.py` mocks AgentCore and runs state-summary, fallback, and parse tests offline. Add `--llm` to hit Bedrock. Run from the position dir. `lib/test_helpers.py` also has `mock_agentcore_memory()` and `mock_agentcore_gateway()` for those team variants.
</content>
