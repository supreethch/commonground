import { expect, test } from "@playwright/test";

/* The critical path, end to end: the demo account signs in, opens the seeded
 * room, builds a playlist and votes on it.
 *
 * This is the walk-through a recruiter does. If it breaks, the demo is broken,
 * which is not something the unit tests can tell us -- they mock the network,
 * and every interesting failure in this project so far has been at a boundary.
 */

test.describe("the demo walk-through", () => {
  test("signs in, opens the room, builds a playlist and votes", async ({ page }) => {
    await page.goto("/");

    await expect(page.getByRole("heading", { name: "CommonGround" })).toBeVisible();
    await page.getByRole("button", { name: "Try the demo account" }).click();

    // Lands on the rooms list with the seeded room.
    await expect(page.getByRole("heading", { name: "Rooms" })).toBeVisible();
    const room = page.getByRole("link", { name: /The car/ });
    await expect(room).toBeVisible();
    await room.click();

    await expect(page.getByRole("heading", { name: "The car" })).toBeVisible();
    // Six seeded personas with deliberately conflicting taste.
    await expect(page.getByText("Alex", { exact: false }).first()).toBeVisible();

    // The socket reports itself connected rather than the UI assuming it.
    await expect(page.getByText("Live")).toBeVisible({ timeout: 15_000 });

    const build = page.getByRole("button", { name: /Build playlist|Regenerate/ });
    await build.click();

    const firstTrack = page.locator("article").first();
    await expect(firstTrack).toBeVisible({ timeout: 30_000 });

    // Every track carries a reason. That is the product's whole claim, so an
    // empty one is a failure even if the list rendered.
    await expect(firstTrack.getByText(/Recommended because/)).toBeVisible();

    const upvote = firstTrack.getByRole("button", { name: "Vote up" });
    await upvote.click();
    await expect(upvote).toHaveAttribute("aria-pressed", "true");
    await expect(firstTrack.getByText("+1")).toBeVisible();

    // Clicking the held vote retracts it.
    await upvote.click();
    await expect(upvote).toHaveAttribute("aria-pressed", "false");
  });

  test("shows who each track serves least", async ({ page }) => {
    await page.goto("/");
    await page.getByRole("button", { name: "Try the demo account" }).click();
    await page.getByRole("link", { name: /The car/ }).click();

    const build = page.getByRole("button", { name: /Build playlist|Regenerate/ });
    await build.click();
    await expect(page.locator("article").first()).toBeVisible({ timeout: 30_000 });

    await page
      .getByRole("button", { name: "Predicted satisfaction per member" })
      .first()
      .click();

    await expect(page.getByText("Predicted satisfaction")).toBeVisible();
    await expect(page.getByText(/is served least by this track/)).toBeVisible();
  });

  test("refuses a bad invite link with a usable message", async ({ page }) => {
    // A real account, not the demo one: the demo account is blocked from
    // joining at all, so it would get the read-only message and this test would
    // pass without ever exercising the invalid-invite path.
    const email = `e2e-${Date.now()}-${Math.random().toString(36).slice(2, 8)}@example.com`;
    await page.goto("/");
    await page.getByRole("button", { name: "Create one" }).click();
    await page.getByLabel("Name").fill("E2E");
    await page.getByLabel("Email").fill(email);
    await page.getByLabel("Password").fill("correct horse battery staple");
    await page.getByRole("button", { name: "Create account" }).click();
    await expect(page.getByRole("heading", { name: "Rooms" })).toBeVisible();

    await page.goto("/#/join/not-a-real-token");

    // The server's own wording, not a generic "something went wrong": an
    // expired invite and a full room need different responses from the reader.
    await expect(page.getByRole("alert")).toContainText(/invite is not valid/i);
  });
});
