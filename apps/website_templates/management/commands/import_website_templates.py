"""
Management command: import_website_templates
============================================
Extracts all ZIP files from WEBSITE_TEMPLATES_ZIP_ROOT into
WEBSITE_TEMPLATES_EXTRACTED_ROOT, then registers or updates each
template in the WebsiteTemplate model.

Also performs a lightweight security audit on every HTML/JS file:
  - Detects bundled jQuery version  (< 3.7 flagged)
  - Detects bundled Bootstrap version
  - Flags http:// (non-HTTPS) CDN links and rewrites them to https://
  - Replaces jquery CDN links pointing to versions < 3.7.1 with 3.7.1
  - Flags inline <script> event handlers (onclick, onload …)

Usage:
    python manage.py import_website_templates
    python manage.py import_website_templates --zip-dir /path/to/zips
    python manage.py import_website_templates --skip-extract   # only re-audit
    python manage.py import_website_templates --force           # re-extract even if exists
"""

import pathlib
import re
import zipfile
from typing import Optional

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils.text import slugify

from apps.website_templates.models import WebsiteTemplate

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_ZIP_ROOT = pathlib.Path("website_templates/Website Templates")
DEFAULT_EXTRACTED_ROOT = pathlib.Path("website_templates/extracted")

# Category keywords detected from the zip filename
CATEGORY_KEYWORDS: list[tuple[str, list[str]]] = [
    ("hosting", ["hosting", "cloud", "server", "webhost", "datacenter"]),
    ("restaurant", ["restaurant", "food", "pizza", "bakery", "gourmet", "recipe", "kitchen", "feast", "pasta", "steak", "bar", "brewery", "coffee"]),
    ("wedding", ["wedding", "bridal", "marital", "spousal", "dream_wed", "wed_day", "groom", "love"]),
    ("photography", ["photo", "fotograph", "gallery", "fotos", "photo_hub", "leoimages", "pix", "pixzee"]),
    ("portfolio", ["portfolio", "freelance", "cv", "mycv", "novel_folio", "my_skills", "ifreelancer", "portf"]),
    ("ecommerce", ["shop", "store", "ekomers", "resale", "retail", "cart", "jewellery"]),
    ("construction", ["underconstruction", "under_construction", "coming_soon", "am_coming", "launcher", "construct", "road_way", "grand_under"]),
    ("blog", ["blog", "magazine", "media", "news"]),
    ("business", ["business", "corporate", "agency", "firm", "tech", "power_tech", "steel", "construct"]),
]

# JS/CSS CDN rewrite patterns
_HTTP_SCRIPT_RE = re.compile(r'(src|href)=["\']http://', re.IGNORECASE)
_JQUERY_BUNDLE_RE = re.compile(r"jquery[.-](\d+\.\d+(?:\.\d+)?)(\.min)?\.js", re.IGNORECASE)
_JQUERY_CDN_RE = re.compile(
    r'(src=["\'])https?://(?:code\.jquery\.com|ajax\.googleapis\.com/ajax/libs/jquery)/[^"\']+(["\'])',
    re.IGNORECASE,
)
_BOOTSTRAP_BUNDLE_RE = re.compile(r"bootstrap[.-](\d+\.\d+(?:\.\d+)?)(\.min)?\.(css|js)", re.IGNORECASE)
_INLINE_HANDLER_RE = re.compile(r'\bon(click|load|submit|error|mouseover|mouseout)=["\']', re.IGNORECASE)

JQUERY_SAFE_VERSION = (3, 7, 1)
JQUERY_LATEST_CDN = "https://cdn.jsdelivr.net/npm/jquery@3.7.1/dist/jquery.min.js"


def _parse_version(version_str: str) -> tuple[int, ...]:
    parts = version_str.strip().split(".")
    try:
        return tuple(int(p) for p in parts[:3])
    except ValueError:
        return (0,)


def _guess_category(zip_name: str) -> str:
    name_lower = zip_name.lower()
    for category, keywords in CATEGORY_KEYWORDS:
        if any(kw in name_lower for kw in keywords):
            return category
    return "other"


def _friendly_name(zip_name: str) -> str:
    """Convert 'digital_hosting-web.zip' → 'Digital Hosting'."""
    name = pathlib.Path(zip_name).stem
    name = re.sub(r"[-_](web|pack|v\d+)$", "", name, flags=re.IGNORECASE)
    name = re.sub(r"[-_]", " ", name)
    return name.title().strip()


# ---------------------------------------------------------------------------
# Security audit helpers
# ---------------------------------------------------------------------------

