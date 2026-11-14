// Progressive enhancement for the insight feed. Without this file everything
// still works: cards link to /i/<key>, forms post and redirect, "Load more" is a
// link. JS adds the reader overlay, in-place actions, sharing, view tracking.
(() => {
  const feed = document.getElementById("feed");

  // A selected chip far along the row (a resume point in the Interview tab)
  // starts out of view; bring it in without moving the page.
  const activeChip = document.querySelector(".chips .chip.on");
  if (activeChip && activeChip !== activeChip.parentElement.firstElementChild) {
    activeChip.scrollIntoView({ block: "nearest", inline: "center" });
  }
  const sessionId = feed && feed.dataset.session;
  const overlay = document.getElementById("reader-overlay");
  const toastEl = document.getElementById("toast");

  // --- Seen = actually on screen ---------------------------------------------
  // A card counts once it is mostly visible (or fills half the screen) for
  // DWELL_MS; opening it in the reader counts immediately. Views are batched to
  // /seen; a beacon flushes the rest when the page is hidden or closed.
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

  function markSeen(key, { now = false } = {}) {
    if (!key || counted.has(key)) return;
    counted.add(key);
    pending.add(key);
    const card = feedCard(key);
    if (card) {
      card.classList.remove("is-new");
      const dot = card.querySelector(".dot");
      if (dot) dot.remove();
    }
    clearTimeout(flushTimer);
    if (now) flush(false);
    else flushTimer = setTimeout(() => flush(false), 1500);
  }

  function onScreen(entry) {
    if (!entry.isIntersecting) return false;
    const viewport = (entry.rootBounds && entry.rootBounds.height) || innerHeight;
    return entry.intersectionRatio >= 0.6 || entry.intersectionRect.height >= viewport / 2;
  }

  const seenObserver = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) {
        const key = entry.target.dataset.key;
        if (!key || counted.has(key)) continue;
        if (onScreen(entry)) {
          if (!timers.has(key)) {
            timers.set(
              key,
              setTimeout(() => {
                timers.delete(key);
                seenObserver.unobserve(entry.target);
                markSeen(key);
              }, DWELL_MS),
            );
          }
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

  function feedCard(key) {
    if (!key || !feed) return null;
    for (const card of feed.querySelectorAll("[data-card][data-key]")) {
      if (card.dataset.key === key) return card;
    }
    return null;
  }

  // --- Infinite scroll ----------------------------------------------------------
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

  // --- Toast, copy, share ------------------------------------------------------
  let toastTimer = null;
  function toast(message) {
    if (!toastEl) return;
    toastEl.textContent = message;
    toastEl.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => (toastEl.hidden = true), 1800);
  }

  function legacyCopy(text) {
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    document.execCommand("copy");
    area.remove();
  }

  async function copy(text) {
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
      } else {
        legacyCopy(text); // the Clipboard API needs HTTPS; plain-http tailnet URL
      }
    } catch {
      legacyCopy(text);
    }
    toast("Copied to clipboard");
  }

  async function share(button) {
    const title = button.dataset.title || "";
    const text = `${title}\n\n${button.dataset.text || ""}`.trim();
    if (navigator.share) {
      try {
        await navigator.share({ title, text });
        return;
      } catch (error) {
        if (error && error.name === "AbortError") return;
      }
    }
    copy(text); // no share sheet (needs HTTPS): copy instead
  }

  // --- Reader text size (the "Aa" button) -------------------------------------------
  // Stored on <html> as data-reader-size. It must not share the button's
  // data-textsize name, or every click would match closest("[data-textsize]").
  const SIZES = [
    ["", "Default"],
    ["lg", "Large"],
    ["xl", "Extra large"],
  ];
  const root = document.documentElement;
  root.dataset.readerSize = localStorage.getItem("readerTextSize") || "";
  function cycleTextSize() {
    const index = SIZES.findIndex(([value]) => value === (root.dataset.readerSize || ""));
    const [value, name] = SIZES[(index + 1) % SIZES.length];
    root.dataset.readerSize = value;
    localStorage.setItem("readerTextSize", value);
    toast(`Text size: ${name}`);
  }

  // --- Reader overlay: tap a card, read, ✕ or Back returns to the same spot ----------
  // Each article opened in the overlay is one history entry with a depth, so
  // Back steps to the previous article (Read next) while ✕ closes to the feed.
  let feedUrl = null;

  function readerDepth() {
    return history.state && history.state.reader ? history.state.depth || 1 : 0;
  }

  async function openReader(href, push = true) {
    const url = new URL(href, location.href);
    if (overlay.hidden) feedUrl = location.pathname + location.search;
    const fragment = new URL(url);
    fragment.searchParams.set("partial", "1");
    fragment.searchParams.set("from", feedUrl || "/");
    try {
      const res = await fetch(fragment);
      if (!res.ok) throw new Error(String(res.status));
      overlay.innerHTML = await res.text();
    } catch {
      location.href = href;
      return;
    }
    overlay.hidden = false;
    overlay.scrollTop = 0;
    root.classList.add("reading");
    if (push) {
      history.pushState({ reader: url.pathname, depth: readerDepth() + 1 }, "", url.pathname);
    }
    const reader = overlay.querySelector("[data-reader]");
    markSeen(reader && reader.dataset.key, { now: true });
    askReady();
  }

  function hideReader() {
    if (!overlay || overlay.hidden) return;
    overlay.hidden = true;
    overlay.innerHTML = "";
    root.classList.remove("reading");
  }

  function closeReader() {
    const depth = readerDepth();
    if (depth) history.go(-depth);
    else hideReader();
  }

  addEventListener("popstate", (event) => {
    if (!overlay) return;
    if (event.state && event.state.reader) openReader(event.state.reader, false);
    else hideReader();
  });
  addEventListener("keydown", (event) => {
    if (event.key === "Escape" && overlay && !overlay.hidden) closeReader();
  });

  // A standalone /i/<key> page: opening it is a view.
  if (!overlay) {
    const standalone = document.querySelector("[data-reader]");
    if (standalone) markSeen(standalone.dataset.key, { now: true });
  }

  // --- Clicks: menus, share, copy, text size, open/close reader ---------------------
  function closeMenus(except) {
    document.querySelectorAll("details.menu[open]").forEach((menu) => {
      if (menu !== except) menu.open = false;
    });
  }

  document.addEventListener("click", (event) => {
    const target = event.target;
    closeMenus(target.closest("details.menu"));
    const shareButton = target.closest("[data-share]");
    if (shareButton) {
      event.preventDefault();
      share(shareButton);
      return;
    }
    const copyButton = target.closest("[data-copy]");
    if (copyButton) {
      event.preventDefault();
      closeMenus();
      copy(copyButton.dataset.copy);
      return;
    }
    if (target.closest("[data-textsize]")) {
      event.preventDefault();
      cycleTextSize();
      return;
    }
    if (target.closest("[data-close]") && overlay && !overlay.hidden) {
      event.preventDefault();
      closeReader();
      return;
    }
    const opener = target.closest("a[data-open]");
    const plainClick =
      event.button === 0 && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey;
    if (opener && overlay && plainClick) {
      event.preventDefault();
      openReader(opener.getAttribute("href"));
    }
  });

  // --- Save / hide in place --------------------------------------------------------
  async function refreshCard(key) {
    const card = feedCard(key);
    if (!card) return;
    const url = new URL(`/card/${encodeURIComponent(key)}`, location.href);
    const view = feed.dataset.view;
    if (view === "for-you" || view === "interview") url.searchParams.set("v", view);
    url.searchParams.set("next", feed.dataset.next || "/");
    try {
      const res = await fetch(url);
      if (res.ok) card.outerHTML = await res.text();
      watch();
    } catch {
      /* the card refreshes on the next load anyway */
    }
  }

  document.addEventListener("submit", async (event) => {
    const form = event.target;
    if (!form.matches("form[data-action]")) return;
    event.preventDefault();
    const owner = form.closest("[data-key]");
    const key = owner && owner.dataset.key;
    const inReader = Boolean(form.closest("[data-reader]"));
    const target = form.hasAttribute("data-swap-card")
      ? feedCard(key)
      : form.closest("[data-swap]");
    try {
      const res = await fetch(form.action, {
        method: "POST",
        headers: { "X-Requested-With": "fetch" },
      });
      if (!res.ok) throw new Error(String(res.status));
      const html = await res.text();
      if (target) target.outerHTML = html;
      closeMenus();
      if (form.hasAttribute("data-close-reader")) {
        if (overlay && !overlay.hidden) closeReader();
        else location.href = "/";
      } else if (inReader) {
        refreshCard(key);
      }
      watch();
    } catch {
      form.submit(); // actions are idempotent, so a plain post is a safe fallback
    }
  });

  // --- Ask about this: post a question, then refresh the thread until answered ------
  // Claude answers on the Mac (maybe much later; Telegram says when). While a
  // question is open, the thread is re-fetched every ASK_POLL_MS. Only the
  // thread is replaced, so a question being typed is never lost.
  const ASK_POLL_MS = 10_000;
  let askTimer = null;

  function swapThread(section, html) {
    const tpl = document.createElement("template");
    tpl.innerHTML = html;
    const fresh = tpl.content.querySelector("[data-ask]");
    if (!fresh || !section.isConnected) return;
    section.querySelector("[data-ask-thread]").replaceWith(fresh.querySelector("[data-ask-thread]"));
    if (fresh.dataset.askPoll) section.dataset.askPoll = fresh.dataset.askPoll;
    else delete section.dataset.askPoll;
  }

  function pollAsk() {
    clearTimeout(askTimer);
    if (!document.querySelector("[data-ask][data-ask-poll]")) return;
    askTimer = setTimeout(async () => {
      const section = document.querySelector("[data-ask][data-ask-poll]");
      if (!section) return;
      if (!document.hidden) {
        try {
          const res = await fetch(section.dataset.askPoll, { headers: { "X-Requested-With": "fetch" } });
          if (res.ok) swapThread(section, await res.text());
        } catch {
          /* offline or server restarting; try again next tick */
        }
      }
      pollAsk();
    }, ASK_POLL_MS);
  }
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) pollAsk();
  });

  // The composer grows with its text; its send button is off while it's empty
  // (without JS the button stays on and `required` guards the post).
  function fitComposer(box) {
    const send = box.form && box.form.querySelector(".send");
    if (send && !box.form.dataset.sending) send.disabled = !box.value.trim();
    box.style.height = "40px";
    box.style.height = Math.min(box.scrollHeight, 160) + "px";
  }
  function askReady() {
    document.querySelectorAll("[data-ask-box]").forEach(fitComposer);
    pollAsk();
  }
  document.addEventListener("input", (event) => {
    if (event.target.matches("[data-ask-box]")) fitComposer(event.target);
  });
  document.addEventListener("keydown", (event) => {
    const box = event.target;
    if (box.matches("[data-ask-box]") && event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
      event.preventDefault();
      box.form.requestSubmit();
    }
  });

  document.addEventListener("submit", async (event) => {
    const form = event.target;
    if (!form.matches("form[data-ask-form]")) return;
    event.preventDefault();
    const section = form.closest("[data-ask]");
    const button = form.querySelector("button");
    if (form.dataset.sending || button.disabled) return;
    form.dataset.sending = "1";
    button.disabled = true;
    try {
      const res = await fetch(form.action, {
        method: "POST",
        body: new URLSearchParams(new FormData(form)),
        headers: { "X-Requested-With": "fetch" },
      });
      if (!res.ok && res.status !== 400 && res.status !== 429) throw new Error(String(res.status));
      section.outerHTML = await res.text(); // the thread with the new question, or the error
      if (res.ok) toast("Asked. Telegram will ping you when it's answered.");
      askReady();
    } catch {
      delete form.dataset.sending;
      fitComposer(form.querySelector("[data-ask-box]"));
      toast("Couldn't send the question. Try again.");
    }
  });
  askReady(); // a standalone reader page (maybe with an open question)

  // --- Rate it: 👍 / 👎, then "why?" (issue #126) -------------------------------------
  // 👎 on a card folds it into an undo note; 👎 at the end of an item in the
  // overlay does the same to its card and closes the reader. Either way a sheet
  // then asks why (optional), which Claude sees when writing new items.
  const REASONS = [
    ["basic", "Too basic"],
    ["irrelevant", "Not relevant to me"],
    ["wrong", "Wrong or outdated"],
    ["repeat", "Repeats another"],
  ];
  let whySheet = null;

  function closeWhy() {
    if (!whySheet) return;
    const { sheet, backdrop } = whySheet;
    whySheet = null;
    sheet.classList.remove("open");
    backdrop.remove();
    setTimeout(() => sheet.remove(), 250);
  }

  function askWhy(key, source, kinds) {
    closeWhy();
    const backdrop = document.createElement("div");
    backdrop.className = "sheet-backdrop";
    const sheet = document.createElement("div");
    sheet.className = "sheet";
    sheet.setAttribute("role", "dialog");
    sheet.setAttribute("aria-label", "Why wasn't it useful?");
    sheet.innerHTML =
      '<div class="sheet-grab"></div><h2>Why wasn’t it useful?</h2>' +
      '<p class="sheet-sub">Optional. Claude uses this to write better items.</p>' +
      '<div class="why-chips"></div><div class="sheet-row">' +
      '<button type="button" class="sheet-skip">Skip</button>' +
      '<button type="button" class="sheet-done">Done</button></div>';
    const chips = sheet.querySelector(".why-chips");
    const add = (code, text, wide) => {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "why-chip" + (wide ? " is-wide" : "");
      chip.dataset.code = code;
      chip.setAttribute("aria-pressed", "false");
      chip.textContent = text;
      chips.append(chip);
    };
    REASONS.forEach(([code, text]) => add(code, text));
    if (source) add("source", `Fewer from ${source}`, true);
    if (kinds) add("type", `Fewer ${kinds}`, true);
    chips.addEventListener("click", (event) => {
      const chip = event.target.closest(".why-chip");
      if (!chip) return;
      chip.setAttribute("aria-pressed", chip.getAttribute("aria-pressed") === "true" ? "false" : "true");
    });
    backdrop.addEventListener("click", closeWhy);
    sheet.querySelector(".sheet-skip").addEventListener("click", closeWhy);
    sheet.querySelector(".sheet-done").addEventListener("click", async () => {
      const codes = [...chips.querySelectorAll('[aria-pressed="true"]')].map((c) => c.dataset.code);
      closeWhy();
      if (!codes.length) return;
      try {
        const res = await fetch(`/items/${encodeURIComponent(key)}/reasons`, {
          method: "POST",
          body: new URLSearchParams({ reasons: codes.join(",") }),
          headers: { "X-Requested-With": "fetch" },
        });
        if (!res.ok) throw new Error(String(res.status));
        toast("Thanks. Claude will write fewer like this.");
      } catch {
        toast("Couldn't save that. Try again.");
      }
    });
    document.body.append(backdrop, sheet);
    whySheet = { sheet, backdrop };
    requestAnimationFrame(() => requestAnimationFrame(() => sheet.classList.add("open")));
  }
  addEventListener("keydown", (event) => {
    if (event.key === "Escape" && whySheet) closeWhy();
  });

  document.addEventListener("submit", async (event) => {
    const form = event.target;
    if (!form.matches("form[data-rate]")) return;
    event.preventDefault();
    if (form.dataset.busy) return;
    form.dataset.busy = "1";
    const kind = form.dataset.rate; // up | down | undo
    const owner = form.closest("[data-key]");
    const key = owner && owner.dataset.key;
    const box = form.closest("[data-rate-box]");
    const fromOverlay = Boolean(box && overlay && !overlay.hidden && overlay.contains(form));
    const removeFromOverlay = kind === "down" && fromOverlay;
    const url = new URL(form.action, location.href);
    if (removeFromOverlay) url.searchParams.set("as", "card");
    try {
      const res = await fetch(url, { method: "POST", headers: { "X-Requested-With": "fetch" } });
      if (!res.ok) throw new Error(String(res.status));
      const html = await res.text();
      if (removeFromOverlay) {
        const card = feedCard(key);
        if (card) card.outerHTML = html;
        closeReader();
      } else if (box) {
        box.outerHTML = html;
      } else {
        const card = form.closest("[data-swap]");
        if (card) card.outerHTML = html;
      }
      if (kind === "down") askWhy(key, form.dataset.source, form.dataset.kinds);
      watch();
    } catch {
      delete form.dataset.busy;
      form.submit(); // rating is idempotent, so a plain post is a safe fallback
    }
  });


  // --- "N new insights" pill ----------------------------------------------------------
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
