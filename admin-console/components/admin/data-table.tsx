"use client";

import * as React from "react";
import { ChevronDown, ChevronLeft, ChevronRight, ChevronUp, Search } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { EmptyState, type EmptyStateProps } from "@/components/admin/empty-state";
import { ErrorState } from "@/components/admin/error-state";
import { Skeleton } from "@/components/admin/skeleton";
import type { AdminError, AdminQuery, ColumnDef, RowAction } from "@/lib/admin-api/types";
import { useI18n } from "@/lib/i18n";
import { cn } from "@/lib/utils";

export interface DataTableProps<T> {
  rows: T[];
  columns: ColumnDef<T>[];
  rowKey: (row: T) => string;
  loading?: boolean;
  error?: AdminError;
  query: AdminQuery;
  onQueryChange: (next: DataTableProps<T>["query"]) => void;
  totalRows: number;
  pageSizeOptions?: number[];
  searchable?: boolean;
  searchPlaceholderKey?: string;
  selection?: { selected: Set<string>; onChange(ids: Set<string>): void };
  rowActions?: (row: T) => RowAction[];
  empty: EmptyStateProps;
  ariaLabel: string;
}

const alignClass = {
  start: "text-left",
  center: "text-center",
  end: "text-right",
};

function displayValue(value: unknown): React.ReactNode {
  if (React.isValidElement(value) || typeof value === "string" || typeof value === "number") return value;
  if (value == null) return "—";
  return String(value);
}

