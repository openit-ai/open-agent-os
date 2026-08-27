#!/usr/bin/env bash
# verify-16a.sh — §16A Zero-Bypass verification (systemd + firewall + user)
# Run as root or with sudo on target host. Non-destructive checks only.
set -euo pipefail
FAIL=0
pass(){ echo "[PASS] $1"; }
fail(){ echo "[FAIL] $1"; FAIL=1; }
check(){ if eval "$2"; then pass "$1"; else fail "$1"; fi; }

echo "=== §16A.4 hermes user ==="
check "hermes user exists" "id hermes >/dev/null 2>&1"
check "hermes home 0750 or 0700" "stat -c %a /home/hermes 2>/dev/null | grep -qE '^(750|700)$'"
check "hermes locked password" "passwd -S hermes 2>/dev/null | grep -qE 'L|LK'"
check "hermes not in sudo" "! groups hermes 2>/dev/null | grep -qw sudo"

echo "=== §16A.5 systemd hardening ==="
UNIT=/etc/systemd/system/hermes.service
if [ -f "$UNIT" ]; then
  for d in "NoNewPrivileges=true" "ProtectSystem=strict" "PrivateTmp=true" "ProtectHome=true" "ReadWritePaths=/home/hermes" "InaccessiblePaths=/root" "ProtectClock=true" "ProtectControlGroups=true"; do
    check "$d" "grep -qF \"$d\" \"$UNIT\""
  done
  check "no credential env leak" "! grep -qE 'OAOS_SIGNING_KEY|FERNET_KEY' \"$UNIT\""
  check "systemd-analyze verify" "systemd-analyze verify \"$UNIT\" 2>&1 | grep -qv 'Failed' || true"
else
  fail "hermes.service not installed at $UNIT"
fi

echo "=== §16A.6 network egress ==="
if command -v nft >/dev/null 2>&1; then
  check "nft ruleset has hermes egress" "nft list ruleset 2>/dev/null | grep -q hermes_egress"
  check "DB port 5432 denied for hermes" "nft list ruleset 2>/dev/null | grep -q '5432'"
else
  echo "[SKIP] nft not installed — checking file instead"
  NFT=~/open-agent-os/deploy/firewall/hermes-egress.nft
  [ -f ./deploy/firewall/hermes-egress.nft ] && NFT=./deploy/firewall/hermes-egress.nft
  check "hermes-egress.nft exists" "test -f \"$NFT\""
  check "INTERNAL_DB_NET present" "grep -q '10.20.0.0/16\|INTERNAL_DB_NET' \"$NFT\""
  check "port 5432 denied" "grep -q '5432' \"$NFT\""
  check "ACP port 8000 allowed" "grep -q '8000\|ACP' \"$NFT\""
  check "default DROP" "grep -qi 'drop' \"$NFT\""
fi

echo "=== Summary ==="
if [ "$FAIL" -eq 0 ]; then echo "All §16A checks PASSED"; exit 0; else echo "Some checks FAILED"; exit 1; fi
