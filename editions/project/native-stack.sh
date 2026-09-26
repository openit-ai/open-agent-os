#!/usr/bin/env bash
# Sourced by install.sh after stack_dir, stack_get and project_domain are defined.
repo_root=${repo_root:?native stack requires the Project installer}

native_package() {
  local package missing=()
  for package in "$@"; do
    dpkg-query -W -f='${Status}' "$package" 2>/dev/null | grep -Fq 'install ok installed' || missing+=("$package")
  done
  ((${#missing[@]} == 0)) && return 0
  if ! run_root apt-get update >/dev/null ||
     ! run_root env DEBIAN_FRONTEND=noninteractive apt-get install -y "${missing[@]}" >/dev/null; then
    warn 'Native package installation failed; inspect apt logs.'; return 3;
  fi
}

native_enable() {
  local service=$1
  if ! systemctl is-enabled --quiet "$service" 2>/dev/null; then
    run_root systemctl enable "$service" >/dev/null || return 3
  fi
  if ! systemctl is-active --quiet "$service" 2>/dev/null; then
    run_root systemctl start "$service" || return 3
  fi
}

native_seed_env() {
  local dir=$1 chat=$2 note=$3 portal=$4 key value
  mkdir -p "$dir" && chmod 700 "$dir" || return 3
  for key in CHAT_DOMAIN NOTE_DOMAIN PORTAL_DOMAIN; do
    case $key in CHAT_DOMAIN) value=$chat;; NOTE_DOMAIN) value=$note;; PORTAL_DOMAIN) value=$portal;; esac
    if [[ -n $(stack_get "$key") && $(stack_get "$key") != "$value" ]]; then
      warn "Existing $key differs; preserving stack configuration."; return 3
    fi
    [[ $(stack_get "$key") == "$value" ]] || stack_put "$key" "$value" || return 3
  done
  for key in MM_DB_PASSWORD OUTLINE_DB_PASSWORD OUTLINE_SECRET_KEY OUTLINE_UTILS_SECRET; do
    if [[ -z $(stack_get "$key") ]]; then
      value=$(openssl rand -hex 32) || return 3
      stack_put "$key" "$value" || return 3
    fi
  done
  chmod 600 "$dir/.env" || return 3
}

native_postgres() {
  local mm_password outline_password pg_major
  native_package postgresql postgresql-client || return 3
  pg_major=$(psql --version | awk '{split($3, parts, "."); print parts[1]}')
  if [[ ! $pg_major =~ ^[0-9]+$ ]] || ((pg_major < 14)); then
    warn 'PostgreSQL 14 or later is required.'; return 3
  fi
  native_enable postgresql || return 3
  pg_isready -h 127.0.0.1 -p 5432 >/dev/null 2>&1 || { warn 'PostgreSQL does not accept local connections.'; return 3; }
  mm_password=$(stack_get MM_DB_PASSWORD)
  outline_password=$(stack_get OUTLINE_DB_PASSWORD)
  [[ $mm_password =~ ^[a-f0-9]{64}$ && $outline_password =~ ^[a-f0-9]{64}$ ]] || {
    warn 'Database secrets have an unexpected format; preserving them for review.'; return 3;
  }
  run_root runuser -u postgres -- psql -X -q -v ON_ERROR_STOP=1 postgres >/dev/null 2>&1 <<SQL || {
SELECT 'CREATE ROLE mattermost LOGIN' WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='mattermost') \gexec
SELECT 'CREATE ROLE outline LOGIN' WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='outline') \gexec
ALTER ROLE mattermost WITH PASSWORD '$mm_password';
ALTER ROLE outline WITH PASSWORD '$outline_password';
SELECT 'CREATE DATABASE mattermost OWNER mattermost' WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname='mattermost') \gexec
SELECT 'CREATE DATABASE outline OWNER outline' WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname='outline') \gexec
SQL
    warn 'PostgreSQL role or database setup failed; inspect PostgreSQL logs without exposing credentials.'; return 3;
  }
}

