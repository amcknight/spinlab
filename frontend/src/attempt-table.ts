import { formatTime, formatAgo } from "./format";

export interface AttemptRow {
  id: number;
  order: number;
  clean_tail_ms: number | null;
  total_ms: number | null;
  deaths: number;
  created_at: string;
  completed: boolean;
  invalidated: boolean;
  is_floor: boolean;
}

export type SortColumn = "order" | "clean_tail_ms" | "total_ms" | "deaths" | "created_at";
export interface SortState { column: SortColumn; dir: "asc" | "desc"; }

export const DEFAULT_SORT: SortState = { column: "clean_tail_ms", dir: "desc" };

// Times/deaths default to biggest-first (outliers on top); order/age oldest-first.
function defaultDir(column: SortColumn): "asc" | "desc" {
  return column === "order" || column === "created_at" ? "asc" : "desc";
}

export function nextSortColumn(state: SortState, column: SortColumn): SortState {
  // Re-clicking the active column reverses it; a new column uses its default dir.
  if (column === state.column) {
    return { column, dir: state.dir === "asc" ? "desc" : "asc" };
  }
  return { column, dir: defaultDir(column) };
}

export function flipSortDir(state: SortState): SortState {
  return { column: state.column, dir: state.dir === "asc" ? "desc" : "asc" };
}

export function sortAttempts(rows: AttemptRow[], state: SortState): AttemptRow[] {
  const sign = state.dir === "asc" ? 1 : -1;
  const val = (r: AttemptRow): number =>
    state.column === "created_at" ? Date.parse(r.created_at) : (r[state.column] as number | null) ?? NaN;
  return [...rows].sort((a, b) => {
    const av = val(a), bv = val(b);
    const aNull = Number.isNaN(av);
    const bNull = Number.isNaN(bv);
    if (aNull && bNull) return 0;
    if (aNull) return 1;
    if (bNull) return -1;
    return av < bv ? -1 * sign : av > bv ? 1 * sign : 0;
  });
}

const HEADERS: { col: SortColumn; label: string }[] = [
  { col: "order", label: "Order" },
  { col: "clean_tail_ms", label: "Clean Tail" },
  { col: "total_ms", label: "Total" },
  { col: "deaths", label: "Deaths" },
  { col: "created_at", label: "Ago" },
];

/** Render the surgery table into `host`. onToggle(id, nextInvalidated) fires on
 *  an action click. Sort state is held locally; a header click sorts, and
 *  re-clicking the active column reverses it. nowMs is injectable for tests. */
export function renderAttemptTable(
  host: HTMLElement,
  rows: AttemptRow[],
  onToggle: (id: number, nextInvalidated: boolean) => void,
  nowMs: number = Date.now(),
): void {
  let sort = DEFAULT_SORT;

  const draw = () => {
    const sorted = sortAttempts(rows, sort);
    const ths = HEADERS.map(h => {
      const arrow = h.col === sort.column ? (sort.dir === "asc" ? " ▲" : " ▼") : "";
      return `<th data-col="${h.col}" class="num" style="cursor:pointer">${h.label}${arrow}</th>`;
    }).join("") + "<th></th>";
    const trs = sorted.map(r => {
      const star = r.is_floor ? " ★" : "";
      const ct = r.clean_tail_ms == null ? "—" : formatTime(r.clean_tail_ms) + star;
      const action = r.invalidated
        ? `<button data-id="${r.id}" data-next="0">⟲ restore</button>`
        : `<button data-id="${r.id}" data-next="1">⊘ invalidate</button>`;
      return `<tr class="${r.invalidated ? "invalidated" : ""}${r.is_floor ? " floor" : ""}">`
        + `<td class="num">${r.order}</td><td class="num">${ct}</td>`
        + `<td class="num">${r.total_ms == null ? "—" : formatTime(r.total_ms)}</td>`
        + `<td class="num">${r.deaths}</td><td class="num">${formatAgo(r.created_at, nowMs)}</td>`
        + `<td>${action}</td></tr>`;
    }).join("");
    host.innerHTML = `<table class="attempt-surgery"><thead><tr>${ths}</tr></thead><tbody>${trs}</tbody></table>`;

    host.querySelectorAll<HTMLElement>("th[data-col]").forEach(th => {
      const col = th.dataset.col as SortColumn;
      th.addEventListener("click", () => { sort = nextSortColumn(sort, col); draw(); });
    });
    host.querySelectorAll<HTMLButtonElement>("button[data-id]").forEach(btn => {
      btn.addEventListener("click", () =>
        onToggle(Number(btn.dataset.id), btn.dataset.next === "1"));
    });
  };

  draw();
}
