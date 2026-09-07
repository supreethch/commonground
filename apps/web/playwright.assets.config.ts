import base from "./playwright.config";
import { defineConfig } from "@playwright/test";

/* Regenerates the README's screenshots and demo GIF.
 *
 * Separate from the test config so CI never runs it: these specs drive the app
 * to produce assets rather than to assert anything about it, and a failure here
 * means a stale image, not a broken product.
 *
 *   npm run assets   (with the API and dev server already running)
 */
export default defineConfig({
  ...base,
  testIgnore: [],
  testMatch: ["**/screenshots.spec.ts", "**/demo.spec.ts"],
});
