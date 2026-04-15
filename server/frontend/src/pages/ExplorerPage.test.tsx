import { describe, it, expect, beforeEach, vi, afterEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router";
import { ExplorerPage } from "./ExplorerPage";
import type { ReviewItem, ReviewStatsResponse } from "../types";

const originalFetch = globalThis.fetch;

function makeUnit(overrides: Partial<{
  id: string;
  domains: string[];
  summary: string;
  detail: string;
  action: string;
  confidence: number;
  confirmations: number;
  languages: string[];
  frameworks: string[];
  created_by: string;
}>): ReviewItem {
  return {
    knowledge_unit: {
      id: overrides.id ?? "ku_test1",
      version: 1,
      domains: overrides.domains ?? ["python"],
      insight: {
        summary: overrides.summary ?? "Test summary",
        detail: overrides.detail ?? "Test detail",
        action: overrides.action ?? "Test action",
      },
      context: {
        languages: overrides.languages ?? [],
        frameworks: overrides.frameworks ?? [],
        pattern: "",
      },
      evidence: {
        confidence: overrides.confidence ?? 0.8,
        confirmations: overrides.confirmations ?? 3,
        first_observed: "2024-06-01T00:00:00Z",
        last_confirmed: "2024-06-10T00:00:00Z",
      },
      tier: "private",
      created_by: overrides.created_by ?? "agent",
      superseded_by: null,
      flags: [],
    },
    status: "approved",
    reviewed_by: "demo",
    reviewed_at: "2024-06-05T00:00:00Z",
  };
}

const MOCK_STATS: ReviewStatsResponse = {
  counts: { pending: 2, approved: 5, rejected: 1 },
  domains: { python: 3, clickhouse: 2, postgres: 1 },
  confidence_distribution: { "0.0-0.3": 0, "0.3-0.6": 1, "0.6-0.8": 2, "0.8-1.0": 2 },
  recent_activity: [],
  trends: { daily: [] },
};

const MOCK_UNITS: ReviewItem[] = [
  makeUnit({ id: "ku_1", domains: ["python", "debugging"], summary: "Mutable defaults persist", detail: "Python mutable default arguments", action: "Use None sentinel", confidence: 0.85 }),
  makeUnit({ id: "ku_2", domains: ["clickhouse"], summary: "MergeTree ordering", detail: "ORDER BY determines sort", action: "Choose ORDER BY carefully", confidence: 0.72, languages: ["sql"] }),
  makeUnit({ id: "ku_3", domains: ["postgres", "python"], summary: "SSL required on Aiven", detail: "Aiven Postgres needs sslmode=require", action: "Add sslmode=require", confidence: 0.5 }),
];

function mockFetchResponses(statsResp: ReviewStatsResponse, unitsResp: ReviewItem[]) {
  globalThis.fetch = vi.fn().mockImplementation((url: string) => {
    if (url.includes("/review/stats")) {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve(statsResp),
      });
    }
    if (url.includes("/review/units")) {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve(unitsResp),
      });
    }
    return Promise.resolve({
      ok: true,
      status: 200,
      json: () => Promise.resolve({}),
    });
  });
}

