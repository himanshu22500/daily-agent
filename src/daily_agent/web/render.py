"""Small, safe text helpers for the web feed templates."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone

from markupsafe import Markup, escape

_CODE = re.compile(r"`([^`\n]+)`")
_BOLD = re.compile(r"\*\*([^*\n]+)\*\*")
_PARAGRAPH = re.compile(r"\n\s*\n")
_SLOT = re.compile(r"\x00(\d+)\x00")
_FENCE = "```"
_LANG = re.compile(r"[^a-z0-9+#-]")
_HEADING = re.compile(r"^#{1,6} +(\S.*)$")
_ITEM = re.compile(r"^(?:([-*])|(\d{1,3})[.)]) +(\S.*)$")
_TABLE_RULE = re.compile(r"^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?\s*$")
_CELL_SPLIT = re.compile(r"(?<!\\)\|")


def _inline(text: str) -> str:
    """`code` and **bold** in one escaped run of text; newlines become <br>.

    Code spans are set aside first, so ``**`` inside code stays literal and a
    bold phrase may contain code (``**Why `LOWER()`?**``).
    """
    parts = _CODE.split(text)
    # re.split with one group alternates: text, code, text, code, ...
    codes = [f"<code>{escape(part)}</code>" for part in parts[1::2]]
    html = "".join(
        f"\x00{i // 2}\x00" if i % 2 else str(escape(part))
        for i, part in enumerate(parts)
    )
    html = _BOLD.sub(r"<strong>\1</strong>", html)
    html = _SLOT.sub(lambda m: codes[int(m.group(1))], html)
    return html.replace("\n", "<br>")


def inline_markdown(text: str) -> Markup:
    """Render the markdown subset items use: paragraphs, `code`, **bold**,
    and (#137, for prep steps) fenced code blocks, tables, lists and headings.

    Everything is HTML-escaped first; only the tags generated here are emitted,
    so item text (which is model output) can never inject markup.
    """
    return Markup("".join(_blocks((text or "").strip().replace("\x00", ""))))


def inline_spans(text: str) -> Markup:
    """Only `code` and **bold**, no paragraph (a drill option, a label)."""
    return Markup(_inline((text or "").strip().replace("\x00", "")))


def _blocks(text: str) -> list[str]:
    """Fenced code first (it may hold blank lines), then the prose between."""
    out: list[str] = []
    prose: list[str] = []
    lines = text.split("\n")
    n = 0
    while n < len(lines):
        line = lines[n]
        if not line.lstrip().startswith(_FENCE):
            prose.append(line)
            n += 1
            continue
        out += _prose("\n".join(prose))
        prose = []
        indent = len(line) - len(line.lstrip())
        lang = _LANG.sub("", line.lstrip()[3:].strip().split(" ")[0].lower())
        code: list[str] = []
        n += 1
        while n < len(lines) and not lines[n].lstrip().startswith(_FENCE):
            raw = lines[n]
            cut = min(indent, len(raw) - len(raw.lstrip(" ")))
            code.append(raw[cut:])
            n += 1
        n += 1  # the closing fence (an unclosed fence runs to the end)
        out.append(_code_block("\n".join(code), lang))
    out += _prose("\n".join(prose))
    return out


_PY_KEYWORDS = (
    "and as assert async await break class continue def del elif else except "
    "finally for from global if import in is lambda nonlocal not or pass raise "
    "return try while with yield None True False"
).split()
_PY_TOKEN = re.compile(
    r"(?P<c>#[^\n]*)"
    r'|(?P<s>"""[\s\S]*?"""|' + r"'''[\s\S]*?'''"
    r'|"(?:\\.|[^"\\\n])*"' + r"|'(?:\\.|[^'\\\n])*')"
    r"|(?P<k>\b(?:" + "|".join(_PY_KEYWORDS) + r")\b)"
    r"|(?P<n>\b\d+(?:\.\d+)?\b)"
)
_PYTHON = {"python", "py", "python3"}


def _highlight_python(code: str) -> str:
    """Comments, strings, keywords and numbers in spans; all of it escaped."""
    out: list[str] = []
    last = 0
    for token in _PY_TOKEN.finditer(code):
        out.append(str(escape(code[last : token.start()])))
        out.append(f'<span class="tk-{token.lastgroup}">{escape(token.group())}</span>')
        last = token.end()
    out.append(str(escape(code[last:])))
    return "".join(out)


def _code_block(code: str, lang: str) -> str:
    """Monospace, never wrapped; it scrolls sideways on a phone (CSS)."""
    data = f' data-lang="{lang}"' if lang else ""
    body = _highlight_python(code) if lang in _PYTHON else str(escape(code))
    return f'<pre class="code"{data}><code>{body}</code></pre>'


def _prose(text: str) -> list[str]:
    return [
        html
        for block in _PARAGRAPH.split(text.strip())
        if block.strip()
        for html in _block(block.split("\n"))
    ]


def _block(lines: list[str]) -> list[str]:
    """One blank-line-separated block: paragraphs, with any table, list or
    heading inside it split out (model output rarely leaves the blank line)."""
    out: list[str] = []
    para: list[str] = []

    def flush() -> None:
        if para:
            out.append("<p>" + _inline("\n".join(para)) + "</p>")
            para.clear()

    n = 0
    while n < len(lines):
        line = lines[n]
        heading = _HEADING.match(line)
        if heading:
            flush()
            out.append(f'<h4 class="md-h">{_inline(heading.group(1).strip())}</h4>')
            n += 1
        elif (
            "|" in line
            and n + 1 < len(lines)
            and "|" in lines[n + 1]
            and _TABLE_RULE.match(lines[n + 1])
        ):
            flush()
            end = n + 2
            while end < len(lines) and "|" in lines[end] and lines[end].strip():
                end += 1
            out.append(_table(line, lines[n + 1], lines[n + 2 : end]))
            n = end
        elif _ITEM.match(line):
            flush()
            n, html = _list(lines, n)
            out.append(html)
        else:
            para.append(line)
            n += 1
    flush()
    return out


def _cells(row: str) -> list[str]:
    row = row.strip()
    row = row[1:] if row.startswith("|") else row
    row = row[:-1] if row.endswith("|") and not row.endswith("\\|") else row
    return [cell.strip().replace("\\|", "|") for cell in _CELL_SPLIT.split(row)]


def _table(head: str, rule: str, rows: list[str]) -> str:
    """A GFM table in a wrapper that scrolls sideways on a phone."""
    aligns = []
    for cell in _cells(rule):
        left, right = cell.startswith(":"), cell.endswith(":")
        aligns.append(
            "center" if left and right else "right" if right else "left" if left else ""
        )

    def row_html(cells: list[str], tag: str) -> str:
        tds = []
        for i, cell in enumerate(cells[: len(aligns)] or cells):
            align = aligns[i] if i < len(aligns) else ""
            style = f' style="text-align:{align}"' if align else ""
            tds.append(f"<{tag}{style}>{_inline(cell)}</{tag}>")
        return "<tr>" + "".join(tds) + "</tr>"

    body = "".join(row_html(_cells(r), "td") for r in rows)
    return (
        '<div class="table-wrap"><table>'
        f"<thead>{row_html(_cells(head), 'th')}</thead>"
        + (f"<tbody>{body}</tbody>" if body else "")
        + "</table></div>"
    )


def _list(lines: list[str], start: int) -> tuple[int, str]:
    """A run of ``- item`` or ``1. item`` lines; an indented line continues
    the item above it. Returns where the list ends and its HTML."""
    first = _ITEM.match(lines[start])
    ordered = first.group(2) is not None
    items: list[list[str]] = []
    n = start
    while n < len(lines):
        line = lines[n]
        item = _ITEM.match(line)
        if item and (item.group(2) is not None) == ordered:
            items.append([item.group(3)])
        elif items and line[:1] in (" ", "\t") and line.strip():
            items[-1].append(line.strip())
        else:
            break
        n += 1
    tag = "ol" if ordered else "ul"
    number = int(first.group(2)) if ordered else 1
    attrs = f' start="{number}"' if number != 1 else ""
    lis = "".join(f"<li>{_inline(chr(10).join(item))}</li>" for item in items)
    return n, f"<{tag}{attrs}>{lis}</{tag}>"


# Prep types (#130) whose name doesn't read well capitalized.
_LABELS = {"dsa": "DSA", "drill": "Pattern drill", "design": "System design"}


def label(value: str) -> str:
    """``"some-type"`` -> ``"Some type"`` for display."""
    if value in _LABELS:
        return _LABELS[value]
    words = re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).split()
    return " ".join(words).capitalize() or "General"


# "Fewer <kinds>" on the 👎 sheet (issue #126).
_KINDS = {
    "gotcha": "gotchas",
    "repo": "repo notes",
    "architecture": "architecture notes",
    "technique": "techniques",
    "general": "general notes",
    "video": "videos",
    "instagram": "Instagram posts",
    "interview": "interview questions",
    "concept": "concept explainers",
    "dsa": "DSA problems",
    "drill": "pattern drills",
    "design": "system design cards",
}


def kinds(value: str) -> str:
    """``"gotcha"`` -> ``"gotchas"``: an item type, plural, for display."""
    return _KINDS.get(value or "", label(value).lower() + " items")


def type_class(value: str) -> str:
    """CSS-safe class suffix for an insight type."""
    return re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-") or "general"


def ago(moment: datetime, now: datetime | None = None) -> str:
    """Compact relative time: ``just now``, ``5m``, ``3h``, ``yesterday``, ``Oct 30``."""
    current = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    seconds = (current - moment).total_seconds()
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    if seconds < 2 * 86400:
        return "yesterday"
    local = moment.astimezone()
    if local.year == current.astimezone().year:
        return f"{local:%b} {local.day}"
    return f"{local:%b} {local.day}, {local.year}"


def until(moment: datetime, now: datetime | None = None) -> str:
    """Compact time-until: ``in 40m``, ``in 5h``, ``tomorrow``, ``in 3 days``."""
    current = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    seconds = (moment - current).total_seconds()
    if seconds < 60:
        return "any moment"
    if seconds < 3600:
        return f"in {int(seconds // 60)}m"
    if seconds < 86400:
        return f"in {int(seconds // 3600)}h"
    if seconds < 2 * 86400:
        return "tomorrow"
    return f"in {int(seconds // 86400)} days"


def back_in(moment: datetime, now: datetime | None = None) -> str:
    """When a graded card comes back, in whole days: ``tomorrow``, ``in 3
    days``. (``until`` would say "in 23h" for a card due in one day.)"""
    current = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    days = round((moment - current).total_seconds() / 86400)
    if days <= 0:
        return "later today"
    return "tomorrow" if days == 1 else f"in {days} days"


# Base hue per insight type for generated covers; other types hash to a hue.
_TYPE_HUES = {"gotcha": 18, "repo": 215, "architecture": 265, "technique": 158}


def _hash(value: str) -> int:
    return int(hashlib.sha1(value.encode()).hexdigest()[:12], 16)


def cover_style(key: str, type: str) -> str:
    """CSS custom properties for a deterministic, per-insight generated cover.

    Hue family comes from the type; the exact hues, angle, and highlight
    position vary by key, so neighbouring cards of one type still differ.
    """
    h = _hash(key)
    base = _TYPE_HUES.get(type_class(type), _hash(type or "general") % 360)
    hue1 = (base + h % 36 - 18) % 360
    hue2 = (hue1 + 20 + (h >> 6) % 45) % 360
    angle = (h >> 12) % 180
    x, y = 12 + (h >> 20) % 76, 10 + (h >> 28) % 60
    return f"--h1:{hue1};--h2:{hue2};--angle:{angle}deg;--x:{x}%;--y:{y}%"


def source_hue(source: str) -> int:
    """Stable hue for a project's little 'publisher' badge."""
    return _hash(source or "?") % 360


def source_initial(source: str) -> str:
    """The letter on a source's badge: its first letter or digit, so a label
    with a leading symbol ("▶ Hello Interview") still shows "H"."""
    return next((ch.upper() for ch in source or "" if ch.isalnum()), "?")


def longdate(moment: datetime) -> str:
    """``November 11, 2026, 5:38 PM`` in local time."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    local = moment.astimezone()
    hour = local.hour % 12 or 12
    meridiem = "AM" if local.hour < 12 else "PM"
    return f"{local:%B} {local.day}, {local.year}, {hour}:{local:%M} {meridiem}"


def ago_long(moment: datetime, now: datetime | None = None) -> str:
    """Spelled-out relative time: ``38 mins ago``, ``3 hours ago``, ``Oct 30``."""
    short = ago(moment, now)
    if short.endswith("m") and short[:-1].isdigit():
        n = int(short[:-1])
        return f"{n} min{'s' if n != 1 else ''} ago"
    if short.endswith("h") and short[:-1].isdigit():
        n = int(short[:-1])
        return f"{n} hour{'s' if n != 1 else ''} ago"
    return short
