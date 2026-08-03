"""
AgentMediaBox — Performance scoring engine (v1)

Rubric is modeled on SeLoger's own published quality-score mechanics
(photos/video, description, DPE/attributes, floor plan), which
SeLoger has stated correlate with up to 16x more first-page visibility.
We reuse that logic as our scoring backbone because it's a mechanism
agents already indirectly trust, then extend it to work across any
listing page (agent site, portal, PDF-less microsite), not just SeLoger.

v1 is deliberately rule-based / DOM-based — no computer vision yet.
Photo *quality* (blur, lighting, composition) is a v2 addition once
we wire in a vision model; v1 scores photo *completeness and structure*
(count, presence of a hero shot, gallery size) which is already ~45%
of the real algorithm's weight and doesn't require ML to get right.
"""

from dataclasses import dataclass, field
import re


@dataclass
class Signal:
    key: str
    label: str
    points: int
    max_points: int
    status: str  # "good" | "warning" | "bad"
    detail: str
    upsell: str | None = None


@dataclass
class ScoreResult:
    total: int
    max_total: int
    grade: str
    signals: list = field(default_factory=list)

    @property
    def percent(self):
        return round(100 * self.total / self.max_total) if self.max_total else 0


VIDEO_KEYWORDS = ["visite virtuelle", "virtual tour", "matterport", "video", "vidéo", "3d tour"]
DPE_KEYWORDS = [
    "dpe",
    "diagnostic de performance",
    "classe énergie",
    "consommation énergétique",
    # Broadened after a real miss: on junot.fr the actual gauge graphic
    # (A-G bars) is a rendered image with no text in the DOM, but sites
    # that embed it almost always still have a real, readable section
    # heading or caption around it — these catch that heading even when
    # the numbers themselves are unreadable pixels.
    "diagnostics",
    "classe climat",
    "passoire énergétique",
    "kg co2",
    "kwh/m",
]
HERO_KEYWORDS = ["façade", "exterieur", "exterior", "jardin", "vue extérieure"]


