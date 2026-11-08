"""Optional JavaScript rendering with Playwright for single-page-app sites.

Off unless RENDER_JS=auto|always and the `playwright` package is installed
(`pip install -e '.[browser]'` + a Chromium). `auto` renders only pages whose
static HTML looks like an empty JS shell (web_extract.looks_like_spa).
PLAYWRIGHT_CHROMIUM_PATH points to a Chromium binary when Playwright's own
download is not present (e.g. /opt/pw-browsers/chromium).
"""

from __future__ import annotations

from loguru import logger

from . import costs
from .config import settings

_browser = None
_pw = None
_failed = False


def enabled() -> bool:
    return settings.render_js in ("auto", "always") and available()


def available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    return not _failed


def _get_browser():
    global _browser, _pw, _failed
    if _browser is not None:
        return _browser
    from playwright.sync_api import sync_playwright

    _pw = sync_playwright().start()
    kwargs = {"headless": True}
    if settings.playwright_chromium_path:
        kwargs["executable_path"] = settings.playwright_chromium_path
    try:
        _browser = _pw.chromium.launch(**kwargs)
    except Exception as exc:  # noqa: BLE001 - any launch failure disables rendering for the run
        logger.warning("Playwright no pudo abrir Chromium ({}); se sigue sin renderizar", str(exc).splitlines()[0])
        _failed = True
        _pw.stop()
        _pw = None
        raise
    return _browser


def render(url: str | None = None, html: str | None = None, timeout_ms: int = 20000) -> str | None:
    """Rendered DOM of `url` (or of an HTML string, for tests); None on any failure."""
    try:
        browser = _get_browser()
    except Exception:  # noqa: BLE001
        return None
    page = browser.new_page(user_agent=settings.user_agent)
    try:
        if html is not None:
            page.set_content(html, wait_until="load", timeout=timeout_ms)
        else:
            page.goto(url, wait_until="networkidle", timeout=timeout_ms)
        costs.tracker.add("playwright_render")
        return page.content()
    except Exception as exc:  # noqa: BLE001
        logger.debug("render {}: {}", url, exc)
        return None
    finally:
        page.close()


def close() -> None:
    global _browser, _pw
    if _browser is not None:
        _browser.close()
        _browser = None
    if _pw is not None:
        _pw.stop()
        _pw = None
