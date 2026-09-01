import asyncio


async def test_profile_worker_serializes_same_owner_operations():
    from control_plane.adaptive_profile import worker

    active = 0
    maximum = 0

    async def operation():
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.02)
        active -= 1

    await asyncio.gather(
        worker.run_owner_serialized("default", "employee:mykim", operation),
        worker.run_owner_serialized("default", "employee:mykim", operation),
    )

    assert maximum == 1
