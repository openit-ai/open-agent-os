"""Guard for OAOS DB rename: oaos is canonical, openagentos/open_agent_os must not appear in DB connection context."""
from __future__ import annotations
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Files that are allowed to still contain vault prefix openagentos/
VAULT_ALLOW_FILES = {
    "security/credential-vault/vault/external.py",
    "security/credential-vault/vault/vault.py",
    "admin-console/backend/personal_wiki.py",
    "docs/vault-externalization-design.md",
    "docs/architecture-v1.7.0.md",
    "docs/architecture-v1.7.1-design.md",
}

# Lines containing these substrings are exempt (vault paths, package names)
EXEMPT_LINE_SUBSTRS = [
    "openagentos/",        # Vault KV prefix
    "open_agent_os_",      # package distribution names
    ".egg-info",
    "open-agent-os",       # product/repo name, namespace open-agent-os
    "openagentos:secret@", # guard test assertion (negative check)
    "legacy",              # backward-compat shim _normalize_db / file alias comments
    "Legacy",
    "Transition",
    "_normalize_db",
    "*openagentos*",       # legacy dump file alias compatibility
    "*open_agent_os*",
]

# Regexes that indicate a stale OAOS DB reference (must NOT be present)
STALE_PATTERNS = [
    re.compile(r"openagentos:secret@"),
    re.compile(r"postgresql(\+asyncpg)?://openagentos", re.I),
    re.compile(r"postgresql(\+asyncpg)?://open_agent_os", re.I),
    re.compile(r"POSTGRES_DB.*openagentos", re.I),
    re.compile(r"POSTGRES_USER.*openagentos", re.I),
    re.compile(r"POSTGRES_DB.*open_agent_os", re.I),
    re.compile(r"POSTGRES_USER.*open_agent_os", re.I),
    re.compile(r"pg_isready.*openagentos", re.I),
    re.compile(r"pg_isready.*open_agent_os", re.I),
    re.compile(r"psql.*openagentos", re.I),
    re.compile(r"DATABASE_URL.*openagentos", re.I),
    re.compile(r"DATABASE_URL.*open_agent_os", re.I),
]

# Paths/patterns to ignore entirely (historical docs frozen)
IGNORE_PATH_CONTAINS = [
    "docs/architecture-v1.6",  # frozen history
    "docs/architecture-v1.5",
    "docs/architecture-v1.4",
    "docs/architecture-v1.3",
    "docs/architecture-v1.2",
    "docs/architecture-v1.1",
    "docs/GAP_AUDIT",
    "docs/reviews/",
]

def _git_tracked_files():
    out = subprocess.check_output(["git", "ls-files"], cwd=ROOT, text=True)
    return [p for p in out.splitlines() if p.strip()]

def test_no_stale_oaos_db_connection_references():
    offenders = []
    for rel in _git_tracked_files():
        if rel in ("tests/test_db_rename_guard.py", "tests/test_db_oaos_legacy.py"):
            continue
        if any(ign in rel for ign in IGNORE_PATH_CONTAINS):
            continue
        if "packages/" in rel and ("egg-info" in rel or ".pyc" in rel):
            continue
        p = ROOT / rel
        if not p.is_file():
            continue
        # only text-ish
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            low = line.lower()
            # skip exempt lines
            if any(s in line for s in EXEMPT_LINE_SUBSTRS):
                continue
            # need to detect openagentos/open_agent_os in DB context
            # if line contains either but not exempt, check against stale patterns
            # also generic catch: if line contains openagentos/open_agent_os and looks like DB url/config, flag
            for pat in STALE_PATTERNS:
                if pat.search(line):
                    offenders.append(f"{rel}:{i}: {line.strip()}")
                    break
            else:
                # generic: any plain openagentos/open_agent_os outside exempt and not in vault/package context
                # but we already filtered exempt; for remaining, any occurrence in tracked compose/k8s/scripts/alembic/config/security/docs current is stale
                sensitive_paths = ("deploy/", "alembic", "control-plane/", "security/models/db.py", ".env.example", "memory_service/", "docs/k8s-grant-isolation.sql", "README")
                if ("openagentos" in low or "open_agent_os" in low) and any(rel.startswith(sp) or sp in rel for sp in sensitive_paths):
                    # double check not vault prefix (already exempt)
                    if "openagentos/" not in line:
                        offenders.append(f"{rel}:{i}: {line.strip()}")
    assert not offenders, "Stale OAOS DB refs (openagentos/open_agent_os) found:\n" + "\n".join(offenders)

