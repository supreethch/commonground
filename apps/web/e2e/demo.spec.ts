import { expect, test } from "@playwright/test";

/* Records the README's demo video.
 *
 * Driven by the same Playwright as the tests, against the same live stack, so
 * the GIF is a recording of the product rather than a mock-up. If the app
 * breaks, this stops producing a video instead of quietly advertising a version
 * that no longer runs.
 *
 * The route is chosen to show the *depth*, not just that it loads:
 *   sign in as one of six seeded listeners
 *   → a room of people with deliberately conflicting taste
 *   → build a playlist, with a reason under every track
 *   → open the per-track satisfaction and see who it serves least
 *   → switch the ranking mode and watch the same room re-rank
 *   → vote, and watch it land
 *
 * Deliberate pauses: a viewer needs time to read a sentence. A recording that
 * moves at the speed the machine can click is unreadable.
 */

test.use({
  video: { mode: "on", size: { width: 1120, height: 760 } },
  viewport: { width: 1120, height: 760 },
});

const BEAT = 900;

test("demo recording", async ({ page }) => {
  await page.goto("/");
  await page.waitForTimeout(BEAT);

  await page.getByRole("button", { name: "Try the demo account" }).click();
  await expect(page.getByRole("heading", { name: "Rooms" })).toBeVisible();
  await page.waitForTimeout(BEAT);

  await page.getByRole("link", { name: /The car/ }).click();
  await expect(page.getByRole("heading", { name: "The car" })).toBeVisible();
  await page.waitForTimeout(BEAT * 1.4);

  // Build the playlist.
  await page.getByRole("button", { name: /Build playlist|Regenerate/ }).click();
  await expect(page.locator("article").first()).toBeVisible({ timeout: 30_000 });
  await page.waitForTimeout(BEAT * 1.6);

  // Read a couple of reasons.
  await page.mouse.wheel(0, 260);
  await page.waitForTimeout(BEAT * 1.6);

  // Who does track one serve least?
  await page.getByRole("button", { name: "Predicted satisfaction per member" }).first().click();
  await page.waitForTimeout(BEAT * 2.2);
  await page.getByRole("button", { name: "Predicted satisfaction per member" }).first().click();
  await page.waitForTimeout(BEAT * 0.5);

  // Vote, and watch the tally move.
  const firstTrack = page.locator("article").first();
  await firstTrack.getByRole("button", { name: "Vote up" }).click();
  await expect(firstTrack.getByText("+1")).toBeVisible();
  await page.waitForTimeout(BEAT * 1.3);

  // Back to the top, then re-rank the same room a different way.
  await page.mouse.wheel(0, -400);
  await page.waitForTimeout(BEAT);

  await page.getByRole("radio", { name: "Discovery" }).click();
  await expect(page.locator("article").first()).toBeVisible({ timeout: 30_000 });
  await page.waitForTimeout(BEAT * 2);

  await page.getByRole("radio", { name: "Consensus" }).click();
  await expect(page.locator("article").first()).toBeVisible({ timeout: 30_000 });
  await page.waitForTimeout(BEAT * 2.2);

  // End on the summary: the floor, and who it belongs to.
  await page.mouse.wheel(0, 120);
  await page.waitForTimeout(BEAT * 2);
});
