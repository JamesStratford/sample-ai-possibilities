"""Local test for the Aggressive Manager team (memory + A2A directive channel).

Offline by default: mocks AgentCore + memory, disables the GK manager thread,
and exercises state summary, the StrategyDirective (serialization / prompt block
/ config tuning), the A2A envelope cache path, the directive-tuned fallback, and
parsing. Add --llm to hit Bedrock. Run from the position dir.
"""

import os
import sys

# Manager thread must not start during import (it would touch AWS).
os.environ["MANAGER_AUTOSTART"] = "0"
# The memory session manager is mocked; a dummy id satisfies the required-env check.
os.environ.setdefault("MEMORY_ID", "test-memory")
os.environ.setdefault("TEAM_ID", "test-team")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "lib"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))   # team-level modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from test_helpers import mock_agentcore_memory, GAME_STATE, TEAM_ID
mock_agentcore_memory()

from state import summarize_state
from parsing import parse_commands
from fallback import build_fallback, GK_CONFIG, DEF_CONFIG, MID_CONFIG, FWD1_CONFIG, FWD2_CONFIG
from directive import StrategyDirective, DEFAULT_DIRECTIVE
from strategy_channel import build_envelope, receive_directive, get_cached_directive

from main import MY_PLAYER_ID, POSITION_LABEL, SYSTEM_PROMPT

CFG = {"GK": GK_CONFIG, "DEF": DEF_CONFIG, "MID": MID_CONFIG,
       "FWD1": FWD1_CONFIG, "FWD2": FWD2_CONFIG}[POSITION_LABEL]


def test_summarize():
    print(f"=== STATE SUMMARY ({POSITION_LABEL}, player {MY_PLAYER_ID}) ===")
    print(summarize_state(GAME_STATE, TEAM_ID, MY_PLAYER_ID, POSITION_LABEL))
    print()


def test_directive():
    print("=== DIRECTIVE (serialize / prompt / tune) ===")
    d = StrategyDirective(stance=2, press_intensity=0.9, line_height=0.3,
                          mark_target_id=3, risk="all_out", tempo="fast",
                          focus_side="left", notes="press high", version=7)
    # round trip, with clamping of noisy values
    d2 = StrategyDirective.from_json(d.to_json())
    assert d2.stance == 2 and d2.mark_target_id == 3 and d2.version == 7, "FAIL: round trip"
    assert StrategyDirective.from_dict({"press_intensity": 5.0}).press_intensity == 1.0, "FAIL: clamp"
    block = d.as_prompt_block()
    assert "MANAGER'S ORDERS" in block, "FAIL: prompt block header"
    tuned = d.tune_config(CFG)
    assert tuned.default_stance == 2, "FAIL: tune_config stance"
    print("  round trip, clamping, prompt block, and config tuning OK")
    print()


def test_strategy_channel():
    print("=== A2A STRATEGY CHANNEL (envelope -> cache) ===")
    d = StrategyDirective(stance=1, risk="all_out", version=99)
    prompt_data = build_envelope(d)
    assert prompt_data["messageType"] == "STRATEGY_DIRECTIVE", "FAIL: envelope type"
    received = receive_directive(prompt_data)
    assert received is not None and received.version == 99, "FAIL: receive"
    assert get_cached_directive().version == 99, "FAIL: cache not updated"
    # a normal game tick payload must NOT be treated as a directive
    assert receive_directive({"gameState": GAME_STATE, "teamId": TEAM_ID}) is None, "FAIL: false positive"
    print("  envelope build, receive, cache, and non-directive rejection OK")
    print()


def test_fallback():
    print(f"=== DIRECTIVE-TUNED FALLBACK ({POSITION_LABEL}) ===")
    fb = build_fallback(DEFAULT_DIRECTIVE.tune_config(CFG))
    cmds = fb(GAME_STATE, TEAM_ID, MY_PLAYER_ID)
    for c in cmds:
        ok = "OK" if c["playerId"] == MY_PLAYER_ID and c["teamId"] == TEAM_ID else "WRONG"
        print(f"  [{ok}] P{c['playerId']} T{c['teamId']}: {c['commandType']} {c.get('parameters', {})}")
    assert all(c["playerId"] == MY_PLAYER_ID for c in cmds), "FAIL: wrong playerId"
    assert all(c["teamId"] == TEAM_ID for c in cmds), "FAIL: wrong teamId"
    print(f"  All {len(cmds)} commands correct")
    print()


def test_parse():
    print("=== PARSE TESTS ===")
    tests = [
        (f'[{{"commandType":"MOVE_TO","playerId":{MY_PLAYER_ID},"parameters":{{"target_x":10,"target_y":0,"sprint":true}},"duration":0}}]', 1),
        ("not json", 0),
        ("[]", 0),
    ]
    for resp, expected in tests:
        cmds = parse_commands(resp, TEAM_ID, MY_PLAYER_ID)
        ok = len(cmds) == expected and all(c["playerId"] == MY_PLAYER_ID for c in cmds)
        print(f"  [{'PASS' if ok else 'FAIL'}] {resp[:50]}... -> {len(cmds)} (expected {expected})")
    print()


def test_llm():
    print(f"=== LLM TEST ({POSITION_LABEL}) ===")
    try:
        from strands import Agent
        from strands.models import BedrockModel
        agent = Agent(model=BedrockModel(model_id="us.anthropic.claude-haiku-4-5"),
                      system_prompt=SYSTEM_PROMPT)
        prompt = f"{DEFAULT_DIRECTIVE.as_prompt_block()}\n\n{summarize_state(GAME_STATE, TEAM_ID, MY_PLAYER_ID, POSITION_LABEL)}"
        cmds = parse_commands(str(agent(prompt)), TEAM_ID, MY_PLAYER_ID)
        print(f"  parsed {len(cmds)} commands: {[c.get('commandType') for c in cmds]}")
        print("  PASSED" if cmds else "  FAILED (no commands parsed)")
    except Exception as e:
        print(f"  LLM test error: {e}")


if __name__ == "__main__":
    test_summarize()
    test_directive()
    test_strategy_channel()
    test_fallback()
    test_parse()
    if "--llm" in sys.argv:
        test_llm()
    else:
        print("Skipping LLM test. Run with --llm to test.")
