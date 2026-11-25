"""The Interview tab's tracks, step-by-step reader, drills and grade buttons
(issue #137). Offline: tmp_path SQLite (or Postgres schemas in CI)."""

from __future__ import annotations

import html
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, quote, urlsplit

import pytest
from starlette.testclient import TestClient

from daily_agent.feed.insights_store import InsightStore
from daily_agent.feed.prep_grades import GOT, MISSED, SHAKY, GradeStore
from daily_agent.feed.prep_tracks import (
    CARD_TYPE,
    DESIGN,
    DESIGN_TYPE,
    DRILL_TYPE,
    DSA,
    PROBLEM_TYPE,
    RESUME,
    PrepItem,
    PrepItems,
    item_key,
)
from daily_agent.feed.prep_view import (
    kicker,
    parse_drill,
    reveal_label,
    steps_of,
    tag_label,
    teaser,
)
from daily_agent.feed.web_feed import WebFeed, _past_title
from daily_agent.models import Insight
from daily_agent.web.app import create_app
from daily_agent.web.render import back_in, inline_markdown, source_initial

T0 = datetime(2026, 11, 25, 9, 0, tzinfo=timezone.utc)
FETCH = {"X-Requested-With": "fetch"}

PROBLEM = """Purchase events stream in as `(sku, t)`. Return the top-K SKUs of
the last hour, at any moment.

### Pattern
Sliding time window + count map + heap.

### Key idea
Evict events older than an hour from a deque, then:

```python
def top_k(counts, k):
    return heapq.nlargest(k, counts.items(), key=lambda kv: kv[1])
```

### Complexity
| Step | Time |
|---|---:|
| ingest | `O(1)` |
| query | O(U log k) |
"""

DRILL = """You get a stream of daily temperatures. For each day, find how many
days until a warmer one.

Which pattern fits best?
A) Sliding window
B) Monotonic stack
C) Two pointers
D) Binary search

### Answer
B) Monotonic stack: keep days waiting for a warmer one; pop when it comes.

### Why not the others
A window has no fixed size here.
"""

DESIGN_TEXT = """Design a URL shortener for 100M links a month.

### Requirements
Reads dominate writes 100:1.

### Caching
Cache hot links in Redis with an LRU policy.
"""

PROBLEM_KEY = item_key("dsa-blinkit-r1", "top-k")
DRILL_KEY = item_key("dsa-daily", "temperatures")
DESIGN_KEY = item_key("design-daily", "shortener")
CARD_KEY = "prep:t5-q1"


@pytest.fixture
def db(tmp_path) -> str:
    path = str(tmp_path / "prep.db")
    items = PrepItems(path)
    items.put(
        "dsa-blinkit-r1",
        "Blinkit DSA pack",
        [
            PrepItem(
                PROBLEM_KEY,
                PROBLEM_TYPE,
                "Top-K trending SKUs",
                PROBLEM,
                ["blinkit", "sliding-window", "heap"],
            )
        ],
        track=DSA,
        now=T0,
    )
    items.put(
        "dsa-daily",
        "Daily DSA",
        [
            PrepItem(
                DRILL_KEY,
                DRILL_TYPE,
                "Days until warmer",
                DRILL,
                ["monotonic-stack"],
            )
        ],
        track=DSA,
        now=T0,
    )
    items.put(
        "design-daily",
        "System design daily",
        [
            PrepItem(
                DESIGN_KEY,
                DESIGN_TYPE,
                "Design a URL shortener",
                DESIGN_TEXT,
                ["caching", "lru-cache"],
            )
        ],
        track=DESIGN,
        now=T0,
    )
    InsightStore(path).add(
        Insight(
            key=CARD_KEY,
            text="Because the master was at capacity.",
            title="Why move at all?",
            type=CARD_TYPE,
            tags=["t5", "surface"],
            captured_at=T0,
        )
    )
    InsightStore(path).add(
        Insight(
            key="insight:x", text="A coding insight.", type="gotcha", captured_at=T0
        )
    )
    return path


@pytest.fixture
def client(db) -> TestClient:
    return TestClient(create_app(db))


def _keys(page: str) -> list[str]:
    return list(
        dict.fromkeys(re.findall(r'data-card data-swap data-key="([^"]+)"', page))
    )


