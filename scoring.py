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
