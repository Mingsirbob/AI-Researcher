import { expect, test } from "@playwright/test";

const routes = ["paper", "company", "theses", "quant", "factor-development", "factor-evaluation", "backtest", "factor-library", "decisions", "acceptance"];

for (const route of routes) {
  test(`${route} renders without runtime errors or horizontal overflow`, async ({ page }) => {
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    page.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });
    await page.goto(`/next/${route}`);
    await page.waitForLoadState("networkidle");
    await expect(page.locator("main.workspace")).toBeVisible();
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow).toBeLessThanOrEqual(1);
    expect(errors).toEqual([]);
  });
}

test("retired legacy route is not available", async ({ request }) => {
  const response = await request.get("/legacy");
  expect(response.status()).toBe(404);
});
