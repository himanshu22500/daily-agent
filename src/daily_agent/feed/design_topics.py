"""The curated system design topics behind the daily design cards (issue #134).

Pitched at a **mid-level** interview: a complete, working design plus solid
answers to the usual probes. Three groups of topics:

- **Case studies** (``CASE``), written as "Design <X>" walkthroughs. The
  Blinkit-flavoured ones (quick commerce: dark stores, riders, 10-minute
  delivery) come first, then the classics, then the rest.
- **Building blocks** (``BLOCK``): one concept, asked as an interview question.
  Their slugs are the tag vocabulary: a case study is tagged with the blocks
  it leans on, so a weak block (``prep_grades.weak_tags``) pulls the case
  studies that use it forward, and the other way round.
- **One cheat sheet** of latency and throughput numbers, written by hand here
  (no model call), so its numbers are checked once and never drift.

Each topic's ``brief`` fixes the scope the writer covers, so two runs of the
same topic answer the same question.
"""

from __future__ import annotations

from dataclasses import dataclass

CASE, BLOCK = "case", "block"


@dataclass(frozen=True)
class DesignTopic:
    slug: str
    kind: str  # CASE or BLOCK
    title: str  # the card's title: "Design a URL shortener", "Consistent hashing"
    brief: str  # the scope the writer must cover
    tags: tuple[str, ...] = ()  # building blocks it uses (block slugs)
    blinkit: bool = False  # quick-commerce flavoured (Blinkit is a target company)
    classic: bool = False  # among the most-asked; queued before the rest


def _case(slug, title, brief, tags, **flags) -> DesignTopic:
    return DesignTopic(slug, CASE, title, brief, tuple(tags), **flags)


def _block(slug, title, brief, tags=(), **flags) -> DesignTopic:
    return DesignTopic(slug, BLOCK, title, brief, tuple(tags), **flags)


