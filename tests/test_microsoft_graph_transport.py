from __future__ import annotations

import os

import pytest

from adapters.microsoft.adapter import MicrosoftAdapter


class FakeResponse:
    status_code = 200
    text = "ok"

    def raise_for_status(self):
        return None

    def json(self):
        return {"value": [{"id": "m1"}]}


class FakeClient:
    def __init__(self, *args, **kwargs):
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return FakeResponse()


@pytest.mark.asyncio
async def test_microsoft_tool_uses_real_graph_transport(monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr("adapters.microsoft.adapter.httpx.AsyncClient", lambda *a, **k: fake)
    adapter = MicrosoftAdapter()
    result = await adapter.call_tool(
        "ms_mail_search", {"q": "hello"}, {"user_id": "employee:kim"}, access_token="token"
    )
    assert result["transport"] == "real"
    assert result["data"]["value"][0]["id"] == "m1"
    assert fake.calls[0][0] == "GET"
    assert fake.calls[0][1].endswith("/me/messages")


@pytest.mark.asyncio
async def test_microsoft_missing_token_fails_closed_in_production(monkeypatch):
    monkeypatch.setenv("OAOS_ENV", "production")
    with pytest.raises(RuntimeError, match="access token required"):
        await MicrosoftAdapter().call_tool("ms_mail_search", {}, {"user_id": "employee:kim"})