native_redis() {
  local changed
  native_package redis-server redis-tools || return 3
  changed=$(run_root python3 - <<'PY'
import os, pathlib, re, tempfile
path = pathlib.Path('/etc/redis/redis.conf')
old = path.read_text()
lines = [line for line in old.splitlines() if not re.match(r'^\s*(?:bind|protected-mode)\s+', line)]
new = 'bind 127.0.0.1\nprotected-mode yes\n' + '\n'.join(lines) + '\n'
if old == new:
    print('unchanged')
else:
    mode = path.stat()
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, prefix='.redis-oaos-', delete=False) as tmp:
        tmp.write(new)
        os.fchmod(tmp.fileno(), mode.st_mode & 0o777)
        os.fchown(tmp.fileno(), mode.st_uid, mode.st_gid)
        name = tmp.name
    os.replace(name, path)
    print('changed')
PY
  ) || return 3
  native_enable redis-server || return 3
  if [[ $changed == changed ]]; then run_root systemctl restart redis-server || return 3; fi
  [[ $(redis-cli -h 127.0.0.1 ping 2>/dev/null) == PONG ]] || { warn 'Redis loopback ping failed.'; return 3; }
}

native_repo_key() {
  local url=$1 target=$2 tmp key
  [[ -s $target ]] && return 0
  tmp=$(mktemp -d) || return 3
  if ! curl -fsSL --proto '=https' "$url" -o "$tmp/key.asc" ||
     ! gpg --batch --yes --dearmor -o "$tmp/key.gpg" "$tmp/key.asc" ||
     ! run_root install -m 644 "$tmp/key.gpg" "$target"; then
    rm -rf -- "$tmp"
    warn 'Signed repository key installation failed.'; return 3
  fi
  rm -rf -- "$tmp"
}

native_mattermost_dropin() {
  printf '[Unit]\nAfter=postgresql.service\nRequires=postgresql.service\n'
}

native_mattermost() {
  local codename arch source expected changed was_active=0
  local dropin=/etc/systemd/system/mattermost.service.d/oaos.conf
  # shellcheck disable=SC1091
  . /etc/os-release
  [[ ${ID:-} == ubuntu && ${VERSION_ID:-} =~ ^(22\.04|24\.04)$ ]] || {
    warn 'Mattermost signed APT packages require a supported Ubuntu LTS release.'; return 3;
  }
  codename=${VERSION_CODENAME:-}
  arch=$(dpkg --print-architecture)
  [[ $codename =~ ^[a-z]+$ ]] && [[ $arch == amd64 || $arch == arm64 ]] || return 3
  native_package gnupg || return 3
  native_repo_key https://deb.packages.mattermost.com/pubkey.gpg /usr/share/keyrings/mattermost-archive-keyring.gpg || return 3
  source=/etc/apt/sources.list.d/mattermost.list
  expected="deb [arch=$arch signed-by=/usr/share/keyrings/mattermost-archive-keyring.gpg] https://deb.packages.mattermost.com $codename main"
  if [[ -e $source ]]; then
    [[ $(cat "$source") == "$expected" ]] || { warn 'Existing Mattermost APT source differs; review it manually.'; return 3; }
  else
    printf '%s\n' "$expected" | run_root tee "$source" >/dev/null || return 3
  fi
  native_package mattermost || return 3
  [[ -x /opt/mattermost/bin/mmctl && -x /opt/mattermost/bin/mattermost ]] || {
    warn 'Mattermost package lacks the expected server or mmctl binary.'; return 3;
  }
  run_root install -d -o mattermost -g mattermost -m 750 /opt/mattermost/data || return 3
  changed=$(run_root python3 - "$(stack_dir)/.env" /opt/mattermost/config/config.json <<'PY'
import json, os, pathlib, tempfile, sys
env_path, path = map(pathlib.Path, sys.argv[1:])
values = dict(line.split('=', 1) for line in env_path.read_text().splitlines() if '=' in line)
if path.exists():
    current = json.loads(path.read_text())
else:
    current = json.loads(path.with_name('config.defaults.json').read_text())
updated = json.loads(json.dumps(current))
updated['SqlSettings']['DriverName'] = 'postgres'
updated['SqlSettings']['DataSource'] = 'postgres://mattermost:' + values['MM_DB_PASSWORD'] + '@127.0.0.1:5432/mattermost?sslmode=disable&connect_timeout=10'
updated['ServiceSettings']['SiteURL'] = 'https://' + values['CHAT_DOMAIN']
updated['ServiceSettings']['ListenAddress'] = '127.0.0.1:8065'
updated['ServiceSettings']['EnableLocalMode'] = True
updated['FileSettings']['DriverName'] = 'local'
updated['FileSettings']['Directory'] = '/opt/mattermost/data'
if updated == current:
    print('unchanged')
else:
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, prefix='.config-oaos-', delete=False) as tmp:
        os.fchmod(tmp.fileno(), 0o600)
        os.fchown(tmp.fileno(), __import__('pwd').getpwnam('mattermost').pw_uid, __import__('grp').getgrnam('mattermost').gr_gid)
        json.dump(updated, tmp, indent=2)
        tmp.write('\n')
        name = tmp.name
    os.replace(name, path)
    print('changed')
PY
  ) || { warn 'Mattermost configuration failed.'; return 3; }
  run_root chown mattermost:mattermost /opt/mattermost/config/config.json &&
    run_root chmod 600 /opt/mattermost/config/config.json || return 3
  if [[ -e $dropin ]]; then
    if ! native_mattermost_dropin | run_root cmp -s - "$dropin"; then
      warn 'Existing Mattermost service drop-in differs; preserving it for review.'; return 3
    fi
  else
    run_root install -d -m 755 /etc/systemd/system/mattermost.service.d || return 3
    native_mattermost_dropin | run_root tee "$dropin" >/dev/null || return 3
    run_root systemctl daemon-reload || return 3
  fi
  systemctl is-active --quiet mattermost && was_active=1
  native_enable mattermost || return 3
  if [[ $changed == changed && $was_active == 1 ]]; then run_root systemctl restart mattermost || return 3; fi
  run_root runuser -u mattermost -- /opt/mattermost/bin/mmctl --help >/dev/null 2>&1 || {
    warn 'Mattermost mmctl binary cannot run as the service user.'; return 3;
  }
}

