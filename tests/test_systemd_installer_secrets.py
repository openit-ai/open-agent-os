"""TDD for systemd installer secret generation / preservation / rotation.

Spec:
- NEW install (canonical env missing) auto-generates 64-hex secrets for
  JWT_SIGNING_KEY, AUDIT_SIGNING_KEY, ADMIN_JWT_SECRET, and one encryption key
  (VAULT_ENCRYPTION_KEY and OAOS_ENCRYPTION_KEY treated as aliases — same value).
- Existing install preserves secrets (no overwrite).
- Existing weak env without --rotate-secrets => clear error mentioning --rotate-secrets.
- --rotate-secrets is opt-in, rotates canonical secrets, warns, does not print values.
- Docker compose files remain unchanged.
- Never prints secret values.
"""
import os
import re
import subprocess
import pathlib
import tempfile
import shutil
import textwrap

ROOT = pathlib.Path(__file__).resolve().parents[1]
INSTALL = ROOT / "deploy" / "systemd" / "install-systemd.sh"
CHECK = ROOT / "scripts" / "check-production-config.sh"
EXAMPLE = ROOT / "config" / "oaos.env.example"

HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
CANONICAL_KEYS = ["JWT_SIGNING_KEY", "AUDIT_SIGNING_KEY", "ADMIN_JWT_SECRET"]

def parse_env(path: pathlib.Path) -> dict:
    env = {}
    for line in path.read_text().splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("export "):
            s = s[len("export "):].strip()
        if "=" not in s:
            continue
        k, v = s.split("=", 1)
        k = k.strip()
        v = v.strip()
        if len(v) >= 2 and ((v[0]=='"' and v[-1]=='"') or (v[0]=="'" and v[-1]=="'")):
            v = v[1:-1]
        # strip inline comment outside quotes (approx)
        # we keep as is for test; most generated values are hex no comment
        # but strip trailing comment if present and not in quotes
        if "#" in v:
            # if value is hex, # not present
            v = v.split("#")[0].strip()
        env[k] = v
    return env

def run_install(args, cwd=None, env=None):
    # Run installer with --dry-run disabled but avoid systemd calls by using --no-enable and --user and isolated REPO_ROOT
    # We call bash script directly
    cmd = ["bash", str(INSTALL)] + args
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd or str(ROOT), env=env)
    return result

def test_docker_compose_unchanged():
    # Docker path must remain parallel and unchanged — installer must not touch compose files
    prod = ROOT / "deploy" / "docker-compose.prod.yml"
    dev = ROOT / "deploy" / "docker-compose.dev.yml"
    assert prod.exists() and dev.exists()
    # Ensure they still contain env_file reference and not systemd-specific generation
    prod_txt = prod.read_text()
    assert "OAOS_ENCRYPTION_KEY" in prod_txt
    install_txt = INSTALL.read_text()
    # The installer must not execute Docker commands; the Docker distribution remains separate.
    assert "docker compose" not in install_txt.lower()

