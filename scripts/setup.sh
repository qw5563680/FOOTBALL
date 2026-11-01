#!/usr/bin/env bash
# Codex 云端环境安装脚本：在环境设置的「安装脚本」里填 bash scripts/setup.sh
set -euo pipefail
cd "$(dirname "$0")/.."
python -m pip install --upgrade pip
pip install -e ".[dev]"
