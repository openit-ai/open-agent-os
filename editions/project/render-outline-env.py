#!/usr/bin/env python3
"""Render /etc/oaos/outline.env for the OAOS Project edition.

Usage: render-outline-env.py STACK_ENV HERMES_ENV TARGET
Prints "changed" or "unchanged". Run as root by editions/project/native-stack.sh.

- OAOS-managed keys come from ~/oaos/stack/.env (database, secrets, URL) and the
  Hermes .env (G9 SMTP values); SMTP_* keys are dropped when G9 is incomplete.
- Identity-provider keys stored in the Hermes .env as OUTLINE_<KEY> (OIDC_*, SLACK_CLIENT_*,
  GOOGLE_CLIENT_*, AZURE_*, DISCORD_*) are written as <KEY>.
- Any other key already present in TARGET (an operator addition) is preserved.
"""
import os, pathlib, re, tempfile, sys
stack_path, hermes_path, target = map(pathlib.Path, sys.argv[1:])
def parse(path):
    values = {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        match = re.match(r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$', line)
        if not match:
            continue
        value = match.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1].replace('\\"', '"').replace('\\\\', '\\')
        elif len(value) >= 2 and value[0] == value[-1] == "'":
            value = value[1:-1]
        values[match.group(1)] = value
    return values
values = parse(stack_path)
hermes = parse(hermes_path)
data = {
    'NODE_ENV': 'production', 'URL': 'https://' + values['NOTE_DOMAIN'],
    'PORT': '3000', 'WEB_CONCURRENCY': '1',
    'DATABASE_URL': 'postgres://outline:' + values['OUTLINE_DB_PASSWORD'] + '@127.0.0.1:5432/outline',
    'PGSSLMODE': 'disable', 'REDIS_URL': 'redis://127.0.0.1:6379',
    'SECRET_KEY': values['OUTLINE_SECRET_KEY'], 'UTILS_SECRET': values['OUTLINE_UTILS_SECRET'],
    'FILE_STORAGE': 'local', 'FILE_STORAGE_LOCAL_ROOT_DIR': '/var/lib/outline/data',
    'FORCE_HTTPS': 'false', 'PROXY_HEADERS_TRUSTED': 'true',
}
smtp_keys = ('SMTP_HOST', 'SMTP_PORT', 'SMTP_USERNAME', 'SMTP_PASSWORD', 'SMTP_FROM_EMAIL', 'SMTP_SECURE')
managed = set(data) | set(smtp_keys)
mail = {key: hermes.get(key) or values.get(key) for key in ('OAOS_MAIL_ADDRESS', 'OAOS_MAIL_PASSWORD', 'OAOS_MAIL_SMTP_HOST')}
if all(mail.values()):
    data.update(SMTP_HOST=mail['OAOS_MAIL_SMTP_HOST'], SMTP_PORT='587',
                SMTP_USERNAME=mail['OAOS_MAIL_ADDRESS'], SMTP_PASSWORD=mail['OAOS_MAIL_PASSWORD'],
                SMTP_FROM_EMAIL=mail['OAOS_MAIL_ADDRESS'], SMTP_SECURE='false')
idp = re.compile(r'^(OIDC_[A-Z_]+|SLACK_CLIENT_(ID|SECRET)|GOOGLE_CLIENT_(ID|SECRET)|AZURE_(CLIENT_ID|CLIENT_SECRET|RESOURCE_APP_ID|TENANT_ID)|DISCORD_(CLIENT_ID|CLIENT_SECRET|SERVER_ID|SERVER_ROLES))$')
for key, value in sorted(hermes.items()):
    if key.startswith('OUTLINE_') and idp.match(key[len('OUTLINE_'):]) and value:
        data[key[len('OUTLINE_'):]] = value
for key, value in parse(target).items():
    if key not in managed and key not in data:
        data[key] = value
for value in data.values():
    if '\n' in value or '\r' in value:
        sys.exit('multi-line value refused')
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