def _chips(page: str) -> list[str]:
    return [
        html.unescape(c.strip())
        for c in re.findall(r'<a class="chip[^"]*"[^>]*>([^<]+)<', page)
    ]


def _item(key: str) -> str:
    return "/items/" + quote(key, safe="")


# --- how a prep card reads ---------------------------------------------------- #


def test_tags_read_as_words():
    assert tag_label("sliding-window") == "Sliding window"
    assert tag_label("lru-cache") == "LRU cache"
    assert tag_label("bfs") == "BFS"
    assert tag_label("redis-cluster") == "Redis cluster"
    assert tag_label("") == ""
    # DSA patterns keep the names the practice packs use (#135).
    assert tag_label("heap") == "Heap / priority queue"
    assert tag_label("dp") == "Dynamic programming"


def test_kicker_names_the_type_and_first_topic_tag():
    key = item_key("dsa-blinkit-r1", "x")
    # "blinkit" names the source (its label and its key), not a topic.
    assert kicker(
        PROBLEM_TYPE, key, ["blinkit", "sliding-window"], "Blinkit DSA pack"
    ) == ("DSA · Sliding window")
    assert kicker(DESIGN_TYPE, "prep:x:y", ["system-design", "caching"]) == (
        "System design · Caching"
    )
    # A drill's tag is its answer: never shown.
    assert kicker(DRILL_TYPE, key, ["monotonic-stack"]) == "Pattern drill"
    assert kicker(PROBLEM_TYPE, key, ["dsa", "blinkit"], "Blinkit DSA pack") == "DSA"
    # A pack's priority tag isn't a topic either.
    assert kicker(PROBLEM_TYPE, key, ["must-do", "union-find"]) == "DSA · Union-Find"
    assert kicker(CARD_TYPE, key, ["t5"]) == ""


def test_step_buttons_read_like_hints():
    assert reveal_label("Pattern") == "Show pattern"
    assert reveal_label("Key idea") == "Show key idea"
    assert reveal_label("BFS order") == "Show BFS order"
    assert reveal_label("O(n) pass") == "Show O(n) pass"


def test_a_drill_parses_its_options_and_answer():
    drill = parse_drill(DRILL)
    assert drill is not None
    assert drill.prompt.endswith("Which pattern fits best?")
    assert [(o.letter, o.text) for o in drill.options] == [
        ("A", "Sliding window"),
        ("B", "Monotonic stack"),
        ("C", "Two pointers"),
        ("D", "Binary search"),
    ]
    assert drill.answer == "B" and drill.right("B") and not drill.right("A")
    assert [s.title for s in drill.steps] == ["Answer", "Why not the others"]
    assert steps_of(DRILL_TYPE, DRILL).drill == drill


@pytest.mark.parametrize(
    "text",
    [
        DRILL.replace("### Answer", "### Pattern"),  # no Answer step first
        DRILL.replace("B) Monotonic stack:", "Monotonic stack:"),  # no letter
        DRILL.replace("B) Monotonic stack:", "E) Monotonic stack:"),  # no option E
        DRILL.replace("C) Two pointers", "C) Two pointers\nor maybe"),  # text between
        DRILL.replace("B) Monotonic stack\n", ""),  # letters skip B
        "Which one?\nA) Only one\n\n### Answer\nA) That one.",  # one option
    ],
)
def test_a_malformed_drill_falls_back_to_plain_steps(text):
    assert parse_drill(text) is None
    view = steps_of(DRILL_TYPE, text)
    assert view.drill is None and view.steps


def test_the_teaser_never_gives_a_step_away():
    assert teaser(PROBLEM_TYPE, PROBLEM).startswith("Purchase events stream")
    assert "heap" not in teaser(PROBLEM_TYPE, PROBLEM)
    drill = teaser(DRILL_TYPE, DRILL)
    assert drill.endswith("Which pattern fits best?") and "Monotonic" not in drill
    code = "Given:\n```python\nxs = [1]\n```\n| a | b |\n|---|---|\n## Task\nSort it."
    assert teaser(PROBLEM_TYPE, code) == "Given:\nSort it."
    # A pack problem's bold labels are headings too: the teaser starts with
    # the scenario itself, not "Scenario A big sale...".
    labelled = "**Scenario**\nA big sale is coming.\n\n**Task:**\nReturn **k**."
    assert teaser(PROBLEM_TYPE, labelled) == "A big sale is coming.\n\nReturn **k**."