def test_new_install_generates_64hex_secrets_user_mode():
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        # Create a fake repo root with minimal structure to test generation
        # Use --user mode which uses config/oaos.env as canonical.
        # We'll run installer against a temp repo root by patching REPO_ROOT via symlink trick:
        # Instead, we directly test the helper: simulate new install by removing canonical and running installer with --user --dry-run --no-enable
        # We need an isolated repo copy? Simpler: run installer with OAOS env overriding canonical path via --env-file to a temp canonical that doesn't exist
        # For --user, canonical is $REPO_ROOT/config/oaos.env. We can create a temp repo root.
        repo = td / "repo"
        shutil.copytree(str(ROOT), str(repo), symlinks=True, ignore=shutil.ignore_patterns(".git", "__pycache__", ".venv", "venv"))
        canonical = repo / "config" / "oaos.env"
        if canonical.exists():
            canonical.unlink()
        # Ensure example exists
        assert (repo / "config" / "oaos.env.example").exists()
        # Provide a minimal env with valid DATABASE_URL so preflight can pass after generation, but without secrets (will be generated)
        # The installer should generate secrets when canonical missing. We will call --user --env-file with a valid DB URL file
        # Create a source env file with only DB and OAOS_ENV
        src = td / "src.env"
        src.write_text(textwrap.dedent("""\
            DATABASE_URL=postgresql+asyncpg://oaos:strongpass@localhost:5432/oaos
            OAOS_ENV=production
            REDIS_URL=redis://localhost:6379/0
            # secrets intentionally omitted — installer should generate
            """))
        result = subprocess.run(
            ["bash", str(repo / "deploy" / "systemd" / "install-systemd.sh"), "--user", "--env-file", str(src), "--no-enable", "--dry-run"],
            capture_output=True, text=True, cwd=str(repo)
        )
        # Dry-run should indicate would generate secrets, not fail
        # Before implementation, this will fail because installer expects secrets — RED
        # After implementation, it should succeed or at least mention generating
        combined = result.stdout + result.stderr
        # After fix, should not contain placeholder error for JWT, and should mention generating
        # For RED phase, we assert generation — this will fail initially
        assert "Generating" in combined or "generate" in combined.lower() or result.returncode == 0, f"expected generation in dry-run, got rc={result.returncode} out={combined[:2000]}"

def test_new_install_actually_writes_64hex():
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        repo = td / "repo"
        shutil.copytree(str(ROOT), str(repo), symlinks=True, ignore=shutil.ignore_patterns(".git", "__pycache__", ".venv", "venv"))
        canonical = repo / "config" / "oaos.env"
        if canonical.exists():
            canonical.unlink()
        src = td / "src.env"
        src.write_text(textwrap.dedent("""\
            DATABASE_URL=postgresql+asyncpg://oaos:strongpass@localhost:5432/oaos
            OAOS_ENV=production
            REDIS_URL=redis://localhost:6379/0
            """))
        # Real run (not dry-run) but with --no-enable to avoid systemd, --user mode
        result = subprocess.run(
            ["bash", str(repo / "deploy" / "systemd" / "install-systemd.sh"), "--user", "--env-file", str(src), "--no-enable"],
            capture_output=True, text=True, cwd=str(repo)
        )
        combined = result.stdout + result.stderr
        assert result.returncode == 0, f"new install should succeed, rc={result.returncode} out={combined[:3000]}"
        assert canonical.exists(), "canonical should be created"
        env = parse_env(canonical)
        for k in CANONICAL_KEYS:
            assert k in env, f"{k} missing"
            assert HEX64_RE.match(env[k]), f"{k} not 64-hex: {env[k][:20]}"
        enc = env.get("VAULT_ENCRYPTION_KEY") or env.get("OAOS_ENCRYPTION_KEY")
        assert enc and HEX64_RE.match(enc), f"encryption key not 64-hex: {enc}"
        # alias: both should be present and equal (or at least one present and they are aliases)
        if "VAULT_ENCRYPTION_KEY" in env and "OAOS_ENCRYPTION_KEY" in env:
            assert env["VAULT_ENCRYPTION_KEY"] == env["OAOS_ENCRYPTION_KEY"], "aliases must be same value"
        # must not print secret values
        for k in CANONICAL_KEYS + ["VAULT_ENCRYPTION_KEY", "OAOS_ENCRYPTION_KEY"]:
            v = env.get(k, "")
            if v and len(v) >= 8:
                assert v not in combined, f"secret {k} leaked in logs"
                assert v[:16] not in combined, f"secret prefix leaked"

