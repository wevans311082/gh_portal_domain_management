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
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile
import time
import uuid
from typing import Mapping, Optional, Tuple

from django.template.loader import render_to_string
from apps.core.process_limits import ProcessLimitError, run_bounded

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


MAX_HTML_BYTES = 1024 * 1024
MAX_PDF_BYTES = 10 * 1024 * 1024
RENDER_SECONDS = 15


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ProcessLimitError("timeout")
    return remaining


def _try_pdfkit(html: str, *, header_html: Optional[str], footer_html: Optional[str], deadline=None) -> Optional[bytes]:
    try:
        import pdfkit
    except ImportError:
        return None
    binary = shutil.which("wkhtmltopdf")
    if not binary:
        return None
    deadline = time.monotonic() + RENDER_SECONDS if deadline is None else deadline
    options = dict(_DEFAULT_OPTIONS)
    try:
        with tempfile.TemporaryDirectory(prefix="portal-document-") as directory:
            for kind, content in (("header", header_html), ("footer", footer_html)):
                if content:
                    path = Path(directory) / f"{kind}.html"
                    path.write_text(content, encoding="utf-8")
                    options[f"{kind}-html"] = str(path)
                    options["margin-top" if kind == "header" else "margin-bottom"] = "30mm" if kind == "header" else "25mm"
            # Do not discover arbitrary options from HTML meta tags. The random
            # prefix is selected after rendering and cannot occur in that input.
            configuration = pdfkit.configuration(wkhtmltopdf=binary, meta_tag_prefix=f"portal-{uuid.uuid4().hex}-")
            runner = pdfkit.PDFKit(html, "string", options=options, configuration=configuration)
            result = run_bounded(runner.command(), html.encode("utf-8"), timeout=_remaining(deadline),
                                 max_input=MAX_HTML_BYTES, max_output=MAX_PDF_BYTES)
            if not result.startswith(b"%PDF-"):
                raise ProcessLimitError("invalid_pdf")
            return result
    except OSError:
        logger.warning("wkhtmltopdf is unavailable")
        return None
    except ProcessLimitError as error:
        if error.code in {"engine_unavailable", "engine_failed"}:
            logger.warning("wkhtmltopdf failed code=%s", error.code)
            return None
        raise


def _try_weasyprint(html: str, *, base_url: Optional[str], deadline=None) -> Optional[bytes]:
    try:
        if importlib.util.find_spec("weasyprint") is None:
            return None
    except (ImportError, ValueError):
        return None
    deadline = time.monotonic() + RENDER_SECONDS if deadline is None else deadline
    packet = json.dumps({"html": html, "base_url": base_url}, ensure_ascii=False).encode("utf-8")
    try:
        result = run_bounded([sys.executable, "-m", "apps.billing.pdf_worker"], packet,
                             timeout=_remaining(deadline), max_input=2 * MAX_HTML_BYTES + 65536,
                             max_output=MAX_PDF_BYTES, cwd=Path(__file__).resolve().parents[2])
        if not result.startswith(b"%PDF-"):
            raise ProcessLimitError("invalid_pdf")
        return result
    except ProcessLimitError as error:
        if error.code in {"engine_unavailable", "engine_failed"}:
            logger.warning("WeasyPrint failed code=%s", error.code)
            return None
        raise


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

    if sum(len(value.encode("utf-8")) for value in (html, header_html or "", footer_html or "")) > MAX_HTML_BYTES:
        raise ProcessLimitError("input_limit")
    if base_url is not None and len(base_url) > 2048:
        raise ProcessLimitError("input_limit")
    deadline = time.monotonic() + RENDER_SECONDS
    try:
        pdf_bytes = _try_pdfkit(html, header_html=header_html, footer_html=footer_html, deadline=deadline)
        if pdf_bytes:
            return pdf_bytes, "application/pdf", "pdf"
        pdf_bytes = _try_weasyprint(html, base_url=base_url, deadline=deadline)
        if pdf_bytes:
            return pdf_bytes, "application/pdf", "pdf"
    except ProcessLimitError as error:
        logger.warning("Document engine stopped code=%s", error.code)

    logger.warning("No PDF engine available; serving HTML fallback for %s", template_name)
    banner = (
        "<!-- PDF engine unavailable or limited: returning the rendered document as HTML. -->\n"
    )
    return (banner + html).encode("utf-8"), "text/html", "html"
