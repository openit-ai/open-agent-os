from pathlib import Path
import importlib.util

ROOT = Path(__file__).resolve().parents[1]

def test_behavioral_migration_declares_all_tables_and_head():
    path = ROOT / "alembic/versions/017_profile_behavioral_observations.py"
    src = path.read_text()
    assert 'revision = "017_profile_behavioral"' in src
    for table in ("profile_observations", "profile_feature_aggregates", "profile_projections", "profile_settings"):
        assert f'"{table}"' in src

def test_behavioral_orm_models_match_migration_names():
    src = (ROOT / "security/models/orm.py").read_text()
    for table in ("profile_observations", "profile_feature_aggregates", "profile_projections", "profile_settings"):
        assert f'__tablename__ = "{table}"' in src