def test_existing_strong_preserved():
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        repo = td / "repo"
        shutil.copytree(str(ROOT), str(repo), symlinks=True, ignore=shutil.ignore_patterns(".git", "__pycache__", ".venv", "venv"))
        canonical = repo / "config" / "oaos.env"
        # Create existing strong env
        strong = "a" * 64  # hex-like but deterministic for test — use hex chars
        strong_hex = "ab12cd34ef56" * 6  # 72 but we need 64
        strong_hex = "a" * 64
        # Ensure hex
        strong_hex = "0123456789abcdef" * 4
        env_content = textwrap.dedent(f"""\
            DATABASE_URL=postgresql+asyncpg://oaos:strongpass@localhost:5432/oaos
            OAOS_ENV=production
            JWT_SIGNING_KEY={strong_hex}
            AUDIT_SIGNING_KEY={strong_hex}
            ADMIN_JWT_SECRET={strong_hex}
            VAULT_ENCRYPTION_KEY={strong_hex}
            OAOS_ENCRYPTION_KEY={strong_hex}
            """)
        canonical.parent.mkdir(parents=True, exist_ok=True)
        canonical.write_text(env_content)
        canonical.chmod(0o600)
        orig = canonical.read_text()
        src = td / "src.env"
        src.write_text(env_content)  # same
        result = subprocess.run(
            ["bash", str(repo / "deploy" / "systemd" / "install-systemd.sh"), "--user", "--env-file", str(src), "--no-enable"],
            capture_output=True, text=True, cwd=str(repo)
        )
        combined = result.stdout + result.stderr
        assert result.returncode == 0, f"preserve should succeed rc={result.returncode} out={combined[:2000]}"
        after = canonical.read_text()
        assert orig == after, "existing strong env must be preserved unchanged"
        # ensure not leaked
        assert strong_hex not in combined

def test_existing_weak_without_rotate_fails():
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        repo = td / "repo"
        shutil.copytree(str(ROOT), str(repo), symlinks=True, ignore=shutil.ignore_patterns(".git", "__pycache__", ".venv", "venv"))
        canonical = repo / "config" / "oaos.env"
        weak_content = textwrap.dedent("""\
            DATABASE_URL=postgresql+asyncpg://oaos:strongpass@localhost:5432/oaos
            OAOS_ENV=production
            JWT_SIGNING_KEY=CHANGE_ME_32_BYTES_MIN_JWT_SIGNING_KEY
            AUDIT_SIGNING_KEY=CHANGE_ME_AUDIT_SIGNING_KEY_32B_MINIMUM
            ADMIN_JWT_SECRET=CHANGE_ME_ADMIN_JWT_32B_MINIMUM
            VAULT_ENCRYPTION_KEY=CHANGE_ME_32_BYTE_BASE64_ENC_KEY==
            OAOS_ENCRYPTION_KEY=CHANGE_ME_32_BYTE_BASE64_ENC_KEY==
            """)
        canonical.parent.mkdir(parents=True, exist_ok=True)
        canonical.write_text(weak_content)
        canonical.chmod(0o600)
        src = td / "src.env"
        src.write_text(weak_content)
        result = subprocess.run(
            ["bash", str(repo / "deploy" / "systemd" / "install-systemd.sh"), "--user", "--env-file", str(src), "--no-enable"],
            capture_output=True, text=True, cwd=str(repo)
        )
        combined = result.stdout + result.stderr
        assert result.returncode != 0, f"weak without --rotate should fail, got rc=0 out={combined[:2000]}"
        # Must mention --rotate-secrets
        assert "--rotate-secrets" in combined, f"error should mention --rotate-secrets, got {combined[:2000]}"
        # Must not print secret values (placeholder is ok but not generated)
        # also ensure it warns about weak keys
        assert "weak" in combined.lower() or "placeholder" in combined.lower() or "rotate" in combined.lower()

