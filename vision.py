"""
AgentMediaBox — photo quality vision pass (v2)

Everything in analyzer.py/scoring.py up to this point is DOM heuristics:
it can count photos and check a filename for "dpe", but it can't actually
look at a photo and tell you it's crooked, dark, or oversaturated. That's
exactly what this module adds, using Gemini's vision API to judge the
sample of real gallery photos analyzer.py already extracted.

Deliberately optional and fail-open: if GEMINI_API_KEY isn't configured,
if the API call fails, or if fewer than MIN_PHOTOS download successfully,
this returns None and the caller (app.py) just omits the photo-quality
section — same pattern as the SEO score being skipped on manual fallback,
and the ZenRows key being optional for portal fallback. A flaky vision
call should never break the core score.
"""

import base64
import io
import json

import requests
from PIL import Image

GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

MAX_PHOTOS = 6          # cap cost/latency — a representative sample, not exhaustive
MIN_PHOTOS = 2           # below this a "consistency" verdict is meaningless
MAX_DIMENSION = 640      # downsize before upload: cuts both token cost and request size
DOWNLOAD_TIMEOUT = 4     # seconds, per photo — a slow photo host shouldn't stall the whole pass
GEMINI_TIMEOUT = 25      # seconds, for the single batched vision call

PROMPT = """You are a real-estate listing photo quality auditor. You will \
receive several photos from the same property listing, in order.

For EACH photo, judge:
- straight: are verticals (walls, door/window frames) reasonably level, \
not obviously tilted?
- well_exposed: is it neither too dark nor blown-out/overexposed?
- sharp: is it in focus, not blurry?
- natural_editing: does it look like a normal photo, not obviously \
over-processed (harsh HDR halos, unnatural oversaturation, crushed \
contrast)?
- has_watermark: any visible watermark, logo overlay, or "for sale" \
graphic burned into the image?
- shows_clutter: visible mess, personal items, unmade bed, dishes, \
laundry, etc.?
- shows_people: any person (or a reflection of one, e.g. the \
photographer) visible?
- is_interior_room: is this a photo of an interior room (bedroom, living \
room, kitchen, dining room, office)? false for exterior/facade shots, \
gardens, bathrooms, hallways, floor plans, DPE diagrams, or maps.
- is_empty_room: ONLY meaningful when is_interior_room is true — is the \
room unfurnished/empty (no bed, sofa, table, or other furniture), the \
kind of room virtual staging would help sell? false if is_interior_room \
is false.

Then judge the SET as a whole:
- consistent_style: do the photos share a similar color grading/style, \
or do they look like a mismatched mix (different white balance, some \
day/some night, phone snapshots mixed with professional shots)?
- notes: one short, concrete sentence in French summarizing the single \
biggest issue across the set, or "" if there's nothing notable.

Respond with ONLY this JSON shape, no other text:
{
  "photos": [
    {"straight": true, "well_exposed": true, "sharp": true, \
"natural_editing": true, "has_watermark": false, "shows_clutter": false, \
"shows_people": false, "is_interior_room": true, "is_empty_room": false}
  ],
  "consistent_style": true,
  "notes": ""
}
The "photos" array must have exactly one entry per photo, in the order \
given."""


def _download_and_resize(url: str) -> tuple[str, str] | None:
    """Fetch one photo and return (base64_jpeg, mime_type), or None on
    any failure — a single bad photo URL should never sink the whole
    batch."""
    try:
        resp = requests.get(url, timeout=DOWNLOAD_TIMEOUT)
        resp.raise_for_status()
        img = Image.open(io.BytesIO(resp.content))
        img = img.convert("RGB")
        img.thumbnail((MAX_DIMENSION, MAX_DIMENSION))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=72)
        return base64.b64encode(buf.getvalue()).decode("ascii"), "image/jpeg"
    except Exception as e:
        print(f"[GEMINI] failed to download/resize {url}: {e}", flush=True)
        return None


def analyze_photo_quality(photo_urls: list, api_key: str) -> dict | None:
    if not api_key or not photo_urls:
        return None

    encoded = []
    for url in photo_urls[:MAX_PHOTOS]:
        result = _download_and_resize(url)
        if result:
            encoded.append(result)
    if len(encoded) < MIN_PHOTOS:
        print(f"[GEMINI] only {len(encoded)} photo(s) downloaded successfully — skipping photo-quality pass", flush=True)
        return None

    parts = [{"text": PROMPT}]
    for b64_data, mime_type in encoded:
        parts.append({"inline_data": {"mime_type": mime_type, "data": b64_data}})

    body = {
        "contents": [{"parts": parts}],
        "generationConfig": {
            "response_mime_type": "application/json",
            "temperature": 0.1,
        },
    }

    try:
        resp = requests.post(f"{GEMINI_URL}?key={api_key}", json=body, timeout=GEMINI_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        parsed = json.loads(text)
    except Exception as e:
        print(f"[GEMINI] vision call failed: {e}", flush=True)
        return None

    photos = parsed.get("photos") or []
    n = len(photos)
    if n == 0:
        print("[GEMINI] response had no per-photo results — skipping", flush=True)
        return None

    def pct(key):
        return round(100 * sum(1 for p in photos if p.get(key)) / n)

    interior_room_count = sum(1 for p in photos if p.get("is_interior_room"))
    empty_room_count = sum(1 for p in photos if p.get("is_interior_room") and p.get("is_empty_room"))

    print(f"[GEMINI] photo-quality pass succeeded for {n} photo(s), {empty_room_count} empty room(s) detected", flush=True)
    return {
        "sample_size": n,
        "pct_straight": pct("straight"),
        "pct_well_exposed": pct("well_exposed"),
        "pct_sharp": pct("sharp"),
        "pct_natural_editing": pct("natural_editing"),
        "consistent_style": bool(parsed.get("consistent_style", True)),
        "has_watermark": any(p.get("has_watermark") for p in photos),
        "shows_clutter": any(p.get("shows_clutter") for p in photos),
        "shows_people": any(p.get("shows_people") for p in photos),
        "interior_room_count": interior_room_count,
        "empty_room_count": empty_room_count,
        "notes": (parsed.get("notes") or "").strip(),
    }
