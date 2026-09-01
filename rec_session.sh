#!/bin/bash
# 一键起录制会话。参数透传给 rec_session.py（如 --miot、--seconds 30）。
set -euo pipefail

cd "$(dirname "$0")"
# --frozen --offline：环境已由 uv sync 建好，开录前不该再联网解析依赖。
# 配了只能内网访问的 PyPI 镜像时，裸 uv run 会先卡几十秒再失败。
exec uv run --frozen --offline rec_session.py "$@"
