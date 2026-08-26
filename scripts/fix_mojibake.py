"""Repair UTF-8 text that was mis-decoded as Windows-1252/Latin-1 (£, →, etc.)."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    "website_templates",
    "static/vendor",
    "example_code",
}
EXTENSIONS = {".html", ".py", ".js", ".css", ".md", ".txt", ".json"}
MARKERS = ("Â", "â€", "â†", "", "…", "’", "“", "â€\x9d", "â\x80", "·", "©")


def _should_skip(path: Path) -> bool:
    rel = path.relative_to(ROOT).as_posix()
    for part in path.relative_to(ROOT).parts:
        if part in SKIP_DIRS or part.startswith("."):
            if part not in {".github"}:
                return True
    if "static/vendor" in rel or "website_templates" in rel:
        return True
    return False


def _fix_text(text: str) -> str:
    if text.startswith("\ufeff"):
        text = text.lstrip("\ufeff")
    changed = True
    rounds = 0
    while changed and rounds < 3:
        changed = False
        rounds += 1
        if any(marker in text for marker in MARKERS):
            for encoding in ("cp1252", "latin-1"):
                try:
                    candidate = text.encode(encoding).decode("utf-8")
                except (UnicodeDecodeError, UnicodeEncodeError):
                    continue
                if candidate != text:
                    text = candidate
                    changed = True
                    break
    replacements = {
        "£": "£",
        "·": "·",
        "©": "©",
        " ": " ",
        "—": "—",
        "–": "–",
        "‘": "‘",
        "’": "’",
        "“": "“",
        "â€\x9d": "”",
        "…": "…",
        "→": "→",
        "←": "←",
        "": "",
        "\u00c2\u00a3": "£",
    }
    for old, new in replacements.items():
        if old in text:
            text = text.replace(old, new)
    return text


def main() -> None:
    updated = 0
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in EXTENSIONS:
            continue
        if _should_skip(path):
            continue
        try:
            original = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            original = path.read_text(encoding="utf-8", errors="replace")
        fixed = _fix_text(original)
        if fixed != original:
            path.write_text(fixed, encoding="utf-8")
            updated += 1
            print(path.relative_to(ROOT).as_posix())
    print(f"Updated {updated} files")


if __name__ == "__main__":
    main()
