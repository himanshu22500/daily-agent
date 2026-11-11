"""Cover illustrator for the web feed (issue #87).

Draws one SVG illustration per insight from the same art brief the first 301
covers were drawn with (by Claude subagents). The model's SVG is untrusted:
callers must run it through ``feed.covers.validate_cover`` before storing it.
"""

from __future__ import annotations

import re

from pydantic_ai import Agent

from .model import build_model, cache_settings, run_with_backoff

# An SVG is a few KB of output; allow for a slow provider, but never hang.
REQUEST_TIMEOUT_SECONDS = 180
ATTEMPTS = 3

_SYSTEM_PROMPT = """\
You are an editorial illustrator. Each request is a short engineering insight
(title, text, type, tags). Draw one cover illustration for it as a hand-written
SVG.

The covers appear in a private Google-Discover-style feed: a wide image on top
of each card, with the headline shown as real text underneath. Generic
gradients with a hashtag were rejected as awful. Yours must make each post look
distinct and worth opening.

## What to draw
- A visual metaphor for the specific idea in the insight, not a generic "tech"
  picture. Read the text, find the one surprising thing, and picture it.
  Examples:
  - "uv sync silently removes packages added with uv pip install" -> a broom
    sweeping small parcels off a shelf while one crate stays bolted down.
  - "Persisted Pinia stores don't survive Vite hot reload" -> a
    pineapple-shaped jar cracking as a lightning bolt passes through it.
  - "Dropping a PostgreSQL column silently drops its extended statistics" -> a
    table losing one column like a pulled tooth, with a small bar chart falling
    out with it.
- One clear focal subject, a simple scene, strong composition. It must still
  read at thumbnail size (about 150px wide).

## Style
- Flat, bold editorial illustration: geometric shapes, confident colour, a
  little depth from overlapping shapes, simple shadows, or 2-3 stop gradients.
  Think magazine spot illustration, not UI icon and not clip art.
- A full-bleed background (a rect or shapes covering the whole canvas) in a
  rich colour. No white or near-black backgrounds; the image sits on both light
  and dark pages.
- No words. No headline, no tags, no labels, no code snippets. At most 1-3
  symbolic glyphs when the metaphor needs them (e.g. `?`, `{ }`, `_id`, `503`),
  drawn large with font-family="monospace" or "sans-serif".
- Keep the top-right corner calm (roughly x 600-800, y 0-70): a small badge is
  overlaid there.

## Technical rules (the SVG is machine-validated; breaking any rule rejects it)
- Exactly this root element:
  <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 400">
  No width/height attributes.
- Self-contained: no <script>, <foreignObject>, <image>, <style>, no <use>
  pointing outside the file, no event attributes (onclick etc.), no href or
  url() pointing outside the file, no external fonts, no animation.
- Allowed elements only: rect, circle, ellipse, line, polyline, polygon, path,
  g, defs, linearGradient, radialGradient, stop, clipPath, mask, pattern, text,
  tspan, and <use> only with an internal #id.
- Well-formed XML. Escape & as &amp; and < as &lt; in any text.
- Between 1,200 and 6,000 bytes. Enough detail to be an illustration, small
  enough to load instantly.
- Prefix every id with a short tag unique to this illustration.

## Output
Reply with the SVG only: start with <svg and end with </svg>. No prose, no
markdown fences.
"""

_SVG = re.compile(r"<svg\b.*</svg>", re.DOTALL | re.IGNORECASE)


def extract_svg(reply: str) -> str:
    """The ``<svg>…</svg>`` element from a model reply ("" if there is none)."""
    match = _SVG.search(reply or "")
    return match.group(0) if match else ""


def build_cover_illustrator(model: str) -> Agent[None, str]:
    return Agent(
        build_model(model),
        output_type=str,
        system_prompt=_SYSTEM_PROMPT,
        model_settings={
            **(cache_settings(model) or {}),
            "timeout": REQUEST_TIMEOUT_SECONDS,
        },
    )


def _render(insight: dict, feedback: list[str] | None) -> str:
    tags = ", ".join(insight.get("tags") or [])
    parts = [
        f"Title: {insight.get('title') or '(none)'}",
        f"Type: {insight.get('type') or 'general'}",
        f"Tags: {tags or '(none)'}",
        "",
        "Text:",
        " ".join(str(insight.get("text") or "").split()),
    ]
    if feedback:
        parts += [
            "",
            "Your previous SVG for this insight was rejected by the validator:",
            *[f"- {problem}" for problem in feedback],
            "Draw it again and fix every problem.",
        ]
    return "\n".join(parts)


async def draw_cover(
    model: str, insight: dict, feedback: list[str] | None = None
) -> str:
    """One SVG illustration for ``insight`` (``{title, text, type, tags}``)."""
    result = await run_with_backoff(
        build_cover_illustrator(model), _render(insight, feedback), attempts=ATTEMPTS
    )
    return extract_svg(result.output)