native_node() {
  local arch source expected
  if have_cmd node && [[ $(node --version) =~ ^v22\. ]] && have_cmd corepack; then
    run_root corepack enable >/dev/null || return 3
    return 0
  fi
  native_package gnupg || return 3
  arch=$(dpkg --print-architecture)
  [[ $arch == amd64 || $arch == arm64 ]] || { warn 'Node.js 22 package architecture unsupported.'; return 3; }
  native_repo_key https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key /usr/share/keyrings/nodesource.gpg || return 3
  source=/etc/apt/sources.list.d/oaos-nodesource.list
  expected="deb [arch=$arch signed-by=/usr/share/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main"
  if [[ -e $source ]]; then
    [[ $(cat "$source") == "$expected" ]] || { warn 'Existing NodeSource APT source differs; review it manually.'; return 3; }
  else
    printf '%s\n' "$expected" | run_root tee "$source" >/dev/null || return 3
  fi
  run_root apt-get update >/dev/null && run_root env DEBIAN_FRONTEND=noninteractive apt-get install -y nodejs >/dev/null || return 3
  if [[ ! $(node --version) =~ ^v22\. ]] || ! have_cmd corepack; then
    warn 'Node.js 22 or Corepack unavailable after install.'; return 3
  fi
  run_root corepack enable >/dev/null || return 3
}

