"""Capture the active Chrome tab over the DevTools Protocol.

Connects to a Chrome that was started with --remote-debugging-port (see
scripts/launch-chrome.sh), finds the tab you're actually looking at, and pulls
its readable text plus its diagrams as element screenshots (so they come out
correct even when the page is behind a login).
"""

from __future__ import annotations

import base64
import re

import trafilatura
from playwright.async_api import Page, async_playwright

from app.config import DIAGRAMS_DIR, settings
from app.models import Diagram, PageCapture

# Ignore tiny images (icons, spacers, logos); keep real diagrams.
MIN_W, MIN_H = 180, 110
MAX_DIAGRAMS = 25

# Per-image, in the browser: pull alt text and nearby caption/heading for context.
_CONTEXT_JS = """
(img) => {
  const alt = img.getAttribute('alt') || '';
  let context = '';
  const fig = img.closest('figure');
  if (fig) {
    const cap = fig.querySelector('figcaption');
    if (cap) context = cap.innerText.trim();
  }
  if (!context) {
    let el = img;
    for (let i = 0; i < 6 && el; i++) {
      el = el.previousElementSibling || el.parentElement;
      if (el && /^H[1-6]$/.test(el.tagName)) { context = el.innerText.trim(); break; }
    }
  }
  return { alt, context };
}
"""


def _is_player_url(url: str) -> bool:
    """True for the app's own player page, so we never narrate the control panel."""
    port = settings.port
    return any(
        url.startswith(f"http://{host}:{port}")
        for host in (settings.host, "127.0.0.1", "localhost")
    )


async def _pick_active_page(pages: list[Page]) -> Page | None:
    """Choose the visible http(s) tab; fall back to the last http(s) page.

    The app's own player tab is skipped, so capturing always targets the lesson
    page even if the player happens to be the foreground tab.
    """
    fallback: Page | None = None
    for p in pages:
        if not p.url.startswith("http") or _is_player_url(p.url):
            continue
        fallback = p
        try:
            if await p.evaluate("document.visibilityState === 'visible'"):
                return p
        except Exception:
            continue
    return fallback


# Many sites (WikiHow, news, docs...) lazy-load images: the real URL lives in
# data-src / srcset and `src` is a blank placeholder until you scroll. Resolve the
# real source, assign it, and wait for it to paint so the screenshot isn't empty.
_LOAD_JS = """
async (img) => {
  const first = (s) => (s || '').trim().split(',')[0].trim().split(/\\s+/)[0];
  let src = img.currentSrc || img.src || '';
  if (!src || src.startsWith('data:image/gif') || src.startsWith('data:image/svg')) {
    const real = img.getAttribute('data-src') || first(img.getAttribute('data-srcset'))
              || first(img.getAttribute('srcset')) || '';
    if (real) { try { img.src = real; } catch (e) {} }
  }
  try { if (img.decode) await img.decode(); } catch (e) {}
  return { src: img.currentSrc || img.src || '', w: img.naturalWidth || 0, h: img.naturalHeight || 0 };
}
"""

# Fallback when the article extractor (trafilatura) comes up empty on a rendered
# DOM: take the visible text of the main content region.
_DOM_TEXT_JS = """
() => {
  const pick = document.querySelector('main, article, [role="main"]') || document.body;
  if (!pick) return '';
  // textContent is layout-independent, so it returns text even when the tab is
  // backgrounded (innerText goes empty for non-rendered tabs). Strip non-content.
  const clone = pick.cloneNode(true);
  clone.querySelectorAll('script, style, noscript, template, svg').forEach((n) => n.remove());
  return (clone.textContent || '').replace(/[ \\t]+/g, ' ').replace(/\\n\\s*\\n\\s*\\n+/g, '\\n\\n').trim();
}
"""

# Quick look at what's actually on the page, for a useful "nothing found" error.
_DIAG_JS = "() => (document.body ? document.body.innerText : '').replace(/\\s+/g,' ').trim().slice(0, 160)"

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


async def _fetch_png(page: Page, src: str) -> bytes | None:
    """Download the real PNG bytes (through the authenticated session). None if
    it isn't a fetchable PNG — caller then falls back to a screenshot."""
    try:
        if src.startswith("data:image/png"):
            return base64.b64decode(src.split(",", 1)[1])
        if src.startswith("http"):
            resp = await page.context.request.get(src)
            if resp.ok:
                body = await resp.body()
                if body[:8] == _PNG_MAGIC:
                    return body
    except Exception:
        return None
    return None