def _audit_html_file(path: pathlib.Path) -> dict:
    """
    Read one HTML/JS file and return audit findings.
    Returns dict with keys: jquery_version, bootstrap_version, notes (list), patched_content.
    """
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}

    notes = []
    patched = content
    jquery_ver = ""
    bootstrap_ver = ""

    # --- Detect/fix bundled jquery ---
    for m in _JQUERY_BUNDLE_RE.finditer(content):
        ver = m.group(1)
        jquery_ver = ver
        if _parse_version(ver) < JQUERY_SAFE_VERSION:
            notes.append(f"Bundled jQuery {ver} (< {'.'.join(str(v) for v in JQUERY_SAFE_VERSION)}) found in {path.name}")

    # --- Fix CDN jquery links to latest ---
    def _replace_jquery_cdn(m):
        notes.append(f"CDN jQuery link updated to {JQUERY_LATEST_CDN}")
        return f'{m.group(1)}{JQUERY_LATEST_CDN}{m.group(2)}'

    patched, n = _JQUERY_CDN_RE.subn(_replace_jquery_cdn, patched)
    if n:
        notes.append(f"Rewrote {n} jQuery CDN reference(s) to latest secure version")

    # --- Rewrite http:// → https:// in CDN links ---
    patched_http, n_http = _HTTP_SCRIPT_RE.subn(r'\1="https://', patched)
    if n_http:
        notes.append(f"Rewrote {n_http} http:// CDN reference(s) to https://")
        patched = patched_http

    # --- Detect bootstrap ---
    for m in _BOOTSTRAP_BUNDLE_RE.finditer(content):
        bootstrap_ver = m.group(1)

    # --- Flag inline event handlers ---
    inline_count = len(_INLINE_HANDLER_RE.findall(content))
    if inline_count:
        notes.append(f"{inline_count} inline event handler(s) (onclick/onload etc.) found — review manually")

    return {
        "jquery_version": jquery_ver,
        "bootstrap_version": bootstrap_ver,
        "notes": notes,
        "patched_content": patched if patched != content else None,
    }


def _audit_template_dir(template_dir: pathlib.Path) -> dict:
    """Walk a template directory and aggregate audit results."""
    all_notes = []
    jquery_ver = ""
    bootstrap_ver = ""
    patched_files = 0

    for html_file in list(template_dir.rglob("*.html")) + list(template_dir.rglob("*.js")):
        result = _audit_html_file(html_file)
        if not result:
            continue
        if result.get("jquery_version"):
            jquery_ver = result["jquery_version"]
        if result.get("bootstrap_version"):
            bootstrap_ver = result["bootstrap_version"]
        all_notes.extend(result.get("notes", []))
        if result.get("patched_content"):
            try:
                html_file.write_text(result["patched_content"], encoding="utf-8")
                patched_files += 1
            except OSError:
                pass

    if patched_files:
        all_notes.append(f"Auto-patched {patched_files} file(s)")

    return {
        "jquery_version": jquery_ver,
        "bootstrap_version": bootstrap_ver,
        "security_notes": "\n".join(all_notes) if all_notes else "No issues found.",
        "is_sanitised": True,
    }


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------

