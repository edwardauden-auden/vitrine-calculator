import logging
import os
import re
import sys
import time
import traceback
import uuid

from flask import Flask, render_template, request, redirect, url_for, jsonify

logging.basicConfig(level=logging.INFO, stream=sys.stdout, force=True)
logger = logging.getLogger("vitrine")

from analyzer import analyze_url, AnalysisFailed
from scoring import score_listing, score_seo, score_photo_quality
from vision import analyze_photo_quality

app = Flask(__name__)
SCREENSHOT_DIR = os.path.join(app.root_path, "static", "screenshots")
os.makedirs(SCREENSHOT_DIR, exist_ok=True)


@app.after_request
def add_cors_headers(response):
    # Landing page lives on a different domain (Netlify) than this backend
    # (Fly.io), so allow it to call the JSON API below.
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response


@app.route("/health")
def health():
    # RENDER_GIT_COMMIT is set automatically by Render to the SHA it
    # actually deployed — exposing it here lets us confirm from outside
    # (curl/WebFetch) whether a given fix is really live yet, instead of
    # guessing from timing or screenshotting the dashboard each time.
    return jsonify(status="ok", commit=os.environ.get("RENDER_GIT_COMMIT", "unknown"))

UPSELLS = {
    "upload_photos": {
        "title": "Shooting photo professionnel",
        "pitch": "On envoie un photographe ou vous uploadez vos photos brutes — on complète la galerie et on la met aux normes.",
        "cta": "Voir l'offre photo",
    },
    "photo_reorder": {
        "title": "Réorganisation de galerie",
        "pitch": "On réordonne vos photos existantes (façade en premier, puis pièces de vie) — inclus dans l'audit gratuit.",
        "cta": "Réorganiser mes photos",
    },
    "photo_to_video": {
        "title": "Vidéo à partir de vos photos",
        "pitch": "Transformez vos photos existantes en vidéo dynamique en quelques minutes, sans tournage.",
        "cta": "Générer ma vidéo",
    },
    "copywriting": {
        "title": "Rédaction d'annonce optimisée",
        "pitch": "Une description réécrite pour la longueur et les mots-clés qui génèrent des contacts.",
        "cta": "Faire rédiger mon annonce",
    },
    "dpe_reminder": {
        "title": "Rappel DPE",
        "pitch": "Ajoutez votre DPE — même une mauvaise note vaut mieux qu'un champ vide pour vos clics.",
        "cta": "Comment l'ajouter",
    },
    "floor_plan_reminder": {
        "title": "Rappel plan du bien",
        "pitch": "Ajoutez un plan 2D/3D — les biens avec un plan se vendent environ 20% plus vite.",
        "cta": "Comment l'ajouter",
    },
    "seo_audit": {
        "title": "Audit et optimisation SEO",
        "pitch": "Sur un portail, vous ne pouvez rien changer à ça. Sur votre propre site, si — on s'en occupe pour vous.",
        "cta": "Voir l'offre SEO",
    },
    "photo_editing": {
        "title": "Retouche photo professionnelle",
        "pitch": "Redressement, exposition, couleurs harmonisées entre toutes les photos — sans nouveau shooting.",
        "cta": "Voir l'offre retouche",
    },
}