async def _extract_diagrams(page: Page) -> list[Diagram]:
    diagrams: list[Diagram] = []
    handles = await page.query_selector_all("img")
    for handle in handles:
        if len(diagrams) >= MAX_DIAGRAMS:
            break
        try:
            # Resolve lazy-loaded sources and wait for the image to paint, so we
            # can both size it correctly and screenshot it without a blank frame.
            loaded = await handle.evaluate(_LOAD_JS)
            box = await handle.bounding_box()
            disp_w = box["width"] if box else 0
            disp_h = box["height"] if box else 0
            nat_w = loaded.get("w", 0) or 0
            nat_h = loaded.get("h", 0) or 0
            # Keep images that are large either on screen or in their own pixels
            # (lazy ones may not have a layout box until scrolled into view).
            if max(disp_w, nat_w) < MIN_W or max(disp_h, nat_h) < MIN_H:
                continue
            idx = len(diagrams)
            png_name = f"diagram-{idx}.png"
            dest = DIAGRAMS_DIR / png_name

            # Prefer the real PNG file — element screenshots of transparent PNGs
            # bleed the page content behind them into the image. Non-PNG images
            # (JPG/WebP, common on the open web) fall back to a screenshot, which
            # is reliable now that the image is loaded.
            src = loaded.get("src", "")
            data = await _fetch_png(page, src) if src else None
            if data:
                dest.write_bytes(data)
            else:
                await handle.scroll_into_view_if_needed(timeout=2000)
                await handle.screenshot(path=str(dest))

            meta = await handle.evaluate(_CONTEXT_JS)
            diagrams.append(
                Diagram(
                    idx=idx,
                    png_path=png_name,
                    alt=meta.get("alt", ""),
                    context=meta.get("context", ""),
                )
            )
        except Exception:
            continue  # skip images that won't load/screenshot (lazy/offscreen/etc.)
    return diagrams


# Hosts whose images are ads/trackers, never lesson content.
_AD_HOSTS = (
    "doubleclick", "googlesyndication", "googleadservices", "google-analytics",
    "adnxs", "amazon-adsystem", "criteo", "pubmatic", "rubiconproject", "openx",
    "adsystem", "adservice", "scorecardresearch", "moatads", "/ads/", "/ad/",
    "pixel", "/sync", "usersync", "/beacon", "1x1", "spacer",
)

_EXT_BY_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
)


def _img_ext(body: bytes, url: str) -> str | None:
    """File extension from magic bytes (falls back to a WebP/RIFF check)."""
    for magic, ext in _EXT_BY_MAGIC:
        if body.startswith(magic):
            return ext
    if body[:4] == b"RIFF" and body[8:12] == b"WEBP":
        return "webp"
    return None


def _img_context(img) -> str:
    """Nearest figcaption or preceding heading text, for the vision/writer engines."""
    node = img
    for _ in range(6):
        node = node.getparent()
        if node is None:
            break
        cap = node.find(".//figcaption")
        if cap is not None and (cap.text_content() or "").strip():
            return cap.text_content().strip()[:300]
        prev = node.getprevious()
        while prev is not None:
            if isinstance(prev.tag, str) and re.fullmatch(r"h[1-6]", prev.tag, re.I):
                return (prev.text_content() or "").strip()[:300]
            prev = prev.getprevious()
    return ""


async def _diagrams_from_html(page: Page, html: str, base_url: str) -> list[Diagram]:
    """Pull content images out of the *server* HTML and download them through the
    authenticated session. Used when the live DOM is unreadable (ad-heavy pages
    poison the main-frame context, so on-page screenshotting fails)."""
    from urllib.parse import urljoin

    import lxml.html

    try:
        doc = lxml.html.fromstring(html)
    except Exception:
        return []

    diagrams: list[Diagram] = []
    seen: set[str] = set()
    for img in doc.iter("img"):
        if len(diagrams) >= MAX_DIAGRAMS:
            break
        raw = (
            img.get("data-src")
            or img.get("src")
            or (img.get("srcset") or img.get("data-srcset") or "").strip().split(",")[0].split(" ")[0]
        )
        if not raw or raw.startswith("data:"):
            continue
        url = urljoin(base_url, raw)
        if url in seen or any(h in url.lower() for h in _AD_HOSTS):
            continue
        seen.add(url)
        # Drop images the markup declares too small (icons, avatars, spacers).
        try:
            w = int(re.sub(r"\D", "", img.get("width") or "") or 0)
            h = int(re.sub(r"\D", "", img.get("height") or "") or 0)
        except ValueError:
            w = h = 0
        if (w and w < MIN_W) or (h and h < MIN_H):
            continue
        try:
            resp = await page.context.request.get(url, timeout=8000)
            if not resp.ok:
                continue
            body = await resp.body()
        except Exception:
            continue  # slow/broken image — skip it, never stall the capture
        ext = _img_ext(body, url)
        if ext is None or len(body) < 3000:  # unknown type or too small to be a diagram
            continue
        idx = len(diagrams)
        name = f"diagram-{idx}.{ext}"
        (DIAGRAMS_DIR / name).write_bytes(body)
        diagrams.append(
            Diagram(
                idx=idx,
                png_path=name,
                alt=(img.get("alt") or "").strip(),
                context=_img_context(img),
            )
        )
    return diagrams


