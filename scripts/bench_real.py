"""Real bench: async collection against a staging deployment (chantier 1.5).

Measures the end-to-end wall time of `POST /collect/aws` in sqs mode on a
real (staging) environment — the number the sqlite bench in
`scripts/bench_runner.py` can only approximate. Methodology and results
live in docs/operations/benchmarks.md ("Réel (staging, 35 comptes)").

What it does:
  1. POST /collect/aws once per target account (one job per account, so
     per-account wall time is visible), with force=true.
  2. Poll GET /collect/aws/jobs/{job_id} until every job is terminal.
  3. Print wall time per job, per-region durations taken from the job
     detail (server-side timings), HTTP status codes seen, and the
     overall total.

What it CANNOT measure: AWS-side throttling detail (which API call was
throttled and how long the adaptive retry absorbed) — that stays in the
worker logs / CloudWatch, not in the job detail.

Zero new dependencies: stdlib urllib only (httpx is NOT installed in
this repo's venv — do not add it for a bench script).

Usage:
    export CONSTAT_API_KEY=<operator-key>   # reader key gets 403
    python scripts/bench_real.py \
        --base-url https://staging.constat.example.com \
        --targets staging-targets.json

`--targets` is a JSON array in the same shape as the scan-targets secret
(see infra/variables.tf): [{"aws_account_id": ..., "role_arn": ...,
"external_id": ..., "name": ..., "regions": [...]}, ...]

NOTE (2026-07-18): written against the chantier-1 API contract as
specified (202 + job_id, GET /collect/aws/jobs/{job_id} with per-region
detail). The endpoints are being implemented in parallel; the response
parsing below is deliberately tolerant of small field-name drift, but
re-check it against the shipped router before the first staging run.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from typing import Any

# Job statuses considered final. Tolerant superset: the shipped API may
# spell success "succeeded" or "success", and a partially-failed job
# "partial".
TERMINAL_STATUSES = {"succeeded", "success", "failed", "partial", "completed", "cancelled"}

USER_AGENT = "constat-bench-real/1.0"


def _request(
    method: str,
    url: str,
    api_key: str,
    body: dict[str, Any] | None,
    status_counter: Counter[int],
) -> dict[str, Any]:
    """One HTTP call; records the status code and returns parsed JSON."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "X-API-Key": api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            status_counter[resp.status] += 1
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        status_counter[exc.code] += 1
        payload = exc.read().decode(errors="replace")
        raise SystemExit(
            f"ERROR: {method} {url} -> HTTP {exc.code}: {payload[:500]}\n"
            "(403 = you used a reader key; POST /collect/aws requires an operator key)"
        ) from exc


def _job_id_of(payload: dict[str, Any]) -> str:
    for key in ("job_id", "jobId", "id"):
        if key in payload:
            return str(payload[key])
    raise SystemExit(f"ERROR: cannot find a job id in the POST response: {payload!r}")


def _status_of(job: dict[str, Any]) -> str:
    return str(job.get("status", "unknown")).lower()


def _region_rows(job: dict[str, Any]) -> list[dict[str, Any]]:
    """Best-effort extraction of the per-region breakdown from a job detail."""
    for key in ("regions", "results", "items", "work_items"):
        rows = job.get(key)
        if isinstance(rows, list):
            return [r for r in rows if isinstance(r, dict)]
    return []


def _fmt(seconds: float) -> str:
    return f"{seconds:8.1f} s"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="Staging API base URL (env: CONSTAT_BENCH_BASE_URL).",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="Operator API key (env: CONSTAT_API_KEY). Reader keys get 403.",
    )
    parser.add_argument(
        "--targets",
        required=True,
        help="Path to the targets JSON file (same shape as the scan-targets secret).",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=5.0,
        help="Seconds between job-status polls (default: 5).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=3600.0,
        help="Abort if not all jobs are terminal after this many seconds (default: 3600).",
    )
    args = parser.parse_args()

    base_url = (args.base_url or os.environ.get("CONSTAT_BENCH_BASE_URL") or "").rstrip("/")
    api_key = args.api_key or os.environ.get("CONSTAT_API_KEY") or ""
    if not base_url:
        parser.error("--base-url or CONSTAT_BENCH_BASE_URL is required")
    if not api_key:
        parser.error("--api-key or CONSTAT_API_KEY is required (operator key)")

    with open(args.targets, encoding="utf-8") as fh:
        targets: list[dict[str, Any]] = json.load(fh)
    if not targets:
        parser.error("targets file is an empty array")

    status_counter: Counter[int] = Counter()
    t_start = time.monotonic()

    # One job per target account: per-account wall time stays visible and
    # a poison account does not hide the others' timings.
    jobs: dict[str, dict[str, Any]] = {}
    for target in targets:
        payload = _request(
            "POST",
            f"{base_url}/collect/aws",
            api_key,
            body={"targets": [target], "force": True},
            status_counter=status_counter,
        )
        job_id = _job_id_of(payload)
        jobs[job_id] = {
            "account": target.get("aws_account_id", "?"),
            "submitted_at": time.monotonic(),
            "finished_at": None,
            "detail": None,
        }
        print(f"submitted job {job_id}  account={target.get('aws_account_id', '?')}")

    deadline = t_start + args.timeout
    pending = set(jobs)
    while pending and time.monotonic() < deadline:
        time.sleep(args.poll_interval)
        for job_id in sorted(pending):
            detail = _request(
                "GET",
                f"{base_url}/collect/aws/jobs/{job_id}",
                api_key,
                body=None,
                status_counter=status_counter,
            )
            if _status_of(detail) in TERMINAL_STATUSES:
                jobs[job_id]["finished_at"] = time.monotonic()
                jobs[job_id]["detail"] = detail
                pending.discard(job_id)
                print(
                    f"terminal  job {job_id}  status={_status_of(detail)}  "
                    f"wall={_fmt(jobs[job_id]['finished_at'] - jobs[job_id]['submitted_at'])}"
                )

    if pending:
        print(f"\nTIMEOUT after {_fmt(args.timeout)} — still pending: {sorted(pending)}")

    # --- Report ---
    print("\n=== Per job (account) ===")
    for job_id, info in jobs.items():
        if info["finished_at"] is None:
            print(f"  {info['account']}  job={job_id}  NOT TERMINAL")
            continue
        wall = info["finished_at"] - info["submitted_at"]
        print(
            f"  {info['account']}  job={job_id}  "
            f"status={_status_of(info['detail'])}  wall={_fmt(wall)}"
        )

    print("\n=== Per region (server-side durations from job detail) ===")
    any_region = False
    for info in jobs.values():
        detail = info["detail"] or {}
        for row in _region_rows(detail):
            any_region = True
            region = row.get("region", "?")
            status = row.get("status", "?")
            duration = row.get("duration_seconds", row.get("duration"))
            dur_txt = _fmt(float(duration)) if isinstance(duration, (int, float)) else "     n/a"
            print(f"  {info['account']}  {region:<15}  {status:<10}  {dur_txt}")
    if not any_region:
        print("  (no per-region breakdown found in the job detail — check the")
        print("   GET /collect/aws/jobs/{id} response shape against the router)")

    total = time.monotonic() - t_start
    print("\n=== Totals ===")
    print(f"  jobs submitted : {len(jobs)}")
    print(f"  jobs terminal  : {len(jobs) - len(pending)}")
    print(f"  total wall time: {_fmt(total)}")
    print(
        "  HTTP statuses  : "
        + ", ".join(f"{code}x{n}" for code, n in sorted(status_counter.items()))
    )
    return 1 if pending else 0


if __name__ == "__main__":
    sys.exit(main())