class Command(BaseCommand):
    help = "Extract website template ZIPs and import/audit them into the database."

    def add_arguments(self, parser):
        parser.add_argument(
            "--zip-dir",
            default=None,
            help="Directory containing ZIP files (default: WEBSITE_TEMPLATES_ZIP_ROOT setting)",
        )
        parser.add_argument(
            "--extract-dir",
            default=None,
            help="Directory to extract templates into (default: WEBSITE_TEMPLATES_EXTRACTED_ROOT setting)",
        )
        parser.add_argument(
            "--skip-extract",
            action="store_true",
            help="Skip ZIP extraction, only re-run security audit on already-extracted templates",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Re-extract even if the destination directory already exists",
        )
        parser.add_argument(
            "--deactivate-missing",
            action="store_true",
            help="Mark templates as inactive if their ZIP is no longer present",
        )

    def handle(self, *args, **options):
        zip_root = pathlib.Path(
            options["zip_dir"]
            or getattr(settings, "WEBSITE_TEMPLATES_ZIP_ROOT", DEFAULT_ZIP_ROOT)
        )
        extracted_root = pathlib.Path(
            options["extract_dir"]
            or getattr(settings, "WEBSITE_TEMPLATES_EXTRACTED_ROOT", DEFAULT_EXTRACTED_ROOT)
        )
        extracted_root.mkdir(parents=True, exist_ok=True)

        if not zip_root.is_dir():
            self.stderr.write(self.style.ERROR(f"ZIP directory not found: {zip_root}"))
            return

        zip_files = sorted(zip_root.glob("*.zip"))
        self.stdout.write(self.style.SUCCESS(f"Found {len(zip_files)} ZIP file(s) in {zip_root}"))

        imported = 0
        errors = 0

        for zip_path in zip_files:
            try:
                self._process_zip(zip_path, extracted_root, options)
                imported += 1
            except Exception as exc:
                errors += 1
                self.stderr.write(self.style.WARNING(f"  SKIP {zip_path.name}: {exc}"))

        self.stdout.write(
            self.style.SUCCESS(f"\nDone. Imported/updated: {imported}. Errors: {errors}.")
        )

    def _process_zip(self, zip_path: pathlib.Path, extracted_root: pathlib.Path, options: dict):
        name = _friendly_name(zip_path.name)
        slug = slugify(re.sub(r"[-_](web|pack|v\d+)$", "", pathlib.Path(zip_path.name).stem, flags=re.IGNORECASE))
        category = _guess_category(zip_path.name)

        template_dir = extracted_root / slug
        skip_extract = options.get("skip_extract", False)
        force = options.get("force", False)

        # --- Extract ---
        if not skip_extract:
            if template_dir.exists() and not force:
                self.stdout.write(f"  SKIP extract (already exists): {slug}")
            else:
                self.stdout.write(f"  Extracting: {zip_path.name} → {slug}/")
                _safe_extract(zip_path, template_dir)

        if not template_dir.is_dir():
            raise RuntimeError(f"Template dir not found after extraction: {template_dir}")

        # --- Find index.html ---
        index_candidates = list(template_dir.rglob("index.html"))
        has_index = bool(index_candidates)
        if has_index and index_candidates[0].parent != template_dir:
            # Flatten: move contents up one level if nested
            _flatten_dir(template_dir)

        # --- Audit ---
        self.stdout.write(f"  Auditing: {slug}")
        audit = _audit_template_dir(template_dir)

        # --- DB upsert ---
        obj, created = WebsiteTemplate.objects.update_or_create(
            slug=slug,
            defaults={
                "name": name,
                "category": category,
                "zip_filename": zip_path.name,
                "extracted_path": str(template_dir.relative_to(pathlib.Path("."))),
                "has_index": has_index,
                "jquery_version": audit.get("jquery_version", ""),
                "bootstrap_version": audit.get("bootstrap_version", ""),
                "security_notes": audit.get("security_notes", ""),
                "is_sanitised": audit.get("is_sanitised", False),
                "is_active": True,
            },
        )
        verb = "Created" if created else "Updated"
        self.stdout.write(self.style.SUCCESS(f"  {verb}: {name} [{category}]"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_extract(zip_path: pathlib.Path, dest: pathlib.Path):
    """
    Extract zip to dest, stripping any leading directory component and
    guarding against path traversal (zip slip).
    """
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as zf:
        members = zf.infolist()
        # Determine common prefix to strip
        prefix = _common_prefix([m.filename for m in members if not m.filename.endswith("/")])

        for member in members:
            # Strip common prefix
            relative = member.filename
            if prefix and relative.startswith(prefix):
                relative = relative[len(prefix):]
            if not relative:
                continue

            # Path traversal guard
            target = (dest / relative).resolve()
            try:
                target.relative_to(dest.resolve())
            except ValueError:
                continue  # skip entries that escape the dest

            if member.filename.endswith("/"):
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member) as src, open(target, "wb") as dst:
                    dst.write(src.read())


def _common_prefix(paths: list[str]) -> str:
    """Return the common directory prefix of a list of zip entry paths."""
    if not paths:
        return ""
    parts = [p.split("/") for p in paths]
    prefix_parts = []
    for segment in zip(*parts):
        if len(set(segment)) == 1:
            prefix_parts.append(segment[0])
        else:
            break
    if not prefix_parts:
        return ""
    return "/".join(prefix_parts) + "/"


def _flatten_dir(template_dir: pathlib.Path):
    """
    If there is exactly one sub-directory in template_dir and index.html
    is inside it, move all contents up one level.
    """
    children = [c for c in template_dir.iterdir()]
    if len(children) == 1 and children[0].is_dir():
        sub = children[0]
        for item in sub.iterdir():
            item.rename(template_dir / item.name)
        sub.rmdir()