def test_a_card_preview_never_repeats_its_title():
    question = "Why split retrieval and ranking?"
    # A practice card whose prompt is its question: say nothing twice.
    assert _past_title(question, question) == "Tap to answer"
    assert _past_title(question + " Assume 1B items.", question) == "Assume 1B items."
    assert _past_title("Given a grid.", "Islands") == "Given a grid."


def test_a_source_badge_shows_a_letter():
    assert source_initial("▶ Hello Interview") == "H"
    assert source_initial("tranzact-v2") == "T"
    assert source_initial("") == "?"


def test_back_in_counts_whole_days():
    assert back_in(T0 + timedelta(days=1) - timedelta(seconds=2), T0) == "tomorrow"
    assert back_in(T0 + timedelta(days=3), T0) == "in 3 days"
    assert back_in(T0 + timedelta(hours=2), T0) == "later today"


# --- markdown blocks ---------------------------------------------------------- #


def test_fenced_code_keeps_its_lines_and_escapes_everything():
    out = str(
        inline_markdown(
            "Use this:\n```python\ndef f(x):\n\n    return x < 1  # **no**\n```\nDone."
        )
    )
    assert out.startswith("<p>Use this:</p>")
    assert '<pre class="code" data-lang="python"><code>' in out
    assert "\n\n    <span" in out  # the blank line and the indent survive
    assert "&lt;" in out and "<strong>" not in out
    assert '<span class="tk-k">def</span>' in out
    assert '<span class="tk-c"># **no**</span>' in out
    assert out.endswith("<p>Done.</p>")
    # A fence that never closes runs to the end; other languages aren't coloured.
    tail = str(inline_markdown("```sql\nSELECT '<b>'"))
    assert tail == (
        '<pre class="code" data-lang="sql"><code>SELECT &#39;&lt;b&gt;&#39;</code></pre>'
    )


def test_tables_scroll_and_keep_inline_markup():
    out = str(inline_markdown("| Step | Time |\n|:--|--:|\n| `a\\|b` x | **O(1)** |"))
    assert out.startswith('<div class="table-wrap"><table><thead><tr>')
    assert '<th style="text-align:left">Step</th>' in out
    assert '<td style="text-align:right"><strong>O(1)</strong></td>' in out
    # An escaped pipe stays in its cell (GFM), even inside code.
    assert '<td style="text-align:left"><code>a|b</code> x</td>' in out
    # Right after a line of prose, too; a short row keeps its cells.
    out = str(inline_markdown("Costs:\n| a | b |\n|---|---|\n| 1 |"))
    assert out == (
        '<p>Costs:</p><div class="table-wrap"><table><thead><tr><th>a</th>'
        "<th>b</th></tr></thead><tbody><tr><td>1</td></tr></tbody></table></div>"
    )


def test_lists_and_headings():
    out = str(
        inline_markdown("Edge cases:\n- empty\n- **one** item\n  wraps\n\n3. c\n4. d")
    )
    assert out == (
        "<p>Edge cases:</p><ul><li>empty</li><li><strong>one</strong> item<br>wraps</li>"
        '</ul><ol start="3"><li>c</li><li>d</li></ol>'
    )
    assert str(inline_markdown("#### Code\nx")) == '<h4 class="md-h">Code</h4><p>x</p>'


def test_prose_still_renders_as_before():
    assert str(inline_markdown("a\nb\n\n• **c**")) == (
        "<p>a<br>b</p><p>• <strong>c</strong></p>"
    )
    assert "<script>" not in str(inline_markdown("- <script>x</script>"))


# --- the deck ------------------------------------------------------------------ #


def _deck(feed: WebFeed, **narrow) -> set[str]:
    edition = feed.new_edition(interview=True, now=T0, **narrow)
    return {e.key for e in feed.edition_page(edition, limit=50).entries}


