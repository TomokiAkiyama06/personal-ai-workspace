import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App } from "../App";
import { ApiError } from "../api/client";
import {
  abnormalReport,
  fakeHealthSource,
  healthSummary,
  normalReport,
} from "../test/healthFixture";
import { mockApi, Providers, reply, session } from "../test/helpers";
import { type HealthSource, HealthSourceProvider } from "./model";

afterEach(() => {
  vi.unstubAllGlobals();
});

function renderShell(source: HealthSource | null, role: string) {
  mockApi({ "GET /auth/session": reply(200, session({ role })) });
  window.history.replaceState(null, "", "/memory");
  return render(
    <Providers>
      <HealthSourceProvider source={source}>
        <App />
      </HealthSourceProvider>
    </Providers>,
  );
}

describe("the header's health chip", () => {
  it("shows GPU and the queue to an Admin and opens サーバー監視", async () => {
    const source = fakeHealthSource(normalReport());
    const summary = vi.spyOn(source, "summary");
    renderShell(source, "admin");
    const chip = await screen.findByRole("link", {
      name: "システムの状態: 正常 · GPU 38% · Queue 3",
    });
    expect(chip).toHaveAttribute("href", "/admin/monitoring");
    expect(chip).toHaveTextContent("GPU 38% · Queue 3");
    // The detail is read for an Admin; the summary is for everyone else.
    expect(summary).not.toHaveBeenCalled();
  });

  it("names the worst component when something is abnormal", async () => {
    renderShell(fakeHealthSource(abnormalReport()), "owner");
    const chip = await screen.findByRole("link", { name: /システムの状態: 異常/ });
    expect(chip).toHaveTextContent("Backup 異常 +1");
    expect(chip).toHaveClass("sev-error");
  });

  it("shows a Member only the summary, with nothing to open", async () => {
    const source = fakeHealthSource(normalReport());
    const report = vi.spyOn(source, "report");
    renderShell(source, "user");
    const chip = await screen.findByRole("status", {
      name: "システムの状態: 正常 · Codex · Claude 利用可",
    });
    expect(chip.tagName).toBe("SPAN");
    expect(report).not.toHaveBeenCalled();
  });

  it("tells a Member which external agent is unavailable", async () => {
    renderShell(
      fakeHealthSource(normalReport(), {
        summary: async () =>
          healthSummary({
            severity: "warning",
            connections: { codex: "available", claude: "unavailable" },
          }),
      }),
      "user",
    );
    expect(
      await screen.findByRole("status", { name: "システムの状態: 警告 · Claude 利用不可" }),
    ).toBeVisible();
  });

  it("shows nothing without a source or when the state cannot be read", async () => {
    const { unmount } = renderShell(null, "owner");
    expect(await screen.findByRole("banner")).toBeVisible();
    expect(screen.queryByRole("link", { name: /システムの状態/ })).not.toBeInTheDocument();
    unmount();
    const report = vi.fn(async () => {
      throw new ApiError(403, "forbidden", "x");
    });
    renderShell(fakeHealthSource(normalReport(), { report }), "owner");
    await waitFor(() => expect(report).toHaveBeenCalled());
    expect(screen.queryByRole("link", { name: /システムの状態/ })).not.toBeInTheDocument();
  });
});
