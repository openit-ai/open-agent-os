import asyncio
import time
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "control-plane"))
from control_plane.adaptive_profile.queue import enqueue, reset_for_tests


def test_enqueue_does_not_wait_for_slow_job():
    async def run():
        reset_for_tests()
        started = asyncio.Event()
        release = asyncio.Event()
        def slow_job():
            started.set()
            # Deliberately block a worker thread, not the event loop.
            time.sleep(0.15)
            release.set()
        t0 = time.perf_counter()
        assert enqueue(slow_job)
        enqueue(slow_job)
        enqueue_elapsed = time.perf_counter() - t0
        await asyncio.wait_for(started.wait(), timeout=0.5)
        # The event loop remains responsive while the archive is slow.
        await asyncio.sleep(0.01)
        assert enqueue_elapsed < 0.05
        await asyncio.sleep(0.2)
        assert release.is_set()
        reset_for_tests()
    asyncio.run(run())