def test_existing_weak_with_rotate_succeeds_and_rotates():
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        repo = td / "repo"
        shutil.copytree(str(ROOT), str(repo), symlinks=True, ignore=shutil.ignore_patterns(".git", "__pycache__", ".venv", "venv"))
        canonical = repo / "config" / "oaos.env"
        weak_content = textwrap.dedent("""\
            DATABASE_URL=postgresql+asyncpg://oaos:strongpass@localhost:5432/oaos
            OAOS_ENV=production
            JWT_SIGNING_KEY=short
            AUDIT_SIGNING_KEY=CHANGE_ME_AUDIT_SIGNING_KEY_32B_MINIMUM
            ADMIN_JWT_SECRET=CHANGE_ME_ADMIN_JWT_32B_MINIMUM
            VAULT_ENCRYPTION_KEY=CHANGE_ME_32_BYTE_BASE64_ENC_KEY==
            """)
        canonical.parent.mkdir(parents=True, exist_ok=True)
        canonical.write_text(weak_content)
        canonical.chmod(0o600)
        result = subprocess.run(
            ["bash", str(repo / "deploy" / "systemd" / "install-systemd.sh"), "--user", "--env-file", str(canonical), "--no-enable", "--rotate-secrets"],
            capture_output=True, text=True, cwd=str(repo)
        )
        combined = result.stdout + result.stderr
        assert result.returncode == 0, f"rotate should succeed rc={result.returncode} out={combined[:3000]}"
        assert "rotate" in combined.lower() and "warn" in combined.lower() or "rotating" in combined.lower(), f"should warn about rotation {combined[:2000]}"
        env = parse_env(canonical)
        for k in CANONICAL_KEYS:
            assert HEX64_RE.match(env[k]), f"{k} not rotated to 64-hex: {env.get(k)}"
        enc = env.get("VAULT_ENCRYPTION_KEY") or env.get("OAOS_ENCRYPTION_KEY")
        assert HEX64_RE.match(enc)
        if "VAULT_ENCRYPTION_KEY" in env and "OAOS_ENCRYPTION_KEY" in env:
            assert env["VAULT_ENCRYPTION_KEY"] == env["OAOS_ENCRYPTION_KEY"]
        # no secret leakage
        for k in CANONICAL_KEYS:
            v = env.get(k, "")
            assert v not in combined

def test_rotate_strong_also_rotates():
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        repo = td / "repo"
        shutil.copytree(str(ROOT), str(repo), symlinks=True, ignore=shutil.ignore_patterns(".git", "__pycache__", ".venv", "venv"))
        canonical = repo / "config" / "oaos.env"
        strong_hex = "0123456789abcdef" * 4
        env_content = textwrap.dedent(f"""\
            DATABASE_URL=postgresql+asyncpg://oaos:strongpass@localhost:5432/oaos
            OAOS_ENV=production
            JWT_SIGNING_KEY={strong_hex}
            AUDIT_SIGNING_KEY={strong_hex}
            ADMIN_JWT_SECRET={strong_hex}
            VAULT_ENCRYPTION_KEY={strong_hex}
            OAOS_ENCRYPTION_KEY={strong_hex}
            """)
        canonical.parent.mkdir(parents=True, exist_ok=True)
        canonical.write_text(env_content)
        canonical.chmod(0o600)
        result = subprocess.run(
            ["bash", str(repo / "deploy" / "systemd" / "install-systemd.sh"), "--user", "--env-file", str(canonical), "--no-enable", "--rotate-secrets"],
            capture_output=True, text=True, cwd=str(repo)
        )
        combined = result.stdout + result.stderr
        assert result.returncode == 0
        env = parse_env(canonical)
        # should have rotated (new value != old)
        assert env["JWT_SIGNING_KEY"] != strong_hex, "rotate should change value"
        assert HEX64_RE.match(env["JWT_SIGNING_KEY"])
        assert "warn" in combined.lower() or "rotat" in combined.lower()