export function DataTable<T>({
  rows,
  columns,
  rowKey,
  loading = false,
  error,
  query,
  onQueryChange,
  totalRows,
  pageSizeOptions = [20, 50, 100],
  searchable = false,
  searchPlaceholderKey,
  selection,
  rowActions,
  empty,
  ariaLabel,
}: DataTableProps<T>) {
  const { t } = useI18n();
  const [search, setSearch] = React.useState(query.search ?? "");

  React.useEffect(() => setSearch(query.search ?? ""), [query.search]);
  React.useEffect(() => {
    if (search === (query.search ?? "")) return;
    const timer = window.setTimeout(() => onQueryChange({ ...query, search: search.trim() || undefined, page: 1 }), 300);
    return () => window.clearTimeout(timer);
  }, [onQueryChange, query, search]);

  function changeSort(column: ColumnDef<T>) {
    if (!column.sortable) return;
    const direction = query.sort?.id === column.id && query.sort.direction === "asc" ? "desc" : "asc";
    onQueryChange({ ...query, sort: { id: column.id, direction }, page: 1 });
  }

  const pageCount = Math.max(1, Math.ceil(totalRows / query.pageSize));
  const visibleIds = rows.map(rowKey);
  const allSelected = Boolean(selection && visibleIds.length > 0 && visibleIds.every((id) => selection.selected.has(id)));

  function toggleAll() {
    if (!selection) return;
    const next = new Set(selection.selected);
    visibleIds.forEach((id) => allSelected ? next.delete(id) : next.add(id));
    selection.onChange(next);
  }

  if (loading) return <Skeleton variant="table" rows={query.pageSize > 5 ? 5 : query.pageSize} ariaLabel={t("admin.loading.content")} />;
  if (error && rows.length === 0) {
    return <ErrorState title={t(error.message_key)} description={t(error.message_key)} code={error.code} correlationId={error.correlation_id} />;
  }
  if (rows.length === 0) {
    return (
      <div className="space-y-4">
        {searchable ? (
          <label className="relative block max-w-sm">
            <span className="sr-only">{t(searchPlaceholderKey ?? "admin.table.search")}</span>
            <Search aria-hidden="true" className="absolute left-3 top-2.5 h-4 w-4 text-muted-foreground" />
            <Input value={search} onChange={(event) => setSearch(event.target.value)} className="pl-9" placeholder={t(searchPlaceholderKey ?? "admin.table.search")} />
          </label>
        ) : null}
        <EmptyState {...empty} filtered={empty.filtered ?? Boolean(query.search)} />
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {error ? <ErrorState compact title={t(error.message_key)} description={t(error.message_key)} code={error.code} correlationId={error.correlation_id} /> : null}
      <div className="flex flex-wrap items-center justify-between gap-3">
        {searchable ? (
          <label className="relative block w-full max-w-sm">
            <span className="sr-only">{t(searchPlaceholderKey ?? "admin.table.search")}</span>
            <Search aria-hidden="true" className="absolute left-3 top-2.5 h-4 w-4 text-muted-foreground" />
            <Input value={search} onChange={(event) => setSearch(event.target.value)} className="pl-9" placeholder={t(searchPlaceholderKey ?? "admin.table.search")} />
          </label>
        ) : <span />}
        <span className="text-sm text-muted-foreground">{t("admin.table.totalRows", { total: totalRows })}</span>
      </div>

      <Table aria-label={ariaLabel}>
        <TableHeader>
          <TableRow>
            {selection ? (
              <TableHead className="w-10">
                <input type="checkbox" checked={allSelected} aria-label={t("admin.table.selectAll")} onChange={toggleAll} />
              </TableHead>
            ) : null}
            {columns.map((column) => {
              const active = query.sort?.id === column.id;
              const ariaSort = active ? (query.sort?.direction === "asc" ? "ascending" : "descending") : "none";
              return (
                <TableHead
                  key={column.id}
                  aria-sort={column.sortable ? ariaSort : undefined}
                  className={alignClass[column.align ?? "start"]}
                  style={{ width: column.width }}
                >
                  {column.sortable ? (
                    <button type="button" className="inline-flex items-center gap-1 rounded-sm" onClick={() => changeSort(column)}>
                      {column.header}
                      {active && query.sort?.direction === "desc" ? <ChevronDown aria-hidden="true" className="h-4 w-4" /> : <ChevronUp aria-hidden="true" className={cn("h-4 w-4", !active && "opacity-40")} />}
                    </button>
                  ) : column.header}
                </TableHead>
              );
            })}
            {rowActions ? <TableHead className="text-right">{t("admin.table.actions")}</TableHead> : null}
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((row) => {
            const id = rowKey(row);
            const selected = selection?.selected.has(id) ?? false;
            return (
              <TableRow key={id} data-state={selected ? "selected" : undefined}>
                {selection ? (
                  <TableCell>
                    <input
                      type="checkbox"
                      checked={selected}
                      aria-label={`${t("admin.table.selectRow")} ${id}`}
                      onChange={() => {
                        const next = new Set(selection.selected);
                        if (selected) next.delete(id); else next.add(id);
                        selection.onChange(next);
                      }}
                    />
                  </TableCell>
                ) : null}
                {columns.map((column) => (
                  <TableCell key={column.id} className={alignClass[column.align ?? "start"]}>
                    {column.cell ? column.cell(row) : displayValue(column.accessor?.(row))}
                  </TableCell>
                ))}
                {rowActions ? (
                  <TableCell>
                    <div className="flex justify-end gap-1">
                      {rowActions(row).map((action) => {
                        const Icon = action.icon;
                        return (
                          <Button key={action.id} type="button" variant={action.tone === "danger" ? "destructive" : "ghost"} size="sm" disabled={action.disabled} onClick={action.onSelect}>
                            {Icon ? <Icon aria-hidden="true" className="h-4 w-4" /> : null}{action.label}
                          </Button>
                        );
                      })}
                    </div>
                  </TableCell>
                ) : null}
              </TableRow>
            );
          })}
        </TableBody>
      </Table>

      <div className="flex flex-wrap items-center justify-between gap-3">
        <label className="flex items-center gap-2 text-sm">
          <span>{t("admin.table.rowsPerPage")}</span>
          <select
            className="h-9 rounded-md border bg-background px-2"
            value={query.pageSize}
            onChange={(event) => onQueryChange({ ...query, pageSize: Number(event.target.value), page: 1 })}
          >
            {pageSizeOptions.map((size) => <option key={size} value={size}>{size}</option>)}
          </select>
        </label>
        <div className="flex items-center gap-2">
          <span className="text-sm" aria-live="polite">{t("admin.table.pageStatus", { page: query.page, pages: pageCount })}</span>
          <Button type="button" variant="outline" size="icon" aria-label={t("admin.table.previousPage")} disabled={query.page <= 1} onClick={() => onQueryChange({ ...query, page: query.page - 1 })}>
            <ChevronLeft aria-hidden="true" className="h-4 w-4" />
          </Button>
          <Button type="button" variant="outline" size="icon" aria-label={t("admin.table.nextPage")} disabled={query.page >= pageCount} onClick={() => onQueryChange({ ...query, page: query.page + 1 })}>
            <ChevronRight aria-hidden="true" className="h-4 w-4" />
          </Button>
        </div>
      </div>
    </div>
  );
}
