#!/usr/bin/env bash
set -Eeuo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
temp_root=$(mktemp -d)
trap 'rm -rf -- "$temp_root"' EXIT
# shellcheck source=bootstrap/lib/common.sh
. "$repo_root/bootstrap/lib/common.sh"
# shellcheck source=bootstrap/lib/platform.sh
. "$repo_root/bootstrap/lib/platform.sh"

fail() { printf 'FAIL: %s\n' "$1" >&2; exit 1; }

# uname is mocked only inside this subshell; no host service or package is touched.
(
  uname() { case $1 in -s) printf '%s\n' "$fake_kernel" ;; -m) printf '%s\n' "$fake_arch" ;; esac; }
  fake_kernel=Linux fake_arch=x86_64
  [[ $(os_detect) == linux ]] || fail 'Linux detection'
  fake_kernel=Darwin fake_arch=arm64
  [[ $(os_detect) == macos ]] || fail 'Apple Silicon detection'
  fake_arch=x86_64
  if os_detect >/dev/null 2>&1; then fail 'Intel Mac accepted'; fi
  fake_kernel=MINGW64_NT fake_arch=x86_64 MSYSTEM=MINGW64
  export MSYSTEM
  [[ $(os_detect) == windows-gitbash ]] || fail 'Git Bash detection'
  unset MSYSTEM
  if os_detect >/dev/null 2>&1; then fail 'Windows without Git Bash accepted'; fi
  fake_kernel=FreeBSD fake_arch=x86_64
  if os_detect >/dev/null 2>&1; then fail 'unsupported kernel accepted'; fi
)
printf 'PASS: actual kernel/architecture/Git Bash lane rules with mocked uname\n'

HOME="$temp_root/home"; export HOME
mkdir -p "$HOME"
OAOS_TEST_PLATFORM=macos
export OAOS_TEST_PLATFORM
if os_detect >/dev/null 2>&1; then fail 'override accepted without test mode'; fi
OAOS_TEST_MODE=1; export OAOS_TEST_MODE
for OAOS_TEST_PLATFORM in macos windows-gitbash; do
  export OAOS_TEST_PLATFORM
  os_detect >/dev/null || fail 'test override rejected'
  code=0
  platform_memory_kib >"$temp_root/stub.out" 2>"$temp_root/stub.err" || code=$?
  if [[ $code != 3 ]] || ! grep -Fq BLOCKED "$temp_root/stub.err"; then fail 'stub did not BLOCK'; fi
  code=0
  HOME="$HOME" OAOS_TEST_MODE=1 OAOS_TEST_PLATFORM="$OAOS_TEST_PLATFORM" \
    bash "$repo_root/editions/personal/install.sh" --dry-run --stage prep >"$temp_root/entry.out" 2>"$temp_root/entry.err" || code=$?
  if [[ $code != 3 ]] || ! grep -Fq BLOCKED "$temp_root/entry.err"; then fail 'entry did not BLOCK before prep'; fi
done
[[ ! -e $HOME/.oaos && ! -e $HOME/.oaos-install ]] || fail 'stub entry changed HOME'
printf 'PASS: test-only overrides and P2/P3 BLOCKED without side effects\n'

OAOS_TEST_PLATFORM=linux; export OAOS_TEST_PLATFORM
os_detect >/dev/null
OAOS_DRY_RUN=1 OAOS_NO_LOG_FILE=1
export OAOS_DRY_RUN OAOS_NO_LOG_FILE
printf 'original\n' > "$HOME/private"
chmod 644 "$HOME/private"
printf 'replacement\n' | platform_atomic_write "$HOME/private" >/dev/null
platform_secret_protect "$HOME/private" >/dev/null
platform_timezone_set UTC >/dev/null
platform_sleep_policy >/dev/null
platform_gateway_install >/dev/null
platform_gateway_watchdog >/dev/null
platform_install_hermes "$HOME/missing-installer" >/dev/null
[[ $(cat "$HOME/private") == original && $(platform_file_mode "$HOME/private") == 644 ]] || fail 'dry-run changed file'
[[ ! -e $HOME/private.tmp && ! -e $HOME/.oaos ]] || fail 'dry-run created files'
printf 'PASS: platform mutators honor dry-run without file or service changes\n'

OAOS_DRY_RUN=0; export OAOS_DRY_RUN
printf 'replacement\n' | platform_atomic_write "$HOME/private"
if [[ $(cat "$HOME/private") != replacement ]] || ! platform_secret_check "$HOME/private"; then fail 'atomic write or secret mode'; fi
[[ $(platform_sha256 "$HOME/private") == $(sha256sum "$HOME/private" | awk '{print $1}') ]] || fail 'SHA-256 value'
[[ $(platform_hermes_home) == "$HOME/.hermes" && $(platform_path state) == "$HOME/.oaos-install/state.json" ]] || fail 'Linux paths'
stage_mark wiki applied 'test'
if [[ $(stage_status wiki) != applied ]] || stage_done wiki; then fail 'applied status'; fi
stage_mark wiki verified 'test'
if [[ $(stage_status wiki) != verified ]] || stage_done wiki; then fail 'verified status'; fi
stage_mark optional SKIP 'explicit optional item'
if [[ $(stage_status optional) != SKIP ]] || stage_done optional; then fail 'SKIP status'; fi
if (stage_mark wiki SKIP 'forbidden') >/dev/null 2>&1; then fail 'required stage accepted SKIP'; fi
stage_mark wiki 'done' 'legacy'
stage_done wiki || fail 'legacy done resume'
printf 'PASS: Linux hash, atomic mode, paths, and legacy/new status read-back\n'

backup_dir="$HOME/backups with spaces"
mkdir -p "$backup_dir"
python3 - "$backup_dir" <<'PY'
import os, pathlib, sys
root = pathlib.Path(sys.argv[1])
for i, name in enumerate(('hermes-verify-a.zip', 'hermes-verify-\nline.zip', 'hermes-verify-c.zip')):
    path = root / name
    path.write_bytes(b'x')
    os.utime(path, (100 + i, 100 + i))
PY
platform_backup_prune "$backup_dir"
[[ ! -e $backup_dir/hermes-verify-a.zip && -e $backup_dir/hermes-verify-c.zip && -e $backup_dir/$'hermes-verify-\nline.zip' ]] || fail 'portable backup pruning'
printf 'PASS: backup pruning handles spaces and newlines\n'
