#!/usr/bin/env bash
# demo-warmup.sh — 演示预热脚本（Story 2-12）
# 提前 clone 实验 repo、安装依赖、缓存数据，确保演示时无需等待网络
#
# Usage: bash scripts/demo/demo-warmup.sh [--skip-clone] [--skip-install]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

DEMO_DIR="${DEMO_DIR:-/tmp/sf-demo}"
BERT_REPO="https://github.com/google-research/bert"
BERT_DIR="$DEMO_DIR/bert"

# Parse args
SKIP_CLONE=false
SKIP_INSTALL=false
for arg in "$@"; do
    case $arg in
        --skip-clone)    SKIP_CLONE=true ;;
        --skip-install)  SKIP_INSTALL=true ;;
    esac
done

echo "================================================="
echo " Xueban Hackathon Demo — Warmup"
echo "================================================="
echo "Demo dir: $DEMO_DIR"
mkdir -p "$DEMO_DIR"

# ── Step 1: Clone BERT repo ──────────────────────────────────────────────────
if $SKIP_CLONE; then
    echo "[skip] Skipping repo clone"
elif [ -d "$BERT_DIR/.git" ]; then
    echo "[ok] BERT repo already cloned at $BERT_DIR"
else
    echo "[clone] Cloning BERT repo (shallow)..."
    git clone --depth=1 "$BERT_REPO" "$BERT_DIR"
    echo "[ok] Clone complete"
fi

# ── Step 2: Install Python dependencies ─────────────────────────────────────
if $SKIP_INSTALL; then
    echo "[skip] Skipping pip install"
else
    echo "[install] Installing BERT dependencies..."
    pip install --quiet tensorflow==2.13.0 absl-py 2>/dev/null || \
        echo "[warn] pip install failed (non-fatal in mock mode)"
    echo "[ok] Dependencies ready"
fi

# ── Step 3: Download MRPC dataset placeholder ────────────────────────────────
MRPC_DIR="$DEMO_DIR/glue_data/MRPC"
if [ -d "$MRPC_DIR" ]; then
    echo "[ok] MRPC dataset already present"
else
    echo "[data] Creating MRPC placeholder (mock dataset for demo)..."
    mkdir -p "$MRPC_DIR"
    # Minimal TSV files so run_classifier.py doesn't crash on startup
    printf "Quality\t#1 ID\t#2 ID\t#1 String\t#2 String\n" > "$MRPC_DIR/train.tsv"
    printf "0\t1\t2\tThe cat sat.\tA cat was sitting.\n" >> "$MRPC_DIR/train.tsv"
    cp "$MRPC_DIR/train.tsv" "$MRPC_DIR/dev.tsv"
    cp "$MRPC_DIR/train.tsv" "$MRPC_DIR/test.tsv"
    echo "[ok] MRPC placeholder created"
fi

# ── Step 4: Verify Xueban is up ─────────────────────────────────────────
echo "[check] Checking Xueban ACP Server..."
if curl -sf http://localhost:8000/api/health > /dev/null 2>&1; then
    echo "[ok] Xueban is running"
else
    echo "[warn] Xueban not detected at localhost:8000 — run 'docker compose -f docker/docker-compose.yml up' first"
fi

# ── Step 5: Start Hermes Adapter in background ───────────────────────────────
ADAPTER_PID_FILE="$DEMO_DIR/adapter.pid"
if [ -f "$ADAPTER_PID_FILE" ] && kill -0 "$(cat "$ADAPTER_PID_FILE")" 2>/dev/null; then
    echo "[ok] Hermes Adapter already running (pid=$(cat "$ADAPTER_PID_FILE"))"
else
    echo "[start] Starting Hermes Adapter in mock mode (background)..."
    nohup python "$PROJECT_ROOT/scripts/demo/hermes-adapter.py" --mock \
        > "$DEMO_DIR/adapter.log" 2>&1 &
    echo $! > "$ADAPTER_PID_FILE"
    sleep 1
    if kill -0 "$(cat "$ADAPTER_PID_FILE")" 2>/dev/null; then
        echo "[ok] Adapter started (pid=$(cat "$ADAPTER_PID_FILE")) — logs: $DEMO_DIR/adapter.log"
    else
        echo "[warn] Adapter may have failed to start; check $DEMO_DIR/adapter.log"
    fi
fi

echo ""
echo "================================================="
echo " Warmup complete! Demo environment ready."
echo "================================================="
echo ""
echo "Next steps:"
echo "  1. Open http://localhost:3000 in browser"
echo "  2. Go to Team Builder → 论文复现团队"
echo "  3. Confirm Hermes shows 🟢 online in Agent list"
echo "  4. Run: bash scripts/demo/demo-run.sh"