def test_compose_k8s_use_oaos_defaults():
    # docker-compose prod
    prod = (ROOT / "deploy" / "docker-compose.prod.yml").read_text()
    assert 'POSTGRES_DB: ${POSTGRES_DB:-oaos}' in prod
    assert 'POSTGRES_USER: ${POSTGRES_USER:-oaos}' in prod
    assert 'pg_isready -U ${POSTGRES_USER:-oaos} -d ${POSTGRES_DB:-oaos}' in prod
    assert 'DATABASE_URL: postgresql+asyncpg://${POSTGRES_USER:-oaos}' in prod
    assert prod.count('${POSTGRES_DB:-oaos}') >= 4
    # dev
    dev = (ROOT / "deploy" / "docker-compose.dev.yml").read_text()
    assert 'POSTGRES_DB: oaos' in dev
    assert 'POSTGRES_USER: oaos' in dev
    assert 'openagentos' not in dev
    # k8s configmap
    cm = (ROOT / "deploy" / "k8s" / "configmap.yaml").read_text()
    assert 'POSTGRES_DB: "oaos"' in cm
    assert 'POSTGRES_USER: "oaos"' in cm
    assert 'postgresql+asyncpg://oaos:' in cm
    # managed values
    mv = (ROOT / "deploy" / "k8s" / "managed-values.yaml").read_text()
    assert 'postgresDb: oaos' in mv
    assert 'postgresUser: oaos' in mv
    # statefulset
    st = (ROOT / "deploy" / "k8s" / "postgres-statefulset.yaml").read_text()
    assert st.count('"oaos"') >= 4
    assert 'openagentos' not in st
    # scripts
    backup = (ROOT / "deploy" / "scripts" / "backup.sh").read_text()
    assert 'BACKUP_PG_DBS="${BACKUP_PG_DBS:-oaos ' in backup
    assert '--db may be repeated to filter (oaos|mattermost|outline)' in backup
    restore = (ROOT / "deploy" / "scripts" / "restore.sh").read_text()
    assert 'or: $0 --pg-file <file> [--db oaos]' in restore
    health = (ROOT / "deploy" / "scripts" / "health-check.sh").read_text()
    assert 'PGUSER="${POSTGRES_USER:-oaos}"' in health
    assert 'PGDB="${POSTGRES_DB:-oaos}"' in health
    install = (ROOT / "deploy" / "scripts" / "install.sh").read_text()
    assert 'postgresql+asyncpg://oaos:secret@localhost:5432/oaos' in install
    # alembic/config
    alembic_ini = (ROOT / "alembic.ini").read_text()
    assert 'postgresql+asyncpg://oaos:secret@localhost:5432/oaos' in alembic_ini
    env_py = (ROOT / "alembic" / "env.py").read_text()
    assert 'postgresql+asyncpg://oaos:secret@localhost:5432/oaos' in env_py
    grant = (ROOT / "docs" / "k8s-grant-isolation.sql").read_text()
    assert 'FROM oaos' in grant
    assert 'openagentos' not in grant
    env_example = (ROOT / ".env.example").read_text()
    assert 'postgresql+asyncpg://oaos:' in env_example or 'DATABASE_URL=postgresql+asyncpg://oaos' in env_example
    assert 'REVOKE ALL ON DATABASE mattermost FROM oaos' in env_example
