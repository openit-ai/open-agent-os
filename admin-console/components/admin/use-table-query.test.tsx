import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderWithProviders } from "@/test/render";
import { useTableQuery, useUrlFilter } from "./use-table-query";

const push = vi.fn();
let params = new URLSearchParams();

vi.mock("next/navigation", () => ({
  usePathname: () => "/control/audit",
  useRouter: () => ({ push }),
  useSearchParams: () => params,
}));

function Harness() {
  const table = useTableQuery();
  const [risk, setRisk] = useUrlFilter("risk", "all");
  return (
    <>
      <output>{JSON.stringify(table.query)}</output>
      <button onClick={() => table.onQueryChange({ search: "actor", sort: { id: "timestamp", direction: "desc" }, page: 3, pageSize: 50 })}>table</button>
      <button onClick={() => setRisk("HIGH")}>risk</button>
      <span>{risk}</span>
    </>
  );
}

describe("Phase 4 URL list query", () => {
  beforeEach(() => {
    push.mockReset();
    params = new URLSearchParams("search=alice&sort=timestamp&direction=desc&page=2&pageSize=50&risk=LOW");
  });

  it("hydrates search, sort, page, page size, and filters from URL and preserves unrelated conditions", async () => {
    const user = userEvent.setup();
    renderWithProviders(<Harness />);
    expect(screen.getByText(/"search":"alice"/)).toHaveTextContent('"page":2');
    expect(screen.getByText(/"search":"alice"/)).toHaveTextContent('"pageSize":50');
    expect(screen.getByText("LOW")).toBeVisible();

    await user.click(screen.getByRole("button", { name: "table" }));
    expect(push).toHaveBeenLastCalledWith(
      "/control/audit?search=actor&sort=timestamp&direction=desc&page=3&pageSize=50&risk=LOW",
      { scroll: false },
    );
    await user.click(screen.getByRole("button", { name: "risk" }));
    expect(push).toHaveBeenLastCalledWith(
      "/control/audit?search=alice&sort=timestamp&direction=desc&pageSize=50&risk=HIGH",
      { scroll: false },
    );
  });
});
