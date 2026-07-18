# API key rotation

> How to rotate `CONSTAT_API_KEY` and `CONSTAT_METRICS_KEY` without
> locking out the web app or the Prometheus scraper. V1 is a single
> shared key per surface (see `apps/api/src/constat_api/auth.py`), read
> once at process start — rotation always means a restart.

## Where the values live

Both keys are plain environment variables, read once at import time into
the frozen `Settings` dataclass
(`apps/api/src/constat_api/settings.py`). There is no reload endpoint:
a key change only takes effect on process restart.

- **Local dev:** `.env` (copied from `.env.example`, never committed —
  it is in `.gitignore`). When `CONSTAT_API_KEY` is unset, auth is open
  and a warning is logged at startup. Never deploy that way.
- **Pilot deployment (single Fargate task):** task-definition
  environment. Store the values in AWS Secrets Manager and inject them
  as task secrets rather than plaintext env vars; per AGENTS.md the
  dedicated secrets-manager wiring is V2, but the values themselves
  should never sit in a repo or a ticket.

`CONSTAT_METRICS_KEY` gates only `/metrics` (header `X-Metrics-Key`).
When it is unset, `/metrics` is open on the trusted network, same trust
model as `/health` — see [`metrics.md`](./metrics.md).

## The honest constraint: no true zero-downtime in V1

One API process, one shared key per surface, compared in constant time.
There is no dual-key window (old + new both valid). Every rotation has
a short mismatch window where one side presents the new key and the
other still expects the old one. The procedures below minimize that
window; schedule rotations in a low-traffic slot.

## Rotating CONSTAT_METRICS_KEY

Do the scraper first — Prometheus scrapes every interval, so a
mismatch here is self-healing noise, not a user-facing outage.

1. Add the new value to Secrets Manager (or your env source).
2. Update the Prometheus scrape config to send the new key in the
   `X-Metrics-Key` header. On recent Prometheus versions that is the
   `http_headers` scrape option:

   ```yaml
   scrape_configs:
     - job_name: constat
       static_configs:
         - targets: ['constat-api:8000']
       metrics_path: /metrics
       http_headers:
         X-Metrics-Key:
           values: ['<new-key>']
   ```

   (On older Prometheus without `http_headers`, put a small reverse
   proxy in front of `/metrics` that injects the header.)

   `promtool check config prometheus.yml`, then reload Prometheus
   (`kill -HUP` or the `/-/reload` endpoint).
3. Restart the API with `CONSTAT_METRICS_KEY=<new-key>`.
4. Between steps 2 and 3 the scrapes fail with 401 — at most one
   scrape interval (30 s in the reference config), then they recover.
   Confirm via `up{job="constat"}` and the
   `constat_http_requests_total{status="401"}` counter returning to
   zero rate.

## Rotating CONSTAT_API_KEY

The client is the Next.js web app (and any scripts using the CLI over
HTTP). A mismatched key means user-facing 401s, so keep the window to
seconds:

1. Add the new value to Secrets Manager (or your env source).
2. Stage the web app's new key (env var for the Next.js server side) so
   it is ready to deploy.
3. In one coordinated step: restart the API with
   `CONSTAT_API_KEY=<new-key>`, then immediately redeploy the web app
   with the matching key.
4. Verify: `curl -H "X-API-Key: <new-key>" https://<api>/insights`
   returns 200, and the same call with the old key returns 401.

## Rollback

Both keys are stateless — there is nothing to unwind in the database.

- **API key:** restart the API with the old `CONSTAT_API_KEY`, redeploy
  the web app with the old key.
- **Metrics key:** restart the API with the old `CONSTAT_METRICS_KEY`,
  revert the scrape config, reload Prometheus.

If the old value was rotated out because it leaked, rollback is only a
diagnostic step — rotate forward to a third key instead of staying on
the compromised one.

## Cadence

- **Scheduled:** every 90 days.
- **Event-driven, immediately:** suspected leak (key in a log line, a
  ticket, a screenshot, a committed file), offboarding of anyone who
  knew the value, or any unexplained 2xx from a client you don't
  recognize in the access log.

## Audit trail — what is actually true today

- **Auth failures surface in logs.** `auth.py` itself logs nothing, but
  the request middleware emits a `request.complete` access line per
  request, and 4xx statuses log at warning level (see
  [`logging.md`](./logging.md)). A spike of 401s after a rotation is
  how you spot a client still on the old key.
- **`audit_events` does NOT log key usage.** The table records
  privileged *operations* (scans, insight runs, retention, cleanup) with
  `system:*` actors. `audit.py::actor_for_api_key` exists — it formats
  an actor as `api_key:<sha256[:16]>` so a key can be identified without
  storing it — but no request path calls it in V1. "Which key accessed
  what" is not answerable from `audit_events` today; that wiring is V2
  work.

## See also

- [`metrics.md`](./metrics.md) — the `/metrics` endpoint and scrape config
- [`logging.md`](./logging.md) — the access log where 401s surface
- [`../development/known-issues.md`](../development/known-issues.md) —
  V1 auth limitations