def test_the_deck_narrows_to_a_track_and_a_tag(db):
    feed = WebFeed(db)
    assert _deck(feed) == {PROBLEM_KEY, DRILL_KEY, DESIGN_KEY, CARD_KEY}
    assert _deck(feed, track=DSA) == {PROBLEM_KEY, DRILL_KEY}
    assert _deck(feed, track=DESIGN) == {DESIGN_KEY}
    assert _deck(feed, track=RESUME) == {CARD_KEY}
    assert _deck(feed, tag="heap") == {PROBLEM_KEY}  # last of its tags
    assert _deck(feed, tag="blinkit") == {PROBLEM_KEY}  # first
    assert _deck(feed, tag="sliding-window") == {PROBLEM_KEY}  # middle
    assert _deck(feed, tag="monotonic-stack") == {DRILL_KEY}  # the only one
    assert _deck(feed, tag="cache") == set()  # not part of "lru-cache"
    assert _deck(feed, track=DESIGN, tag="heap") == set()
    assert _deck(feed, track="nope") == _deck(feed)


def test_tracks_count_visible_cards(db):
    feed = WebFeed(db)
    assert feed.interview_tracks() == [
        (RESUME, "Resume", 1),
        (DSA, "DSA", 2),
        (DESIGN, "System design", 1),
    ]
    feed.set_hidden(DESIGN_KEY, True)
    assert feed.interview_tracks()[2] == (DESIGN, "System design", 0)


def test_next_revisit_is_per_track(db):
    feed = WebFeed(db)
    grades = GradeStore(db)
    grades.grade(DESIGN_KEY, SHAKY, now=T0)
    grades.grade(PROBLEM_KEY, MISSED, now=T0)
    assert feed.next_revisit_at(interview=True, now=T0) == T0 + timedelta(days=1)
    assert feed.next_revisit_at(interview=True, track=DESIGN, now=T0) == (
        T0 + timedelta(days=3)
    )
    assert feed.next_revisit_at(interview=True, track=DSA, tag="heap", now=T0) == (
        T0 + timedelta(days=1)
    )
    assert feed.next_revisit_at(interview=True, track=RESUME, now=T0) is None
    assert feed.due_at(DESIGN_KEY) == T0 + timedelta(days=3)


# --- the Interview tab ----------------------------------------------------------- #


def test_track_chips_hide_empty_tracks(client, db):
    page = client.get("/?view=interview").text
    assert _chips(page) == ["All", "Resume", "DSA", "System design"]
    WebFeed(db).set_hidden(DESIGN_KEY, True)
    page = client.get("/?view=interview").text
    assert _chips(page) == ["All", "Resume", "DSA"]
    # A picked track stays, even with nothing left in it.
    page = client.get("/?view=interview&track=design").text
    assert "System design" in _chips(page)
    assert "No system design cards yet." in page


def test_a_track_chip_narrows_the_deck(client):
    page = client.get("/?view=interview&track=dsa").text
    assert set(_keys(page)) == {PROBLEM_KEY, DRILL_KEY}
    assert 'class="chip chip-track on" href="/?view=interview&amp;track=dsa"' in page


def test_a_tag_link_shows_a_chip_that_clears(client):
    page = client.get("/?view=interview&track=dsa&tag=Heap").text
    assert _keys(page) == [PROBLEM_KEY]
    clear = re.search(r'class="chip chip-tag on" href="([^"]+)"', page)
    assert clear and html.unescape(clear.group(1)) == "/?view=interview&track=dsa"
    assert 'Heap / priority queue<span class="chip-x"' in page
    # The cards' own actions come back to the same deck.
    assert 'data-next="/?view=interview&amp;track=dsa&amp;tag=heap"' in page
    empty = client.get("/?view=interview&track=dsa&tag=tries").text
    assert "No DSA cards tagged Tries yet." in empty


def test_resume_points_sit_under_resume(client):
    # Point chips need the deep-dive tracker, so check the plumbing: a point
    # alone (an old link) means the Resume track, and other tracks drop it.
    page = client.get("/?view=interview&point=t5").text
    assert 'class="chip chip-track on" href="/?view=interview&amp;track=resume"' in page
    assert "All points" not in client.get("/?view=interview").text
    assert set(_keys(client.get("/?view=interview&track=dsa&point=t5").text)) == {
        PROBLEM_KEY,
        DRILL_KEY,
    }


