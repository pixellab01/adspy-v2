"""Re-resolve expired Facebook CDN video URLs from the public Ad Library page.

Port of ``meta_main14/services/media_refresh.py``. Why it exists: every fbcdn
video URL carries a signed expiry in its ``oe=`` query parameter (a hex unix
epoch). Once it passes the CDN answers 403, and on the owner's database that is
**13,580 of 13,580** stored video URLs — so without this, every transcription
run on existing data fails 100% before a single byte of audio is heard.

The ad's public page, ``https://www.facebook.com/ads/library/?id=<library_id>``,
renders WITHOUT a login in a headless Chromium and carries a freshly signed
``<video src>`` for the same creative. This module drives that page through
Playwright and reads one URL out of it.

What this deliberately is NOT:

* **Not the extension's job.** The extension is paced at 20 pages/hour for
  account safety and its target is a page URL, not an ad; refreshing 200 video
  links through it would take ten hours. Headless Chromium with no cookies is a
  different browser, a different identity and a different rate — 2.5 s between
  page opens, one worker, two jobs in flight.
* **Not a hard dependency.** Playwright is imported lazily. Without it,
  ``refresh_video_url`` answers ``error="playwright_unavailable"`` and the
  transcription panel offers "Re-scan pages" (the extension re-captures media
  URLs on every scan) instead of "Refresh links".
* **Never raises.** Callers always get ``{"url", "thumbnail", "error"}``; exactly
  one of ``url``/``error`` is set. ``login_wall`` is a hard signal (the page
  demanded auth — stop, do not retry in a loop).

Threading: the sync Playwright API is bound to the thread that started it, so
ONE dedicated worker thread owns the lazily launched browser; callers submit
jobs to it and a bounded semaphore caps the queue.

Install (optional):  pip install playwright && playwright install chromium
"""

from __future__ import annotations

import logging
import re
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from typing import Any

log = logging.getLogger("adspy2.media_refresh")

AD_LIBRARY_URL = "https://www.facebook.com/ads/library/?id={library_id}&country=IN"

MIN_INTERVAL_SECONDS = 2.5
MAX_IN_FLIGHT = 2
DEFAULT_TIMEOUT_MS = 45000
# Refresh a link this many seconds BEFORE it actually expires: a download that
# starts at oe-1s and takes a minute still 403s half way through.
DEFAULT_SAFETY_SECONDS = 1800

# oe= values decoding below this are not plausible unix epochs (~2001); treat
# them as malformed rather than "expired in 1970". This is also what makes the
# test fixtures' `oe=ABC123` read as "unsigned" rather than "expired".
_EPOCH_FLOOR = 10**9

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_state_lock = threading.Lock()
_executor: ThreadPoolExecutor | None = None
_inflight = threading.BoundedSemaphore(MAX_IN_FLIGHT)

# The three below are owned exclusively by the executor's single thread.
_playwright: Any = None
_browser: Any = None
_last_open_monotonic = 0.0


# ===========================================================================
# expiry parsing — pure functions, no Playwright involved
# ===========================================================================
def parse_signed_expiry(url: Any) -> int | None:
    """Unix epoch from the fbcdn ``oe=`` hex param, or None if absent/bogus."""
    try:
        query = urllib.parse.urlsplit(str(url or "")).query
    except ValueError:
        return None
    values = urllib.parse.parse_qs(query).get("oe")
    if not values:
        return None
    raw = str(values[0] or "").strip()
    if not raw:
        return None
    try:
        expiry = int(raw, 16)
    except ValueError:
        return None
    if expiry < _EPOCH_FLOOR:
        return None
    return expiry


def has_signed_expiry(url: Any) -> bool:
    """True when the URL carries a parseable fbcdn signed-expiry param."""
    return parse_signed_expiry(url) is not None


def is_url_expired(url: Any, safety_seconds: int = DEFAULT_SAFETY_SECONDS) -> bool:
    """True when the URL's signed expiry has passed, or will within
    ``safety_seconds``. A URL with no parseable ``oe=`` is treated as live —
    there is nothing to say otherwise, and a download will tell."""
    expiry = parse_signed_expiry(url)
    if expiry is None:
        return False
    return time.time() + max(0, int(safety_seconds or 0)) > expiry


def expiry_label(url: Any) -> str:
    """'expired', 'expires soon' or 'live' — the word next to a link in the UI."""
    expiry = parse_signed_expiry(url)
    if expiry is None:
        return "unsigned"
    now = time.time()
    if now > expiry:
        return "expired"
    if now + DEFAULT_SAFETY_SECONDS > expiry:
        return "expires soon"
    return "live"


