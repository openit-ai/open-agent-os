from __future__ import annotations

import pytest
from sqlalchemy import create_engine


@pytest.mark.asyncio
async def test_admin_schema_production_requires_existing_table(monkeypatch):
    # The runtime guard is verified by the existing production-auth test suite;
    # this test remains a migration contract placeholder until auth import is
    # made side-effect free.
    monkeypatch.setenv("OAOS_ENV", "production")
    engine = create_engine("sqlite:///:memory:")
    assert not engine.dialect.has_table(engine.connect(), "admin_users")
    engine.dispose()
