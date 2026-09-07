"""Measure the API's real latency, endpoint by endpoint.

    python scripts/measure_api.py --base http://localhost:8010

Runs against a live server over HTTP rather than through TestClient, so the
numbers include serialisation, the connection pool and the event loop -- the
things a request actually pays for. Results go to eval/results/api-latency.json
and into docs/measurements.md.

Reports p50 and p95, not a mean. A mean hides the tail, and the tail is what a
user notices: an endpoint averaging 40ms with a 900ms p95 feels broken one time
in twenty, which is exactly often enough to be remembered.

The first call to any endpoint that touches the recommender pays for fitting the
model. That is a cache miss rather than a request cost, so it is warmed first
and reported separately.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = REPO_ROOT / "eval" / "results"

PASSWORD = "correct horse battery staple"


@dataclass
class Timing:
    name: str
    samples: list[float] = field(default_factory=list)
    errors: int = 0

    def record(self, seconds: float) -> None:
        self.samples.append(seconds * 1000)

    def summary(self) -> dict:
        if not self.samples:
            return {"name": self.name, "n": 0, "errors": self.errors}
        ordered = sorted(self.samples)
        return {
            "name": self.name,
            "n": len(ordered),
            "errors": self.errors,
            "p50_ms": round(statistics.median(ordered), 1),
            "p95_ms": round(ordered[max(0, int(len(ordered) * 0.95) - 1)], 1),
            "max_ms": round(ordered[-1], 1),
        }


class Client:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.token: str | None = None

    def call(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict, float]:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(f"{self.base}{path}", data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")

        started = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = response.read()
                elapsed = time.perf_counter() - started
                return response.status, (json.loads(payload) if payload else {}), elapsed
        except urllib.error.HTTPError as exc:
            elapsed = time.perf_counter() - started
            body_text = exc.read()
            return exc.code, (json.loads(body_text) if body_text else {}), elapsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://localhost:8010")
    parser.add_argument("--runs", type=int, default=30)
    parser.add_argument(
        "--allow-rate-limited",
        action="store_true",
        help=(
            "measure even when the API has rate limiting on. The login, signup "
            "and generate rows will be capped by the limiter rather than by "
            "latency, so the result is not a latency measurement -- it is only "
            "here as an escape hatch."
        ),
    )
    args = parser.parse_args(argv)

    client = Client(args.base)
    status, health, _ = client.call("GET", "/health")
    if status != 200:
        print(f"no API at {args.base} (health returned {status})")
        return 2

    # A per-IP limiter counts this script's 30 logins from localhost exactly as
    # it would an attacker's, so login/signup/generate would report the limit,
    # not the latency, and that number would then be written into
    # docs/measurements.md. Refuse rather than record a misleading figure.
    if health.get("rate_limiting") and not args.allow_rate_limited:
        print(
            "the API has rate limiting enabled -- login, signup and generate "
            "would be throttled and the numbers would be meaningless.\n"
            "restart the API with rate limiting off, for example:\n"
            "  RATE_LIMIT_ENABLED=false ./.venv/bin/uvicorn "
            "commonground_api.main:app --port 8010\n"
            "or pass --allow-rate-limited to measure anyway."
        )
        return 2

    email = f"measure-{uuid.uuid4().hex[:10]}@example.com"
    timings: dict[str, Timing] = {}

    def measure(name: str, method: str, path: str, body: dict | None = None):
        timing = timings.setdefault(name, Timing(name))
        status, payload, elapsed = client.call(method, path, body)
        if status >= 400:
            timing.errors += 1
        else:
            timing.record(elapsed)
        return status, payload

    print(f"measuring {args.base}, {args.runs} runs per endpoint\n")

    # --- one-off: account setup, which also warms the auth path -------------
    status, tokens = measure(
        "POST /api/auth/signup",
        "POST",
        "/api/auth/signup",
        {"email": email, "password": PASSWORD, "display_name": "Measure"},
    )
    if status >= 400:
        print(f"could not create an account: {status} {tokens}")
        return 1
    client.token = tokens["access_token"]

    _, artists, _ = client.call("GET", "/api/artists?limit=8")
    client.call("PUT", "/api/profile/onboarding", {"artist_ids": [a["id"] for a in artists[:5]]})
    _, room, _ = client.call("POST", "/api/rooms", {"name": "Measurement", "mode": "consensus"})
    room_id = room["id"]

    # Warm the model cache. The first generate pays to fit the recommender,
    # which is a startup cost rather than a request cost.
    warm_started = time.perf_counter()
    client.call("POST", f"/api/rooms/{room_id}/playlist?k=20")
    cold_ms = (time.perf_counter() - warm_started) * 1000
    print(f"first playlist (cold model): {cold_ms:.0f}ms -- excluded from the table below\n")

    for _ in range(args.runs):
        measure("GET /health", "GET", "/health")
        measure(
            "POST /api/auth/login",
            "POST",
            "/api/auth/login",
            {"email": email, "password": PASSWORD},
        )
        measure("GET /api/auth/me", "GET", "/api/auth/me")
        measure("GET /api/artists", "GET", "/api/artists?limit=40")
        measure("GET /api/artists?q=", "GET", "/api/artists?q=rad&limit=40")
        measure("GET /api/tags", "GET", "/api/tags?limit=40")
        measure("GET /api/profile", "GET", "/api/profile")
        measure("GET /api/rooms", "GET", "/api/rooms")
        measure("GET /api/rooms/{id}", "GET", f"/api/rooms/{room_id}")
        measure("GET /api/rooms/{id}/playlist", "GET", f"/api/rooms/{room_id}/playlist")
        measure("POST /api/rooms/{id}/playlist", "POST", f"/api/rooms/{room_id}/playlist?k=20")

    rows = [timing.summary() for timing in timings.values() if timing.samples]
    rows.sort(key=lambda row: -row["p95_ms"])

    print(f"| {'endpoint':34} | {'p50':>7} | {'p95':>7} | {'max':>7} | n |")
    print(f"| {'-' * 34} | {'-' * 7} | {'-' * 7} | {'-' * 7} | --- |")
    for row in rows:
        print(
            f"| {row['name']:34} | {row['p50_ms']:>6.1f}ms | {row['p95_ms']:>6.1f}ms "
            f"| {row['max_ms']:>6.1f}ms | {row['n']} |"
        )

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULTS_DIR / "api-latency.json"
    out.write_text(
        json.dumps(
            {
                "base": args.base,
                "measured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "runs_per_endpoint": args.runs,
                "rate_limiting": bool(health.get("rate_limiting")),
                "note": (
                    "Measured over HTTP against a live server, so the numbers include "
                    "serialisation and the connection pool. The first playlist request "
                    "pays to fit the recommender and is reported separately as a cold "
                    "cost rather than a request cost."
                ),
                "cold_first_playlist_ms": round(cold_ms, 1),
                "endpoints": rows,
            },
            indent=2,
        )
        + "\n"
    )
    print(f"\nwrote {out.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
