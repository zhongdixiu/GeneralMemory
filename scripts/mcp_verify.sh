#!/bin/bash
# MCP 协议验证脚本
# 用法: ./scripts/mcp_verify.sh [BASE_URL] [TOKEN]
# 默认: http://localhost:8000 test-token

set -e

BASE="${1:-http://localhost:8000}"
TOKEN="${2:-test-token}"
PASS=0
FAIL=0

green() { echo -e "\033[32m$1\033[0m"; }
red()   { echo -e "\033[31m$1\033[0m"; }
cyan()  { echo -e "\033[36m$1\033[0m"; }

check() {
    local name="$1" expected="$2" response="$3" jq_filter="${4:-.}"
    local actual
    actual=$(echo "$response" | jq -r "$jq_filter" 2>/dev/null || echo "PARSE_ERROR")
    if echo "$actual" | grep -q "$expected"; then
        green "  PASS: $name"
        PASS=$((PASS + 1))
    else
        red "  FAIL: $name (expected contains '$expected', got '$actual')"
        FAIL=$((FAIL + 1))
    fi
}

mcp_call() {
    local id="$1" body="$2" extra_headers="${3:-}"
    curl -s "$BASE/mcp" \
        -H "Authorization: Bearer $TOKEN" \
        -H "Content-Type: application/json" \
        -H "Accept: application/json, text/event-stream" \
        -H "X-Memory-Source-System: test" \
        $extra_headers \
        -d "$body"
}

echo "========================================"
echo " MCP 协议验证"
echo " BASE: $BASE"
echo " TOKEN: $TOKEN"
echo "========================================"
echo ""

# ---------- 1. 连通性 ----------
cyan "1. MCP 端点连通性"
resp=$(mcp_call 1 '{"jsonrpc":"2.0","id":1,"method":"tools/list"}')
check "端点可达" "result" "$resp"

# ---------- 2. 未认证拒绝 ----------
cyan "2. 未认证拒绝"
resp=$(curl -s "$BASE/mcp" \
    -H "Content-Type: application/json" \
    -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}')
check "401 拒绝" "UNAUTHENTICATED" "$resp" ".error.code"

# ---------- 3. 初始化 ----------
cyan "3. MCP 初始化"
resp=$(mcp_call 10 '{
  "jsonrpc":"2.0","id":10,"method":"initialize",
  "params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"verify","version":"1"}}
}')
check "serverInfo 存在" "serverInfo" "$resp"

# ---------- 4. 工具列表 ----------
cyan "4. 工具列表"
resp=$(mcp_call 20 '{"jsonrpc":"2.0","id":20,"method":"tools/list"}')
check "add_memory" "add_memory" "$resp"
check "search_memories" "search_memories" "$resp"
check "get_user_profile" "get_user_profile" "$resp"
check "get_memory_task" "get_memory_task" "$resp"

# ---------- 5. add_memory ----------
cyan "5. add_memory"
resp=$(mcp_call 30 '{
  "jsonrpc":"2.0","id":30,"method":"tools/call",
  "params":{"name":"add_memory","arguments":{"text":"用户喜欢简洁的报告风格","user_id":"user_1","session_id":"session_1"}}
}' " -H 'X-Memory-User-Id: user_1' -H 'X-Memory-Session-Id: session_1'")
TASK_ID=$(echo "$resp" | jq -r '.result.structuredContent.task_id // .result.content[0].text' | jq -r '.task_id // empty' 2>/dev/null)
check "返回 task_id" "task_id" "$resp"

# ---------- 6. search_memories ----------
cyan "6. search_memories"
resp=$(mcp_call 40 '{
  "jsonrpc":"2.0","id":40,"method":"tools/call",
  "params":{"name":"search_memories","arguments":{"query":"报告风格","user_id":"user_1","limit":5}}
}' " -H 'X-Memory-User-Id: user_1'")
check "返回 results" "results" "$resp"

# ---------- 7. get_user_profile ----------
cyan "7. get_user_profile"
resp=$(mcp_call 50 '{
  "jsonrpc":"2.0","id":50,"method":"tools/call",
  "params":{"name":"get_user_profile","arguments":{"user_id":"user_1","include_inferred":false}}
}' " -H 'X-Memory-User-Id: user_1'")
check "返回 profiles" "profiles" "$resp"

# ---------- 8. get_memory_task ----------
cyan "8. get_memory_task"
if [ -n "$TASK_ID" ]; then
    resp=$(mcp_call 60 "{
      \"jsonrpc\":\"2.0\",\"id\":60,\"method\":\"tools/call\",
      \"params\":{\"name\":\"get_memory_task\",\"arguments\":{\"task_id\":\"$TASK_ID\"}}
    }")
    check "任务可查" "task_id" "$resp"
else
    yellow "  跳过（无 task_id）"
fi

# ---------- 9. 身份冲突 ----------
cyan "9. 身份冲突检测"
resp=$(mcp_call 70 '{
  "jsonrpc":"2.0","id":70,"method":"tools/call",
  "params":{"name":"add_memory","arguments":{"text":"test","user_id":"arg_user","session_id":"s1"}}
}' " -H 'X-Memory-User-Id: header_user' -H 'X-Memory-Session-Id: s1'")
check "IDENTITY_CONFLICT" "IDENTITY_CONFLICT" "$resp"

# ---------- 结果 ----------
echo ""
echo "========================================"
echo " 结果: $PASS 通过, $FAIL 失败"
echo "========================================"
[ "$FAIL" -eq 0 ] && green "全部通过!" || red "存在失败项"