# The order here is the order within each group.
TOPICS: tuple[DesignTopic, ...] = (
    # --- case studies: Blinkit-flavoured ----------------------------------- #
    _case(
        "dark-store-inventory",
        "Design dark-store inventory with stock reservations",
        "Per-store stock for a few thousand SKUs in each of hundreds of dark "
        "stores. Checkout reserves stock with a short TTL; a cancel, a failed "
        "payment or a timeout releases it; never oversell. Picking and restocks "
        "update counts, and the app shows in or out of stock for the user's store.",
        ["idempotency", "distributed-locks", "caching", "sharding"],
        blinkit=True,
    ),
    _case(
        "rider-dispatch",
        "Design rider assignment for 10-minute delivery",
        "While an order is packed at a dark store, assign the best available "
        "rider within seconds. Riders send GPS every few seconds; a rider never "
        "gets two assignments by mistake; two orders going the same way can be "
        "batched; reassign when a rider rejects or doesn't respond.",
        ["geospatial", "realtime", "distributed-locks", "queues"],
        blinkit=True,
    ),
    _case(
        "delivery-eta",
        "Design delivery ETA for a quick-commerce app",
        "Show a promised delivery time on the home screen before checkout and a "
        "live ETA after ordering. Inputs: the store's picking queue, rider "
        "availability and travel time. The home-screen ETA must be fast, so it "
        "is precomputed per store and area; learn from actual delivery times.",
        ["geospatial", "caching", "stream-processing"],
        blinkit=True,
    ),
    _case(
        "flash-sale",
        "Design a flash sale with limited stock",
        "1,000 units of a hot item go on sale at 6 PM and a million users tap "
        "Buy within seconds. No overselling, at most one per user, and the rest "
        "of the app stays up: admission control, an atomic stock decrement, and "
        "orders created asynchronously.",
        ["rate-limiting", "caching", "queues", "idempotency"],
        blinkit=True,
    ),
    _case(
        "cart-checkout",
        "Design cart and checkout with stock holds",
        "A user's cart is tied to their nearest dark store. Checkout holds "
        "stock, takes payment through a payment gateway and confirms the order. "
        "Payment callbacks can be slow or arrive twice; holds expire; one "
        "checkout makes exactly one order.",
        ["idempotency", "distributed-locks", "sql-vs-nosql", "queues"],
        blinkit=True,
    ),
    _case(
        "order-tracking",
        "Design live order tracking",
        "The customer sees the order's status (packed, picked up, arriving) and "
        "the rider moving on a map. Riders send GPS every 3-5 seconds; updates "
        "go to the one customer watching; mobile networks drop and reconnect.",
        ["realtime", "geospatial", "queues", "caching"],
        blinkit=True,
    ),
    _case(
        "store-catalog",
        "Design per-store catalog and pricing",
        "One master catalog of products, with availability, price and offers "
        "that differ per dark store. Home and category pages load fast for the "
        "user's store; a price change reaches every app within a minute; read "
        "traffic peaks at dinner time.",
        ["caching", "cdn", "sql-vs-nosql", "indexes"],
        blinkit=True,
    ),
    _case(
        "grocery-autocomplete",
        "Design grocery search autocomplete",
        "Type-ahead suggestions on a grocery app, under 100 ms per keystroke. "
        "Handles misspellings and transliterated Hindi ('aata' for 'atta'), "
        "ranks by popularity, and suggests only what the user's dark store has "
        "in stock.",
        ["search", "caching", "indexes"],
        blinkit=True,
    ),
    # --- case studies: classics -------------------------------------------- #
    _case(
        "url-shortener",
        "Design a URL shortener",
        "Like bit.ly: create a short link for a long URL, with an optional "
        "custom alias and expiry, and redirect fast. Reads far outnumber writes.",
        ["caching", "indexes", "sharding"],
        classic=True,
    ),
    _case(
        "rate-limiter",
        "Design a rate limiter",
        "A distributed rate limiter in front of an API: limits per user and per "
        "IP, a few milliseconds of added latency at most, shared by many "
        "gateway instances, and a clear 429 when a client is limited.",
        ["rate-limiting", "caching"],
        classic=True,
    ),
    _case(
        "news-feed",
        "Design a news feed",
        "Like Facebook or X: users follow others, post, and read a home feed of "
        "the people they follow, newest first, loaded fast. Some accounts have "
        "millions of followers.",
        ["caching", "queues", "sharding"],
        classic=True,
    ),
    _case(
        "chat",
        "Design WhatsApp",
        "1:1 and small group chat: send and receive in real time, deliver to "
        "offline users when they reconnect, sent/delivered/read receipts, and "
        "order kept within a chat.",
        ["realtime", "queues", "sharding", "sql-vs-nosql"],
        classic=True,
    ),
    _case(
        "ticket-booking",
        "Design Ticketmaster",
        "Browse and search events, see available seats, hold a seat for about "
        "10 minutes while paying, and never sell a seat twice. Popular events "
        "bring huge spikes the moment sales open.",
        ["distributed-locks", "caching", "search", "queues"],
        classic=True,
    ),
    _case(
        "ride-hailing",
        "Design Uber",
        "Riders request a ride and see a fare; a nearby driver is matched; "
        "drivers send their location every few seconds; a driver gets one "
        "request at a time and can accept or decline.",
        ["geospatial", "realtime", "distributed-locks"],
        classic=True,
    ),
    _case(
        "file-sync",
        "Design Dropbox",
        "Upload, download and share files, and sync changes across a user's "
        "devices. Large files (up to tens of GB) upload in resumable chunks.",
        ["blob-storage", "cdn", "queues"],
        classic=True,
    ),
    _case(
        "video-streaming",
        "Design YouTube",
        "Creators upload videos and viewers watch them smoothly on any network: "
        "transcoding into several resolutions, adaptive bitrate streaming, a "
        "CDN, and view counts.",
        ["blob-storage", "cdn", "queues"],
        classic=True,
    ),
    _case(
        "notification-service",
        "Design a notification service",
        "Send push, SMS and email for many teams in one company: user "
        "preferences and opt-outs, templates, retries without duplicate sends, "
        "and OTPs ahead of marketing.",
        ["queues", "idempotency", "rate-limiting"],
        classic=True,
    ),
    _case(
        "web-crawler",
        "Design a web crawler",
        "Crawl about a billion pages in a few days for a search index: the URL "
        "frontier, politeness per domain, robots.txt, de-duplication, and "
        "recovery from crashes.",
        ["queues", "blob-storage", "rate-limiting"],
        classic=True,
    ),
    _case(
        "search-typeahead",
        "Design search typeahead",
        "The top suggestions for each prefix as the user types into a large "
        "search box, under 100 ms, ranked by popularity and refreshed regularly.",
        ["caching", "search"],
        classic=True,
    ),
    _case(
        "distributed-cache",
        "Design a distributed cache",
        "A Redis- or Memcached-like cache cluster: get and set with a TTL, "
        "eviction when full, scaling out by adding nodes, hot keys, and node "
        "failures.",
        ["caching", "consistent-hashing", "replication"],
        classic=True,
    ),
    # --- case studies: the rest -------------------------------------------- #
    _case(
        "payment-system",
        "Design a payment system",
        "Take payments for an online store through an external processor (like "
        "Stripe or Razorpay): an idempotent payment API, webhooks, a ledger, "
        "refunds, and daily reconciliation. Never charge twice.",
        ["idempotency", "queues", "sql-vs-nosql"],
    ),
    _case(
        "leaderboard",
        "Design a real-time leaderboard",
        "A game with 10M players: update scores as games end, and show the top "
        "100, a player's own rank and the players around them, within seconds.",
        ["caching", "sharding"],
    ),
    _case(
        "job-scheduler",
        "Design a distributed job scheduler",
        "Schedule one-off and recurring (cron) jobs on a pool of workers: "
        "at-least-once with retries, no job running twice at once, and "
        "thousands of jobs a second at peak.",
        ["queues", "distributed-locks", "idempotency"],
    ),
    _case(
        "metrics-monitoring",
        "Design a metrics and monitoring system",
        "Collect metrics from 100k servers every 10 seconds, store them as time "
        "series, power dashboards and alerts, and keep old data at lower "
        "resolution.",
        ["stream-processing", "sharding", "queues"],
    ),
    _case(
        "hotel-booking",
        "Design a hotel booking system",
        "Search hotels by city and dates, see availability and prices, book a "
        "room without overbooking (inventory per room type per night), and "
        "cancel.",
        ["distributed-locks", "search", "caching", "sql-vs-nosql"],
    ),
    _case(
        "ad-click-aggregator",
        "Design an ad click aggregator",
        "Record ad clicks (10k a second, spikes to 100k), redirect the user to "
        "the advertiser, and show advertisers clicks per ad per minute within "
        "seconds, counting each click once.",
        ["stream-processing", "queues", "idempotency"],
    ),
    _case(
        "online-judge",
        "Design LeetCode",
        "Browse problems, submit code in several languages and get results in a "
        "few seconds. Untrusted code runs isolated; contests have a live "
        "leaderboard and a spike at the start.",
        ["queues", "caching", "rate-limiting"],
    ),
    # --- building blocks: classics ----------------------------------------- #
    _block(
        "caching",
        "Caching strategies",
        "Cache-aside, write-through and write-back; TTLs and invalidation; "
        "eviction; stampedes and hot keys; what to cache and where.",
        classic=True,
    ),
    _block(
        "sql-vs-nosql",
        "SQL vs NoSQL",
        "Choosing between a relational database (Postgres) and NoSQL "
        "(DynamoDB, Cassandra, MongoDB) from access patterns, transactions and "
        "scale, and why Postgres is a sound default.",
        classic=True,
    ),
    _block(
        "indexes",
        "Database indexes",
        "B-tree indexes, composite index column order, covering indexes, and "
        "what indexes cost on writes; a glance at geospatial and full-text "
        "indexes.",
        classic=True,
    ),
    _block(
        "sharding",
        "Sharding and partitioning",
        "Choosing a shard key, hash vs range partitioning, hot partitions, "
        "cross-shard queries, and resharding.",
        classic=True,
    ),
    _block(
        "replication",
        "Replication and consistency",
        "Leader-follower replication, synchronous vs asynchronous, read "
        "replicas and replication lag, read-your-writes, failover; quorums in "
        "brief.",
        classic=True,
    ),
    _block(
        "queues",
        "Message queues and Kafka",
        "When to put a queue between services; a queue (SQS) vs a log (Kafka); "
        "partitions, consumer groups and per-key ordering; at-least-once "
        "delivery, retries and dead-letter queues; backpressure.",
        classic=True,
    ),
    _block(
        "idempotency",
        "Idempotency and exactly-once",
        "Idempotency keys and dedupe tables; at-least-once delivery plus an "
        "idempotent consumer as 'effectively once'; the transactional outbox.",
        classic=True,
    ),
    _block(
        "distributed-locks",
        "Distributed locks",
        "When you need one, and when a database row lock or a conditional "
        "update is enough; Redis SET NX with a TTL, leases and fencing tokens; "
        "ZooKeeper or etcd.",
        classic=True,
    ),
    _block(
        "consistent-hashing",
        "Consistent hashing",
        "The hash ring, virtual nodes, and minimal key movement when nodes "
        "join or leave; where it's used (cache clusters, Cassandra and "
        "DynamoDB partitioning).",
        ["sharding", "caching"],
        classic=True,
    ),
    _block(
        "cap",
        "CAP and PACELC",
        "At mid level: during a network partition, choose consistency or "
        "availability per feature, with examples; PACELC's latency vs "
        "consistency trade-off the rest of the time; strong vs eventual "
        "consistency.",
        ["replication"],
        classic=True,
    ),
    _block(
        "load-balancing",
        "Load balancing",
        "L4 vs L7 load balancers; round robin, least connections and hashing; "
        "health checks; sticky sessions and why to avoid them; scaling "
        "stateless servers.",
        classic=True,
    ),
    _block(
        "geospatial",
        "Geospatial indexes",
        "Finding nearby things fast: geohashes, quadtrees, Redis GEO and "
        "PostGIS; frequent location updates; neighbours across cell edges.",
        ["indexes"],
        classic=True,
    ),
    _block(
        "realtime",
        "WebSockets vs SSE vs polling",
        "Short polling, long polling, server-sent events and WebSockets, and "
        "when each fits; holding many open connections; routing a message to "
        "the server that holds the user's connection.",
        classic=True,
    ),
    # --- building blocks: the rest ----------------------------------------- #
    _block(
        "rate-limiting",
        "Rate-limiting algorithms",
        "Token bucket, leaky bucket, fixed window, and sliding window log and "
        "counter; implementing them with Redis; where to enforce limits.",
    ),
    _block(
        "cdn",
        "CDNs",
        "Edge caching of static files and media; pull vs push; cache keys, "
        "TTLs and invalidation; signed URLs; what not to put on a CDN.",
        ["caching"],
    ),
    _block(
        "blob-storage",
        "Blob storage and presigned URLs",
        "Files in S3-like object storage with their metadata in a database; "
        "presigned URLs for direct upload and download; multipart uploads; a "
        "CDN in front.",
        ["cdn"],
    ),
    _block(
        "search",
        "Search and inverted indexes",
        "How an inverted index works; Elasticsearch as a secondary index; "
        "tokenizing, fuzzy matching and ranking basics; keeping it in sync "
        "with the main database (CDC).",
        ["indexes"],
    ),
    _block(
        "stream-processing",
        "Stream processing",
        "Aggregating events as they arrive (Kafka with Flink or Kafka "
        "Streams): tumbling and sliding windows, event time vs processing "
        "time and late events, state and checkpoints; when a batch job is "
        "enough.",
        ["queues"],
    ),
    _block(
        "estimation",
        "Back-of-envelope estimation",
        "Turning users into QPS, storage and bandwidth; rounding hard; and "
        "estimating only when the number changes a design decision.",
    ),
)

