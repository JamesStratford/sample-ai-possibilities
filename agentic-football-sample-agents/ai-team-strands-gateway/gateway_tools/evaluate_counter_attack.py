"""Gateway Tool: evaluate_counter_attack

Modern World-Cup-style transition analysis. The instant a team wins the ball,
the biggest chances come from attacking BEFORE the opponent's shape recovers
(cf. Argentina/France/Morocco 2022). This tool decides whether a fast break is
on right now: are opponents caught upfield, is there space ahead of the ball,
and which teammate should be sprung.

Consumes the FULL game_state (matching the gateway-advertised schema) and
extracts positions internally, so it works with both the new
(agentId/teamCode/possessionAgentId) and old (playerId/teamId/possessionPlayerId)
game-server formats.

Deployed as a Lambda behind AgentCore Gateway.
"""

import json
import math

FIELD_X = 55.0
FIELD_Y = 35.0


# ---- format-agnostic helpers (mirror lib/state.py, inlined for the Lambda) ----

def _player_idx(p: dict) -> int:
    if "agentId" in p:
        try:
            return int(p["agentId"].rsplit("_", 1)[-1])
        except (ValueError, IndexError):
            return 0
    return p.get("playerId", 0)


def _is_my_team(p: dict, team_id: int) -> bool:
    if "teamCode" in p:
        return p["teamCode"] == ("home" if team_id == 0 else "away")
    return p.get("teamId") == team_id


def _possession_idx(ball: dict):
    agent_id = ball.get("possessionAgentId")
    if agent_id is not None:
        try:
            return int(agent_id.rsplit("_", 1)[-1])
        except (ValueError, IndexError):
            return None
    return ball.get("possessionPlayerId")


def _pos(p: dict) -> dict:
    return p.get("position", {"x": 0.0, "y": 0.0})


def _distance(a: dict, b: dict) -> float:
    return math.sqrt((a.get("x", 0) - b.get("x", 0)) ** 2 + (a.get("y", 0) - b.get("y", 0)) ** 2)


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def lambda_handler(event, context):
    """Input: full game_state, team_id, player_id (the ball-winner / carrier)."""
    body = json.loads(event.get("body", "{}")) if isinstance(event.get("body"), str) else event

    game_state = body["game_state"]
    team_id = body["team_id"]
    player_id = body.get("player_id", 0)

    players = game_state.get("players", [])
    ball = game_state.get("ball", {})
    ball_pos = ball.get("position", {"x": 0.0, "y": 0.0})

    # Attack direction: team 0 (HOME) attacks +x, team 1 (AWAY) attacks -x.
    opp_goal_x = FIELD_X if team_id == 0 else -FIELD_X
    attack_dir = 1 if team_id == 0 else -1

    my_team = [p for p in players if _is_my_team(p, team_id)]
    opponents = [p for p in players if not _is_my_team(p, team_id)]

    possession_id = _possession_idx(ball)
    we_have_ball = any(_player_idx(p) == possession_id for p in my_team)

    def ahead_of_ball(pos: dict) -> bool:
        return (pos.get("x", 0) - ball_pos.get("x", 0)) * attack_dir > 0

    # Opponents goal-side of the ball = recovering defenders (fewer -> better).
    opp_ahead = [o for o in opponents if ahead_of_ball(_pos(o))]
    # Opponents on the wrong side of the ball = caught upfield (committed forward).
    opp_upfield = [o for o in opponents if not ahead_of_ball(_pos(o))]

    # Teammates ahead of the ball = runners we can spring (exclude GK id 0 & self).
    runners = [
        p for p in my_team
        if _player_idx(p) not in (0, player_id) and ahead_of_ball(_pos(p))
    ]

    # Numerical advantage in the attacking channel (attackers vs recovered defenders).
    numerical_advantage = len(runners) - len(opp_ahead)

    # Space in front of the ball = distance to nearest recovering opponent.
    if opp_ahead:
        space_ahead = round(min(_distance(ball_pos, _pos(o)) for o in opp_ahead), 1)
    else:
        space_ahead = 999.0

    # Pick the best teammate to spring: most advanced + most space.
    best_target = None
    best_score = -1.0
    for r in runners:
        rp = _pos(r)
        advancement = (rp.get("x", 0) - ball_pos.get("x", 0)) * attack_dir
        nearest_opp = min((_distance(rp, _pos(o)) for o in opponents), default=999.0)
        score = advancement + nearest_opp * 0.5
        if score > best_score:
            best_score = score
            best_target = {
                "player_id": _player_idx(r),
                "position": {"x": round(rp.get("x", 0), 1), "y": round(rp.get("y", 0), 1)},
                "advancement": round(advancement, 1),
                "space": round(nearest_opp, 1),
            }

    # A run target ahead of that teammate, toward the opponent goal, into open space.
    target_run_point = None
    if best_target:
        tx = best_target["position"]["x"]
        ty = best_target["position"]["y"]
        target_run_point = {
            "x": round(_clamp(tx + attack_dir * 12.0, -FIELD_X, FIELD_X), 1),
            "y": round(_clamp(ty * 0.6, -FIELD_Y, FIELD_Y), 1),
        }

    # Decision. A counter is on when we hold the ball, have at least parity in the
    # attacking channel, and there is runway ahead.
    viable = we_have_ball and numerical_advantage >= 0 and space_ahead >= 12

    if viable and best_target and best_target["advancement"] > 8:
        recommended_action = "PASS"          # spring the runner with a through ball
    elif viable and space_ahead >= 20:
        recommended_action = "DRIBBLE"        # carry into the vacated space
    else:
        recommended_action = "HOLD"           # no break on — recycle possession

    # Urgency: the more opponents caught upfield, the faster the window closes.
    if len(opp_upfield) >= 2:
        urgency = "HIGH"
    elif len(opp_upfield) == 1:
        urgency = "MEDIUM"
    else:
        urgency = "LOW"

    return {
        "statusCode": 200,
        "body": json.dumps({
            "should_counter": viable,
            "recommended_action": recommended_action,
            "recommended_target": best_target,
            "target_run_point": target_run_point,
            "numerical_advantage": numerical_advantage,
            "runners_ahead": len(runners),
            "defenders_recovered": len(opp_ahead),
            "opponents_caught_upfield": len(opp_upfield),
            "space_ahead": space_ahead,
            "urgency": urgency,
            "we_have_ball": we_have_ball,
        }),
    }
