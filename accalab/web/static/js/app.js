"use strict";

document.querySelectorAll("table[data-sortable]").forEach((table) => {
  const body = table.tBodies[0];
  if (!body) return;
  const headers = [...table.tHead.rows[0].cells];
  const sortRows = (header, columnIndex, ascending) => {
    headers.forEach((cell) => cell.setAttribute("aria-sort", "none"));
    header.setAttribute("aria-sort", ascending ? "ascending" : "descending");
    const kind = header.dataset.sortKind || "text";
    const rows = [...body.rows].map((row, index) => ({ row, index }));
    rows.sort((left, right) => {
      const a = left.row.cells[columnIndex].dataset.sortValue || left.row.cells[columnIndex].textContent.trim();
      const b = right.row.cells[columnIndex].dataset.sortValue || right.row.cells[columnIndex].textContent.trim();
      let comparison;
      if (kind === "date") comparison = Date.parse(a) - Date.parse(b);
      else if (kind === "numeric" || kind === "odds") comparison = Number(a) - Number(b);
      else comparison = a.localeCompare(b, undefined, { numeric: true, sensitivity: "base" });
      return (ascending ? comparison : -comparison) || left.index - right.index;
    });
    rows.forEach(({ row }) => body.appendChild(row));
  };
  headers.forEach((header, columnIndex) => {
    const button = header.querySelector("button.table-sort");
    if (!button) return;
    button.addEventListener("click", () => {
      const ascending = header.getAttribute("aria-sort") !== "ascending";
      sortRows(header, columnIndex, ascending);     });
    });
  const defaultKey = table.dataset.defaultSort;
  const defaultIndex = headers.findIndex((header) => header.dataset.sortKey === defaultKey);
  if (defaultIndex >= 0) sortRows(headers[defaultIndex], defaultIndex, true);
  });
});

const themeToggle = document.querySelector("[data-theme-toggle]");
if (themeToggle) {
  themeToggle.addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    localStorage.setItem("accalab-theme", next);
  });
}

const savedTheme = localStorage.getItem("accalab-theme");
if (savedTheme) document.documentElement.dataset.theme = savedTheme;

const textSize = document.querySelector("[data-text-size]");
if (textSize) {
  textSize.value = localStorage.getItem("accalab-text-size") || "100";
  document.documentElement.style.fontSize = `${textSize.value}%`;
  textSize.addEventListener("change", () => {
    document.documentElement.style.fontSize = `${textSize.value}%`;
    localStorage.setItem("accalab-text-size", textSize.value);
  });
}
