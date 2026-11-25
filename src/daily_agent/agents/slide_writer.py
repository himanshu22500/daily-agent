"""Slide story writer for the web feed (issue #158).

One ``claude -p`` call turns a feed item into 3-5 slides, each a short bold
caption and a portrait SVG illustration, returned as structured JSON. The SVGs
are untrusted: callers store them only through ``feed.slides.SlideStore.put``,
which validates every slide.
"""

from __future__ import annotations

from .claude_code import ClaudeCodeError, is_claude_code, run_claude

# 3-5 hand-written SVGs are a lot of output; allow for it, but never hang.
TIMEOUT_SECONDS = 480.0

SLIDES_SCHEMA = {
    "type": "object",
    "properties": {
        "slides": {
            "type": "array",
            "minItems": 3,
            "maxItems": 5,
            "items": {
                "type": "object",
                "properties": {
                    "caption": {"type": "string", "maxLength": 80},
                    "svg": {"type": "string"},
                },
                "required": ["caption", "svg"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["slides"],
    "additionalProperties": False,
}

_SYSTEM_PROMPT = """\
You turn one item from a private learning feed into a short slide story, like
the news carousels on YouTube and Instagram: 3-5 slides the reader swipes
through in the feed card, each a big bold caption over its own illustration.
Tapping the card opens the full text, so the slides are the hook and the gist,
not everything.

## The story
- 3 slides for a small idea, up to 5 for a rich one. Every slide earns its
  place; no filler, no "in conclusion".
- Each caption is one punchy line, at most 80 characters, plain text (no
  markdown, no emoji, no hashtags). It is shown in bold capitals, so write
  normal sentence case and keep it short: aim for 30-60 characters.
- Slide 1 is the hook: the surprising fact, the problem, or the claim.
- The captions read as a sequence. Together they tell the story on their own.
- Mode "story" (an engineering insight, a video summary, a concept): hook ->
  what happens / how it works -> why -> the takeaway or fix.
- Mode "practice" (an interview practice card whose answer is revealed only
  after the reader has tried): set the problem up, the constraints and scale,
  the tension or trade-off, and end on the question to think about. NEVER give
  the answer, the design, or the solution away.
- Stay true to the text. Don't invent numbers, names or claims that aren't in
  it.

## The illustrations
- One SVG per slide, each a visual metaphor or a simple diagram for that
  slide's caption. Together they look like one set: the same palette and
  style across the slides, a different scene on each.
- Flat, bold editorial illustration: geometric shapes, confident colour, a
  little depth from overlapping shapes, simple shadows, 2-3 stop gradients.
- A full-bleed background in a rich colour (no white, no near-black).
- The caption is overlaid on the bottom 40% (y 600-1000) over a dark fade:
  keep the subject in the top 60%, and keep the bottom calm (background
  only). Keep the top corners calm too (a counter and a badge go there).
- No words in the art: no captions, labels, titles or code. At most 1-3
  symbolic glyphs when the metaphor needs them (e.g. `?`, `{ }`, `429`),
  drawn large with font-family="monospace" or "sans-serif".

## Technical rules (machine-validated; breaking any rule rejects the story)
- Exactly this root element on every slide:
  <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 1000">
  No width/height attributes.
- Self-contained: no <script>, <foreignObject>, <image>, <style>, no event
  attributes (onclick etc.), no href or url() pointing outside the file, no
  external fonts, no animation.
- Allowed elements only: rect, circle, ellipse, line, polyline, polygon, path,
  g, defs, linearGradient, radialGradient, stop, clipPath, mask, pattern, text,
  tspan, and <use> only with an internal #id.
- Well-formed XML. Escape & as &amp; and < as &lt; in any text.
- Between 1,200 and 6,000 bytes per slide.
- Prefix every id with a short tag unique to that slide.

## Output
JSON matching the schema: {"slides": [{"caption": "...", "svg": "<svg ...>...</svg>"}]}.
"""


def render(record: dict, feedback: list[str] | None) -> str:
    """The user prompt for one item (``{title, text, type, tags, mode}``)."""
    tags = ", ".join(record.get("tags") or [])
    parts = [
        f"Mode: {record.get('mode') or 'story'}",
        f"Title: {record.get('title') or '(none)'}",
        f"Type: {record.get('type') or 'general'}",
        f"Tags: {tags or '(none)'}",
        "",
        "Text:",
        str(record.get("text") or "").strip(),
    ]
    if feedback:
        parts += [
            "",
            "Your previous slides for this item were rejected by the validator:",
            *[f"- {problem}" for problem in feedback],
            "Write them again and fix every problem.",
        ]
    return "\n".join(parts)


async def write_slides(
    model: str, record: dict, feedback: list[str] | None = None
) -> list:
    """3-5 ``{caption, svg}`` slides for ``record`` (unvalidated)."""
    if not is_claude_code(model):
        raise ClaudeCodeError(f"slides need a claude-code model, not {model!r}")
    result = await run_claude(
        render(record, feedback),
        model=model,
        system_prompt=_SYSTEM_PROMPT,
        json_schema=SLIDES_SCHEMA,
        timeout=TIMEOUT_SECONDS,
    )
    reply = result.structured if isinstance(result.structured, dict) else {}
    slides = reply.get("slides")
    return slides if isinstance(slides, list) else []
