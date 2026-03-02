#!/usr/bin/env bash
set -euo pipefail
BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
cp "$BASE_DIR/index.html" "$(cd "$BASE_DIR/../.." && pwd)/index.html"
cp "$BASE_DIR/refresh.py" "$(cd "$BASE_DIR/../.." && pwd)/scripts/refresh.py"
cp "$BASE_DIR/sources.yml" "$(cd "$BASE_DIR/../.." && pwd)/config/sources.yml"
cp "$BASE_DIR/news.json" "$(cd "$BASE_DIR/../.." && pwd)/data/news.json"
cp "$BASE_DIR/items.json" "$(cd "$BASE_DIR/../.." && pwd)/data/items.json"
echo "[OK] restored checkpoint from $BASE_DIR"
