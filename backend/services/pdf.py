"""PDF page rasterization -- the pypdfium2-based equivalent of ocr-desktop-app's
`src-tauri/src/commands/file_io.rs::rasterize_pdf` (which uses the `pdfium-render` Rust
crate). Same underlying pdfium library, different language binding; produces the identical
`{page, mime, base64, width, height}` shape the frontend (`app.js`'s page-preview/thumbnail
rendering, blocks-view.js's bbox overlay) already expects, so none of that code needs to
change.
"""
from __future__ import annotations

import base64
import io
from typing import Optional

import pypdfium2 as pdfium
from PIL import Image


def rasterize_pdf(pdf_path: str, dpi: int = 150, only_pages: Optional[list[int]] = None) -> list[dict]:
    """Render PDF pages to PNG (base64-encoded), matching file_io.rs's `PageImage` shape.

    `only_pages`: 0-indexed page numbers to render; omit (None) to render every page.
    """
    scale = dpi / 72.0
    only = set(only_pages) if only_pages is not None else None

    pages_out: list[dict] = []
    pdf = pdfium.PdfDocument(pdf_path)
    try:
        for index in range(len(pdf)):
            if only is not None and index not in only:
                continue
            page = pdf[index]
            try:
                bitmap = page.render(scale=scale)
                image: Image.Image = bitmap.to_pil()
            finally:
                page.close()

            buf = io.BytesIO()
            image.save(buf, format="PNG")
            pages_out.append({
                "page": index,
                "mime": "image/png",
                "base64": base64.b64encode(buf.getvalue()).decode("ascii"),
                "width": image.width,
                "height": image.height,
            })
    finally:
        pdf.close()

    return pages_out
