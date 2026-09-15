import { describe, expect, it } from "vitest";
import { applyClientListQuery } from "./list-query";

const rows = [
  { id: "1", name: "Zulu", created: 1 },
  { id: "2", name: "Alpha", created: 3 },
  { id: "3", name: "Alpine", created: 2 },
];

describe("applyClientListQuery", () => {
  it("filters, sorts, and paginates without mutating source rows", () => {
    const result = applyClientListQuery(
      rows,
      { search: "al", sort: { id: "created", direction: "desc" }, page: 2, pageSize: 1 },
      [(row) => row.name],
      { created: (row) => row.created },
    );
    expect(result).toEqual({ rows: [rows[2]], totalRows: 2 });
    expect(rows.map((row) => row.id)).toEqual(["1", "2", "3"]);
  });
});
