import * as React from "react";
import { act, fireEvent, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { DataTable, type DataTableProps } from "./data-table";
import { AdminErrorCode, type ColumnDef } from "@/lib/admin-api/types";
import { renderWithProviders } from "@/test/render";

interface Row { id: string; name: string }
const rows: Row[] = [{ id: "1", name: "Alpha" }, { id: "2", name: "Beta" }];
const columns: ColumnDef<Row>[] = [{ id: "name", header: "Name", accessor: (row) => row.name, sortable: true }];

function props(overrides: Partial<DataTableProps<Row>> = {}): DataTableProps<Row> {
  return {
    rows,
    columns,
    rowKey: (row) => row.id,
    query: { page: 1, pageSize: 20 },
    onQueryChange: vi.fn(),
    totalRows: 42,
    searchable: true,
    empty: { title: "Nothing here", description: "Create the first item." },
    ariaLabel: "Providers",
    ...overrides,
  };
}

describe("DataTable", () => {
  afterEach(() => vi.useRealTimers());

  it("provides accessible sorting, paging, debounced search, and row actions", async () => {
    vi.useFakeTimers();
    const onQueryChange = vi.fn();
    const action = vi.fn();
    renderWithProviders(<DataTable {...props({
      onQueryChange,
      rowActions: (row) => [{ id: `edit-${row.id}`, label: `Edit ${row.name}`, onSelect: action }],
    })} />);

    expect(screen.getByRole("table", { name: "Providers" })).toBeVisible();
    const nameHeader = screen.getByRole("columnheader", { name: /Name/ });
    expect(nameHeader).toHaveAttribute("aria-sort", "none");
    fireEvent.click(screen.getByRole("button", { name: /Name/ }));
    expect(onQueryChange).toHaveBeenLastCalledWith(expect.objectContaining({ sort: { id: "name", direction: "asc" }, page: 1 }));

    fireEvent.click(screen.getByRole("button", { name: "Next page" }));
    expect(onQueryChange).toHaveBeenLastCalledWith(expect.objectContaining({ page: 2 }));

    fireEvent.change(screen.getByRole("textbox", { name: "Search" }), { target: { value: "alp" } });
    act(() => vi.advanceTimersByTime(299));
    expect(onQueryChange).not.toHaveBeenLastCalledWith(expect.objectContaining({ search: "alp" }));
    act(() => vi.advanceTimersByTime(1));
    expect(onQueryChange).toHaveBeenLastCalledWith(expect.objectContaining({ search: "alp", page: 1 }));

    fireEvent.click(screen.getByRole("button", { name: "Edit Alpha" }));
    expect(action).toHaveBeenCalledOnce();
  });

  it("supports row selection from the keyboard-accessible checkbox", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderWithProviders(<DataTable {...props({ selection: { selected: new Set(), onChange } })} />);
    await user.click(screen.getByRole("checkbox", { name: "Select row 1" }));
    expect(onChange).toHaveBeenCalledWith(new Set(["1"]));
  });

  it("renders loading, empty, and error states", () => {
    const { rerender } = renderWithProviders(<DataTable {...props({ loading: true })} />);
    expect(screen.getByRole("status", { name: "Loading content" })).toBeVisible();

    rerender(<DataTable {...props({ rows: [], totalRows: 0 })} />);
    expect(screen.getByText("Nothing here")).toBeVisible();

    rerender(<DataTable {...props({ rows: [], totalRows: 0, error: {
      code: AdminErrorCode.UNREACHABLE,
      message_key: "admin.common.error.unreachable",
      correlation_id: "corr-123",
    } })} />);
    expect(screen.getByRole("alert")).toHaveTextContent("The service cannot be reached.");
    expect(screen.getByRole("alert")).toHaveTextContent("corr-123");
  });
});
