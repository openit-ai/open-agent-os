"""Legacy restore accepts old dump names for transition."""
from pathlib import Path
import subprocess, tempfile, gzip, json
ROOT = Path(__file__).resolve().parents[1]
def test_restore_accepts_legacy_dump_name():
    script = ROOT / "deploy" / "scripts" / "restore.sh"
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        legacy = td / "oaos-20260101-openagentos.sql.gz"
        with gzip.open(legacy, "wb") as f:
            f.write(b"-- dummy dump")
        r = subprocess.run(["bash", str(script), "--pg-file", str(legacy), "--dry-run"], capture_output=True, text=True, timeout=15)
        assert r.returncode == 0
        assert "db=oaos" in (r.stdout + r.stderr)
def test_restore_accepts_legacy_db_flag():
    script = ROOT / "deploy" / "scripts" / "restore.sh"
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        legacy = td / "pg.sql.gz"
        with gzip.open(legacy, "wb") as f:
            f.write(b"-- legacy")
        r = subprocess.run(["bash", str(script), "--pg-file", str(legacy), "--db", "openagentos", "--dry-run"], capture_output=True, text=True, timeout=15)
        assert r.returncode == 0
        assert "db=oaos" in (r.stdout + r.stderr)
def test_restore_manifest_legacy_db():
    script = ROOT / "deploy" / "scripts" / "restore.sh"
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        pg = td / "oaos-20260829-openagentos.sql.gz"
        pg.write_bytes(b"dummy")
        manifest = td / "manifest.json"
        manifest.write_text(json.dumps({"backup_dir": str(td), "postgres_dbs": [{"db": "openagentos", "file": pg.name, "status": "ok"}], "redis_file": ""}))
        r = subprocess.run(["bash", str(script), "--manifest", str(manifest), "--dry-run"], capture_output=True, text=True, timeout=15)
        assert r.returncode == 0
        assert "db=oaos" in (r.stdout + r.stderr)
def test_backup_legacy_flag():
    import os
    script = ROOT / "deploy" / "scripts" / "backup.sh"
    with tempfile.TemporaryDirectory() as td:
        env = os.environ.copy(); env["BACKUP_DIR"] = td
        r = subprocess.run(["bash", str(script), "--dry-run", "--db", "openagentos"], capture_output=True, text=True, env=env, timeout=15)
        assert r.returncode == 0
        assert "db=oaos" in (r.stdout + r.stderr)
