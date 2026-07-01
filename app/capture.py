"""Capture the active Chrome tab over the DevTools Protocol.

Connects to a Chrome that was started with --remote-debugging-port (see
scripts/launch-chrome.sh), finds the tab you're actually looking at, and pulls
its readable text plus its diagrams as element screenshots (so they come out
correct even when the page is behind a login).
"""

from __future__ import annotations

import asyncio
import base64
import re

import trafilatura
from playwright.async_api import Page, async_playwright

from app.config import DIAGRAMS_DIR, settings
from app.models import Diagram, PageCapture, Step

# Ignore tiny images (icons, spacers, logos); keep real diagrams.
# Minimum natural size for an image to count as a "diagram". Set above small
# secondary thumbnails (e.g. iFixit shows ~186x140 thumbnail strips beside each
# step's main 593-wide image) so those don't eat the diagram budget and crowd
# out later steps — we want one real image per step, not three tiny ones.
MIN_W, MIN_H = 240, 160
MAX_DIAGRAMS = 25

# Per-image, in the browser: pull alt text and nearby caption/heading for context.
_CONTEXT_JS = r"""
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
  // Video detection: is this image a poster inside (or beside) a video player?
  // If so, the lesson should send the learner to the source page to watch it.
  let isVideo = false, anchor = '';
  let p = img;
  for (let i = 0; i < 4 && p && p.tagName !== 'BODY'; i++) {
    const cls = (p.className && p.className.toString) ? p.className.toString() : '';
    if (p.tagName === 'VIDEO') isVideo = true;
    if (/\b(video|player|vjs|jw-?player|media-player|brightcove|kaltura)\b/i.test(cls)) isVideo = true;
    // a sibling player directly inside this small container (not the whole page)
    for (const c of p.children) {
      if (c.tagName === 'VIDEO') isVideo = true;
      if (c.tagName === 'IFRAME' && /player|video|youtube|vimeo/i.test(c.getAttribute('src') || '')) isVideo = true;
    }
    if (!anchor && p.id) anchor = '#' + p.id;
    if (isVideo) break;
    p = p.parentElement;
  }
  return { alt, context, isVideo, anchor };
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
    """Pick the lesson tab: prefer a visible http(s) tab, else any loaded
    http(s) tab. The app's own player tab is always skipped.

    Returns None if every non-player tab reports an empty url — Chrome has
    *discarded* (frozen) it, so Playwright has no live page for it. The caller
    then wakes it over raw CDP (see _wake_lesson_tab).
    """
    others = [p for p in pages if not _is_player_url(p.url)]
    fallback: Page | None = None
    for p in others:
        if not p.url.startswith("http"):
            continue
        fallback = fallback or p
        try:
            if await asyncio.wait_for(
                p.evaluate("document.visibilityState === 'visible'"), timeout=4
            ):
                return p
        except Exception:
            continue
    return fallback


async def _cdp_page_targets(cdp_url: str) -> list[dict]:
    """Raw CDP /json/list — the authoritative tab list (real url/title/id) even
    for discarded tabs that Playwright surfaces with an empty url."""
    import json
    import urllib.request

    def _get() -> list[dict]:
        url = cdp_url.rstrip("/") + "/json/list"
        with urllib.request.urlopen(url, timeout=4) as r:
            return json.load(r)

    try:
        loop = asyncio.get_event_loop()
        targets = await loop.run_in_executor(None, _get)
    except Exception:
        return []
    return [t for t in targets if t.get("type") == "page"]


async def _wake_lesson_tab(browser, cdp_url: str) -> Page | None:
    """Last resort when every non-player tab is discarded (empty url in
    Playwright): find the lesson tab via raw CDP and *activate* it, which makes
    Chrome reload the discarded tab without needing an attached live page. Then
    return the now-live Playwright page once its url resolves.
    """
    import urllib.request

    targets = await _cdp_page_targets(cdp_url)
    cand = [
        t for t in targets
        if t.get("url", "").startswith("http") and not _is_player_url(t["url"])
    ]
    if not cand:
        return None
    target_id, want = cand[0]["id"], cand[0]["url"]

    def _activate() -> None:
        try:
            urllib.request.urlopen(
                cdp_url.rstrip("/") + f"/json/activate/{target_id}", timeout=4
            ).read()
        except Exception:
            pass

    await asyncio.get_event_loop().run_in_executor(None, _activate)

    # Activation reloads the discarded tab; wait for its Playwright page to
    # report a real url (same target id, so the page object updates in place).
    for _ in range(24):  # ~7s
        for ctx in browser.contexts:
            for p in ctx.pages:
                if p.url == want or (
                    p.url.startswith("http") and not _is_player_url(p.url)
                ):
                    return p
        await asyncio.sleep(0.3)
    return None


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
  // Wait for the image to paint, but NEVER block forever: for a lazy/offscreen
  // or never-loading image, img.decode() hangs (it neither resolves nor rejects),
  // which would stall the whole capture. Race it against a short timer.
  try {
    if (img.decode) {
      await Promise.race([
        img.decode().catch(() => {}),
        new Promise((r) => setTimeout(r, 1200)),
      ]);
    }
  } catch (e) {}
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

# iFixit (and similar guide CDNs) serve one image at several sizes via a
# filename suffix; the page displays the ~593px ".standard", which looks small
# on a large screen. Bump known-small suffixes to ".large" (~1024px) when we
# fetch. If that variant 404s, the caller falls back to a screenshot.
_IFIXIT_SMALL = {"thumbnail", "200x150", "standard", "medium"}

# WikiHow serves one image at many widths via a `-<N>px-` token in its /thumb/
# path; the inline markup usually points at a small (~460px) variant while the
# page itself displays a larger one via srcset. Bump anything under this up.
_WIKIHOW_MIN_W = 728


def _hires_src(url: str) -> str:
    m = re.match(r"(https://guide-images\.cdn\.ifixit\.com/\S+)\.([a-z0-9]+)$", url)
    if m and m.group(2) in _IFIXIT_SMALL:
        return f"{m.group(1)}.large"
    if "wikihow.com/images/thumb/" in url:
        return re.sub(
            r"-(\d+)px-",
            lambda mo: f"-{max(int(mo.group(1)), _WIKIHOW_MIN_W)}px-",
            url,
        )
    return url


async def _fetch_image_bytes(page: Page, url: str) -> bytes | None:
    """Download image bytes through the authenticated session, preferring a
    hi-res variant (`_hires_src`) but falling back to the original URL if the
    upgraded one fails — so guessing a larger size can never drop the image."""
    candidates = [url]
    hi = _hires_src(url)
    if hi != url:
        candidates.insert(0, hi)
    for cand in candidates:
        try:
            resp = await page.context.request.get(cand, timeout=8000)
            if not resp.ok:
                continue
            body = await resp.body()
            if body:
                return body
        except Exception:
            continue
    return None


async def _fetch_png(page: Page, src: str) -> bytes | None:
    """Download the real PNG bytes (through the authenticated session). None if
    it isn't a fetchable PNG — caller then falls back to a screenshot."""
    try:
        if src.startswith("data:image/png"):
            return base64.b64decode(src.split(",", 1)[1])
        if src.startswith("http"):
            resp = await page.context.request.get(src, timeout=8000)
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
            # Hard backstop: evaluate() has no timeout of its own, so cap it —
            # a single pathological image must never freeze the whole capture.
            loaded = await asyncio.wait_for(handle.evaluate(_LOAD_JS), timeout=6)
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
            data = await _fetch_png(page, _hires_src(src)) if src else None
            if data:
                dest.write_bytes(data)
            else:
                await handle.scroll_into_view_if_needed(timeout=2000)
                await handle.screenshot(path=str(dest), timeout=8000)

            meta = await handle.evaluate(_CONTEXT_JS)
            diagrams.append(
                Diagram(
                    idx=idx,
                    png_path=png_name,
                    alt=meta.get("alt", ""),
                    context=meta.get("context", ""),
                    is_video=bool(meta.get("isVideo")),
                    anchor=meta.get("anchor", "") or "",
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
        body = await _fetch_image_bytes(page, url)
        if body is None:
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


# --- Step-by-step instruction pages (iFixit; WikiHow later) ----------------
#
# Some pages ARE an ordered procedure: each step has its own text, image group,
# and a page anchor. For these we capture the structure so the lesson can run
# one segment per step with a per-step image slideshow and a "jump to this step"
# link, instead of flattening everything into one blob. iFixit selectors are
# confirmed against the live DOM: container `.step` (id "s<NNN>" doubles as the
# scroll anchor), a "Step N ..." title line, and per-step <img> tags served from
# guide-images.cdn.ifixit.com.
STEP_MAX_IMAGES = 220  # safety cap across all steps (a 48-step guide has ~100+)
STEP_MAX_PER_STEP = 5  # cap images per single step so one step can't hog the budget

_STEPS_JS = r"""
() => {
  const all = [...document.querySelectorAll('.step')];
  // keep only top-level .step nodes (iFixit nests image divs with .step-ish ids)
  const top = all.filter(e => !all.some(o => o !== e && o.contains(e)));
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const steps = [];
  for (const el of top) {
    const text = norm(el.innerText);
    if (!text) continue;
    const hdrEl = el.querySelector('.step-title, .step-title-text, .step-number, h2, h3');
    const header = hdrEl ? norm(hdrEl.innerText) : '';
    const m = (header || text).match(/^Step\s+\d+/i);
    const number = m ? m[0] : '';
    const title = header ? norm(header.replace(/^Step\s+\d+\s*/i, '')) : '';
    const imgs = [...el.querySelectorAll('img')]
      .map(i => {
        const ss = i.getAttribute('srcset') || i.getAttribute('data-srcset') || '';
        const first = ss ? ss.split(',')[0].trim().split(' ')[0] : '';
        return i.currentSrc || i.src || i.getAttribute('data-src') || first || '';
      })
      .filter(u => u.includes('guide-images.cdn.ifixit.com'));
    const seen = new Set(), urls = [];
    for (const u of imgs) {
      const h = (u.match(/\/igi\/([^.\/]+)\./) || [])[1];
      if (h && !seen.has(h)) { seen.add(h); urls.push(u); }
    }
    steps.push({ anchor: el.id ? '#' + el.id : '', number, title, text, imgs: urls });
  }
  return steps;
}
"""


def _is_step_page(url: str) -> bool:
    """True for pages we know capture cleanly as an ordered procedure."""
    return bool(re.search(r"ifixit\.com/(Guide|Teardown)/", url, re.I))


def _img_key(url: str) -> str:
    """Dedup key for an image across steps: for iFixit the CDN hash identifies the
    image regardless of size suffix; otherwise the URL itself."""
    m = re.search(r"/igi/([^./]+)\.", url)
    return m.group(1) if m else url


_SCROLL_JS = (
    "async () => { const h = document.body.scrollHeight; "
    "for (let y = 0; y < h; y += 700) { window.scrollTo(0, y); "
    "await new Promise(r => setTimeout(r, 80)); } window.scrollTo(0, 0); }"
)


async def _extract_steps(page: Page) -> list[dict]:
    """Run the step extractor in the page; [] if it isn't a step page after all.

    iFixit lazy-loads step images as you scroll, so below-the-fold steps would
    otherwise have placeholder srcs. Scroll the whole page first to force every
    step image to resolve a real URL, then read."""
    try:
        await asyncio.wait_for(page.evaluate(_SCROLL_JS), timeout=12)
        await page.wait_for_timeout(500)
    except Exception:
        pass
    try:
        raw = await asyncio.wait_for(page.evaluate(_STEPS_JS), timeout=8)
    except Exception:
        return []
    return raw if isinstance(raw, list) else []


async def _build_step_diagrams(page: Page, raw: list[dict]) -> tuple[list[Diagram], list[Step]]:
    """Download each step's images (deduped across steps, capped per-step and
    overall) and map them to Steps. Downloads run concurrently so a long guide
    (100+ images) doesn't stall. Returns (diagrams, steps) where each
    Step.image_idxs indexes into diagrams."""
    # 1) Gather unique images in document order; record each step's keys.
    order: list[tuple[str, str]] = []  # (key, url), deduped, order-preserving
    seen: set[str] = set()
    step_keys: list[list[str]] = []
    for s in raw:
        keys: list[str] = []
        for url in s.get("imgs", [])[:STEP_MAX_PER_STEP]:
            key = _img_key(url)
            if key not in seen:
                if len(order) >= STEP_MAX_IMAGES:
                    continue  # over budget — skip new images, keep references below
                seen.add(key)
                order.append((key, url))
            keys.append(key)
        step_keys.append(keys)

    # 2) Download concurrently (bounded), keep only valid images.
    results: dict[str, tuple[bytes, str]] = {}
    sem = asyncio.Semaphore(8)

    async def fetch(key: str, url: str) -> None:
        async with sem:
            body = await _fetch_image_bytes(page, url)
            if body is None:
                return  # slow/broken image — skip, never stall the capture
            ext = _img_ext(body, url)
            if ext is None or len(body) < 3000:
                return
            results[key] = (body, ext)

    await asyncio.gather(*(fetch(k, u) for k, u in order))

    # 3) Assign diagram idxs in document order and write the files.
    diagrams: list[Diagram] = []
    key_to_idx: dict[str, int] = {}
    for key, _ in order:
        if key not in results:
            continue
        body, ext = results[key]
        idx = len(diagrams)
        name = f"diagram-{idx}.{ext}"
        (DIAGRAMS_DIR / name).write_bytes(body)
        key_to_idx[key] = idx
        diagrams.append(Diagram(idx=idx, png_path=name))

    # 4) Build steps; tag each step's diagrams with the step title for captions.
    steps: list[Step] = []
    for s, keys in zip(raw, step_keys):
        idxs = [key_to_idx[k] for k in keys if k in key_to_idx]
        for i in idxs:
            diagrams[i].alt = s.get("title", "")
            diagrams[i].context = s.get("title", "")
        steps.append(
            Step(
                number=s.get("number", ""),
                title=s.get("title", ""),
                text=s.get("text", ""),
                image_idxs=idxs,
                anchor=s.get("anchor", ""),
            )
        )
    return diagrams, steps


async def capture_active_tab(on_stage=None, step_mode: str = "auto") -> PageCapture:
    """Attach to the running Chrome and capture whatever tab is in front.

    `on_stage(stage, label)`, if given, is called as work progresses so the UI
    can narrate it ("Reading the page text…", "Capturing diagrams…").

    `step_mode` controls Step Mode capture: "auto" (detect step pages),
    "on" (force the step extractor), or "off" (always use the freeform flow).
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
                # Every non-player tab is discarded (Playwright sees an empty
                # url). Wake the lesson tab over raw CDP and re-attach.
                page = await _wake_lesson_tab(browser, settings.cdp_url)
            if page is None:
                # Still nothing — report the authoritative CDP tab list so a page
                # open in the wrong Chrome (or no lesson tab at all) is obvious.
                targets = await _cdp_page_targets(settings.cdp_url)
                seen = [
                    t.get("url", "")[:70]
                    for t in targets
                    if not _is_player_url(t.get("url", ""))
                ]
                detail = "; ".join(s for s in seen if s) or "(none)"
                raise ConnectionError(
                    "No readable web page found in the app's Chrome. Non-player "
                    f"tabs it can see right now: {detail}. Open your lesson page in "
                    "the same Chrome window the player launched in, let it finish "
                    "loading, then click Teach this page again."
                )

            # Bring the lesson tab to the front so it's actually rendered: a
            # backgrounded tab may skip layout (leaving innerText empty) and not
            # finish lazy-loading. The user shouldn't have to switch tabs by hand.
            try:
                await asyncio.wait_for(page.bring_to_front(), timeout=5)
                await page.wait_for_load_state("domcontentloaded", timeout=3000)
                await page.wait_for_timeout(400)  # let layout / consent JS settle
            except Exception:
                pass

            stage("reading", "Reading the page text…")
            try:
                title = (await asyncio.wait_for(page.title(), timeout=5) or "").strip()
            except Exception:
                title = ""
            server_html = ""  # fetched on demand below; reused for image extraction

            # 1) Live rendered DOM first — covers login-gated / JS-rendered pages
            #    (e.g. Cisco) where the server HTML alone wouldn't have the content.
            text = ""
            try:
                html = await asyncio.wait_for(page.content(), timeout=8)
                text = trafilatura.extract(
                    html, include_comments=False, include_tables=True
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

            # 3) Visible text straight from the DOM. trafilatura's article
            #    heuristics under-extract structured step-by-step pages (e.g.
            #    iFixit renders 14k chars of steps but trafilatura keeps only ~5k,
            #    dropping the later steps). The raw DOM textContent is noisier
            #    (nav/footer) but complete, so prefer it when it's much larger —
            #    a strong signal trafilatura missed the bulk of the content.
            try:
                dom_text = await asyncio.wait_for(page.evaluate(_DOM_TEXT_JS), timeout=6)
            except Exception:
                dom_text = ""
            cur = len(text.strip())
            if len(dom_text.strip()) > max(cur * 2, 600):
                text = dom_text
            # Step Mode: a recognized step-by-step page (or forced) is captured as
            # an ordered procedure — per-step text, image group, and anchor — so the
            # lesson can do one segment per step. Falls through to the freeform flow
            # if the extractor finds nothing.
            steps: list[Step] = []
            diagrams: list[Diagram] = []
            if step_mode == "on" or (step_mode == "auto" and _is_step_page(page.url)):
                stage("reading", "Capturing steps…")
                raw = await _extract_steps(page)
                if raw:
                    diagrams, steps = await _build_step_diagrams(page, raw)
                    joined = "\n\n".join(
                        f"{s.number} {s.title}\n{s.text}".strip() for s in steps
                    ).strip()
                    if joined:
                        text = joined

            if not steps:
                stage("reading", "Capturing diagrams…")
                try:
                    diagrams = await _extract_diagrams(page)
                except Exception:
                    diagrams = []
                # The live DOM often exposes only above-the-fold images on lazy-loading
                # how-to pages (and none at all on ad-poisoned ones), so when we have the
                # server HTML, pull its full image set and keep whichever is richer.
                if server_html:
                    try:
                        html_diagrams = await _diagrams_from_html(page, server_html, page.url)
                        if len(html_diagrams) > len(diagrams):
                            diagrams = html_diagrams
                    except Exception:
                        pass

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

            return PageCapture(
                url=page.url, title=title, text=text, diagrams=diagrams, steps=steps
            )
        finally:
            # Detach without closing the user's real browser.
            await browser.close()


async def scroll_to_anchor(anchor: str) -> bool:
    """Bring the original source tab to the front and, if an anchor is given,
    scroll it there. Used by "open this step on the page" (step anchor) and
    "watch this video on the page" (empty anchor = just switch to the tab).
    Reuses the same CDP tab pick/wake as capture. Returns False if Chrome/tab
    can't be reached."""
    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.connect_over_cdp(settings.cdp_url)
        except Exception:
            return False
        try:
            pages = [p for ctx in browser.contexts for p in ctx.pages]
            page = await _pick_active_page(pages)
            if page is None:
                page = await _wake_lesson_tab(browser, settings.cdp_url)
            if page is None:
                return False
            try:
                await asyncio.wait_for(page.bring_to_front(), timeout=5)
                if anchor:
                    await asyncio.wait_for(
                        page.evaluate(
                            "(a) => { try { const el = document.querySelector(a); "
                            "if (el) { el.scrollIntoView({behavior:'smooth', block:'start'}); } "
                            "else { location.hash = a; } } catch (e) { location.hash = a; } }",
                            anchor,
                        ),
                        timeout=5,
                    )
            except Exception:
                return False
            return True
        finally:
            await browser.close()
