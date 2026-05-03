"""
Standalone security patcher — no Django needed.
Run: python scripts/patch_templates.py
Rewrites:
  - http:// CDN links → https://
    - Old remote jQuery references → local /static/vendor/jquery-3.7.1.min.js
  - Reports bundled jQuery versions found in local js/ files
"""
import pathlib
import re
import sys

EXTRACTED_ROOT = pathlib.Path(__file__).parent.parent / "website_templates" / "extracted"

JQUERY_CDN_RE = re.compile(
    r'src=["\']https?://(?:code\.jquery\.com|ajax\.googleapis\.com/ajax/libs/jquery)/[^"\']+["\']',
    re.IGNORECASE,
)
JQUERY_NEW = 'src="/static/vendor/jquery-3.7.1.min.js"'
HTTP_RE = re.compile(r'(src|href)=["\']http://', re.IGNORECASE)
JQUERY_VER_RE = re.compile(r"jQuery\s+v?(\d+\.\d+(?:\.\d+)?)", re.IGNORECASE)


def _parse_version(v: str) -> tuple:
    try:
        return tuple(int(x) for x in v.split(".")[:3])
    except ValueError:
        return (0,)


SAFE = (3, 7, 1)


def patch_file(path: pathlib.Path) -> bool:
    try:
        txt = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    new = JQUERY_CDN_RE.sub(JQUERY_NEW, txt)
    new = HTTP_RE.sub(r'\1="https://', new)
    if new != txt:
        path.write_text(new, encoding="utf-8")
        return True
    return False


def detect_bundled_jquery(template_dir: pathlib.Path) -> list[str]:
    issues = []
    for js_file in template_dir.rglob("jquery*.js"):
        try:
            head = js_file.read_text(encoding="utf-8", errors="replace")[:2000]
        except OSError:
            continue
        m = JQUERY_VER_RE.search(head)
        if m:
            ver = m.group(1)
            if _parse_version(ver) < SAFE:
                issues.append(f"  WARN: Bundled jQuery {ver} in {js_file.relative_to(template_dir)} -- upgrade to 3.7.1 recommended")
    return issues


def main():
    if not EXTRACTED_ROOT.is_dir():
        print(f"ERROR: {EXTRACTED_ROOT} not found", file=sys.stderr)
        sys.exit(1)

    total_files = 0
    total_templates = 0
    warnings = []

    for template_dir in sorted(EXTRACTED_ROOT.iterdir()):
        if not template_dir.is_dir() or template_dir.name.startswith("_"):
            continue
        changed = False
        for ext in ("*.html", "*.js"):
            for f in template_dir.rglob(ext):
                if patch_file(f):
                    total_files += 1
                    changed = True
        if changed:
            total_templates += 1
            print(f"  Patched CDN: {template_dir.name}")

        issues = detect_bundled_jquery(template_dir)
        if issues:
            warnings.append((template_dir.name, issues))

    print(f"\nDone. Patched {total_files} file(s) across {total_templates} template(s).")

    if warnings:
        print(f"\n{'='*60}")
        print(f"BUNDLED JQUERY VERSION WARNINGS ({len(warnings)} template(s)):")
        print(f"{'='*60}")
        for name, issues in warnings:
            print(f"\n{name}:")
            for issue in issues:
                print(issue)
        print(
            f"\nTo fix: replace bundled jquery.min.js files with "
            f"/static/vendor/jquery-3.7.1.min.js"
        )


if __name__ == "__main__":
    main()