function renderExplorer() {
  return render(
    <MemoryRouter initialEntries={["/explore"]}>
      <Routes>
        <Route path="/explore" element={<ExplorerPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("ExplorerPage", () => {
  beforeEach(() => {
    localStorage.setItem("cq_auth_token", "test-jwt");
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
    vi.restoreAllMocks();
    localStorage.clear();
  });

  it("shows loading skeleton initially", () => {
    let resolveStats!: (v: Response) => void;
    globalThis.fetch = vi.fn().mockReturnValue(
      new Promise<Response>((resolve) => { resolveStats = resolve; }),
    );

    renderExplorer();
    expect(screen.getByTestId("explorer-skeleton")).toBeInTheDocument();

    resolveStats({
      ok: true,
      status: 200,
      json: () => Promise.resolve(MOCK_STATS),
    } as Response);
  });

  it("renders domain cloud and knowledge cards after loading", async () => {
    mockFetchResponses(MOCK_STATS, MOCK_UNITS);
    renderExplorer();

    await waitFor(() => {
      expect(screen.getByTestId("domain-cloud")).toBeInTheDocument();
    });

    expect(screen.getByTestId("domain-tag-python")).toBeInTheDocument();
    expect(screen.getByTestId("domain-tag-clickhouse")).toBeInTheDocument();
    expect(screen.getByTestId("domain-tag-postgres")).toBeInTheDocument();

    expect(screen.getByText("Mutable defaults persist")).toBeInTheDocument();
    expect(screen.getByText("MergeTree ordering")).toBeInTheDocument();
    expect(screen.getByText("SSL required on Aiven")).toBeInTheDocument();

    expect(screen.getByTestId("result-count")).toHaveTextContent("3 units");
  });

  it("filters units by text search", async () => {
    mockFetchResponses(MOCK_STATS, MOCK_UNITS);
    const user = userEvent.setup();
    renderExplorer();

    await waitFor(() => {
      expect(screen.getByTestId("domain-cloud")).toBeInTheDocument();
    });

    const searchInput = screen.getByPlaceholderText("Search knowledge units...");
    await user.type(searchInput, "MergeTree");

    expect(screen.getByText("MergeTree ordering")).toBeInTheDocument();
    expect(screen.queryByText("Mutable defaults persist")).not.toBeInTheDocument();
    expect(screen.queryByText("SSL required on Aiven")).not.toBeInTheDocument();
    expect(screen.getByTestId("result-count")).toHaveTextContent("1 unit of 3");
  });

  it("filters units by domain selection", async () => {
    mockFetchResponses(MOCK_STATS, MOCK_UNITS);
    const user = userEvent.setup();
    renderExplorer();

    await waitFor(() => {
      expect(screen.getByTestId("domain-cloud")).toBeInTheDocument();
    });

    await user.click(screen.getByTestId("domain-tag-python"));

    expect(screen.getByText("Mutable defaults persist")).toBeInTheDocument();
    expect(screen.getByText("SSL required on Aiven")).toBeInTheDocument();
    expect(screen.queryByText("MergeTree ordering")).not.toBeInTheDocument();
    expect(screen.getByTestId("result-count")).toHaveTextContent("2 units of 3");
  });

  it("supports multi-domain AND filter", async () => {
    mockFetchResponses(MOCK_STATS, MOCK_UNITS);
    const user = userEvent.setup();
    renderExplorer();

    await waitFor(() => {
      expect(screen.getByTestId("domain-cloud")).toBeInTheDocument();
    });

    await user.click(screen.getByTestId("domain-tag-python"));
    await user.click(screen.getByTestId("domain-tag-postgres"));

    expect(screen.getByText("SSL required on Aiven")).toBeInTheDocument();
    expect(screen.queryByText("Mutable defaults persist")).not.toBeInTheDocument();
    expect(screen.getByTestId("result-count")).toHaveTextContent("1 unit of 3");
  });

  it("toggles domain off when clicked again", async () => {
    mockFetchResponses(MOCK_STATS, MOCK_UNITS);
    const user = userEvent.setup();
    renderExplorer();

    await waitFor(() => {
      expect(screen.getByTestId("domain-cloud")).toBeInTheDocument();
    });

    await user.click(screen.getByTestId("domain-tag-clickhouse"));
    expect(screen.getByTestId("result-count")).toHaveTextContent("1 unit of 3");

    await user.click(screen.getByTestId("domain-tag-clickhouse"));
    expect(screen.getByTestId("result-count")).toHaveTextContent("3 units");
  });

  it("clears all filters via Clear button", async () => {
    mockFetchResponses(MOCK_STATS, MOCK_UNITS);
    const user = userEvent.setup();
    renderExplorer();

    await waitFor(() => {
      expect(screen.getByTestId("domain-cloud")).toBeInTheDocument();
    });

    const searchInput = screen.getByPlaceholderText("Search knowledge units...");
    await user.type(searchInput, "ssl");
    expect(screen.getByTestId("result-count")).toHaveTextContent("1 unit of 3");

    await user.click(screen.getByRole("button", { name: "Clear" }));
    expect(screen.getByTestId("result-count")).toHaveTextContent("3 units");
    expect(searchInput).toHaveValue("");
  });

  it("expands and collapses a knowledge card", async () => {
    mockFetchResponses(MOCK_STATS, MOCK_UNITS);
    const user = userEvent.setup();
    renderExplorer();

    await waitFor(() => {
      expect(screen.getByText("Mutable defaults persist")).toBeInTheDocument();
    });

    expect(screen.queryByText("Python mutable default arguments")).not.toBeInTheDocument();

    const card = screen.getByTestId("knowledge-card-ku_1");
    const expandButton = within(card).getByRole("button", { expanded: false });
    await user.click(expandButton);

    expect(screen.getByText("Python mutable default arguments")).toBeInTheDocument();
    expect(screen.getByText("Use None sentinel")).toBeInTheDocument();

    await user.click(within(card).getByRole("button", { expanded: true }));
    expect(screen.queryByText("Python mutable default arguments")).not.toBeInTheDocument();
  });

  it("shows empty state when no approved units exist", async () => {
    mockFetchResponses(MOCK_STATS, []);
    renderExplorer();

    await waitFor(() => {
      expect(screen.getByTestId("empty-state")).toBeInTheDocument();
    });
    expect(screen.getByTestId("empty-state")).toHaveTextContent("No approved knowledge units yet.");
  });

  it("shows filtered empty state with clear button", async () => {
    mockFetchResponses(MOCK_STATS, MOCK_UNITS);
    const user = userEvent.setup();
    renderExplorer();

    await waitFor(() => {
      expect(screen.getByTestId("domain-cloud")).toBeInTheDocument();
    });

    const searchInput = screen.getByPlaceholderText("Search knowledge units...");
    await user.type(searchInput, "nonexistent_query_xyz");

    expect(screen.getByTestId("empty-state")).toHaveTextContent("No knowledge units match your filters.");
    const clearBtn = screen.getByRole("button", { name: "Clear filters" });
    await user.click(clearBtn);
    expect(screen.getByTestId("result-count")).toHaveTextContent("3 units");
  });

  it("shows error state when API fails", async () => {
    globalThis.fetch = vi.fn().mockRejectedValue(new Error("Network error"));
    renderExplorer();

    await waitFor(() => {
      expect(screen.getByText("Failed to load knowledge base")).toBeInTheDocument();
    });
  });

  it("searches across detail and action text too", async () => {
    mockFetchResponses(MOCK_STATS, MOCK_UNITS);
    const user = userEvent.setup();
    renderExplorer();

    await waitFor(() => {
      expect(screen.getByTestId("domain-cloud")).toBeInTheDocument();
    });

    const searchInput = screen.getByPlaceholderText("Search knowledge units...");
    await user.type(searchInput, "sslmode");

    expect(screen.getByText("SSL required on Aiven")).toBeInTheDocument();
    expect(screen.queryByText("Mutable defaults persist")).not.toBeInTheDocument();
    expect(screen.getByTestId("result-count")).toHaveTextContent("1 unit of 3");
  });

  it("shows context languages and frameworks in expanded card", async () => {
    mockFetchResponses(MOCK_STATS, MOCK_UNITS);
    const user = userEvent.setup();
    renderExplorer();

    await waitFor(() => {
      expect(screen.getByText("MergeTree ordering")).toBeInTheDocument();
    });

    const card = screen.getByTestId("knowledge-card-ku_2");
    await user.click(within(card).getByRole("button", { expanded: false }));

    expect(screen.getByText(/Languages:.*sql/)).toBeInTheDocument();
  });

  it("domain cloud filters when text search is active", async () => {
    mockFetchResponses(MOCK_STATS, MOCK_UNITS);
    const user = userEvent.setup();
    renderExplorer();

    await waitFor(() => {
      expect(screen.getByTestId("domain-cloud")).toBeInTheDocument();
    });

    const searchInput = screen.getByPlaceholderText("Search knowledge units...");
    await user.type(searchInput, "click");

    expect(screen.getByTestId("domain-tag-clickhouse")).toBeInTheDocument();
    expect(screen.queryByTestId("domain-tag-postgres")).not.toBeInTheDocument();
  });
});