def looks_like_url(value: str) -> bool:
    return bool(re.match(r"^https?://", value.strip(), re.IGNORECASE))


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/analyze", methods=["GET", "POST"])
def analyze():
    # Accepts both a normal form POST (used when the analyzer's own pages
    # link here) and a GET with ?listing_url=... (used when an external
    # site, like the Netlify marketing page, navigates the browser here
    # directly — a plain top-level navigation sidesteps CORS entirely,
    # unlike a cross-origin fetch/AJAX call would).
    src = request.values
    url = (src.get("listing_url") or "").strip()
    manual_desc = (src.get("manual_description") or "").strip()
    manual_photos = src.get("manual_photo_count", type=int)
    manual_video = src.get("manual_video") in ("on", "true", "1")
    manual_dpe = src.get("manual_dpe") in ("on", "true", "1")
    manual_floor_plan = src.get("manual_floor_plan") in ("on", "true", "1")

    if not url or not looks_like_url(url):
        return render_template("index.html", error="Merci de coller un lien valide (commençant par http:// ou https://).")

    shot_name = f"{uuid.uuid4().hex}.png"
    shot_path = os.path.join(SCREENSHOT_DIR, shot_name)

    extraction = None
    auto_failed_reason = None

    try:
        extraction = analyze_url(url, shot_path)
    except AnalysisFailed as e:
        auto_failed_reason = e.reason
        logger.warning("Analysis failed gracefully for %s: %s", url, e.reason)
    except Exception as e:
        auto_failed_reason = "Une erreur inattendue est survenue pendant l'analyse automatique."
        tb = traceback.format_exc()
        logger.error("Unexpected error analyzing %s: %s\n%s", url, e, tb)
        # Belt-and-suspenders: print too, in case something upstream of the
        # logging module (e.g. gunicorn's own config) is swallowing it.
        print(f"[ANALYZE ERROR] {url}: {e}\n{tb}", flush=True)

    used_manual_fallback = False
    if extraction is None or (extraction.get("image_count", 0) == 0 and not extraction.get("description_text")):
        # fall back to whatever manual fields were provided
        if manual_photos is not None or manual_desc:
            used_manual_fallback = True
            extraction = {
                "image_count": manual_photos or 0,
                "has_gallery_hero_exterior": None,
                "description_text": manual_desc,
                "has_video_or_tour": manual_video,
                "has_dpe": manual_dpe,
                "has_floor_plan": manual_floor_plan,
                "property_type": None,
                "screenshot_path": shot_path if os.path.exists(shot_path) else None,
            }
        else:
            return render_template(
                "index.html",
                error=auto_failed_reason or "Impossible d'analyser automatiquement ce lien.",
                show_manual=True,
                prefill_url=url,
            )

    result = score_listing(extraction)

    signals_with_upsell = []
    for s in result.signals:
        upsell = UPSELLS.get(s.upsell) if s.upsell else None
        signals_with_upsell.append((s, upsell))

    # SEO score needs real page data (title tag, meta description, etc.)
    # that manual entry simply doesn't collect — showing a score built
    # from all-missing fields would just read as an unfairly bad 0/100,
    # so only compute it when the automatic analysis actually ran.
    seo_result = None
    seo_signals_with_upsell = []
    if not used_manual_fallback and extraction.get("seo"):
        seo_result = score_seo(extraction["seo"])
        for s in seo_result.signals:
            upsell = UPSELLS.get(s.upsell) if s.upsell else None
            seo_signals_with_upsell.append((s, upsell))

    # Photo-quality score: the only section that actually looks AT the
    # photos rather than the surrounding page (crooked, dark, over-edited,
    # inconsistent style, watermarks/clutter/people). Optional and
    # fail-open by design — skipped entirely if GEMINI_API_KEY isn't set
    # in the environment (same pattern as ZENROWS_API_KEY: added directly
    # in Render's dashboard, never passed through this code), if manual
    # fallback was used (no real photo URLs to judge), or if the vision
    # call itself fails for any reason. Also skipped when the listing was
    # fetched via ZenRows — that path is already the slowest/riskiest for
    # the gunicorn timeout, so we don't stack another external API call
    # on top of it.
    photo_quality_result = None
    photo_quality_signals_with_upsell = []
    photo_quality_notes = None
    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if (
        not used_manual_fallback
        and gemini_key
        and extraction.get("photo_urls")
        and not extraction.get("via_zenrows")
    ):
        try:
            pq_data = analyze_photo_quality(extraction["photo_urls"], gemini_key)
        except Exception as e:
            pq_data = None
            logger.warning("Photo quality vision pass failed for %s: %s", url, e)
        if pq_data:
            photo_quality_result = score_photo_quality(pq_data)
            photo_quality_notes = pq_data.get("notes") or None
            for s in photo_quality_result.signals:
                upsell = UPSELLS.get(s.upsell) if s.upsell else None
                photo_quality_signals_with_upsell.append((s, upsell))

    screenshot_url = None
    if extraction.get("screenshot_path") and os.path.exists(extraction["screenshot_path"]):
        screenshot_url = url_for("static", filename=f"screenshots/{os.path.basename(extraction['screenshot_path'])}")

    return render_template(
        "results.html",
        url=url,
        result=result,
        signals_with_upsell=signals_with_upsell,
        seo_result=seo_result,
        seo_signals_with_upsell=seo_signals_with_upsell,
        photo_quality_result=photo_quality_result,
        photo_quality_signals_with_upsell=photo_quality_signals_with_upsell,
        photo_quality_notes=photo_quality_notes,
        screenshot_url=screenshot_url,
        used_manual_fallback=used_manual_fallback,
        auto_failed_reason=auto_failed_reason,
    )


if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    port = int(os.environ.get("PORT", 5050))
    app.run(host="0.0.0.0", port=port, debug=debug)
