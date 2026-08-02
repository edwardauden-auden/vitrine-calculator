"""
AgentMediaBox — page analyzer (v1)

Approach: headless-browser the URL the agent pastes, take a full-page
screenshot (this is what "the AI looks at the site" means concretely —
today via DOM heuristics on the rendered page; the screenshot itself is
saved and is the hook point for a real vision-model pass in v2), and pull
structural signals out of the rendered DOM: image count, description
text, presence of a video/virtual-tour element, DPE mention, floor plan
presence.

Important honesty note for v1: big portals (SeLoger, Bien'ici, Leboncoin,
Figaro) run bot-detection that can block headless Chromium. When that
happens we fail gracefully and tell the agent to paste the fields
manually instead of showing a broken/wrong score. This module is written
so swapping in a stealth/proxy browser later doesn't change the scoring
contract.
"""

import os
import re
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

from scoring import VIDEO_KEYWORDS, DPE_KEYWORDS, HERO_KEYWORDS

BLOCKED_MARKERS = ["captcha", "access denied", "are you a robot", "just a moment", "cloudflare"]

VIEWPORT = {"width": 1100, "height": 800}


class AnalysisFailed(Exception):
    def __init__(self, reason, screenshot_path=None):
        super().__init__(reason)
        self.reason = reason
        self.screenshot_path = screenshot_path


def analyze_url(url: str, screenshot_path: str) -> dict:
    with sync_playwright() as p:
        # Memory-trimming flags: the free host tier this runs on has only
        # 512MB total, and a default Chromium launch alone can eat well
        # past that, causing the whole process to get OOM-killed. These
        # flags disable GPU compositing, shared-memory usage (/dev/shm is
        # tiny in most containers and a common crash cause), and other
        # background Chrome features we don't need for a screenshot+DOM-read.
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--no-sandbox",
                "--disable-extensions",
                "--disable-background-networking",
                "--disable-default-apps",
                "--disable-sync",
                "--metrics-recording-only",
                "--mute-audio",
                "--no-first-run",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
                "--js-flags=--max-old-space-size=256",
            ],
        )
        try:
            page = browser.new_page(viewport=VIEWPORT)
            try:
                return _extract_from_page(page, url, screenshot_path)
            except AnalysisFailed as first_failure:
                # Free, local Chromium got blocked (bot-detection) or
                # timed out. If a ZenRows API key is configured, retry the
                # exact same extraction through ZenRows' stealth browser
                # instead — same DOM-reading logic via connect_over_cdp,
                # just a different, harder-to-fingerprint browser on their
                # end. Costs ZenRows credits, so it's a fallback, not the
                # default path: only major portals (SeLoger, Leboncoin,
                # Figaro Immo, Bien'ici) actually need it; plain agent
                # sites work fine on the free local path above.
                zenrows_key = os.environ.get("ZENROWS_API_KEY", "").strip()
                if not zenrows_key:
                    print(f"[ZENROWS] no ZENROWS_API_KEY configured — skipping fallback for {url} ({first_failure.reason})", flush=True)
                    raise
                print(f"[ZENROWS] local Chromium blocked/failed for {url} ({first_failure.reason}) — attempting ZenRows fallback", flush=True)
                try:
                    # proxy_region=eu: route through European residential
                    # IPs — we're only ever targeting French sites, and a
                    # non-French exit IP is one more thing DataDome-style
                    # protection can flag. Auto-rotate + residential IPs
                    # are already on by default per ZenRows' docs; this
                    # just narrows the region.
                    zr_browser = p.chromium.connect_over_cdp(
                        f"wss://browser.zenrows.com?apikey={zenrows_key}&proxy_region=eu",
                        timeout=20000,
                    )
                except Exception as e:
                    # ZenRows itself unreachable/misconfigured — surface
                    # the original bot-block reason, not a confusing
                    # connection error about an internal fallback service.
                    print(f"[ZENROWS] connect_over_cdp failed for {url}: {e}", flush=True)
                    raise first_failure
                try:
                    zr_page = zr_browser.new_page(viewport=VIEWPORT)
                    result = _extract_from_page(zr_page, url, screenshot_path)
                    print(f"[ZENROWS] fallback succeeded for {url}", flush=True)
                    return result
                except AnalysisFailed as zr_failure:
                    print(f"[ZENROWS] fallback also failed for {url}: {zr_failure.reason}", flush=True)
                    raise
                except Exception as e:
                    print(f"[ZENROWS] fallback raised unexpected error for {url}: {e}", flush=True)
                    raise first_failure
                finally:
                    zr_browser.close()
        finally:
            browser.close()


