#!/usr/bin/env bash
set -euo pipefail

# scripts/paseo/setup-worktree.sh — Paseo worktree 자동 개발환경 구성
# - /home/mykim/.hermes/bin/uv 사용
# - Python 3.11 기반 .venv 생성
# - uv sync --extra dev
# - admin-console npm ci / install
# - .env, 인증정보, Hermes 설정 복사 없음
# - production 서비스/DB 실행 없음

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

UV="/home/mykim/.hermes/bin/uv"

echo "[setup-worktree] ROOT=$ROOT"

if [[ ! -x "$UV" ]]; then
  echo "[setup-worktree] ERROR: uv not found at $UV" >&2
  exit 1
fi

# 1. Python venv
if [[ -d ".venv" ]]; then
  echo "[setup-worktree] .venv already exists, skip creation"
else
  echo "[setup-worktree] creating .venv with Python 3.11"
  "$UV" venv --python 3.11
fi

# 2. Python dependencies (dev 포함)
echo "[setup-worktree] uv sync --extra dev"
"$UV" sync --extra dev

# 3. Node dependencies (admin-console)
if [[ -d "admin-console" ]]; then
  if [[ -f "admin-console/package-lock.json" ]]; then
    echo "[setup-worktree] admin-console/package-lock.json exists -> npm ci"
    npm ci --prefix admin-console
  else
    echo "[setup-worktree] admin-console/package-lock.json not found -> npm install"
    npm install --prefix admin-console
  fi
else
  echo "[setup-worktree] admin-console not found, skip npm"
fi

# 명시적 비수행 항목
echo "[setup-worktree] skip: .env / credentials / Hermes config copy"
echo "[setup-worktree] skip: production service / DB start (docker compose, systemd)"
echo "[setup-worktree] done"
