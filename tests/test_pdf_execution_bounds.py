"""Document integration with bounded native/optional renderer processes."""
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from apps.billing import pdf
from apps.core.process_limits import ProcessLimitError, run_bounded


def template_stub(monkeypatch, text="<html><body>Invoice fixture</body></html>"):
    monkeypatch.setattr(pdf, "render_to_string", lambda *args, **kwargs: text)


def test_oversized_rendered_input_never_starts_an_engine(monkeypatch):
    template_stub(monkeypatch, "x" * (pdf.MAX_HTML_BYTES + 1))
    monkeypatch.setattr(pdf, "_try_pdfkit", lambda *args, **kwargs: pytest.fail("Oversized input started an engine"))
    with pytest.raises(ProcessLimitError, match="input_limit"):
        pdf.render_document_pdf("fixture.html", {})


def test_combined_header_footer_and_body_have_one_input_budget(monkeypatch):
    template_stub(monkeypatch, "x" * (pdf.MAX_HTML_BYTES // 2))
    with pytest.raises(ProcessLimitError, match="input_limit"):
        pdf.render_document_pdf("body.html", {}, header_template="header.html", footer_template="footer.html")


def test_engines_share_a_single_deadline(monkeypatch):
    template_stub(monkeypatch)
    deadlines = []
    def unavailable(*args, **kwargs):
        deadlines.append(kwargs["deadline"])
        return None
    monkeypatch.setattr(pdf, "_try_pdfkit", unavailable)
    monkeypatch.setattr(pdf, "_try_weasyprint", unavailable)
    content, mime, extension = pdf.render_document_pdf("fixture.html", {})
    assert deadlines[0] == deadlines[1]
    assert mime == "text/html" and extension == "html" and b"Invoice fixture" in content


def test_timeout_does_not_start_a_second_engine_or_expose_diagnostics(monkeypatch):
    template_stub(monkeypatch)
    def timeout(*args, **kwargs):
        raise ProcessLimitError("timeout")
    monkeypatch.setattr(pdf, "_try_pdfkit", timeout)
    monkeypatch.setattr(pdf, "_try_weasyprint", lambda *args, **kwargs: pytest.fail("Deadline was restarted"))
    content, mime, extension = pdf.render_document_pdf("fixture.html", {})
    assert mime == "text/html" and b"Invoice fixture" in content


def test_wkhtml_adapter_uses_bounded_process_and_cleans_header_files(monkeypatch):
    import pdfkit
    monkeypatch.setattr(pdf.shutil, "which", lambda name: sys.executable)
    paths = []
    def runner(html, source, options, configuration):
        assert source == "string"
        assert configuration.meta_tag_prefix.startswith("portal-")
        assert configuration.meta_tag_prefix != "pdfkit-"
        for kind in ("header", "footer"):
            path = Path(options[f"{kind}-html"])
            paths.append(path)
            assert path.read_text(encoding="utf-8") == f"<{kind}>fixture</{kind}>"
        return SimpleNamespace(command=lambda: [sys.executable, "-c",
            "import sys; data=sys.stdin.buffer.read(); assert b'body fixture' in data; sys.stdout.buffer.write(b'%PDF-1.4\\nfixture\\n%%EOF')"])
    monkeypatch.setattr(pdfkit, "PDFKit", runner)
    output = pdf._try_pdfkit("body fixture", header_html="<header>fixture</header>", footer_html="<footer>fixture</footer>")
    assert output.startswith(b"%PDF-")
    assert paths and not any(path.exists() for path in paths)


def test_invalid_native_output_is_not_served_as_a_pdf(monkeypatch):
    import pdfkit
    monkeypatch.setattr(pdf.shutil, "which", lambda name: sys.executable)
    monkeypatch.setattr(pdfkit, "PDFKit", lambda *args, **kwargs: SimpleNamespace(command=lambda: [sys.executable, "-c", "import sys;sys.stdin.buffer.read();print('not PDF')"]))
    with pytest.raises(ProcessLimitError, match="invalid_pdf"):
        pdf._try_pdfkit("fixture", header_html=None, footer_html=None)


def test_optional_worker_protocol_runs_without_django_or_secret_environment(tmp_path, monkeypatch):
    # Supply a deterministic engine stand-in inside the real isolated worker.
    # This verifies transport/isolation, not WeasyPrint's native font libraries.
    (tmp_path / "weasyprint.py").write_text(
        "import os\nclass HTML:\n def __init__(self,string,base_url):\n  assert string=='fixture'\n  assert os.getenv('DJANGO_SECRET_KEY') is None\n def write_pdf(self): return b'%PDF-1.4\\nfixture\\n%%EOF'\n", encoding="utf-8")
    wrapper = f"import sys,runpy;sys.path.insert(0,{str(tmp_path)!r});runpy.run_module('apps.billing.pdf_worker',run_name='__main__')"
    monkeypatch.setattr(pdf.importlib.util, "find_spec", lambda name: object())
    def invoke(command, packet, **kwargs):
        assert command[-1] == "apps.billing.pdf_worker"
        assert kwargs["max_output"] == pdf.MAX_PDF_BYTES
        return run_bounded([sys.executable, "-c", wrapper], packet, **kwargs)
    monkeypatch.setattr(pdf, "run_bounded", invoke)
    assert pdf._try_weasyprint("fixture", base_url=None).startswith(b"%PDF-")


def test_optional_worker_rejects_invalid_protocol_before_importing_an_engine():
    worker = Path(pdf.__file__).with_name("pdf_worker.py")
    with pytest.raises(ProcessLimitError, match="engine_failed"):
        run_bounded([sys.executable, str(worker)], b'{"html":[],"base_url":null}')


@pytest.mark.django_db
def test_public_pdf_downloads_have_a_pre_render_budget_and_get_only(client, monkeypatch):
    from apps.billing.models import Quote
    from django.urls import reverse
    quote = Quote.objects.create(number="PDF-RATE-FIXTURE", lead_email="pdf@example.test")
    calls = []
    def render(*args, **kwargs):
        calls.append("rendered")
        return b"%PDF-fixture", "application/pdf", "pdf"
    monkeypatch.setattr("apps.billing.public_views.render_quote_pdf", render)
    url = reverse("billing_public:quote_public_pdf", args=[quote.public_token])
    assert client.post(url).status_code == 405
    for _ in range(5):
        response = client.get(url)
        assert response.status_code == 200 and "no-store" in response["Cache-Control"]
    assert client.get(url).status_code == 429
    assert len(calls) == 5
