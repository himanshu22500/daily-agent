"""The curated system design topics (issue #134): offline, no model."""

from __future__ import annotations

from collections import Counter

from daily_agent.feed.design_topics import (
    BLOCK,
    BY_SLUG,
    CASE,
    CHEAT_SHEET_PROMPT,
    CHEAT_SHEET_SLUG,
    CHEAT_SHEET_STEPS,
    CHEAT_SHEET_TAGS,
    TOPICS,
)
from daily_agent.feed.prep_tracks import join_steps, slug, split_steps


def test_about_45_topics_with_unique_slugs():
    assert 40 <= len(TOPICS) <= 50
    assert len(BY_SLUG) == len(TOPICS)
    assert CHEAT_SHEET_SLUG not in BY_SLUG
    kinds = Counter(t.kind for t in TOPICS)
    assert set(kinds) == {CASE, BLOCK} and kinds[CASE] >= 20 and kinds[BLOCK] >= 12
    for topic in TOPICS:
        # Slugs end up in card keys and tags, so they must survive slugging.
        assert slug(topic.slug, limit=40) == topic.slug
        assert topic.title and len(topic.brief) > 40


def test_case_studies_are_design_questions_and_blinkit_ones_are_cases():
    for topic in TOPICS:
        if topic.kind == CASE:
            assert topic.title.startswith("Design "), topic.slug
            assert topic.tags, topic.slug
        else:
            assert not topic.title.startswith("Design "), topic.slug
    blinkit = [t for t in TOPICS if t.blinkit]
    assert len(blinkit) >= 4 and all(t.kind == CASE for t in blinkit)
    assert {"dark-store-inventory", "rider-dispatch", "delivery-eta", "flash-sale"} <= {
        t.slug for t in blinkit
    }
    assert not any(t.blinkit and t.classic for t in TOPICS)


def test_tags_name_building_blocks():
    # A case study's tags are the blocks it leans on, so weak blocks pull the
    # case studies that use them forward.
    blocks = {t.slug for t in TOPICS if t.kind == BLOCK}
    for topic in TOPICS:
        assert set(topic.tags) <= blocks, topic.slug
        assert topic.slug not in topic.tags
    assert set(CHEAT_SHEET_TAGS) <= blocks
    used = {tag for t in TOPICS if t.kind == CASE for tag in t.tags}
    assert {"caching", "queues", "idempotency", "geospatial"} <= used


def test_cheat_sheet_reveals_in_steps():
    prompt, steps = split_steps(join_steps(CHEAT_SHEET_PROMPT, CHEAT_SHEET_STEPS))
    assert prompt == CHEAT_SHEET_PROMPT.strip()
    assert [s.title for s in steps] == [title for title, _ in CHEAT_SHEET_STEPS]
    assert steps[0].title == "Latency" and "~100 ns" in steps[0].body
    assert all(s.body for s in steps)
