import { defineConfig, devices } from "@playwright/test";

/* End-to-end against a real stack.
 *
 * Assumes the API is already running (CI starts it as a step, and locally it is
 * the same uvicorn you develop against). Playwright starts only the frontend,
 * because starting the API here would duplicate the seeding the tests depend on
 * and hide which of the two failed.
 *
 * Two projects: desktop and a real mobile viewport. The mobile one is not
 * decoration -- "polished, mobile-friendly" is a promise this repository makes,
 * and a promise no test checks is a wish.
 */
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: process.env.CI ? [["github"], ["list"]] : [["list"]],
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:5173",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    { name: "desktop", use: { ...devices["Desktop Chrome"] } },
    { name: "mobile", use: { ...devices["Pixel 7"] } },
  ],
  webServer: process.env.E2E_BASE_URL
    ? undefined
    : {
        command: "npm run dev",
        url: "http://localhost:5173",
        reuseExistingServer: !process.env.CI,
        timeout: 60_000,
      },
});