def test_the_end_of_a_track_says_so(db):
    client = TestClient(create_app(db, page_size=1))
    first = client.get("/?view=interview&track=dsa&tag=heap").text
    assert "every DSA card tagged Heap / priority queue that" in first
    page = client.get("/?view=interview&track=dsa").text
    more = html.unescape(re.search(r'class="more" href="([^"]+)"', page).group(1))
    assert parse_qs(urlsplit(more).query)["track"] == ["dsa"]


def test_step_cards_have_a_kicker_and_no_cover(client):
    page = client.get("/?view=interview").text
    for key, kick in (
        (PROBLEM_KEY, "DSA · Sliding window"),
        (DRILL_KEY, "Pattern drill"),
        (DESIGN_KEY, "System design · Caching"),
    ):
        card = page[page.index(f'data-key="{key}"') :]
        card = card[: card.index("</article>")]
        assert '<p class="kicker k-' in card and kick in card
        assert 'class="cover' not in card
        assert 'data-rate="down"' not in card  # graded in the reader instead
    problem = page[page.index(f'data-key="{PROBLEM_KEY}"') :]
    problem = problem[: problem.index("</article>")]
    assert "Top-K trending SKUs" in problem and "Blinkit DSA pack" in problem
    assert "Purchase events stream in as (sku, t)." in problem
    assert "Sliding time window" not in problem.split("data-share")[0]


# --- the reader ------------------------------------------------------------------- #


def test_steps_reveal_one_at_a_time(client):
    page = client.get(f"/i/{quote(PROBLEM_KEY, safe='')}").text
    assert '<p class="kicker k-dsa">DSA · Sliding window</p>' in page
    assert "From <strong>Blinkit DSA pack</strong>" in page
    labels = re.findall(r'<span class="if-closed">([^<]+)</span>', page)
    assert labels == ["Show pattern", "Show key idea", "Show complexity"]
    # Nested: each step's button sits inside the step before it, and the
    # grade box inside the last one.
    steps = page[page.index('<section class="steps"') : page.index('class="ask"')]
    assert steps.count('<details class="step" data-step>') == 3
    assert steps.index('class="grade"') > steps.rindex('<details class="step"')
    assert re.search(r"</section>\s*</details></details></details>\s*</section>", steps)
    assert '<pre class="code" data-lang="python">' in page
    assert '<div class="table-wrap">' in page
    assert "data-steps-all>Show all</a>" in page
    # Grading replaces 👍 / 👎 on prep cards; no cover either.
    assert "Was this worth reading?" not in page
    assert 'class="reader-figure"' not in page
    assert "How did it go?" in page
    # Topics practise more of the same instead of searching.
    assert (
        'href="/?view=interview&amp;track=dsa&amp;tag=heap">Heap / priority queue</a>'
        in page
    )


def test_show_all_without_js_opens_every_step(client):
    page = client.get(f"/i/{quote(PROBLEM_KEY, safe='')}?steps=all").text
    assert page.count('<details class="step" data-step open>') == 3
    assert "data-steps-all" not in page


def test_a_flashcard_grades_after_its_answer(client):
    page = client.get(f"/i/{quote(CARD_KEY, safe='')}").text
    answer = page[page.index('<details class="answer">') :]
    answer = answer[: answer.index("</details>\n")]
    assert "Show answer" in answer and 'class="grade"' in answer
    assert "Was this worth reading?" not in page


def test_other_items_keep_thumbs_and_no_grades(client):
    page = client.get("/i/insight:x").text
    assert "Was this worth reading?" in page and 'class="grade' not in page
    assert 'data-rate="down"' in client.get("/").text


def test_a_drill_shows_its_options_as_buttons(client):
    page = client.get(f"/i/{quote(DRILL_KEY, safe='')}").text
    assert re.findall(r'name="choice" value="([A-D])"', page) == list("ABCD")
    assert (
        'class="option "' in page and "disabled" not in page.split("</form>")[0][-600:]
    )
    assert "Which pattern fits best?" in page
    assert '<span class="option-text">Monotonic stack</span>' in page
    # Its tag would give the answer away.
    assert "Practice more" not in page and "monotonic-stack</a>" not in page


# --- grading ---------------------------------------------------------------------- #


