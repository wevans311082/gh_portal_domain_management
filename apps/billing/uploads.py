"""Decode and re-encode document logos; never persist uploaded active content."""
import io
import uuid
import warnings

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db.models import FileField
from django.db.models.fields.files import FieldFile
from PIL import Image, ImageOps, UnidentifiedImageError

MAX_LOGO_BYTES = 5 * 1024 * 1024
MAX_LOGO_DIMENSION = 4096
MAX_LOGO_PIXELS = 8_000_000
MAX_OUTPUT_BYTES = 8 * 1024 * 1024
LOGO_HELP = "Upload PNG, JPEG or WebP, up to 5 MB, 4096 pixels per side and 8 million pixels total. Logos are stored as PNG."


def sanitize_logo(upload):
    try:
        if getattr(upload, "size", 0) > MAX_LOGO_BYTES:
            raise ValidationError("The logo must be 5 MB or smaller.", code="logo_size")
        upload.seek(0)
        data = upload.read(MAX_LOGO_BYTES + 1)
        if len(data) > MAX_LOGO_BYTES:
            raise ValidationError("The logo must be 5 MB or smaller.", code="logo_size")
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if image.format not in {"PNG", "JPEG", "WEBP"} or getattr(image, "n_frames", 1) != 1:
                    raise ValidationError("Choose a static PNG, JPEG or WebP logo.", code="logo_format")
                width, height = image.size
                if width > MAX_LOGO_DIMENSION or height > MAX_LOGO_DIMENSION or width * height > MAX_LOGO_PIXELS:
                    raise ValidationError("The logo exceeds the allowed image dimensions.", code="logo_dimensions")
                image.verify()
            with Image.open(io.BytesIO(data)) as image:
                image.load()
                converted = ImageOps.exif_transpose(image).convert("RGBA")
                # A fresh image discards EXIF, comments, profiles and trailing data.
                clean = Image.frombytes("RGBA", converted.size, converted.tobytes())
                output = io.BytesIO()
                clean.save(output, format="PNG")
        if output.tell() > MAX_OUTPUT_BYTES:
            raise ValidationError("The decoded logo is too large. Choose a smaller image.", code="logo_size")
        return ContentFile(output.getvalue(), name=f"{uuid.uuid4().hex}.png")
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
        raise ValidationError("The logo could not be decoded as a safe image.", code="logo_invalid") from error
    finally:
        upload.seek(0)


def validate_logo(upload):
    if upload and not getattr(upload, "_committed", False):
        sanitize_logo(upload)


class SafeLogoFile(FieldFile):
    def save(self, name, content, save=True):
        clean = sanitize_logo(content)
        # Use only a generated PNG name, including direct FieldFile.save calls.
        return super().save(clean.name, clean, save=save)


class SafeLogoField(FileField):
    attr_class = SafeLogoFile