# ===========================================================================
# availability
# ===========================================================================
def playwright_available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401

        return True
    except Exception:                                   # noqa: BLE001 - missing/broken
        return False


def refresh_status() -> dict[str, Any]:
    """What the panel needs to know to pick "Refresh links" vs "Re-scan pages".
    No secret, no browser launch — an import check only."""
    available = playwright_available()
    return {
        "available": available,
        "reason": "" if available else (
            "Playwright is not installed, so expired video links cannot be "
            "re-read from the public Ad Library page here. Either run "
            "`pip install playwright && playwright install chromium` in the "
            "app's venv, or re-scan the pages — the extension captures fresh "
            "links on every scan."
        ),
        "min_interval_seconds": MIN_INTERVAL_SECONDS,
        "max_in_flight": MAX_IN_FLIGHT,
    }


# ===========================================================================
# page scraping — everything below runs on the executor's single worker thread
# ===========================================================================
_EXTRACT_JS = """
(libraryId) => {
  const candidates = [];
  for (const video of document.querySelectorAll('video')) {
    let src = video.currentSrc || video.getAttribute('src') || '';
    if (!src) {
      const source = video.querySelector('source[src]');
      if (source) src = source.getAttribute('src') || '';
    }
    if (!src || !/^https?:/i.test(src)) continue;
    let matches = false;
    let node = video.parentElement;
    for (let depth = 0; node && depth < 10; depth += 1) {
      if (libraryId && (node.textContent || '').includes(libraryId)) {
        matches = true;
        break;
      }
      node = node.parentElement;
    }
    candidates.push({
      url: src,
      thumbnail: video.getAttribute('poster') || null,
      matches,
    });
  }
  if (!candidates.length) return null;
  const preferred = candidates.find((item) => item.matches) || candidates[0];
  return { url: preferred.url, thumbnail: preferred.thumbnail };
}
"""

_BLOCKED_JS = """
() => {
  if (document.querySelector('form[action*="login"] input[name="pass"]')) {
    return true;
  }
  const text = ((document.body && document.body.innerText) || '').slice(0, 6000);
  return /you must log in|log in to continue|log in or sign up to view/i.test(text);
}
"""

LOGIN_WALL_RE = re.compile(
    r"you must log in|log in to continue|log in or sign up to view", re.I
)


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    with _state_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="adspy2-media-refresh"
            )
        return _executor


def _ensure_browser() -> Any:
    """Lazy singleton browser, created and used only on the worker thread."""
    global _playwright, _browser
    if _browser is not None:
        try:
            if _browser.is_connected():
                return _browser
        except Exception as exc:                        # noqa: BLE001 - stale handle
            log.debug("stale browser handle, relaunching: %s", exc)
        _browser = None
    if _playwright is None:
        from playwright.sync_api import sync_playwright

        _playwright = sync_playwright().start()
    _browser = _playwright.chromium.launch(headless=True)
    return _browser


def _dismiss_overlays(page: Any) -> None:
    """Best-effort: close the cookie-consent dialog if one is shown. Always the
    most privacy-preserving choice — decline optional cookies."""
    for label in (
        "Decline optional cookies",
        "Only allow essential cookies",
        "Close",
    ):
        try:
            element = page.query_selector(
                f'[aria-label="{label}"][role="button"], button[aria-label="{label}"]'
            )
            if element:
                element.click(timeout=2000)
                page.wait_for_timeout(300)
                return
        except Exception:                               # noqa: BLE001 - best effort
            continue


def _looks_blocked(page: Any) -> bool:
    try:
        if "/login" in str(page.url or ""):
            return True
        return bool(page.evaluate(_BLOCKED_JS))
    except Exception:                                   # noqa: BLE001
        return False


