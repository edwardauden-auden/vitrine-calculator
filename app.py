import os
import re
import time
import uuid

from flask import Flask, render_template, request, redirect, url_for, jsonify

from analyzer import analyze_url, AnalysisFailed
from scoring import score_listing

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
    return jsonify(status="ok")

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
    "listing_setup_help": {
        "title": "Vérification fiche annonce",
        "pitch": "On vérifie avec vous que l'adresse précise est bien activée sur chaque portail.",
        "cta": "Vérifier ma fiche",
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
    manual_address = src.get("manual_address") in ("on", "true", "1")

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
    except Exception as e:
        auto_failed_reason = "Une erreur inattendue est survenue pendant l'analyse automatique."

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
                "has_precise_address": manual_address,
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

    screenshot_url = None
    if extraction.get("screenshot_path") and os.path.exists(extraction["screenshot_path"]):
        screenshot_url = url_for("static", filename=f"screenshots/{os.path.basename(extraction['screenshot_path'])}")

    return render_template(
        "results.html",
        url=url,
        result=result,
        signals_with_upsell=signals_with_upsell,
        screenshot_url=screenshot_url,
        used_manual_fallback=used_manual_fallback,
        auto_failed_reason=auto_failed_reason,
    )


if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    port = int(os.environ.get("PORT", 5050))
    app.run(host="0.0.0.0", port=port, debug=debug)
