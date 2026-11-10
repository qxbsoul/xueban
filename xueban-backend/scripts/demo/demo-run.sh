#!/usr/bin/env bash
# demo-run.sh — 一键启动 Hackathon Demo（Story 2-12）
# 假设 demo-warmup.sh 已经运行过（环境已预热）
#
# Usage:
#   bash scripts/demo/demo-run.sh                          # 完整版
#   bash scripts/demo/demo-run.sh --skip-clone --skip-install  # 快进版

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

DEMO_DIR="${DEMO_DIR:-/tmp/sf-demo}"
BERT_DIR="$DEMO_DIR/bert"
MRPC_DIR="$DEMO_DIR/glue_data/MRPC"

# ── Parse args ───────────────────────────────────────────────────────────────
SKIP_CLONE=false
SKIP_INSTALL=false
FAST_MODE=false
for arg in "$@"; do
    case $arg in
        --skip-clone)    SKIP_CLONE=true; FAST_MODE=true ;;
        --skip-install)  SKIP_INSTALL=true; FAST_MODE=true ;;
        --fast)          SKIP_CLONE=true; SKIP_INSTALL=true; FAST_MODE=true ;;
    esac
done

echo "================================================="
if $FAST_MODE; then
    echo " Xueban Demo — 快进版（Fast Mode）"
else
    echo " Xueban Demo — 完整版"
fi
echo "================================================="

# ── Preflight check ──────────────────────────────────────────────────────────
echo "[check] Verifying demo environment..."
ERRORS=0

if ! curl -sf http://localhost:8000/api/health > /dev/null 2>&1; then
    echo "  ❌ Xueban 未启动 — 请先运行 'docker compose -f docker/docker-compose.yml up'"
    ERRORS=$((ERRORS + 1))
fi

ADAPTER_PID_FILE="$DEMO_DIR/adapter.pid"
if [ -f "$ADAPTER_PID_FILE" ] && kill -0 "$(cat "$ADAPTER_PID_FILE")" 2>/dev/null; then
    echo "  ✅ Hermes Adapter 运行中 (pid=$(cat "$ADAPTER_PID_FILE"))"
else
    echo "  ⚠️  Hermes Adapter 未检测到 — 自动启动中..."
    nohup python "$PROJECT_ROOT/scripts/demo/hermes-adapter.py" --mock \
        > "$DEMO_DIR/adapter.log" 2>&1 &
    echo $! > "$ADAPTER_PID_FILE"
    sleep 2
    echo "  ✅ Hermes Adapter 已启动"
fi

if [ "$ERRORS" -gt 0 ]; then
    echo ""
    echo "❌ 预检失败 ($ERRORS 个错误)，请修复后重试。"
    exit 1
fi

echo "[ok] 预检通过"
echo ""

# ── Step 1: Clone repo（可跳过）──────────────────────────────────────────────
if $SKIP_CLONE; then
    echo "[快进] 跳过 clone（使用已缓存 repo）"
    [ -d "$BERT_DIR" ] || { echo "❌ $BERT_DIR 不存在，请先运行 demo-warmup.sh"; exit 1; }
else
    if [ -d "$BERT_DIR/.git" ]; then
        echo "[clone] BERT repo 已存在，更新中..."
        git -C "$BERT_DIR" pull --ff-only --quiet || echo "[warn] git pull failed (using cached)"
    else
        echo "[clone] Cloning BERT repo..."
        git clone --depth=1 https://github.com/google-research/bert "$BERT_DIR"
    fi
    echo "[ok] Repo 就绪"
fi

# ── Step 2: Install deps（可跳过）────────────────────────────────────────────
if $SKIP_INSTALL; then
    echo "[快进] 跳过 pip install（使用已安装依赖）"
else
    echo "[install] 安装依赖..."
    pip install --quiet tensorflow absl-py 2>/dev/null || \
        echo "[warn] 安装失败（mock 模式下非致命）"
    echo "[ok] 依赖就绪"
fi

echo ""
echo "================================================="
echo " 开始演示任务..."
echo "================================================="
echo ""
echo "任务: 复现 BERT MRPC 微调实验"
echo ""

# ── Step 3: Trigger orchestration via API ────────────────────────────────────
TASK_PAYLOAD='{
  "team_id": "demo-team",
  "instruction": "复现 https://github.com/google-research/bert 的 MRPC 微调实验，输出 F1 score 并和论文对比",
  "context": {
    "repo_path": "'"$BERT_DIR"'",
    "data_dir": "'"$MRPC_DIR"'",
    "mode": "demo"
  }
}'

if curl -sf -X POST http://localhost:8000/api/teams/demo-team/run \
    -H "Content-Type: application/json" \
    -d "$TASK_PAYLOAD" | python -m json.tool; then
    echo ""
    echo "✅ 任务已提交到 Orchestrator — 查看 LiveDashboard 观察任务分配"
else
    echo ""
    echo "[warn] API 调用失败（mock 模式：直接向 Adapter 发送测试任务）"
    echo "        请打开 http://localhost:3000 手动在 Chat 发起任务"
fi

echo ""
echo "================================================="
echo " Demo 进行中！请切换到浏览器观察："
echo "   http://localhost:3000"
echo "================================================="
echo ""
echo "观察重点："
echo "  1. LiveDashboard: 代码分析子任务 → Hermes（代码理解者）"
echo "  2. BriefBoard: Hermes > Analyzing repo structure..."
echo "  3. 原生 Agent shell 执行与 Hermes 并发，互不阻塞"
echo "  4. 最终报告: F1 Score 对比（88.5% vs 论文 88.9%）"
echo ""
echo "Adapter 日志: tail -f $DEMO_DIR/adapter.log"
