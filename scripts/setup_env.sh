#!/usr/bin/env bash
# Copyright (C) 2026 jkjkiiiii
# SPDX-License-Identifier: GPL-3.0-or-later
# 本程序为自由软件：可依 GPL-3.0 或更高版本条款再发布或修改，详见 LICENSE。
# 重建 pdf2md skill 的专属虚拟环境（uv + 固定 Python 版本）。
# 仅在需要重建环境时执行；日常使用直接调用 .venv 里的解释器即可。
set -euo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_VERSION="${PYTHON_VERSION:-3.12.14}"

uv venv --python "$PYTHON_VERSION" "$SKILL_DIR/.venv"
uv pip install --python "$SKILL_DIR/.venv/Scripts/python.exe" -r "$SKILL_DIR/requirements.txt"

echo "环境就绪：$SKILL_DIR/.venv（Python $PYTHON_VERSION + httpx）"