def test_grading_with_js_answers_with_the_grade_box(client, db):
    res = client.post(
        f"{_item(PROBLEM_KEY)}/grade?as=grade&from=%2F%3Fview%3Dinterview",
        data={"grade": SHAKY},
        headers=FETCH,
    )
    assert res.status_code == 200
    assert 'class="grade is-graded"' in res.text
    assert "Shaky · back in 3 days" in res.text
    # "Change" offers the other two grades.
    assert re.findall(r'name="grade" value="(\w+)"', res.text) == [GOT, MISSED]
    assert GradeStore(db).latest()[PROBLEM_KEY].grade == SHAKY


def test_grading_without_js_comes_back_to_the_reader(client):
    res = client.post(
        f"{_item(CARD_KEY)}/grade?as=grade&from=%2F%3Fview%3Dinterview",
        data={"grade": GOT},
        follow_redirects=False,
    )
    assert res.status_code == 303
    target = res.headers["location"]
    assert target.startswith(f"/i/{quote(CARD_KEY, safe='')}?")
    assert target.endswith("#grade")
    query = parse_qs(urlsplit(target).query)
    assert query == {"from": ["/?view=interview"], "steps": ["all"], "graded": ["got"]}
    page = client.get(target.split("#")[0]).text
    assert '<details class="answer" open>' in page
    assert "Got it · back in 7 days" in page
    assert "Last graded Got it" in page
    assert 'href="/?view=interview" data-close' in page


def test_picking_a_drill_option_grades_it(client, db):
    action = f"{_item(DRILL_KEY)}/grade?as=steps&from=%2F"
    right = client.post(action, data={"choice": "B"}, headers=FETCH)
    assert right.status_code == 200
    assert GradeStore(db).latest()[DRILL_KEY].grade == GOT
    assert '<section class="steps is-drill" data-steps>' in right.text
    assert 'value="B" class="option is-right" disabled' in right.text
    assert (
        "<strong>Right.</strong>" in right.text
        and "Got it · back in 7 days" in right.text
    )
    # The Answer step opens; the next one waits behind its button.
    assert right.text.count('<details class="step" data-step open>') == 1
    assert "Show why not the others" in right.text

    wrong = client.post(action, data={"choice": "a"}, headers=FETCH)
    assert GradeStore(db).latest()[DRILL_KEY].grade == MISSED
    assert 'value="A" class="option is-wrong" disabled' in wrong.text
    assert 'value="B" class="option is-right" disabled' in wrong.text
    assert (
        "Not quite: it&#39;s B." in wrong.text
        and "Missed · back tomorrow" in wrong.text
    )

    plain = client.post(action, data={"choice": "C"}, follow_redirects=False)
    query = parse_qs(urlsplit(plain.headers["location"]).query)
    assert query == {"from": ["/"], "choice": ["C"], "graded": ["missed"]}
    page = client.get(plain.headers["location"].split("#")[0]).text
    assert 'value="C" class="option is-wrong" disabled' in page


def test_grade_errors(client, db):
    assert (
        client.post(f"{_item(DRILL_KEY)}/grade", data={"choice": "E"}).status_code
        == 400
    )
    assert (
        client.post(f"{_item(PROBLEM_KEY)}/grade", data={"choice": "A"}).status_code
        == 400
    )
    assert (
        client.post(f"{_item(PROBLEM_KEY)}/grade", data={"grade": "great"}).status_code
        == 400
    )
    assert client.post(f"{_item(PROBLEM_KEY)}/grade", data={}).status_code == 400
    assert client.post("/items/insight:x/grade", data={"grade": GOT}).status_code == 404
    assert client.post("/items/prep:nope/grade", data={"grade": GOT}).status_code == 404
    refused = client.post(
        f"{_item(PROBLEM_KEY)}/grade",
        data={"grade": GOT},
        headers={"Origin": "https://evil.example"},
    )
    assert refused.status_code == 403
    assert GradeStore(db).latest() == {}
    # The redirect target stays on this site.
    res = client.post(
        f"{_item(PROBLEM_KEY)}/grade?from=//evil.example",
        data={"grade": GOT},
        follow_redirects=False,
    )
    assert parse_qs(urlsplit(res.headers["location"]).query)["from"] == ["/"]