native_outline_config() {
  local changed
  run_root install -d -m 700 /etc/oaos || return 3
  changed=$(run_root python3 - "$(stack_dir)/.env" /etc/oaos/outline.env <<'PY'
import os, pathlib, tempfile, sys
source, target = map(pathlib.Path, sys.argv[1:])
values = dict(line.split('=', 1) for line in source.read_text().splitlines() if '=' in line)
data = {
    'NODE_ENV': 'production', 'URL': 'https://' + values['NOTE_DOMAIN'],
    'PORT': '3000', 'WEB_CONCURRENCY': '1',
    'DATABASE_URL': 'postgres://outline:' + values['OUTLINE_DB_PASSWORD'] + '@127.0.0.1:5432/outline',
    'PGSSLMODE': 'disable', 'REDIS_URL': 'redis://127.0.0.1:6379',
    'SECRET_KEY': values['OUTLINE_SECRET_KEY'], 'UTILS_SECRET': values['OUTLINE_UTILS_SECRET'],
    'FILE_STORAGE': 'local', 'FILE_STORAGE_LOCAL_ROOT_DIR': '/var/lib/outline/data',
    'FORCE_HTTPS': 'false', 'PROXY_HEADERS_TRUSTED': 'true',
}
if all(values.get(key) for key in ('OAOS_MAIL_ADDRESS', 'OAOS_MAIL_PASSWORD', 'OAOS_MAIL_SMTP_HOST')):
    data.update(SMTP_HOST=values['OAOS_MAIL_SMTP_HOST'], SMTP_PORT='587',
                SMTP_USERNAME=values['OAOS_MAIL_ADDRESS'], SMTP_PASSWORD=values['OAOS_MAIL_PASSWORD'],
                SMTP_FROM_EMAIL=values['OAOS_MAIL_ADDRESS'], SMTP_SECURE='false')
content = ''.join(f'{key}={value}\n' for key, value in data.items())
if target.exists() and target.read_text() == content:
    print('unchanged')
else:
    with tempfile.NamedTemporaryFile(mode='w', dir=target.parent, prefix='.outline-oaos-', delete=False) as tmp:
        os.fchmod(tmp.fileno(), 0o600)
        tmp.write(content)
        name = tmp.name
    os.replace(name, target)
    print('changed')
PY
  ) || return 3
  run_root chmod 600 /etc/oaos/outline.env || return 3
  native_outline_changed=$changed
}

native_outline() {
  local path=/opt/outline unit=/etc/systemd/system/oaos-outline.service was_active=0
  native_node || return 3
  native_package cmake build-essential git || return 3
  if ! id outline >/dev/null 2>&1; then
    run_root useradd --system --user-group --home-dir "$path" --shell /usr/sbin/nologin outline || return 3
  fi
  if [[ -e $path && ! -d $path/.git ]]; then warn 'Existing Outline path is not the pinned source checkout; preserving it.'; return 3; fi
  if [[ ! -d $path/.git ]]; then
    run_root git clone --quiet --depth 1 --branch v1.10.1 https://github.com/outline/outline.git "$path" || return 3
  fi
  run_root chown -R outline:outline "$path" || return 3
  [[ $(run_root runuser -u outline -- git -C "$path" rev-parse --short=7 HEAD 2>/dev/null) == 4a5a616 ]] || {
    warn 'Outline checkout does not match the pinned v1.10.1 release commit.'; return 3;
  }
  run_root runuser -u outline -- python3 "$repo_root/editions/project/patch-outline-bind.py" "$path" >/dev/null || return 3
  if [[ ! -f $path/build/server/index.js || ! -f $path/build/server/main.js ]] ||
     ! grep -Eq 'listen\([^)]{0,100}127\.0\.0\.1' "$path/build/server/main.js"; then
    run_root runuser -u outline -- env NODE_OPTIONS=--max-old-space-size=8192 sh -c 'cd /opt/outline && corepack yarn install --immutable && corepack yarn build && corepack yarn workspaces focus --production' >/dev/null || {
      warn 'Outline source build failed; inspect the build log without exposing environment secrets.'; return 3;
    }
  fi
  [[ -f $path/build/server/index.js && -f $path/build/server/main.js ]] || {
    warn 'Outline build entry point or server module is absent.'; return 3;
  }
  grep -Eq 'listen\([^)]{0,100}127\.0\.0\.1' "$path/build/server/main.js" || {
    warn 'Outline build artifact does not show the approved loopback bind.'; return 3;
  }
  run_root install -d -o outline -g outline -m 750 /var/lib/outline/data || return 3
  native_outline_config || return 3
  if [[ -e $unit ]] && ! cmp -s "$repo_root/editions/project/systemd/oaos-outline.service" "$unit"; then
    warn 'Existing Outline service unit differs; preserving it for review.'; return 3
  fi
  if [[ ! -e $unit ]]; then
    run_root install -m 644 "$repo_root/editions/project/systemd/oaos-outline.service" "$unit" || return 3
    run_root systemctl daemon-reload || return 3
  fi
  systemctl is-active --quiet oaos-outline && was_active=1
  native_enable oaos-outline || return 3
  if [[ $native_outline_changed == changed && $was_active == 1 ]]; then run_root systemctl restart oaos-outline || return 3; fi
}