def test_encryption_alias_single_value():
    # Ensure installer treats VAULT and OAOS as single logical key: if only one is set, it satisfies check and generation sets both to same
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        repo = td / "repo"
        shutil.copytree(str(ROOT), str(repo), symlinks=True, ignore=shutil.ignore_patterns(".git", "__pycache__", ".venv", "venv"))
        canonical = repo / "config" / "oaos.env"
        if canonical.exists():
            canonical.unlink()
        src = td / "src.env"
        src.write_text(textwrap.dedent("""\
            DATABASE_URL=postgresql+asyncpg://oaos:strongpass@localhost:5432/oaos
            OAOS_ENV=production
            """))
        result = subprocess.run(
            ["bash", str(repo / "deploy" / "systemd" / "install-systemd.sh"), "--user", "--env-file", str(src), "--no-enable"],
            capture_output=True, text=True, cwd=str(repo)
        )
        assert result.returncode == 0
        env = parse_env(canonical)
        # Both keys should exist and be equal, OR at least one exists and check passes
        # Our spec says they should be aliases — generation should set both to same 64-hex
        assert "VAULT_ENCRYPTION_KEY" in env and "OAOS_ENCRYPTION_KEY" in env
        assert env["VAULT_ENCRYPTION_KEY"] == env["OAOS_ENCRYPTION_KEY"]
        # Now test that check passes with only one set
        # Create env with only VAULT set, run check script
        single = td / "single.env"
        single.write_text(textwrap.dedent(f"""\
            DATABASE_URL=postgresql+asyncpg://oaos:strongpass@localhost:5432/oaos
            OAOS_ENV=production
            JWT_SIGNING_KEY={env['JWT_SIGNING_KEY']}
            AUDIT_SIGNING_KEY={env['AUDIT_SIGNING_KEY']}
            ADMIN_JWT_SECRET={env['ADMIN_JWT_SECRET']}
            VAULT_ENCRYPTION_KEY={env['VAULT_ENCRYPTION_KEY']}
            """))
        single.chmod(0o600)
        cp = subprocess.run(["bash", str(CHECK), "--env-file", str(single)], capture_output=True, text=True)
        assert cp.returncode == 0, f"check should pass with only VAULT, got {cp.stdout} {cp.stderr}"
        # Similarly only OAOS
        single2 = td / "single2.env"
        single2.write_text(textwrap.dedent(f"""\
            DATABASE_URL=postgresql+asyncpg://oaos:strongpass@localhost:5432/oaos
            OAOS_ENV=production
            JWT_SIGNING_KEY={env['JWT_SIGNING_KEY']}
            AUDIT_SIGNING_KEY={env['AUDIT_SIGNING_KEY']}
            ADMIN_JWT_SECRET={env['ADMIN_JWT_SECRET']}
            OAOS_ENCRYPTION_KEY={env['OAOS_ENCRYPTION_KEY']}
            """))
        single2.chmod(0o600)
        cp2 = subprocess.run(["bash", str(CHECK), "--env-file", str(single2)], capture_output=True, text=True)
        assert cp2.returncode == 0

def test_no_secret_printed_on_generation():
    with tempfile.TemporaryDirectory() as td:
        td = pathlib.Path(td)
        repo = td / "repo"
        shutil.copytree(str(ROOT), str(repo), symlinks=True, ignore=shutil.ignore_patterns(".git", "__pycache__", ".venv", "venv"))
        canonical = repo / "config" / "oaos.env"
        if canonical.exists():
            canonical.unlink()
        src = td / "src.env"
        src.write_text(textwrap.dedent("""\
            DATABASE_URL=postgresql+asyncpg://oaos:strongpass@localhost:5432/oaos
            OAOS_ENV=production
            """))
        result = subprocess.run(
            ["bash", str(repo / "deploy" / "systemd" / "install-systemd.sh"), "--user", "--env-file", str(src), "--no-enable"],
            capture_output=True, text=True, cwd=str(repo)
        )
        combined = result.stdout + result.stderr
        env = parse_env(canonical)
        for v in env.values():
            if v and HEX64_RE.match(v):
                assert v not in combined, "generated secret leaked"
