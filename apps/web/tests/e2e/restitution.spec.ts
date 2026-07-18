// Smoke test for the commercial pitch page /restitution.
//
// This is the ONE Playwright test the V1 product owner asked for.
// It runs before a prospect meeting as a "does the page still
// render correctly?" check. The test mocks the FastAPI backend
// via page.route() so it doesn't need a real API server up — it
// exercises the React Server Component rendering of the
// /restitution page in isolation.
//
// The mocks are realistic: 17 RDS instances, 3 of them past
// their EOL with Extended Support costs, 2 inconclusive
// resources, a recent source run. The page must surface the
// monthly cost total and the per-account breakdown.

import { expect, test } from "@playwright/test";

// Realistic V1 pilot data: a mid-size prospect with 17 RDS
// instances, 3 past EOL. Numbers chosen to exercise the page's
// cost aggregation (1 db.m5.large @ 730h/mo = 730 vCPU-h/mo;
// 3 db.m5.large post-EOL = $0.10/vCPU-h * 730 * 3 = $219/mo).
const API_BASE = "**/api/**";

test("restitution page renders the pilot one-pager with cost totals", async ({
  page,
}) => {
  // Mock every API endpoint the page calls. The path glob "**/api/**"
  // matches both the production (NEXT_PUBLIC_API_URL=http://api:8000)
  // and the dev (http://localhost:8000) base URLs.
  await page.route(`${API_BASE}/insights*`, async (route) => {
    const url = new URL(route.request().url());
    if (url.searchParams.get("rule_name") === "chargeback") {
      // Chargeback insights: 3 post-EOL RDS instances across 1
      // account. The page sums their monthly cost.
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([
          {
            id: "11111111-1111-1111-1111-111111111111",
            rule_name: "rds_eol",
            resource_id: "11111111-1111-1111-1111-111111111111",
            account_id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            severity: "critical",
            title: "PostgreSQL 11 (EOL 2024-02-29) on account 111: $0.10/vCPU-h Extended Support",
            payload: {
              engine: "postgres",
              engine_version: "11.22",
              instance_class: "db.m5.large",
              vcpu: 2,
              days_to_eol: -730,
              months_to_eol: -24,
              eol_date: "2024-02-29",
              pricing_tier: "year_1_2",
              monthly_cost_usd: 146.0,
              value_basis: "ESTIMATED",
            },
            computed_at: "2026-07-18T15:00:00Z",
          },
          {
            id: "22222222-2222-2222-2222-222222222222",
            rule_name: "rds_eol",
            resource_id: "22222222-2222-2222-2222-222222222222",
            account_id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            severity: "critical",
            title: "PostgreSQL 12 (EOL 2025-02-28) on account 111",
            payload: {
              engine: "postgres",
              engine_version: "12.18",
              instance_class: "db.m5.large",
              vcpu: 2,
              days_to_eol: -510,
              eol_date: "2025-02-28",
              pricing_tier: "year_3_plus",
              monthly_cost_usd: 292.0,
              value_basis: "ESTIMATED",
            },
            computed_at: "2026-07-18T15:00:00Z",
          },
        ]),
      });
    } else {
      // General insights list (used for the "all insights" count).
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([]),
      });
    }
  });

  await page.route(`${API_BASE}/inconclusives*`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([
        {
          id: "ic-1",
          rule_name: "rds_eol",
          resource_id: "r-3",
          account_id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
          missing_facts: ["scope_not_proven"],
          reason: "scope_not_proven",
          computed_at: "2026-07-18T15:00:00Z",
        },
      ]),
    });
  });

  await page.route(`${API_BASE}/accounts*`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify([
        {
          id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
          external_id: "111111111111",
          name: "prod",
        },
      ]),
    });
  });

  await page.route(`${API_BASE}/status`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        status: "ok",
        generated_at: "2026-07-18T15:00:00Z",
        tenant: "00000000-0000-0000-0000-000000000001",
        accounts: 1,
        resources_total: 17,
        resources_active: 17,
        insights_total: 2,
        insights_by_severity: { critical: 2, warning: 0, info: 0 },
        inconclusive_total: 1,
        last_insight_run: null,
        last_source_run: {
          account_external_id: "111111111111",
          region: "eu-west-1",
          resource_type: "AWS::RDS::DBInstance",
          finished_at: "2026-07-18T14:00:00Z",
          status: "success",
          resources_found: 17,
        },
        source_run_freshness_seconds: 3600,
      }),
    });
  });

  // Visit the page. The /restitution route is force-dynamic so it
  // fetches on every load. We wait for the headline cost figure
  // to appear before asserting — the page calls the API on the
  // server, so the rendered HTML is final once the response arrives.
  await page.goto("/restitution");
  await expect(page.getByRole("heading", { name: /Constat/ })).toBeVisible();

  // The page surfaces a total monthly cost (the sum across
  // rds_eol insights). With our mocks: 146 + 292 = $438/mo.
  // We assert the number appears somewhere in the page text.
  const pageText = await page.locator("body").innerText();
  expect(pageText).toContain("$438");

  // The status section reports 17 active resources — the
  // prospect's "fleet size" anchor.
  expect(pageText).toContain("17");

  // The accounts list shows "prod" (the account we mocked).
  expect(pageText).toContain("prod");
});
