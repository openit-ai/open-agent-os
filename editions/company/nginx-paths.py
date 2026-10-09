"""Add C02 paths to one existing TLS server without touching other server blocks."""

import re
import sys


def fail(message):
    raise SystemExit(message)


source, origin = sys.argv[1:]
host = origin[len("https://"):] if origin.startswith("https://") else ""
if not re.fullmatch(r"[A-Za-z0-9.-]+", host):
    fail("Invalid HTTPS origin")
with open(source, encoding="utf-8") as stream:
    config = stream.read()
matches = list(re.finditer(r"\bserver\s*\{", config))
eligible = []
for match in matches:
    depth = 1
    end = match.end()
    while end < len(config) and depth:
        if config[end] == "{":
            depth += 1
        elif config[end] == "}":
            depth -= 1
        end += 1
    if depth:
        fail("Malformed nginx server block")
    block = config[match.end():end - 1]
    if (re.search(r"\blisten\s+[^;]*443[^;]*ssl[^;]*;", block)
            and re.search(r"\bserver_name\s+[^;]*\b" + re.escape(host) + r"\b", block)
            and "ssl_certificate" in block):
        eligible.append((match.end(), end - 1, block))
if len(eligible) != 1:
    fail("Expected exactly one existing TLS server for origin")
_, end, block = eligible[0]
if "# OAOS Company C02 begin" in config or "# OAOS Company C02 end" in config:
    if (config.count("# OAOS Company C02 begin") != 1
            or config.count("# OAOS Company C02 end") != 1
            or "# OAOS Company C02 begin" not in block
            or "# OAOS Company C02 end" not in block
            or "location = /company/oauth/callback" not in block
            or "location /company/" not in block):
        fail("Existing Company nginx block needs manual review")
    sys.stdout.write(config)
    raise SystemExit(0)
if re.search(r"\blocation\s+(?:=\s*)?/company(?:/|\b)", block):
    fail("Existing Company location needs manual review")
addition = """    # OAOS Company C02 begin
    location = /company/oauth/callback {
        proxy_pass http://127.0.0.1:8765;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
    }
    location /company/ {
        proxy_pass http://127.0.0.1:8765;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
    }
    # OAOS Company C02 end
"""
sys.stdout.write(config[:end] + addition + config[end:])