BY_SLUG: dict[str, DesignTopic] = {t.slug: t for t in TOPICS}


# --- the cheat sheet (hand-written, no model) ------------------------------ #

CHEAT_SHEET_SLUG = "latency-numbers"
CHEAT_SHEET_TITLE = "Latency and throughput numbers every engineer should know"
CHEAT_SHEET_TAGS = ("estimation",)
CHEAT_SHEET_PROMPT = """\
Without looking, put a rough number on each: a RAM read, an SSD read, a round \
trip inside one datacenter, and one across continents. Then: how many simple \
requests a second can one Postgres primary, one Redis node and one Kafka \
broker take?

Say your numbers out loud, then reveal. The nearest order of magnitude is all \
an interviewer expects."""
CHEAT_SHEET_STEPS: tuple[tuple[str, str], ...] = (
    (
        "Latency",
        """\
- **L1 cache:** ~1 ns
- **RAM read:** ~100 ns
- **SSD random read (4 KB):** ~0.1 ms (NVMe: tens of µs)
- **Read 1 MB sequentially from SSD:** ~0.5-1 ms
- **HDD seek:** ~5-10 ms
- **Round trip inside one datacenter:** ~0.5 ms
- **Redis GET over the network:** under 1 ms
- **Indexed Postgres lookup, warm cache:** ~1-5 ms
- **Mobile (4G) round trip:** ~50-100 ms
- **Round trip across continents:** ~100-250 ms
- **What users feel:** ~100 ms feels instant; 1 s breaks the flow.""",
    ),
    (
        "Throughput per node",
        """\
Rules of thumb for one decent machine:
- **App server:** ~1k-10k simple requests/s; far fewer if each does real work.
- **Postgres or MySQL primary:** ~10k-50k simple indexed reads/s, writes in \
the thousands a second; a few TB of data is comfortable.
- **Redis:** ~100k operations/s, sub-millisecond; the data must fit in RAM.
- **Kafka broker:** hundreds of MB/s, up to about a million small messages/s.
- **WebSocket server:** ~100k open connections with tuning.
- **Network:** 1 Gbps ≈ 125 MB/s; 10 Gbps ≈ 1.25 GB/s.""",
    ),
    (
        "Estimation shortcuts",
        """\
- **A day ≈ 86,400 s ≈ 10⁵ s.** 1M requests a day ≈ 12/s; 100M a day ≈ 1,200/s.
- **A year ≈ 3 × 10⁷ s.**
- **Peak ≈ 2-3× average.** Quick commerce peaks harder: dinner time, sales.
- **Sizes:** UUID 16 B, timestamp 8 B, a typical row 100 B-1 KB, a photo \
~200 KB-2 MB, a minute of HD video ~50 MB.
- **Powers of 2:** 2¹⁰ ≈ 10³ (KB), 2²⁰ ≈ 10⁶ (MB), 2³⁰ ≈ 10⁹ (GB), \
2⁴⁰ ≈ 10¹² (TB).""",
    ),
    (
        "Availability",
        """\
- **99.9%:** ~43 min down a month (~8.8 h a year)
- **99.99%:** ~4.3 min a month (~53 min a year)
- **99.999%:** ~26 s a month (~5 min a year)
- Two services in a chain at 99.9% each ≈ 99.8% together.""",
    ),
    (
        "Using them in an interview",
        """\
- Estimate only when the number changes a decision: "about 1,200 reads/s, \
4k at peak: one Postgres primary and a read replica handle that, no sharding \
yet."
- Hold your numbers against these limits to justify a cache, a queue or a \
shard, then move on. Two minutes at most.
- Say "assume" and round hard: 86,400 becomes 10⁵.""",
    ),
)