native_health() {
  local deadline service remaining healthy redis_listener mm_listener outline_listener
  deadline=$((SECONDS + 360))
  while ((SECONDS < deadline)); do
    healthy=1
    for service in postgresql redis-server mattermost oaos-outline; do
      systemctl is-active --quiet "$service" || healthy=0
    done
    redis_listener=$(ss -H -ltn '( sport = :6379 )' 2>/dev/null | awk '{print $4}')
    mm_listener=$(ss -H -ltn '( sport = :8065 )' 2>/dev/null | awk '{print $4}')
    outline_listener=$(ss -H -ltn '( sport = :3000 )' 2>/dev/null | awk '{print $4}')
    if ((healthy)) && [[ $redis_listener == '127.0.0.1:6379' && $mm_listener == '127.0.0.1:8065' && $outline_listener == '127.0.0.1:3000' ]] &&
       pg_isready -h 127.0.0.1 -p 5432 >/dev/null 2>&1 &&
       [[ $(redis-cli -h 127.0.0.1 ping 2>/dev/null) == PONG ]] &&
       curl -fsS --max-time 3 http://127.0.0.1:8065/api/v4/system/ping 2>/dev/null | grep -Fq '"status":"OK"' &&
       [[ $(curl -fsS --max-time 3 http://127.0.0.1:3000/_health 2>/dev/null) == OK ]]; then return 0; fi
    remaining=$((deadline - SECONDS))
    ((remaining > 0)) || break
    sleep 2
  done
  for service in postgresql redis-server mattermost oaos-outline; do
    warn "$service: $(systemctl is-active "$service" 2>/dev/null || true)."
  done
  warn 'Native stack health timeout; inspect service journals without printing secrets.'
  return 3
}

do_stack() {
  local dir key domain chat note portal mm_version
  if ! have_cmd apt-get || ! have_cmd systemctl || ! have_cmd python3 ||
     ! have_cmd openssl || ! have_cmd ss; then
    warn 'Ubuntu apt, systemd, Python, OpenSSL and iproute2 are required.'; return 3
  fi
  # shellcheck disable=SC1091
  . /etc/os-release
  [[ ${ID:-} == ubuntu && ${VERSION_ID:-} =~ ^(22\.04|24\.04)$ ]] || {
    warn 'Native Project stack requires Ubuntu 22.04 or 24.04 LTS with PostgreSQL 14+.'; return 3;
  }
  for key in OAOS_CHAT_DOMAIN OAOS_NOTE_DOMAIN OAOS_PORTAL_DOMAIN; do
    domain=$(project_domain "$key") || { warn 'G6: set the Project domains before stack.'; return 3; }
    case $key in OAOS_CHAT_DOMAIN) chat=$domain;; OAOS_NOTE_DOMAIN) note=$domain;; OAOS_PORTAL_DOMAIN) portal=$domain;; esac
  done
  dir=$(stack_dir)
  native_seed_env "$dir" "$chat" "$note" "$portal" || return 3
  native_postgres || return 3
  native_redis || return 3
  native_mattermost || return 3
  native_outline || return 3
  native_health || return 3
  mm_version=$(dpkg-query -W -f='${Version}' mattermost 2>/dev/null) || mm_version=unknown
  [[ -n $mm_version ]] || mm_version=unknown
  printf 'PostgreSQL: %s\nRedis: %s\nMattermost: %s\nNode.js: %s\nOutline: v1.10.1 (4a5a616)\n' \
    "$(psql --version)" "$(redis-server --version)" "$mm_version" "$(node --version)" > "$dir/versions.txt" || return 3
  chmod 600 "$dir/versions.txt" || return 3
}
