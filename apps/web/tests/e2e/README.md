# Web end-to-end tests (Playwright)

V1 has one Playwright test: `restitution.spec.ts`. It runs before
a prospect meeting as a "does the page still render?" smoke check.

## What it covers

The `/restitution` page is the one-pager the commercial shows
during the pitch. It summarizes the prospect's fleet, the gaps
found (post-EOL RDS instances with their Extended Support cost),
the inconclusive records, and a freshness indicator. The test:

- Mocks the FastAPI backend via `page.route()` (no real API needed)
- Visits `/restitution` against a fresh Next.js dev server
- Asserts the rendered HTML contains the cost total
  (sum of the per-instance monthly Extended Support fees),
  the fleet size (resources count), and the account name

If this test passes before a pitch, the page is rendering. If it
fails, the prospect demo is at risk — debug the failure
(Playwright captures the trace on `retain-on-failure`).

## Local setup (one-time)

```bash
cd apps/web
npm install                  # adds @playwright/test
npx playwright install chromium  # one-time browser install (~150 MB)
```

## Run

```bash
cd apps/web
npm run test:e2e
```

Playwright auto-starts the Next dev server (see
`playwright.config.ts` `webServer` block). The test mocks the
FastAPI backend with `page.route()` so no real API is needed.

## CI

The `webServer` block reuses an existing dev server when not in
CI, and starts one when `process.env.CI` is set. Pin the
Playwright version (`@^1.48.0` is loose) for reproducible CI.
On Linux runners, run `npx playwright install --with-deps
chromium` so the browser's system deps are present.

## Adding more tests

The current `testMatch` is `/restitution\.spec\.ts/`. To add more
specs, update the glob in `playwright.config.ts`. Keep the
smoke-test-on-/-restitution invariant explicit; the page is the
single most visible thing in the V1 product.
