// Playwright config for Constat V1 web e2e.
//
// The smoke test on /restitution runs against the Next.js dev server
// (auto-started by Playwright via `webServer`). The test uses
// `page.route()` to mock the FastAPI backend, so no real backend
// is required for the test to pass — the test exercises the
// frontend rendering only. The commercial pitch uses this test
// as a smoke check before showing the page to a prospect.
//
// To run:
//   cd apps/web
//   npm install                 # install @playwright/test
//   npx playwright install chromium  # one-time browser install
//   npm run test:e2e
//
// For CI: pin the Playwright version (the @^1.48.0 above is loose)
// and use `--with-deps` on the install command for Linux runners.

import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./tests/e2e",
  // Only the /restitution test exists for now. Keep the file glob
  // explicit so adding a new spec file requires updating this list
  // (forces a deliberate decision).
  testMatch: /restitution\.spec\.ts/,
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: process.env.CI ? "github" : "list",
  use: {
    baseURL: "http://localhost:3000",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
  // Auto-start the Next dev server. The test uses page.route() to
  // mock the backend, so we don't need a real API server up.
  webServer: {
    command: "npm run dev",
    url: "http://localhost:3000",
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
});
