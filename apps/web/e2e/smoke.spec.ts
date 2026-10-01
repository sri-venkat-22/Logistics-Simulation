import { expect, test } from "@playwright/test";

test("demo flow: live data, sign in, Copilot, trust pipeline, cascade", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByText("LIVE · API")).toBeVisible();

  // sign in (OAuth2 password flow -> JWT) with the development planner
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.getByLabel("Username").fill("planner");
  await page.getByLabel("Password").fill("aegis-planner");
  await page.getByRole("button", { name: "Sign in", exact: true }).last().click();
  await expect(page.getByRole("button", { name: "Sign out" })).toBeVisible();
  await expect(page.getByRole("banner").getByText("planner", { exact: true }).first()).toBeVisible();

  // the Copilot answers from a tool call
  await page.getByRole("button", { name: /Copilot/ }).click();
  await page.getByRole("button", { name: "Which nodes are most at risk?" }).click();
  await expect(page.getByText("find_at_risk_nodes")).toBeVisible();
  await expect(page.getByText(/Structural risk/)).toBeVisible();
  await page.keyboard.press("Escape");

  // the live Trust Center shows the 9 layers and the red-team benchmark
  await page.goto("/#/trust");
  await expect(page.getByText("Trust pipeline · 9 layers")).toBeVisible();
  await expect(page.getByText("Kalman", { exact: false }).first()).toBeVisible();
  await expect(page.getByText("Red-team benchmark")).toBeVisible();

  // a Motter-Lai cascade from the live API
  await page.goto("/#/network");
  await page.getByRole("button", { name: /^Fail / }).click();
  await expect(page.getByText(/Cascade from/)).toBeVisible();

  // measured model quality
  await page.goto("/#/fidelity");
  await expect(page.getByText("ETA models · LightGBM quantiles")).toBeVisible();
});
