#!/usr/bin/env bash
# Mailbox checks shared by the Personal G5 stage and the Project G9 stage.
# Load common.sh, platform.sh and secret.sh first. Keep syntax compatible with macOS Bash 3.2.
#
#   oaos_mail_valid_address ADDRESS      0 for a plain address (letters, digits, . _ + -).
#   oaos_mail_valid_host HOST            0 for a host name of letters, digits, dot and hyphen.
#   oaos_mail_default_host imap|smtp ADDRESS
#       Prints imap.gmail.com / smtp.gmail.com for Gmail addresses; exit 1 for other domains.
#   oaos_mail_valid_app_password VALUE   0 for one word of [A-Za-z0-9_-] (an app password without spaces).
#   oaos_mail_login imap|smtp ADDRESS HOST PASSWORD_KEY
#       Logs in over IMAP (993, TLS) or SMTP (587, STARTTLS) with the password stored under
#       PASSWORD_KEY in the Hermes .env. The password reaches Python on stdin, never argv,
#       the environment or output. Prints a short reason; exit 0 when the login succeeds.
#       OAOS_MAIL_TIMEOUT (seconds, default 15) bounds each attempt.
#       Smoke tests only (OAOS_TEST_MODE=1): OAOS_TEST_MAIL_IMAP_PORT / OAOS_TEST_MAIL_SMTP_PORT
#       switch to a plaintext local server on that port.

oaos_mail_valid_address() { [[ $1 =~ ^[A-Za-z0-9._+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]]; }
oaos_mail_valid_host() { [[ $1 =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]]; }
oaos_mail_valid_app_password() { [[ $1 =~ ^[A-Za-z0-9_-]+$ ]]; }

oaos_mail_default_host() {
  local domain=${2##*@}
  domain=$(printf '%s' "$domain" | tr '[:upper:]' '[:lower:]')
  case $domain in
    gmail.com|googlemail.com) printf '%s.gmail.com' "$1" ;;
    *) return 1 ;;
  esac
}

oaos_mail_login() {
  local kind=$1 address=$2 host=$3 key=$4 password port='' plain=0
  case $kind in imap|smtp) ;; *) printf 'unknown mail protocol'; return 1 ;; esac
  password=$(oaos_env_get "$key") || { printf '%s absent' "$key"; return 1; }
  if [[ ${OAOS_TEST_MODE:-0} == 1 ]]; then
    if [[ $kind == imap ]]; then port=${OAOS_TEST_MAIL_IMAP_PORT:-}; else port=${OAOS_TEST_MAIL_SMTP_PORT:-}; fi
    [[ -z $port ]] || plain=1
  fi
  [[ -n $port ]] || { if [[ $kind == imap ]]; then port=993; else port=587; fi; }
  printf '%s' "$password" | OAOS_MAIL_KIND=$kind OAOS_MAIL_LOGIN=$address OAOS_MAIL_SERVER=$host \
    OAOS_MAIL_PORT=$port OAOS_MAIL_PLAIN=$plain OAOS_MAIL_WAIT=${OAOS_MAIL_TIMEOUT:-15} oaos_python -c '
import imaplib, os, smtplib, socket, ssl, sys
password = sys.stdin.read()
kind, address, host = os.environ["OAOS_MAIL_KIND"], os.environ["OAOS_MAIL_LOGIN"], os.environ["OAOS_MAIL_SERVER"]
port, plain = int(os.environ["OAOS_MAIL_PORT"]), os.environ["OAOS_MAIL_PLAIN"] == "1"
timeout = float(os.environ["OAOS_MAIL_WAIT"])
try:
    if kind == "imap":
        client = imaplib.IMAP4(host, port, timeout=timeout) if plain else imaplib.IMAP4_SSL(host, port, timeout=timeout)
        try:
            client.login(address, password)
        finally:
            try:
                client.logout()
            except (imaplib.IMAP4.error, OSError):
                pass
    else:
        with smtplib.SMTP(host, port, timeout=timeout) as client:
            if not plain:
                client.starttls(context=ssl.create_default_context())
            client.login(address, password)
except (imaplib.IMAP4.error, smtplib.SMTPAuthenticationError):
    print("login rejected", end=""); sys.exit(1)
except (OSError, socket.timeout, smtplib.SMTPException, ssl.SSLError, ValueError):
    print("connection failed", end=""); sys.exit(1)
print("login succeeded", end="")
'
}

# Himalaya (https://pimalaya.org/himalaya/) is the mail CLI the agent uses. OAOS only
# configures a verified 1.x release; anything else stays a manual step.
oaos_himalaya_version_ok() {
  have_cmd himalaya || return 1
  [[ $(himalaya --version 2>/dev/null) =~ [[:space:]]v?1\. ]]
}

# oaos_himalaya_account_check CONFIG ACCOUNT: lists envelopes with a 30-second bound.
# Output is discarded (it would show mail subjects). Exit 0 when Himalaya read the mailbox.
oaos_himalaya_account_check() {
  local bin config
  bin=$(command -v himalaya) || return 1
  config=$(oaos_native_path "$1") || return 1
  OAOS_HIMALAYA_BIN=$bin oaos_python - "$config" "$2" <<'PY'
import os, subprocess, sys
try:
    result = subprocess.run([os.environ["OAOS_HIMALAYA_BIN"], "--config", sys.argv[1], "envelope", "list", "--account", sys.argv[2]],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=False)
except (OSError, subprocess.TimeoutExpired):
    raise SystemExit(1)
raise SystemExit(0 if result.returncode == 0 else 1)
PY
}
