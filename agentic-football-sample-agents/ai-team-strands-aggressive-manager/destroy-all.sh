#!/bin/bash
set -e

# ============================================================================
# Destroy all 5 AI Team (Aggressive Manager) agents from Bedrock AgentCore
# ============================================================================
#
# Usage:
#   ./destroy-all.sh              # destroy all 5
#   ./destroy-all.sh ai-gk        # destroy one
# ============================================================================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-us-east-1}"
export AWS_DEFAULT_REGION

TEAM_RUNTIME_PREFIX="${TEAM_RUNTIME_PREFIX:-aggmgr_}"

ALL_AGENTS=("ai-gk" "ai-def" "ai-mid" "ai-fwd1" "ai-fwd2")

if [ -n "$1" ]; then
  AGENTS=("$1")
else
  AGENTS=("${ALL_AGENTS[@]}")
fi

echo "=========================================="
echo "  AI Team (Aggressive Manager) — Destroy"
echo "=========================================="
echo ""

command -v aws >/dev/null 2>&1 || { echo "ERROR: 'aws' CLI not found."; exit 1; }
AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text 2>/dev/null) || { echo "ERROR: No valid AWS credentials."; exit 1; }
echo "  AWS Account: $AWS_ACCOUNT_ID"
echo "  AWS Region:  $AWS_DEFAULT_REGION"
echo ""

get_runtime_name() {
  case "$1" in
    ai-gk)   echo "${TEAM_RUNTIME_PREFIX}gk_agent" ;;
    ai-def)  echo "${TEAM_RUNTIME_PREFIX}def_agent" ;;
    ai-mid)  echo "${TEAM_RUNTIME_PREFIX}mid_agent" ;;
    ai-fwd1) echo "${TEAM_RUNTIME_PREFIX}fwd1_agent" ;;
    ai-fwd2) echo "${TEAM_RUNTIME_PREFIX}fwd2_agent" ;;
    *) echo "" ;;
  esac
}

DESTROYED=()
FAILED=()

for agent in "${AGENTS[@]}"; do
  RUNTIME_NAME=$(get_runtime_name "$agent")
  echo "Destroying: $agent ($RUNTIME_NAME)"

  set +e
  RUNTIME_ID=$(python3 - "$RUNTIME_NAME" "$AWS_DEFAULT_REGION" <<'PYEOF'
import sys, subprocess, json
name, region = sys.argv[1], sys.argv[2]
result = subprocess.run(
    ["aws", "bedrock-agentcore-control", "list-agent-runtimes",
     "--region", region, "--output", "json"],
    capture_output=True, text=True
)
if result.returncode != 0:
    sys.exit(1)
data = json.loads(result.stdout)
for r in data.get("agentRuntimes", []):
    if r.get("agentRuntimeName") == name:
        print(r["agentRuntimeId"])
        sys.exit(0)
sys.exit(0)
PYEOF
)
  LOOKUP_EXIT=$?
  set -e

  if [ $LOOKUP_EXIT -ne 0 ]; then
    echo "  ERROR: Failed to list agent runtimes"; FAILED+=("$agent"); echo ""; continue
  fi
  if [ -z "$RUNTIME_ID" ]; then
    echo "  ⚠️  Not found in AgentCore (already deleted or never deployed)"; echo ""; continue
  fi

  echo "  Runtime ID: $RUNTIME_ID"
  set +e
  aws bedrock-agentcore-control delete-agent-runtime \
    --agent-runtime-id "$RUNTIME_ID" --region "$AWS_DEFAULT_REGION"
  DELETE_EXIT=$?
  set -e

  if [ $DELETE_EXIT -eq 0 ]; then
    echo "  ✅ $agent: DESTROYED"; DESTROYED+=("$agent")
  else
    echo "  ❌ $agent: FAILED"; FAILED+=("$agent")
  fi
  echo ""
done

echo "=========================================="
echo "  Summary"
echo "=========================================="
echo "  Destroyed: ${DESTROYED[*]:-none}"
echo "  Failed:    ${FAILED[*]:-none}"
echo ""
echo "Note: the shared AgentCore Memory resource is NOT deleted (it may be reused)."
echo "Delete it manually with the AgentCore console/CLI if no longer needed."

[ ${#FAILED[@]} -gt 0 ] && exit 1 || exit 0
