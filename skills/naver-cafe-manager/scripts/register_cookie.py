#!/usr/bin/env python3
"""Naver cookie onboarding: paste NID_AUT / NID_SES -> a NAVER_COOKIE line.

Run: python3 register_cookie.py [--env PATH]
Input via interactive stdin (never lands in shell history). Values are not
echoed back while typing.

With --env PATH the given environment file is updated in place (mode 600).
Without it, the ready-to-paste line is printed for manual storage.
Never put cookie values on a command line or into a repository — keep them in
your runtime environment or a secret store only.
"""
import argparse
import os
import re
import stat


def valid_aut(v):
    return bool(re.fullmatch(r"[A-Za-z0-9+/=_-]{20,200}", v))


def valid_ses(v):
    return bool(re.fullmatch(r"[A-Za-z0-9+/=_-]{200,4000}", v))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", default="",
                    help="Environment file to update in place (mode kept at "
                         "600). Omit to print the line instead.")
    a = ap.parse_args()
    print("Paste the NID_AUT value, then press Enter:")
    aut = input().strip()
    print("Paste the NID_SES value, then press Enter:")
    ses = input().strip()
    if not valid_aut(aut):
        raise SystemExit("NID_AUT looks malformed — check for a partial copy.")
    if not valid_ses(ses):
        raise SystemExit("NID_SES looks malformed — copy the whole value.")
    line = "NAVER_COOKIE=NID_AUT=" + aut + "; NID_SES=" + ses
    if a.env:
        lines = []
        if os.path.exists(a.env):
            lines = open(a.env).read().splitlines()
        lines = [l for l in lines if not l.startswith("NAVER_COOKIE=")]
        lines.append(line)
        open(a.env, "w").write("\n".join(lines) + "\n")
        os.chmod(a.env, stat.S_IRUSR | stat.S_IWUSR)
        print(f"Saved to {a.env} (mode 600). Verify with one live board fetch:")
        print("python3 scripts/watch.py --cafe <cafe-domain>"
              " --club-id <club-id> --board <board-id> --no-state")
    else:
        print("Store this single line in your runtime environment "
              "(e.g. your agent's environment file or secret store):")
        print(line)
        print("Or re-run with --env <path> to update an environment file "
              "in place.")


if __name__ == "__main__":
    main()
