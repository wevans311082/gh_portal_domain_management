"""PDF rendering for billing documents.

Order of preference:
1. ``pdfkit`` (wkhtmltopdf) — primary. Supports admin-controlled headers and
   footers, accent-coloured branded layouts, inline images.
2. ``weasyprint`` — fallback when wkhtmltopdf is missing on the host.
3. Plain rendered HTML — last resort, with a banner explaining the situation.

Callers should use :func:`render_document_pdf`. The returned tuple is
``(content_bytes, content_type, suggested_filename_ext)``.
"""
from __future__ import annotations

import logging
from typing import Mapping, Optional, Tuple

from django.template.loader import render_to_string

logger = logging.getLogger(__name__)


_DEFAULT_OPTIONS = {
    "page-size": "A4",
    "margin-top": "18mm",
    "margin-right": "15mm",
    "margin-bottom": "18mm",
    "margin-left": "15mm",
    "encoding": "UTF-8",
    "enable-local-file-access": "",
    "quiet": "",
}


def _try_pdfkit(html: str, *, header_html: Optional[str], footer_html: Optional[str]) -> Optional[bytes]:
    """Try to render with wkhtmltopdf. Return ``None`` if it isn't available."""
    try:
        import pdfkit  # type: ignore
    except ImportError:
        return None

    options = dict(_DEFAULT_OPTIONS)

    # wkhtmltopdf header/footer arguments must be paths to HTML files.
    import tempfile

    tmp_paths = []
    try:
        if header_html:
            f = tempfile.NamedTemporaryFile(
                "w", suffix=".html", delete=False, encoding="utf-8"
            )
            f.write(header_html)
            f.close()
            tmp_paths.append(f.name)
            options["header-html"] = f.name
            options["margin-top"] = "30mm"
        if footer_html:
            f = tempfile.NamedTemporaryFile(
                "w", suffix=".html", delete=False, encoding="utf-8"
            )
            f.write(footer_html)
            f.close()
            tmp_paths.append(f.name)
            options["footer-html"] = f.name
            options["margin-bottom"] = "25mm"

        try:
            pdf_bytes = pdfkit.from_string(html, False, options=options)
            return pdf_bytes
        except (OSError, IOError) as exc:
            logger.warning("wkhtmltopdf unavailable or failed: %s", exc)
            return None
        except Exception as exc:  # pdfkit.exceptions.PDFKitError, etc.
            logger.warning("pdfkit raised %s: %s", type(exc).__name__, exc)
            return None
    finally:
        import os
        for path in tmp_paths:
            try:
                os.unlink(path)
            except OSError:
                pass


def _try_weasyprint(html: str, *, base_url: Optional[str]) -> Optional[bytes]:
    try:
        from weasyprint import HTML  # type: ignore
    except ImportError:
        return None
    try:
        return HTML(string=html, base_url=base_url).write_pdf()
    except Exception as exc:
        logger.warning("WeasyPrint failed: %s", exc)
        return None


def render_document_pdf(
    template_name: str,
    context: Mapping,
    *,
    header_template: Optional[str] = None,
    footer_template: Optional[str] = None,
    base_url: Optional[str] = None,
) -> Tuple[bytes, str, str]:
    """Render a Django template to PDF (or HTML fallback).

    Returns ``(content, content_type, ext)``.
    """
    html = render_to_string(template_name, dict(context))
    header_html = render_to_string(header_template, dict(context)) if header_template else None
    footer_html = render_to_string(footer_template, dict(context)) if footer_template else None

    pdf_bytes = _try_pdfkit(html, header_html=header_html, footer_html=footer_html)
    if pdf_bytes:
        return pdf_bytes, "application/pdf", "pdf"

    pdf_bytes = _try_weasyprint(html, base_url=base_url)
    if pdf_bytes:
        return pdf_bytes, "application/pdf", "pdf"

    logger.warning("No PDF engine available; serving HTML fallback for %s", template_name)
    banner = (
        "<!-- PDF engine unavailable: returning HTML. Install wkhtmltopdf "
        "for production-grade PDFs. -->\n"
    )
    return (banner + html).encode("utf-8"), "text/html", "html"
