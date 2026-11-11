// Progressive enhancement for the insight feed. Without this file every action
// still works (forms post and redirect; "Load more" is a plain link).
(() => {
  // Save / hide in place: post via fetch, swap the card with the server fragment.
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
    } catch {
      form.submit(); // actions are idempotent, so a plain post is a safe fallback
    }
  });

  // Infinite scroll: fetch the next page as a fragment when "Load more" nears view.
  const observer = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) if (entry.isIntersecting) loadMore(entry.target);
    },
    { rootMargin: "800px 0px" },
  );

  async function loadMore(link) {
    observer.unobserve(link);
    link.textContent = "Loading…";
    try {
      const url = new URL(link.href);
      url.searchParams.set("partial", "1");
      const res = await fetch(url);
      if (!res.ok) throw new Error(String(res.status));
      link.outerHTML = await res.text();
      watch();
    } catch {
      link.textContent = "Load more";
    }
  }

  function watch() {
    document.querySelectorAll("a[data-more]").forEach((a) => observer.observe(a));
  }
  watch();

  // "N new insights" pill: poll while the page is open and visible.
  const feed = document.getElementById("feed");
  const fresh = document.getElementById("fresh");
  const top = feed && feed.dataset.top;
  if (top && fresh) {
    const poll = async () => {
      if (document.hidden) return;
      try {
        const res = await fetch("/new-count?since=" + encodeURIComponent(top));
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
