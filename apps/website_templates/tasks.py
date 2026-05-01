"""Celery tasks for website template management."""
import logging
from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(bind=True, name="apps.website_templates.tasks.import_templates")
def import_templates(self, force=False, skip_extract=False):
    """
    Celery-runnable wrapper around the import_website_templates management
    command.  Run from admin tools to scan ZIP files and populate the
    WebsiteTemplate table.
    """
    from django.core.management import call_command
    import io

    out = io.StringIO()
    try:
        kwargs = {"stdout": out, "stderr": out}
        if force:
            kwargs["force"] = True
        if skip_extract:
            kwargs["skip_extract"] = True
        call_command("import_website_templates", **kwargs)
        output = out.getvalue()
        logger.info("import_website_templates completed:\n%s", output)
        return {"status": "ok", "output": output}
    except Exception as exc:
        logger.error("import_website_templates task failed: %s", exc)
        raise self.retry(exc=exc, max_retries=0)
