"""Restartable periodic discovery worker with a filesystem pause/kill switch."""
import asyncio
import time
from pathlib import Path

from web_crawler import explore


class DiscoveryWorker:
    def __init__(self, store, seeds, control_dir=".worker"):
        self.store = store
        self.seeds = list(seeds)
        self.control = Path(control_dir)
        self.control.mkdir(parents=True, exist_ok=True)
        self.stop_file = self.control / "STOP"
        self.pause_file = self.control / "PAUSE"

    def stop(self):
        self.stop_file.touch()

    def pause(self):
        self.pause_file.touch()

    def resume(self):
        self.pause_file.unlink(missing_ok=True)

    async def cycle(self, **limits):
        if self.stop_file.exists():
            return {"state": "stopped", "pages": 0}
        if self.pause_file.exists():
            return {"state": "paused", "pages": 0}
        self.store.checkpoint("worker:last_started", time.time())
        result = await explore(self.seeds, store=self.store, **limits)
        self.store.checkpoint("worker:last_finished", {"time": time.time(), "pages": len(result["pages"]), "checkpoints": len(result["checkpoints"])})
        result["state"] = "completed"
        return result

    async def serve(self, interval_seconds=3600, **limits):
        if interval_seconds < 1:
            raise ValueError("worker interval must be positive")
        while not self.stop_file.exists():
            if self.pause_file.exists():
                await asyncio.sleep(1)
                continue
            await self.cycle(**limits)
            await asyncio.sleep(interval_seconds)


def main():
    import argparse
    from discovery_state import DiscoveryStore
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database")
    parser.add_argument("seeds", nargs="+")
    parser.add_argument("--control-dir", default=".worker")
    parser.add_argument("--max-pages", type=int, default=50)
    parser.add_argument("--interval", type=int, default=3600)
    args = parser.parse_args()
    worker = DiscoveryWorker(DiscoveryStore(args.database), args.seeds, args.control_dir)
    asyncio.run(worker.serve(interval_seconds=args.interval, max_pages=args.max_pages))


if __name__ == "__main__":
    main()