async def capture_active_tab(on_stage=None) -> PageCapture:
    """Attach to the running Chrome and capture whatever tab is in front.

    `on_stage(stage, label)`, if given, is called as work progresses so the UI
    can narrate it ("Reading the page text…", "Capturing diagrams…").
    """
    def stage(s: str, label: str) -> None:
        if on_stage:
            on_stage(s, label)

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.connect_over_cdp(settings.cdp_url)
        except Exception as e:
            raise ConnectionError(
                "Couldn't reach Chrome's debugging port. Start Chrome with "
                "scripts/launch-chrome.sh, then log in and open your lesson page."
            ) from e

        try:
            pages = [p for ctx in browser.contexts for p in ctx.pages]
            page = await _pick_active_page(pages)
            if page is None:
                raise ConnectionError("No open web page found in Chrome to narrate.")

            # Bring the lesson tab to the front so it's actually rendered: a
            # backgrounded tab may skip layout (leaving innerText empty) and not
            # finish lazy-loading. The user shouldn't have to switch tabs by hand.
            try:
                await page.bring_to_front()
                await page.wait_for_load_state("domcontentloaded", timeout=3000)
                await page.wait_for_timeout(400)  # let layout / consent JS settle
            except Exception:
                pass

            stage("reading", "Reading the page text…")
            title = (await page.title() or "").strip()
            server_html = ""  # fetched on demand below; reused for image extraction

            # 1) Live rendered DOM first — covers login-gated / JS-rendered pages
            #    (e.g. Cisco) where the server HTML alone wouldn't have the content.
            text = ""
            try:
                text = trafilatura.extract(
                    await page.content(), include_comments=False, include_tables=True
                ) or ""
            except Exception:
                pass

            # 2) Ad-heavy pages (WikiHow, news, ...) can bind the live main-frame
            #    JS context to a rogue ad iframe, so page.content()/evaluate read an
            #    empty document. Fetch the server HTML through the browser's
            #    authenticated session (cookies intact) and prefer it when richer.
            if len(text.strip()) < 600:
                try:
                    resp = await page.context.request.get(page.url, timeout=15000)
                    if resp.ok:
                        server_html = await resp.text()
                        server_text = trafilatura.extract(
                            server_html, include_comments=False, include_tables=True
                        ) or ""
                        if len(server_text.strip()) > len(text.strip()):
                            text = server_text
                        if not title:
                            m = re.search(r"<title[^>]*>(.*?)</title>", server_html, re.I | re.S)
                            if m:
                                title = re.sub(r"\s+", " ", m.group(1)).strip()
                except Exception:
                    pass

            # 3) Last resort: visible text straight from the DOM.
            if len(text.strip()) < 200:
                try:
                    dom_text = await page.evaluate(_DOM_TEXT_JS)
                except Exception:
                    dom_text = ""
                if len(dom_text.strip()) > len(text.strip()):
                    text = dom_text
            stage("reading", "Capturing diagrams…")
            diagrams = await _extract_diagrams(page)
            # If the live DOM gave nothing (ad-poisoned page), pull images out of
            # the server HTML and download them through the authenticated session.
            if not diagrams and server_html:
                diagrams = await _diagrams_from_html(page, server_html, page.url)

            if not text.strip() and not diagrams:
                snippet = ""
                try:
                    snippet = (await page.evaluate(_DIAG_JS)).strip()
                except Exception:
                    pass
                detail = f": “{snippet}…”" if snippet else " (page appears blank)"
                raise ValueError(
                    "Nothing to narrate: no readable text or diagrams on "
                    f"{page.url} — “{title}”. The tab's visible text was "
                    f"{len(snippet)} chars{detail}. If this is a cookie/consent or "
                    "'verify you are human' screen, clear it in the debugging "
                    "Chrome (or reload the page there) and try again."
                )

            # Hand focus back to the player tab (we briefly fronted the lesson tab).
            player = next((p for p in pages if _is_player_url(p.url)), None)
            if player is not None:
                try:
                    await player.bring_to_front()
                except Exception:
                    pass

            return PageCapture(url=page.url, title=title, text=text, diagrams=diagrams)
        finally:
            # Detach without closing the user's real browser.
            await browser.close()
