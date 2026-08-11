#!/bin/bash
set -e

# ============================================================================
# Deploy all 5 AI Team (Aggressive Manager) agents to Bedrock AgentCore
# ============================================================================
#
# Aggressive base + AgentCore Memory (STM) on every agent + a GK-manager that
# broadcasts a StrategyDirective to the outfield players over the A2A strategy
# channel (see strategy_channel.py / manager_loop.py).
#
# Usage:
#   AWS_PROFILE=your-profile MEMORY_ID=xxx ./deploy-all.sh          # deploy all
#   AWS_PROFILE=your-profile MEMORY_ID=xxx ./deploy-all.sh ai-gk    # deploy one
#
# Requires MEMORY_ID (auto-created via create_memory.py if unset).
# ============================================================================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BUILD_DIR="$SCRIPT_DIR/_build"

AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-us-east-1}"
export AWS_DEFAULT_REGION

TEAM_RUNTIME_PREFIX="${TEAM_RUNTIME_PREFIX:-aggmgr_}"
MANAGER_INTERVAL_S="${MANAGER_INTERVAL_S:-6}"

ALL_AGENTS=("ai-gk" "ai-def" "ai-mid" "ai-fwd1" "ai-fwd2")

# Team-level shared modules copied into every agent's staging dir.
TEAM_MODULES=("memory_agent_base.py" "directive.py" "strategy_channel.py" \
              "manager_invoke_handler.py" "manager_loop.py")

# ai-<pos> -> runtime name (must match .bedrock_agentcore.yaml.template + prefix)
runtime_name() {
  case "$1" in
    ai-gk)   echo "${TEAM_RUNTIME_PREFIX}gk_agent" ;;
    ai-def)  echo "${TEAM_RUNTIME_PREFIX}def_agent" ;;
    ai-mid)  echo "${TEAM_RUNTIME_PREFIX}mid_agent" ;;
    ai-fwd1) echo "${TEAM_RUNTIME_PREFIX}fwd1_agent" ;;
    ai-fwd2) echo "${TEAM_RUNTIME_PREFIX}fwd2_agent" ;;
  esac
}

if [ -n "$1" ]; then
  AGENTS=("$1")
else
  AGENTS=("${ALL_AGENTS[@]}")
fi

echo "=========================================="
echo "  AI Team (Aggressive Manager) — Deploy"
echo "=========================================="
echo ""

# ------ Pre-flight ------
echo "Checking prerequisites..."

if [ -z "$MEMORY_ID" ]; then
  echo "  MEMORY_ID not set — creating memory resource automatically..."
  set +e
  CREATE_OUTPUT=$(python3 "$SCRIPT_DIR/create_memory.py" 2>&1)
  CREATE_EXIT=$?
  set -e
  echo "$CREATE_OUTPUT"
  if [ $CREATE_EXIT -ne 0 ]; then
    echo "ERROR: create_memory.py failed (exit $CREATE_EXIT)."
    exit 1
  fi
  MEMORY_ID=$(echo "$CREATE_OUTPUT" | sed -n 's/.*export MEMORY_ID=\([^ ]*\).*/\1/p')
  [ -z "$MEMORY_ID" ] && MEMORY_ID=$(echo "$CREATE_OUTPUT" | sed -n 's/.*Memory resource ready: \([^ ]*\).*/\1/p')
  if [ -z "$MEMORY_ID" ]; then
    echo "ERROR: Could not parse MEMORY_ID from output."
    exit 1
  fi
  export MEMORY_ID
  echo "  Created MEMORY_ID: $MEMORY_ID"
else
  echo "  MEMORY_ID: $MEMORY_ID (from env)"
fi

command -v agentcore >/dev/null 2>&1 || { echo "ERROR: 'agentcore' CLI not found. pip install bedrock-agentcore-starter-toolkit"; exit 1; }
echo "  agentcore CLI: OK"
command -v aws >/dev/null 2>&1 || { echo "ERROR: 'aws' CLI not found."; exit 1; }
echo "  aws CLI: OK"

AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text 2>/dev/null) || { echo "ERROR: No valid AWS credentials."; exit 1; }
export AWS_ACCOUNT_ID
echo "  AWS Account: $AWS_ACCOUNT_ID"
echo "  AWS Region:  $AWS_DEFAULT_REGION"
echo "  Runtime prefix: $TEAM_RUNTIME_PREFIX"
echo ""

cleanup() { echo ""; echo "Cleaning up build directory..."; rm -rf "$BUILD_DIR"; }
trap cleanup EXIT

DEPLOYED=()
FAILED=()