def _refresh_in_worker(library_id: str, timeout_ms: int) -> dict[str, Any]:
    """Open the Ad Library page and pull the first (matching) video src."""
    global _last_open_monotonic

    browser = _ensure_browser()

    # Pacing: page opens >= MIN_INTERVAL_SECONDS apart. Single worker thread,
    # so plain module state is race-free here.
    wait = MIN_INTERVAL_SECONDS - (time.monotonic() - _last_open_monotonic)
    if wait > 0:
        time.sleep(wait)
    _last_open_monotonic = time.monotonic()

    context = browser.new_context(
        user_agent=_USER_AGENT,
        viewport={"width": 1366, "height": 900},
        locale="en-US",
    )
    try:
        page = context.new_page()
        page.goto(
            AD_LIBRARY_URL.format(library_id=urllib.parse.quote(library_id)),
            wait_until="domcontentloaded",
            timeout=timeout_ms,
        )
        _dismiss_overlays(page)

        deadline = time.monotonic() + max(1000, timeout_ms) / 1000.0
        waited_network_idle = False
        while True:
            if _looks_blocked(page):
                return {"url": None, "thumbnail": None, "error": "login_wall"}
            try:
                found = page.evaluate(_EXTRACT_JS, library_id)
            except Exception as exc:                    # noqa: BLE001
                # "Execution context was destroyed" = the page navigated while
                # the script ran. Not a failure: settle and re-loop.
                if "execution context was destroyed" not in str(exc).lower():
                    raise
                log.debug("evaluate raced a navigation; retrying: %s", exc)
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=10_000)
                except Exception:                       # noqa: BLE001
                    pass
                if time.monotonic() >= deadline:
                    return {"url": None, "thumbnail": None, "error": "no_video"}
                continue
            if isinstance(found, dict) and found.get("url"):
                return {
                    "url": str(found["url"]),
                    "thumbnail": found.get("thumbnail") or None,
                    "error": None,
                }
            if time.monotonic() >= deadline:
                return {"url": None, "thumbnail": None, "error": "no_video"}
            if not waited_network_idle:
                waited_network_idle = True
                try:
                    page.wait_for_load_state(
                        "networkidle", timeout=min(10000, timeout_ms)
                    )
                except Exception:                       # noqa: BLE001 - keep polling
                    pass
            else:
                page.wait_for_timeout(500)
    finally:
        try:
            context.close()
        except Exception:                               # noqa: BLE001
            pass


# ===========================================================================
# public entry
# ===========================================================================
def refresh_video_url(
    library_id: str | int, timeout_ms: int = DEFAULT_TIMEOUT_MS
) -> dict[str, Any]:
    """A freshly signed video URL for an ad, read off its public Ad Library page.

    Never raises; always returns ``{"url", "thumbnail", "error"}`` with exactly
    one of ``url``/``error`` set. Errors: ``missing_library_id``,
    ``playwright_unavailable``, ``login_wall`` (the page demanded auth — a hard
    stop, never retried in a loop), ``no_video`` (image-only ad, or the page
    never rendered a video) or ``<ExceptionName>: <message>``.
    """
    result: dict[str, Any] = {"url": None, "thumbnail": None, "error": None}
    library = str(library_id or "").strip()
    if not library:
        result["error"] = "missing_library_id"
        return result
    if not playwright_available():
        result["error"] = "playwright_unavailable"
        return result

    timeout_ms = max(5000, min(int(timeout_ms or DEFAULT_TIMEOUT_MS), 180000))
    _inflight.acquire()
    try:
        future = _get_executor().submit(_refresh_in_worker, library, timeout_ms)
        # Worst case a job waits behind (MAX_IN_FLIGHT - 1) full runs.
        budget = (timeout_ms / 1000.0) * MAX_IN_FLIGHT + 30
        outcome = future.result(timeout=budget)
        if isinstance(outcome, dict) and (outcome.get("url") or outcome.get("error")):
            return outcome
        result["error"] = "no_video"
        return result
    except Exception as exc:                            # noqa: BLE001 - never crash a caller
        message = re.sub(r"\s+", " ", str(exc)).strip()[:300]
        result["error"] = (
            f"{exc.__class__.__name__}: {message}" if message else exc.__class__.__name__
        )
        return result
    finally:
        _inflight.release()


def _shutdown_in_worker() -> None:
    global _playwright, _browser, _last_open_monotonic
    try:
        if _browser is not None:
            _browser.close()
    except Exception as exc:                            # noqa: BLE001
        log.debug("browser close during shutdown failed: %s", exc)
    try:
        if _playwright is not None:
            _playwright.stop()
    except Exception as exc:                            # noqa: BLE001
        log.debug("playwright stop during shutdown failed: %s", exc)
    _browser = None
    _playwright = None
    _last_open_monotonic = 0.0


def close_browser() -> None:
    """Shut the shared browser (and its worker thread) down cleanly. Safe to
    call when nothing was ever launched; a later refresh relaunches."""
    global _executor
    with _state_lock:
        executor = _executor
        _executor = None
    if executor is None:
        return
    try:
        executor.submit(_shutdown_in_worker).result(timeout=20)
    except Exception as exc:                            # noqa: BLE001
        log.debug("worker shutdown did not complete cleanly: %s", exc)
    executor.shutdown(wait=False)


__all__ = [
    "AD_LIBRARY_URL", "MIN_INTERVAL_SECONDS", "MAX_IN_FLIGHT", "LOGIN_WALL_RE",
    "close_browser", "expiry_label", "has_signed_expiry", "is_url_expired",
    "parse_signed_expiry", "playwright_available", "refresh_status",
    "refresh_video_url",
]
