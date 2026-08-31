import asyncio
import importlib


async def test_same_owner_requests_are_serialized():
    webhook = importlib.import_module("control_plane.mattermost_adapter.webhook")
    active = 0
    maximum = 0

    async def operation(value):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.02)
        active -= 1
        return value

    results = await asyncio.gather(
        webhook.run_owner_serialized("tenant-a", "employee:kim", operation, "first"),
        webhook.run_owner_serialized("tenant-a", "employee:kim", operation, "second"),
    )

    assert results == ["first", "second"]
    assert maximum == 1
