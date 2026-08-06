"""Native-PDF text + table extraction -- adapted from ocr-desktop-app's
`worker/parse_tables.py` (a PyInstaller-bundled CLI invoked as a subprocess-per-parse by Rust)
into a plain importable function, called in-process here. Same core logic (pdfplumber for
prose text, Camelot for tables); no more bundling/packaging step is needed at all since the
web server already has these as normal pip dependencies.

Also folds in the reshaping `src-tauri/src/commands/tables.rs::extract_tables` used to do on
the worker's raw JSON, so this returns the final `PageResult[]` shape directly:

    [{"page": 0, "markdown": "...", "blocks": [{"id", "page", "label", "bbox", "html", "text"}]}]

-- the exact shape `app.js`/`blocks-view.js` already expect, so neither needs to change.
"""
from __future__ import annotations

from typing import Optional

VALID_FLAVORS = {"lattice", "stream", "network", "hybrid"}


def _escape_html(value) -> str:
    text = "" if value is None else str(value)
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _rows_to_html_table(rows: list[list]) -> str:
    lines = ["<table>"]
    for row in rows:
        cells = "".join(f"<td>{_escape_html(cell)}</td>" for cell in row)
        lines.append(f"<tr>{cells}</tr>")
    lines.append("</table>")
    return "\n".join(lines)


def _normalize_bbox(bbox, page_width: float, page_height: float) -> list[float]:
    """Camelot's (left, bottom, right, top) PDF-point bbox -> normalized 0-1000, top-left,
    y-down (matches the bbox-overlay convention blocks-view.js already implements)."""
    x0, y0, x1, y1 = bbox
    return [
        max(0.0, min(1000.0, (x0 / page_width) * 1000)),
        max(0.0, min(1000.0, ((page_height - y1) / page_height) * 1000)),
        max(0.0, min(1000.0, (x1 / page_width) * 1000)),
        max(0.0, min(1000.0, ((page_height - y0) / page_height) * 1000)),
    ]


def extract_tables(
    pdf_path: str,
    flavor: str = "lattice",
    table_areas_by_page: Optional[dict[str, list[str]]] = None,
    flavor_by_page: Optional[dict[str, str]] = None,
    pages: Optional[list[int]] = None,
) -> list[dict]:
    """Extract text + tables from a native PDF, returning the frontend-ready PageResult[] shape.

    `table_areas_by_page`/`flavor_by_page`: keyed by 0-indexed page number as a string --
    see worker/parse_tables.py's original module docstring for the full explanation of
    `table_regions` vs `table_areas` and the region-string y-order convention. Only pages the
    user drew a region for or overrode the flavor on need an entry.

    `pages`: optional 0-indexed page subset to restrict extraction to entirely -- both the
    pdfplumber text/size pass and, critically, Camelot's own `pages=` kwarg. Without it,
    Camelot always scans the WHOLE document regardless of how few pages table_areas_by_page/
    flavor_by_page actually have entries for -- confirmed directly (same finding as the Tauri
    desktop app's own worker/parse_tables.py --pages fix) that a Combine-Tables merge of just 2
    pages of a ~100-page protocol cost the same runtime as a full-document parse without this.
    Only the requested pages appear in the returned list at all (sparse, not the full
    0..page_count range) -- safe for any caller that looks up a page by its `page` number
    rather than indexing positionally, which is how every caller here already works. Omit for
    the main workspace parse and "Tables Only" autodetect, which both genuinely need every page.
    """
    if flavor not in VALID_FLAVORS:
        flavor = "lattice"
    table_areas_by_page = table_areas_by_page or {}
    flavor_by_page = flavor_by_page or {}

    import pdfplumber

    with pdfplumber.open(pdf_path) as pdf:
        total_page_count = len(pdf.pages)
        page_indices = (
            [i for i in pages if 0 <= i < total_page_count]
            if pages is not None
            else list(range(total_page_count))
        )
        page_texts = {i: (pdf.pages[i].extract_text() or "").strip() for i in page_indices}
        page_sizes = {i: (pdf.pages[i].width, pdf.pages[i].height) for i in page_indices}

    tables_by_page: dict[int, list[dict]] = {}
    if page_indices:
        import camelot

        per_page = {}
        for page_str in set(table_areas_by_page) | set(flavor_by_page):
            try:
                page_index = int(page_str)
            except ValueError:
                continue
            if page_index not in page_indices:
                continue  # a region override for a page outside the requested scope is unusable
            overrides: dict = {}
            if page_str in table_areas_by_page:
                # See worker/parse_tables.py's docstring: table_regions (not table_areas)
                # tolerates an approximate hand-drawn region far better for Lattice.
                overrides["table_regions"] = table_areas_by_page[page_str]
            if page_str in flavor_by_page:
                overrides["flavor"] = flavor_by_page[page_str]
            per_page[page_index + 1] = overrides  # Camelot pages are 1-indexed.

        camelot_pages = ",".join(str(i + 1) for i in page_indices)

        try:
            table_list = camelot.read_pdf(
                pdf_path,
                pages=camelot_pages,
                flavor=flavor,
                per_page=per_page or None,
                suppress_stdout=True,
            )
            for table in table_list:
                page_index = table.page - 1  # Camelot is 1-indexed; this app is 0-indexed.
                rows = table.df.astype(str).values.tolist()
                bbox = None
                if table._bbox and page_index in page_sizes:
                    width, height = page_sizes[page_index]
                    bbox = _normalize_bbox(table._bbox, width, height)
                tables_by_page.setdefault(page_index, []).append({"rows": rows, "bbox": bbox})
        except Exception:  # noqa: BLE001 - a table-extraction failure (unusual layout, an
            # encrypted/malformed region) shouldn't take down the whole document's text
            # extraction, which already succeeded above.
            pass

    pages_out = []
    for i in page_indices:
        parts = [page_texts[i]] if page_texts[i] else []
        blocks = []
        for j, entry in enumerate(tables_by_page.get(i, [])):
            html = _rows_to_html_table(entry["rows"])
            parts.append(html)
            if entry["bbox"] is not None:
                blocks.append({
                    "id": f"table-{i}-{j}",
                    "page": i,
                    "label": "Table",
                    "bbox": entry["bbox"],
                    "html": html,
                    "text": "Table",
                })
        pages_out.append({"page": i, "markdown": "\n\n".join(parts), "blocks": blocks})

    return pages_out
