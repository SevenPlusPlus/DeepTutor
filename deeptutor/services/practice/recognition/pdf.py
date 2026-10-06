"""Bounded PDF page rendering for question recognition."""

from __future__ import annotations

from dataclasses import dataclass
import math

MAX_PDF_PAGES = 20
MAX_PAGE_PIXELS = 3_000_000
DEFAULT_RENDER_SCALE = 1.5


@dataclass(frozen=True)
class RenderedPdfPage:
    page_number: int
    data: bytes
    width: int
    height: int


def render_pdf_pages(data: bytes) -> list[RenderedPdfPage]:
    """Render a small PDF to RGB PNG pages with bounded pixel dimensions."""

    try:
        import pymupdf

        document = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:
        raise ValueError("The PDF is damaged or cannot be opened") from exc

    try:
        if document.needs_pass:
            raise ValueError("Password-protected PDFs are not supported")
        if document.page_count < 1:
            raise ValueError("The PDF has no pages")
        if document.page_count > MAX_PDF_PAGES:
            raise ValueError(f"PDF imports support at most {MAX_PDF_PAGES} pages")

        rendered: list[RenderedPdfPage] = []
        for index in range(document.page_count):
            page = document.load_page(index)
            page_pixels = max(float(page.rect.width * page.rect.height), 1.0)
            scale = min(DEFAULT_RENDER_SCALE, math.sqrt(MAX_PAGE_PIXELS / page_pixels))
            pixmap = page.get_pixmap(
                matrix=pymupdf.Matrix(scale, scale),
                colorspace=pymupdf.csRGB,
                alpha=False,
            )
            rendered.append(
                RenderedPdfPage(
                    page_number=index + 1,
                    data=pixmap.tobytes("png"),
                    width=pixmap.width,
                    height=pixmap.height,
                )
            )
        return rendered
    finally:
        document.close()


__all__ = ["MAX_PDF_PAGES", "RenderedPdfPage", "render_pdf_pages"]