def _extract_from_page(page, url: str, screenshot_path: str) -> dict:
        try:
            page.goto(url, wait_until="networkidle", timeout=20000)
        except PWTimeout:
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=15000)
            except Exception:
                raise AnalysisFailed("La page n'a pas répondu à temps.")

        page.wait_for_timeout(1500)  # let lazy-loaded galleries settle

        # Some listing sites (especially custom-built agency sites with
        # scroll-reveal design, e.g. Junot-style luxury listings) only
        # mount sections like the DPE/diagnostics block once they've
        # actually scrolled into view — a plain page.goto() never triggers
        # that, so keywords like "classe énergie" are invisible to us even
        # though a human scrolling the page would see them clearly. Walk
        # down the page in steps to trigger any scroll/IntersectionObserver
        # based lazy content, then scroll back to the top so the screenshot
        # still starts from the hero.
        try:
            last_height = 0
            for _ in range(6):  # capped — this only needs to be "enough", not exhaustive
                page.mouse.wheel(0, 1600)
                page.wait_for_timeout(180)
                height = page.evaluate("document.body.scrollHeight")
                if height == last_height:
                    break
                last_height = height
            # Some scroll-reveal sections fetch their content over the
            # network once they mount (not just a synchronous JS toggle),
            # so give any request that just fired a moment to land before
            # we read the DOM. Short + best-effort: if it never quiets
            # down we just move on rather than eating into the request
            # budget.
            try:
                page.wait_for_load_state("networkidle", timeout=4000)
            except Exception:
                pass
            page.evaluate("window.scrollTo(0, 0)")
            page.wait_for_timeout(200)
        except Exception:
            pass

        # The screenshot is a nice-to-have preview, not something the score
        # depends on. On this host, a full-page screenshot of an
        # image-heavy listing can take longer than expected, and if we let
        # a screenshot failure raise, it kills the whole analysis even
        # though we already have everything we need from the DOM. So: try
        # progressively cheaper screenshot attempts with more time each,
        # and if all three fail, just continue without one instead of
        # failing the entire request. Kept deliberately tight (12s/8s/5s,
        # not 20s/15s/8s) — on a slow/image-heavy page this whole chain
        # was eating into the gunicorn worker's request timeout badly
        # enough to surface as a raw 502 to the visitor.
        try:
            page.screenshot(path=screenshot_path, full_page=True, timeout=12000)
        except Exception:
            try:
                page.screenshot(path=screenshot_path, timeout=8000)
            except Exception:
                try:
                    page.screenshot(path=screenshot_path, timeout=5000, clip={"x": 0, "y": 0, "width": 1100, "height": 800})
                except Exception:
                    pass

        body_text = page.inner_text("body").lower() if page.query_selector("body") else ""

        if any(marker in body_text[:3000] for marker in BLOCKED_MARKERS):
            raise AnalysisFailed(
                "Ce site bloque l'analyse automatique (protection anti-robot). "
                "Collez les informations manuellement ci-dessous.",
                screenshot_path=screenshot_path,
            )

        # Gather size + resolved src for every <img> in one round trip
        # (much cheaper than a bounding_box() call per element, which also
        # matters for the request-timeout budget). currentSrc reflects the
        # actually-loaded resource rather than a lazy-load placeholder.
        img_data = page.eval_on_selector_all(
            "img",
            """els => els.map(e => {
                const r = e.getBoundingClientRect();
                return {
                    src: e.currentSrc || e.src || e.getAttribute('data-src') || '',
                    alt: (e.getAttribute('alt') || '').trim(),
                    w: r.width,
                    h: r.height,
                };
            })"""
        )
        # Crude filter: ignore tiny icons/logos by rendered size. Then
        # dedupe by normalized src — carousel/gallery widgets very
        # commonly clone slides in the DOM for a seamless infinite-loop
        # effect (or repeat the same photo in a hero + thumbnail strip),
        # which was inflating the count 4-5x on real listings (a 14-photo
        # listing was reporting 70). Query strings are stripped since the
        # same photo often reappears with different resize/cache params.
        seen_srcs = set()
        real_photo_count = 0
        real_photos_with_alt = 0
        for item in img_data:
            if item["w"] < 150 or item["h"] < 100:
                continue
            normalized_src = item["src"].split("?")[0].strip()
            if not normalized_src or normalized_src in seen_srcs:
                continue
            seen_srcs.add(normalized_src)
            real_photo_count += 1
            if item["alt"]:
                real_photos_with_alt += 1
        image_alt_ratio = (real_photos_with_alt / real_photo_count) if real_photo_count else None

        has_video_tag = page.query_selector("video") is not None
        has_iframe_tour = any(
            kw in (page.query_selector("iframe").get_attribute("src") or "").lower()
            for kw in ["matterport", "tour", "3d"]
        ) if page.query_selector("iframe") else False
        has_video_or_tour = has_video_tag or has_iframe_tour or any(kw in body_text for kw in VIDEO_KEYWORDS)

        has_dpe = any(kw in body_text for kw in DPE_KEYWORDS)
        if not has_dpe:
            # The DPE gauge itself (the A-G colour scale) is very often a
            # rendered image, not live text — many diagnostic tools export
            # it as a flat graphic that agencies just embed as-is, so no
            # amount of body-text keyword matching will ever see it. Fall
            # back to checking alt text and image filenames, which are
            # frequently still descriptive even when the pixels aren't.
            try:
                img_hints = page.eval_on_selector_all(
                    "img",
                    "els => els.map(e => ((e.getAttribute('alt') || '') + ' ' + (e.getAttribute('src') || '')).toLowerCase())"
                )
                has_dpe = any(
                    kw in hint for hint in img_hints for kw in ["dpe", "diagnostic", "classe-energie", "classe_energie", "energie"]
                )
            except Exception:
                pass

        # Floor plan (replaces an earlier "address precision" check — French
        # listings never publish the exact street address publicly, so
        # that was never a meaningful signal here). Check body text for
        # the usual French phrasing, then fall back to image alt/src the
        # same way the DPE check does, since a plan is very often just an
        # embedded image with no surrounding readable text.
        has_floor_plan = bool(re.search(r"\bplan(s)?\s*(du bien|2d|3d|intérieur|de l'appartement|de la maison)\b", body_text)) \
            or "voir le plan" in body_text
        if not has_floor_plan:
            try:
                plan_img_hints = page.eval_on_selector_all(
                    "img",
                    "els => els.map(e => ((e.getAttribute('alt') || '') + ' ' + (e.getAttribute('src') || '')).toLowerCase())"
                )
                has_floor_plan = any(
                    kw in hint for hint in plan_img_hints for kw in ["floor-plan", "floorplan", "plan-2d", "plan_2d", "plan-3d", "plan_3d", "/plan."]
                )
            except Exception:
                pass

        # best-effort description: longest paragraph-like text block
        paragraphs = page.eval_on_selector_all(
            "p, div",
            "els => els.map(e => e.innerText).filter(t => t && t.length > 80)"
        )
        description_text = max(paragraphs, key=len) if paragraphs else ""

        property_type = "studio" if "studio" in body_text[:500] else None

        # Technical/on-page SEO signals — deliberately independent of the
        # listing-quality scoring above. Runs on every URL, portal or
        # personal site alike: on a portal listing the agent can't act on
        # any of this directly, which is the point (it's the argument for
        # owning a dedicated site instead).
        try:
            title_text = page.title() or ""
        except Exception:
            title_text = ""
        try:
            meta_description = page.eval_on_selector(
                "meta[name='description']", "el => el.getAttribute('content') || ''"
            ) or ""
        except Exception:
            meta_description = ""
        try:
            h1_count = len(page.query_selector_all("h1"))
        except Exception:
            h1_count = 0
        try:
            has_viewport_meta = page.query_selector("meta[name='viewport']") is not None
        except Exception:
            has_viewport_meta = False
        try:
            has_structured_data = page.query_selector("script[type='application/ld+json']") is not None
        except Exception:
            has_structured_data = False
        is_https = url.strip().lower().startswith("https://")

        return {
            "image_count": real_photo_count,
            "has_gallery_hero_exterior": None,  # requires vision pass — unknown in v1
            "description_text": description_text,
            "has_video_or_tour": has_video_or_tour,
            "has_dpe": has_dpe,
            "has_floor_plan": has_floor_plan,
            "property_type": property_type,
            "screenshot_path": screenshot_path,
            "seo": {
                "title_text": title_text,
                "meta_description": meta_description,
                "h1_count": h1_count,
                "image_alt_ratio": image_alt_ratio,
                "has_viewport_meta": has_viewport_meta,
                "is_https": is_https,
                "has_structured_data": has_structured_data,
            },
        }
