"""
AgentMediaBox — page analyzer (v1)

Approach: headless-browser the URL the agent pastes, take a full-page
screenshot (this is what "the AI looks at the site" means concretely —
today via DOM heuristics on the rendered page; the screenshot itself is
saved and is the hook point for a real vision-model pass in v2), and pull
structural signals out of the rendered DOM: image count, description
text, presence of a video/virtual-tour element, DPE mention, address
precision.

Important honesty note for v1: big portals (SeLoger, Bien'ici, Leboncoin,
Figaro) run bot-detection that can block headless Chromium. When that
happens we fail gracefully and tell the agent to paste the fields
manually instead of showing a broken/wrong score. This module is written
so swapping in a stealth/proxy browser later doesn't change the scoring
contract.
"""

import re
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

from scoring import VIDEO_KEYWORDS, DPE_KEYWORDS, HERO_KEYWORDS

BLOCKED_MARKERS = ["captcha", "access denied", "are you a robot", "just a moment", "cloudflare"]


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
        # Smaller viewport = smaller compositing/screenshot buffer = less
        # peak memory during full-page capture.
        page = browser.new_page(viewport={"width": 1100, "height": 800})
        try:
            page.goto(url, wait_until="networkidle", timeout=20000)
        except PWTimeout:
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=15000)
            except Exception:
                browser.close()
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
            browser.close()
            raise AnalysisFailed(
                "Ce site bloque l'analyse automatique (protection anti-robot). "
                "Collez les informations manuellement ci-dessous.",
                screenshot_path=screenshot_path,
            )

        images = page.query_selector_all("img")
        # crude filter: ignore tiny icons/logos by checking natural size when available
        real_photo_count = 0
        for img in images:
            try:
                box = img.bounding_box()
                if box and box["width"] >= 150 and box["height"] >= 100:
                    real_photo_count += 1
            except Exception:
                continue

        has_video_tag = page.query_selector("video") is not None
        has_iframe_tour = any(
            kw in (page.query_selector("iframe").get_attribute("src") or "").lower()
            for kw in ["matterport", "tour", "3d"]
        ) if page.query_selector("iframe") else False
        has_video_or_tour = has_video_tag or has_iframe_tour or any(kw in body_text for kw in VIDEO_KEYWORDS)

        has_dpe = any(kw in body_text for kw in DPE_KEYWORDS)

        # address precision: look for a street-style pattern (number + word) near "adresse"/postal code
        has_precise_address = bool(re.search(r"\b\d{1,4}\s+(rue|avenue|impasse|boulevard|chemin|allée|place)\b", body_text))

        # best-effort description: longest paragraph-like text block
        paragraphs = page.eval_on_selector_all(
            "p, div",
            "els => els.map(e => e.innerText).filter(t => t && t.length > 80)"
        )
        description_text = max(paragraphs, key=len) if paragraphs else ""

        property_type = "studio" if "studio" in body_text[:500] else None

        browser.close()

        return {
            "image_count": real_photo_count,
            "has_gallery_hero_exterior": None,  # requires vision pass — unknown in v1
            "description_text": description_text,
            "has_video_or_tour": has_video_or_tour,
            "has_dpe": has_dpe,
            "has_precise_address": has_precise_address,
            "property_type": property_type,
            "screenshot_path": screenshot_path,
        }
