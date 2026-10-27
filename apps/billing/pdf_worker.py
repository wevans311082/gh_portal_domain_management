"""Isolated optional WeasyPrint renderer; no Django setup or database access."""
import json
import sys

MAX_HTML = 1024 * 1024
MAX_PACKET = 2 * MAX_HTML + 65536


def main():
    try:
        packet = sys.stdin.buffer.read(MAX_PACKET + 1)
        if len(packet) > MAX_PACKET:
            return 2
        data = json.loads(packet.decode("utf-8"))
        if not isinstance(data, dict) or set(data) != {"html", "base_url"}:
            return 2
        if not isinstance(data["html"], str) or len(data["html"].encode("utf-8")) > MAX_HTML:
            return 2
        if data["base_url"] is not None and (not isinstance(data["base_url"], str) or len(data["base_url"]) > 2048):
            return 2
        from weasyprint import HTML
        result = HTML(string=data["html"], base_url=data["base_url"]).write_pdf()
        if not result.startswith(b"%PDF-") or len(result) > 10 * 1024 * 1024:
            return 2
        sys.stdout.buffer.write(result)
        return 0
    except Exception:
        # The parent receives only a generic failure and never engine traceback,
        # filesystem paths or the submitted document in its application logs.
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
