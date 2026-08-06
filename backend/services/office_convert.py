"""Word (.docx/.doc) -> PDF conversion -- the server-side equivalent of ocr-desktop-app's
`office2pdf` Rust crate. That crate existed specifically to avoid requiring Word/LibreOffice
on an arbitrary *end-user's* machine; a server the team controls doesn't have that problem, so
the standard, well-supported approach here is a permanently-installed LibreOffice running in
headless mode (the same technique Microsoft's own Power Automate "Extract data from a Word
table" guidance recommends: convert to PDF first, then extract).

Requires LibreOffice installed on the machine running this backend (`soffice` on PATH) --
see the repo README's setup section. Not needed at all for local dev unless you're actually
testing Word-document upload; PDF-only workflows don't touch this module.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


class OfficeConversionError(RuntimeError):
    pass


def _find_soffice() -> str:
    for name in ("soffice", "soffice.exe"):
        found = shutil.which(name)
        if found:
            return found
    raise OfficeConversionError(
        "LibreOffice ('soffice') was not found on PATH. Install LibreOffice on this machine "
        "to support Word (.docx/.doc) upload -- see README.md."
    )


def convert_to_pdf(input_path: str, output_dir: str) -> str:
    """Convert a .docx/.doc file to PDF, writing the result into `output_dir`.

    Returns the path to the produced PDF. Raises OfficeConversionError on failure (LibreOffice
    missing, conversion timeout, or a non-zero exit code).
    """
    soffice = _find_soffice()
    try:
        result = subprocess.run(
            [
                soffice,
                "--headless",
                "--norestore",
                "--convert-to", "pdf",
                "--outdir", output_dir,
                input_path,
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired as exc:
        raise OfficeConversionError(f"LibreOffice conversion timed out: {exc}") from exc

    if result.returncode != 0:
        raise OfficeConversionError(
            f"LibreOffice conversion failed (exit {result.returncode}): {result.stderr or result.stdout}"
        )

    stem = Path(input_path).stem
    output_path = Path(output_dir) / f"{stem}.pdf"
    if not output_path.exists():
        raise OfficeConversionError(
            f"LibreOffice reported success but no output PDF was found at {output_path}"
        )
    return str(output_path)
