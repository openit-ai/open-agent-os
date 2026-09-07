#!/usr/bin/env bash
set -euo pipefail

# scripts/paseo/teardown-worktree.sh — Paseo worktree 정리
# - 캐시만 정리: .pytest_cache, .mypy_cache, .ruff_cache
# - production 서비스, DB, Docker, Hermes 건드리지 않음

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

echo "[teardown-worktree] ROOT=$ROOT"

for d in .pytest_cache .mypy_cache .ruff_cache; do
  if [[ -d "$d" ]]; then
    echo "[teardown-worktree] removing $d"
    rm -rf "$d"
  else
    echo "[teardown-worktree] skip $d (not exists)"
  fi
done

# admin-console/.next 등 빌드 산출물은 유지 (필요 시 수동 정리)

echo "[teardown-worktree] skip: production service / DB / Docker / Hermes"
echo "[teardown-worktree] done"
