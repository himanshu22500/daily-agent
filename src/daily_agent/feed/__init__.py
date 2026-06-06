"""Delivery feed: paced, robust delivery of bite-sized updates.

A staged pipeline over SQLite. The :class:`~daily_agent.feed.delta.DeltaEngine`
turns accumulated activity into :class:`~daily_agent.models.Bite` deltas; the
:class:`~daily_agent.feed.outbox.Outbox` is the durable queue that guarantees
each bite is delivered at-least-once and never duplicated; a
:mod:`~daily_agent.feed.channels` channel does the actual sending.
"""