def score_listing(extraction: dict) -> ScoreResult:
    """
    extraction is a dict produced by the scraper/analyzer with keys:
      - image_count: int
      - has_gallery_hero_exterior: bool | None (None = unknown)
      - description_text: str
      - has_video_or_tour: bool
      - has_dpe: bool
      - has_floor_plan: bool  (2D/3D floor plan detected — French listings
        never show a precise street address publicly for security/privacy
        reasons, so that's not a real quality signal here; a floor plan is)
      - property_type: str | None ("studio" or other, affects photo minimums)
    """
    signals = []
    total = 0

    # --- Photos & video: 45 + 10 = 55 pts (mirrors SeLoger's dominant weighting) ---
    img_count = extraction.get("image_count", 0)
    min_photos = 8 if extraction.get("property_type") == "studio" else 15
    if img_count >= min_photos:
        pts = 35
        status, detail = "good", f"{img_count} photos — au-dessus du minimum recommandé ({min_photos})."
    elif img_count >= max(4, min_photos // 2):
        pts = 20
        status, detail = "warning", f"{img_count} photos — en dessous du minimum recommandé ({min_photos})."
    else:
        pts = 5
        status, detail = "bad", f"Seulement {img_count} photos détectées — bien en dessous des {min_photos} recommandées."
    signals.append(Signal("photo_count", "Nombre de photos", pts, 35, status, detail,
                           upsell="upload_photos" if status != "good" else None))
    total += pts

    hero = extraction.get("has_gallery_hero_exterior")
    if hero is True:
        pts, status, detail = 10, "good", "La première photo est bien une vue extérieure/façade."
    elif hero is False:
        pts, status, detail = 0, "bad", "La photo de couverture n'est pas une vue extérieure — cela réduit les clics."
    else:
        pts, status, detail = 3, "warning", "Impossible de confirmer l'ordre des photos automatiquement."
    signals.append(Signal("hero_shot", "Photo de couverture", pts, 10, status, detail,
                           upsell="photo_reorder" if status != "good" else None))
    total += pts

    if extraction.get("has_video_or_tour"):
        pts, status, detail = 10, "good", "Vidéo ou visite virtuelle présente — cela triple les clics en moyenne."
    else:
        pts, status, detail = 0, "bad", "Aucune vidéo ni visite virtuelle détectée."
    signals.append(Signal("video", "Vidéo / visite virtuelle", pts, 10, status, detail,
                           upsell="photo_to_video" if status != "good" else None))
    total += pts

    # --- Description: 20 pts ---
    desc = extraction.get("description_text", "") or ""
    desc_len = len(desc.strip())
    if desc_len >= 1500:
        pts, status, detail = 20, "good", f"Description de {desc_len} caractères — dans la fourchette recommandée (1500-2000)."
    elif desc_len >= 300:
        pts, status, detail = 12, "warning", f"Description de {desc_len} caractères — en dessous des 1500-2000 recommandés."
    else:
        pts, status, detail = 4, "bad", f"Description de seulement {desc_len} caractères — les descriptions courtes perdent ~30% des contacts."
    signals.append(Signal("description", "Description", pts, 20, status, detail,
                           upsell="copywriting" if status != "good" else None))
    total += pts

    # --- DPE / attributes: 20 pts ---
    if extraction.get("has_dpe"):
        pts, status, detail = 20, "good", "DPE renseigné."
    else:
        pts, status, detail = 0, "bad", "DPE non détecté — son absence divise les clics par 3."
    signals.append(Signal("dpe", "DPE / caractéristiques", pts, 20, status, detail,
                           upsell="dpe_reminder" if status != "good" else None))
    total += pts

    # --- Floor plan: 15 pts ---
    # Not "address precision" — in France, listings never publish a
    # precise street address publicly (agents deliberately withhold it to
    # avoid buyers going around them), so testing for one would penalize
    # every compliant listing. A floor plan is a real, well-documented
    # driver of engagement instead: properties with a 2D/3D plan sell
    # ~20% faster because buyers can picture the layout before visiting.
    if extraction.get("has_floor_plan"):
        pts, status, detail = 15, "good", "Plan du bien présent."
    else:
        pts, status, detail = 0, "bad", "Aucun plan détecté — les biens avec un plan 2D/3D se vendent environ 20% plus vite."
    signals.append(Signal("floor_plan", "Plan du bien", pts, 15, status, detail,
                           upsell="floor_plan_reminder" if status != "good" else None))
    total += pts

    max_total = 35 + 10 + 10 + 20 + 20 + 15  # = 110
    grade = _grade_for(total, max_total)
    return ScoreResult(total=total, max_total=max_total, grade=grade, signals=signals)


def score_seo(seo_extraction: dict) -> ScoreResult:
    """
    Deliberately separate from score_listing(): that score mirrors
    SeLoger's own published portal-visibility algorithm, which has
    nothing to do with technical/on-page SEO. This one runs on *any*
    link, portal or personal site alike — on a portal listing the agent
    has no power to fix what it finds, and that's the point: it's the
    argument for a dedicated site they do control.

    seo_extraction keys:
      - title_text: str | None
      - meta_description: str | None
      - h1_count: int
      - image_alt_ratio: float | None  (0-1, share of real photos with alt text; None if no images)
      - has_viewport_meta: bool
      - is_https: bool
      - has_structured_data: bool  (schema.org / JSON-LD anywhere on the page)
    """
    signals = []
    total = 0

    title = (seo_extraction.get("title_text") or "").strip()
    title_len = len(title)
    if 15 <= title_len <= 65:
        pts, status, detail = 20, "good", f"Balise <title> présente ({title_len} caractères)."
    elif title_len > 0:
        pts, status, detail = 10, "warning", f"Balise <title> présente mais mal dimensionnée ({title_len} caractères — visez 15-65)."
    else:
        pts, status, detail = 0, "bad", "Aucune balise <title> détectée — c'est le premier élément lu par Google."
    signals.append(Signal("seo_title", "Balise title", pts, 20, status, detail,
                           upsell="seo_audit" if status != "good" else None))
    total += pts

    meta_desc = (seo_extraction.get("meta_description") or "").strip()
    meta_len = len(meta_desc)
    if 70 <= meta_len <= 170:
        pts, status, detail = 20, "good", f"Meta description présente ({meta_len} caractères)."
    elif meta_len > 0:
        pts, status, detail = 10, "warning", f"Meta description présente mais mal dimensionnée ({meta_len} caractères — visez 70-170)."
    else:
        pts, status, detail = 0, "bad", "Aucune meta description détectée — Google en génère une au hasard à la place."
    signals.append(Signal("seo_meta_description", "Meta description", pts, 20, status, detail,
                           upsell="seo_audit" if status != "good" else None))
    total += pts

    h1_count = seo_extraction.get("h1_count", 0)
    if h1_count == 1:
        pts, status, detail = 15, "good", "Un seul titre H1 sur la page — structure claire pour Google."
    elif h1_count == 0:
        pts, status, detail = 0, "bad", "Aucun titre H1 détecté."
    else:
        pts, status, detail = 5, "warning", f"{h1_count} balises H1 détectées — Google préfère une hiérarchie claire avec un seul H1."
    signals.append(Signal("seo_h1", "Structure des titres (H1)", pts, 15, status, detail,
                           upsell="seo_audit" if status != "good" else None))
    total += pts

    alt_ratio = seo_extraction.get("image_alt_ratio")
    if alt_ratio is None:
        pts, status, detail = 8, "warning", "Aucune photo détectée pour vérifier le texte alternatif."
    elif alt_ratio >= 0.8:
        pts, status, detail = 15, "good", f"{round(alt_ratio * 100)}% des photos ont un texte alternatif renseigné."
    elif alt_ratio >= 0.3:
        pts, status, detail = 8, "warning", f"Seulement {round(alt_ratio * 100)}% des photos ont un texte alternatif — Google ne peut pas indexer le reste."
    else:
        pts, status, detail = 0, "bad", "Quasiment aucune photo n'a de texte alternatif — invisible pour la recherche d'images Google."
    signals.append(Signal("seo_alt_text", "Texte alternatif des photos", pts, 15, status, detail,
                           upsell="seo_audit" if status != "good" else None))
    total += pts

    if seo_extraction.get("is_https"):
        pts, status, detail = 10, "good", "Le site est en HTTPS."
    else:
        pts, status, detail = 0, "bad", "Le site n'est pas en HTTPS — Google pénalise directement le classement, et les navigateurs affichent un avertissement."
    signals.append(Signal("seo_https", "HTTPS", pts, 10, status, detail,
                           upsell="seo_audit" if status != "good" else None))
    total += pts

    if seo_extraction.get("has_viewport_meta"):
        pts, status, detail = 10, "good", "Le site est adapté mobile (balise viewport détectée)."
    else:
        pts, status, detail = 0, "bad", "Pas de balise viewport détectée — plus de 70% des recherches immobilières se font sur mobile."
    signals.append(Signal("seo_mobile", "Compatibilité mobile", pts, 10, status, detail,
                           upsell="seo_audit" if status != "good" else None))
    total += pts

    if seo_extraction.get("has_structured_data"):
        pts, status, detail = 10, "good", "Données structurées (schema.org) détectées."
    else:
        pts, status, detail = 0, "bad", "Aucune donnée structurée détectée — Google ne peut pas afficher de résultat enrichi pour ce bien."
    signals.append(Signal("seo_structured_data", "Données structurées", pts, 10, status, detail,
                           upsell="seo_audit" if status != "good" else None))
    total += pts

    max_total = 20 + 20 + 15 + 15 + 10 + 10 + 10  # = 100
    grade = _grade_for(total, max_total)
    return ScoreResult(total=total, max_total=max_total, grade=grade, signals=signals)


def score_photo_quality(pq: dict) -> ScoreResult:
    """
    Deliberately separate from score_listing() and score_seo(): this is
    the only score that actually looks AT the photos rather than the
    surrounding page — it comes from vision.analyze_photo_quality(), a
    Gemini vision pass over a sample of the real gallery photos
    (vision.MAX_PHOTOS of them). No amount of DOM-scraping can tell you a
    photo is crooked, dark, or oversaturated; this can.

    Optional by design: if the vision pass wasn't run (no API key
    configured, or it failed), app.py simply never calls this — there's
    no "empty" state to handle here, only present-or-absent upstream.

    pq keys (all produced by vision.analyze_photo_quality):
      - sample_size: int (how many photos were actually judged)
      - pct_straight, pct_well_exposed, pct_sharp, pct_natural_editing: 0-100
      - consistent_style: bool
      - has_watermark, shows_clutter, shows_people: bool
      - notes: str (short free-text callout from the model, may be "")
    """
    signals = []
    total = 0
    n = pq.get("sample_size", 0)
    sample_note = f" (basé sur {n} photo{'s' if n != 1 else ''} analysée{'s' if n != 1 else ''} par IA)" if n else ""

    def _threshold_signal(key, label, pts_max, good_min, warn_min, good_msg, warn_msg, bad_msg):
        nonlocal total
        pct_val = pq.get(key, 0)
        if pct_val >= good_min:
            pts, status, detail = pts_max, "good", good_msg.format(pct=pct_val)
        elif pct_val >= warn_min:
            pts, status, detail = round(pts_max * 0.5), "warning", warn_msg.format(pct=pct_val)
        else:
            pts, status, detail = 0, "bad", bad_msg.format(pct=pct_val)
        signals.append(Signal(key, label, pts, pts_max, status, detail + sample_note,
                               upsell="photo_editing" if status != "good" else None))
        total += pts

    _threshold_signal(
        "pct_straight", "Horizon et lignes droites", 20, 90, 60,
        "{pct}% des photos ont des lignes bien droites.",
        "{pct}% des photos ont des lignes droites — {pct}% seulement, certaines semblent penchées.",
        "Beaucoup de photos semblent penchées ({pct}% seulement ont des lignes droites) — cela donne une impression amateur.",
    )
    _threshold_signal(
        "pct_well_exposed", "Exposition / luminosité", 20, 90, 60,
        "{pct}% des photos ont une exposition correcte.",
        "{pct}% des photos ont une bonne exposition — le reste est trop sombre ou surexposé.",
        "Beaucoup de photos sont trop sombres ou surexposées ({pct}% seulement bien exposées).",
    )
    _threshold_signal(
        "pct_sharp", "Netteté", 15, 90, 60,
        "{pct}% des photos sont nettes.",
        "{pct}% des photos sont nettes — certaines semblent floues.",
        "Beaucoup de photos semblent floues ({pct}% seulement nettes).",
    )
    _threshold_signal(
        "pct_natural_editing", "Retouche naturelle", 15, 80, 40,
        "Les photos ont un rendu naturel, pas de sur-retouche visible.",
        "{pct}% des photos ont un rendu naturel — certaines semblent sur-retouchées (HDR excessif, couleurs trop saturées).",
        "Plusieurs photos semblent sur-retouchées (HDR excessif, couleurs trop saturées) — cela peut sembler trompeur aux acheteurs.",
    )

    if pq.get("consistent_style", True):
        pts, status, detail = 15, "good", "Les photos ont un style cohérent (lumière, couleurs) d'une image à l'autre."
    else:
        pts, status, detail = 0, "bad", "Les photos semblent avoir des styles mélangés (certaines pro, d'autres amateur, ou jour/nuit) — cela nuit à l'impression d'ensemble."
    signals.append(Signal("consistent_style", "Cohérence du style", pts, 15, status, detail + sample_note,
                           upsell="photo_editing" if status != "good" else None))
    total += pts

    issues = []
    if pq.get("has_watermark"):
        issues.append("un filigrane/logo visible sur au moins une photo")
    if pq.get("shows_clutter"):
        issues.append("du désordre ou des affaires personnelles visibles")
    if pq.get("shows_people"):
        issues.append("une personne visible sur au moins une photo")
    if issues:
        pts, status = 0, "bad"
        detail = "Présentation à corriger : " + ", ".join(issues) + "."
    else:
        pts, status = 15, "good"
        detail = "Aucun filigrane, désordre ou personne visible détecté — présentation professionnelle."
    signals.append(Signal("clean_presentation", "Présentation professionnelle", pts, 15, status, detail + sample_note,
                           upsell="upload_photos" if status != "good" else None))
    total += pts

    max_total = 20 + 20 + 15 + 15 + 15 + 15  # = 100
    grade = _grade_for(total, max_total)
    result = ScoreResult(total=total, max_total=max_total, grade=grade, signals=signals)
    return result


def _grade_for(total, max_total):
    pct = 100 * total / max_total
    if pct >= 80:
        return "A"
    if pct >= 65:
        return "B"
    if pct >= 45:
        return "C"
    if pct >= 25:
        return "D"
    return "E"
