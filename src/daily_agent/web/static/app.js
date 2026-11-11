// Progressive enhancement for the insight feed. Without this file every action
// still works (forms post and redirect; "Load more" is a plain link). Only view
// tracking needs JS: without it, nothing is marked seen.
(() => {
  const feed = document.getElementById("feed");
  const sessionId = feed && feed.dataset.session;

  // --- Seen = actually on screen -------------------------------------------
  // A card counts once it is mostly visible (or fills half the screen, for tall
  // cards) for DWELL_MS. Views are batched and posted to /seen; a beacon flushes
  // the rest when the page is hidden or closed.
  const DWELL_MS = 1000;
  const pending = new Set();
  const counted = new Set();
  const timers = new Map();
  let flushTimer = null;

  function flush(beacon) {
    clearTimeout(flushTimer);
    if (!pending.size || !sessionId) return;
    const body = JSON.stringify({ session: sessionId, keys: [...pending] });
    pending.clear();
    if (beacon && navigator.sendBeacon && navigator.sendBeacon("/seen", body)) return;
    fetch("/seen", {
      method: "POST",
      body,
      keepalive: true,
      headers: { "Content-Type": "application/json", "X-Requested-With": "fetch" },
    }).catch(() => {});
  }

  function onScreen(entry) {
    if (!entry.isIntersecting) return false;
    const viewport = (entry.rootBounds && entry.rootBounds.height) || innerHeight;
    return entry.intersectionRatio >= 0.6 || entry.intersectionRect.height >= viewport / 2;
  }

  const seenObserver = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) {
        const card = entry.target;
        const key = card.dataset.key;
        if (!key || counted.has(key)) continue;
        if (onScreen(entry)) {
          if (timers.has(key)) continue;
          timers.set(
            key,
            setTimeout(() => {
              timers.delete(key);
              counted.add(key);
              pending.add(key);
              seenObserver.unobserve(card);
              card.classList.remove("is-new");
              const dot = card.querySelector(".dot");
              if (dot) dot.remove();
              clearTimeout(flushTimer);
              flushTimer = setTimeout(() => flush(false), 1500);
            }, DWELL_MS),
          );
        } else {
          clearTimeout(timers.get(key));
          timers.delete(key);
        }
      }
    },
    { threshold: [0, 0.25, 0.5, 0.6, 0.75, 1] },
  );

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) flush(true);
  });
  addEventListener("pagehide", () => flush(true));

  // --- Infinite scroll ------------------------------------------------------
  const moreObserver = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) if (entry.isIntersecting) loadMore(entry.target);
    },
    { rootMargin: "800px 0px" },
  );

  async function loadMore(link) {
    moreObserver.unobserve(link);
    link.textContent = "Loading…";
    try {
      const url = new URL(link.href);
      url.searchParams.set("partial", "1");
      const res = await fetch(url);
      if (!res.ok && res.status !== 410) throw new Error(String(res.status));
      link.outerHTML = await res.text(); // 410 = expired edition: shows a refresh note
      watch();
    } catch {
      link.textContent = "Load more";
    }
  }

  function watch() {
    document.querySelectorAll("a[data-more]").forEach((a) => moreObserver.observe(a));
    document.querySelectorAll("[data-card][data-key]").forEach((card) => {
      if (!counted.has(card.dataset.key)) seenObserver.observe(card);
    });
  }
  watch();

  // --- Save / hide in place -------------------------------------------------
  document.addEventListener("submit", async (event) => {
    const form = event.target;
    if (!form.matches("form[data-action]")) return;
    event.preventDefault();
    const card = form.closest("[data-card]");
    try {
      const res = await fetch(form.action, {
        method: "POST",
        headers: { "X-Requested-With": "fetch" },
      });
      if (!res.ok) throw new Error(String(res.status));
      card.outerHTML = await res.text();
      watch();
    } catch {
      form.submit(); // actions are idempotent, so a plain post is a safe fallback
    }
  });

  // --- "N new insights" pill --------------------------------------------------
  const fresh = document.getElementById("fresh");
  const opened = feed && feed.dataset.opened;
  if (opened && fresh) {
    const poll = async () => {
      if (document.hidden) return;
      try {
        const res = await fetch("/new-count?since=" + encodeURIComponent(opened));
        const { count } = await res.json();
        if (count > 0) {
          fresh.textContent = `↑ ${count} new insight${count === 1 ? "" : "s"}`;
          fresh.hidden = false;
        }
      } catch {
        /* offline or server restarting; try again next tick */
      }
    };
    setInterval(poll, 60_000);
    document.addEventListener("visibilitychange", poll);
  }
})();
