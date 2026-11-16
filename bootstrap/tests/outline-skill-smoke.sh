#!/usr/bin/env bash
# outline-wiki skill helper against a local mock of the Outline API: every command, JSON
# output, token kept off argv and output, loopback-only plain http, no admin endpoint.
set -Eeuo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
temp_root=$(mktemp -d)
server_pid=''
cleanup() {
  [[ -z $server_pid ]] || kill "$server_pid" 2>/dev/null || true
  rm -rf -- "$temp_root"
}
trap cleanup EXIT
for name in ${!OAOS_@} ${!OUTLINE_@}; do unset "$name"; done
unset HERMES_HOME
helper="$repo_root/skills/outline-wiki/scripts/outline.py"
token='ol_api_agenttoken0123456789abcdefghijklmnopqr'

fail() {
  printf 'FAIL: %s\n' "$1" >&2
  for file in "$temp_root"/*.json "$temp_root"/*.err "$temp_root/requests.log"; do
    [[ -f $file ]] && { printf '\n--- %s ---\n' "$file" >&2; cat "$file" >&2; }
  done
  exit 1
}

# The skill follows the repository skill conventions.
python3 - "$repo_root/skills/outline-wiki" <<'PY' || fail 'skill frontmatter or structure'
import pathlib, re, sys
root = pathlib.Path(sys.argv[1])
text = (root / 'SKILL.md').read_text(encoding='utf-8')
front = text.split('---')[1]
for key in ('name: outline-wiki', 'description: "', 'version: ', 'author: OpenIT (openit-ai), Hermes Agent', 'license: Apache-2.0', 'platforms: [', 'tags: [', 'related_skills: ['):
    assert key in front, key
assert (root / 'README.md').is_file() and (root / 'scripts' / 'outline.py').is_file()
for path in [root / 'SKILL.md', root / 'README.md', root / 'scripts' / 'outline.py']:
    body = path.read_text(encoding='utf-8')
    # Skills-guard hygiene: no curl with an Authorization header, no printing the .env or tokens.
    assert not re.search(r'curl[^\n]*Authorization', body), path
    assert not re.search(r'(cat|printenv|echo)[^\n]*(\.env|OUTLINE_API_TOKEN)', body), path
PY
python3 -c 'import ast, sys; ast.parse(open(sys.argv[1], encoding="utf-8").read())' "$helper" || fail 'helper does not parse'

MOCK_OUTLINE_TOKEN=$token MOCK_OUTLINE_LOG="$temp_root/requests.log" python3 "$repo_root/bootstrap/tests/fixtures/outline_api_mock.py" "$temp_root/port" &
server_pid=$!
for _ in $(seq 1 50); do [[ -s $temp_root/port ]] && break; sleep 0.1; done
port=$(cat "$temp_root/port") || fail 'mock Outline API did not start'

home="$temp_root/home"
mkdir -p "$home/.hermes"
chmod 700 "$home/.hermes"
printf 'OUTLINE_URL=https://note.example.invalid\nOUTLINE_API_TOKEN=%s\n' "$token" > "$home/.hermes/.env"
chmod 600 "$home/.hermes/.env"
run() { env HOME="$home" OUTLINE_URL=https://note.example.invalid OUTLINE_API_TOKEN="$token" OUTLINE_API_BASE="http://127.0.0.1:$port" python3 "$helper" "$@"; }
field() { python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(eval(sys.argv[2], {}, {"d": d}))' "$1" "$2"; }

run whoami > "$temp_root/whoami.json" 2> "$temp_root/whoami.err" || fail 'whoami'
[[ $(field "$temp_root/whoami.json" "d['ok'], d['data']['user']['email'], d['data']['user']['role']") == "(True, 'bot@example.com', 'member')" ]] || fail 'whoami output'
run collections > "$temp_root/collections.json" || fail 'collections'
[[ $(field "$temp_root/collections.json" "d['data'][0]['id']") == col-1 ]] || fail 'collections output'
run search --query onboarding --limit 5 > "$temp_root/search.json" || fail 'search'
[[ $(field "$temp_root/search.json" "[h['id'] for h in d['data']]") == "['doc-1']" ]] || fail 'search output'
run info --id doc-1 > "$temp_root/info.json" || fail 'info'
[[ $(field "$temp_root/info.json" "d['data']['text']") == 'First week: read the handbook.' ]] || fail 'info text'
printf '# Weekly notes\n\n- shipped the relay\n' > "$temp_root/body.md"
run create --title 'Weekly notes' --collection col-1 --text-file "$temp_root/body.md" > "$temp_root/create.json" || fail 'create'
new_id=$(field "$temp_root/create.json" "d['data']['id']")
[[ $new_id == doc-* && $(field "$temp_root/create.json" "d['data']['publishedAt'] is not None") == True ]] || fail 'create output'
printf -- '- appended line\n' | run update --id "$new_id" --text-file - --append > "$temp_root/update.json" || fail 'update'
run info --id "$new_id" > "$temp_root/info2.json" || fail 'info after update'
[[ $(field "$temp_root/info2.json" "d['data']['text'].endswith('- appended line\\n')") == True ]] || fail 'append did not reach the document'
printf 'PASS: whoami, collections, search, info, create and update (append) return JSON from the Outline API\n'

code=0
run info --id missing > "$temp_root/missing.json" || code=$?
[[ $code == 1 && $(field "$temp_root/missing.json" "d['ok'], d['status'], d['error']") == "(False, 404, 'not_found')" ]] || fail 'API error is not exit 1 with JSON'
code=0
env HOME="$home" OUTLINE_URL=https://note.example.invalid OUTLINE_API_BASE="http://127.0.0.1:$port" OUTLINE_API_TOKEN='ol_api_wrongtoken0123456789' python3 "$helper" whoami > "$temp_root/bad.json" || code=$?
[[ $code == 1 && $(field "$temp_root/bad.json" "d['status']") == 401 ]] || fail 'rejected token is not exit 1 / 401'
code=0
env HOME="$home" OUTLINE_URL=https://note.example.invalid OUTLINE_API_TOKEN="$token" OUTLINE_API_BASE='http://note.example.invalid' python3 "$helper" whoami > "$temp_root/plain.json" || code=$?
[[ $code == 2 ]] && grep -Fq 'loopback addresses only' "$temp_root/plain.json" || fail 'plain http to a non-loopback host was not refused'
code=0
env HOME="$home" OUTLINE_URL=https://note.example.invalid python3 "$helper" whoami > "$temp_root/no-token.json" || code=$?
[[ $code == 2 ]] && grep -Fq 'oaos-secret-set OUTLINE_API_TOKEN' "$temp_root/no-token.json" || fail 'missing token is not a configuration error with the helper hint'
code=0
run update --id doc-1 > "$temp_root/noop.json" || code=$?
[[ $code == 2 ]] || fail 'update without changes was not refused'
printf 'PASS: API errors exit 1, configuration errors exit 2, plain http only to loopback\n'

grep -RFq "$token" "$temp_root"/*.json "$temp_root"/*.err && fail 'token appeared in helper output'
grep -Fq ' bad' <(grep -v '^auth.info bad$' "$temp_root/requests.log") && fail 'a request was sent without the right token'
awk '{print $1}' "$temp_root/requests.log" | sort -u > "$temp_root/endpoints"
grep -Evxq 'auth\.info|collections\.list|documents\.(search|info|create|update)' "$temp_root/endpoints" && fail 'helper reached a non-member endpoint'
printf 'PASS: token never in output; only member endpoints reached\n'