for agent in "${AGENTS[@]}"; do
  AGENT_SRC="$SCRIPT_DIR/$agent"
  STAGE="$BUILD_DIR/$agent"
  RT_NAME="$(runtime_name "$agent")"

  echo "=========================================="
  echo "  Deploying: $agent  (runtime: $RT_NAME)"
  echo "=========================================="

  if [ ! -d "$AGENT_SRC" ]; then
    echo "  ERROR: Agent directory not found: $AGENT_SRC"; FAILED+=("$agent"); continue
  fi

  rm -rf "$STAGE"; mkdir -p "$STAGE/src"

  cp "$AGENT_SRC/src/main.py" "$STAGE/src/main.py"

  # Shared lib (../lib full-repo layout, or ./lib team-only layout)
  if [ -d "$SCRIPT_DIR/../lib" ]; then LIB_SRC="$SCRIPT_DIR/../lib"
  elif [ -d "$SCRIPT_DIR/lib" ]; then LIB_SRC="$SCRIPT_DIR/lib"
  else echo "  ERROR: Shared lib not found."; FAILED+=("$agent"); continue; fi
  mkdir -p "$STAGE/lib"
  cp "$LIB_SRC"/*.py "$STAGE/lib/"
  find "$STAGE/lib" -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true

  # Team-level shared modules
  for m in "${TEAM_MODULES[@]}"; do cp "$SCRIPT_DIR/$m" "$STAGE/$m"; done

  cp "$AGENT_SRC/requirements.txt" "$STAGE/requirements.txt"

  sed \
    -e "s|\${AWS_ACCOUNT_ID}|$AWS_ACCOUNT_ID|g" \
    -e "s|\${AWS_DEFAULT_REGION}|$AWS_DEFAULT_REGION|g" \
    "$AGENT_SRC/.bedrock_agentcore.yaml.template" > "$STAGE/.bedrock_agentcore.yaml"

  # Per-agent env. GK also autostarts the manager loop; outfielders never do.
  ENV_ARGS=(--env "MEMORY_ID=$MEMORY_ID"
            --env "AWS_DEFAULT_REGION=$AWS_DEFAULT_REGION"
            --env "TEAM_RUNTIME_PREFIX=$TEAM_RUNTIME_PREFIX"
            --env "AGENT_RUNTIME_NAME=$RT_NAME")
  if [ "$agent" = "ai-gk" ]; then
    ENV_ARGS+=(--env "MANAGER_AUTOSTART=1" --env "MANAGER_INTERVAL_S=$MANAGER_INTERVAL_S")
  else
    ENV_ARGS+=(--env "MANAGER_AUTOSTART=0")
  fi

  echo "  Deploying from: $STAGE"
  if (cd "$STAGE" && agentcore deploy --auto-update-on-conflict "${ENV_ARGS[@]}"); then
    echo "  ✅ $agent: DEPLOYED"; DEPLOYED+=("$agent")
  else
    echo "  ❌ $agent: FAILED"; FAILED+=("$agent")
  fi
  echo ""
done

# ------ Attach IAM to execution roles: Memory + A2A (discover & invoke peers) ------
echo "Attaching Memory + A2A permissions to execution roles..."
set +e
EXEC_ROLES=$(aws iam list-roles \
  --query "Roles[?starts_with(RoleName, 'AmazonBedrockAgentCoreSDKRuntime-${AWS_DEFAULT_REGION}-')].RoleName" \
  --output text 2>/dev/null)
set -e

if [ -n "$EXEC_ROLES" ]; then
  for EXEC_ROLE_NAME in $EXEC_ROLES; do
    aws iam put-role-policy --role-name "$EXEC_ROLE_NAME" \
      --policy-name AgentCoreMemoryAndA2AAccess \
      --policy-document "{
        \"Version\": \"2012-10-17\",
        \"Statement\": [
          {
            \"Effect\": \"Allow\",
            \"Action\": [
              \"bedrock-agentcore:ListEvents\", \"bedrock-agentcore:CreateEvent\",
              \"bedrock-agentcore:GetEvent\", \"bedrock-agentcore:DeleteEvent\",
              \"bedrock-agentcore:RetrieveMemoryRecords\", \"bedrock-agentcore:GetMemoryRecord\",
              \"bedrock-agentcore:ListMemoryRecords\"
            ],
            \"Resource\": \"arn:aws:bedrock-agentcore:${AWS_DEFAULT_REGION}:${AWS_ACCOUNT_ID}:memory/*\"
          },
          {
            \"Effect\": \"Allow\",
            \"Action\": [\"bedrock-agentcore:ListAgentRuntimes\", \"bedrock-agentcore:GetAgentRuntime\"],
            \"Resource\": \"*\"
          },
          {
            \"Effect\": \"Allow\",
            \"Action\": [\"bedrock-agentcore:InvokeAgentRuntime\"],
            \"Resource\": \"arn:aws:bedrock-agentcore:${AWS_DEFAULT_REGION}:${AWS_ACCOUNT_ID}:runtime/*\"
          }
        ]
      }" 2>/dev/null && echo "  ✅ policy attached to: $EXEC_ROLE_NAME" \
      || echo "  ⚠️  failed to attach policy to: $EXEC_ROLE_NAME"
  done
else
  echo "  ⚠️  Could not find execution roles — attach AgentCoreMemoryAndA2AAccess manually."
fi
echo ""

echo "=========================================="
echo "  Deployment Summary"
echo "=========================================="
echo ""
echo "  Deployed: ${DEPLOYED[*]:-none}"
echo "  Failed:   ${FAILED[*]:-none}"
echo "  Account:  $AWS_ACCOUNT_ID"
echo "  Region:   $AWS_DEFAULT_REGION"
echo "  Memory:   $MEMORY_ID"
echo ""

if [ ${#FAILED[@]} -gt 0 ]; then
  echo "Some agents failed to deploy. Check the output above."; exit 1
fi
echo "All agents deployed successfully."
