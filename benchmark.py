"""Reproducible synthetic throughput benchmark; not a live supply estimate."""
import argparse
import json
import tempfile
import time
from discovery_state import DiscoveryStore


def run(count=3000):
    with tempfile.TemporaryDirectory() as directory:
        store = DiscoveryStore(directory + "/bench.sqlite")
        start = time.perf_counter()
        for i in range(count):
            employer = store.add_employer(f"company{i}.example", f"Company {i}")
            store.save_job(employer, {"source": "fixture", "board": employer, "id": i, "title": "Backend Engineer"})
        elapsed = time.perf_counter() - start
        return {"records": count, "unique_employers": count, "seconds": round(elapsed, 4), "records_per_second": round(count / elapsed, 2), "records_per_day_at_measured_rate": round(count / elapsed * 86400), "dataset": "synthetic; storage writes only", "application_submissions": 0}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(); parser.add_argument("--count", type=int, default=3000)
    print(json.dumps(run(parser.parse_args().count), indent=2))
