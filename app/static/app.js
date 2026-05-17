/* Google Maps Lead Scraper — frontend */
(() => {
  const $ = (sel) => document.querySelector(sel);
  const form = $("#form");
  const statusEl = $("#status");
  const bar = $("#bar");
  const tbody = $("#tbody");
  const countEl = $("#count");
  const resultsWrap = $("#results-wrap");
  const goBtn = $("#go");
  const stopBtn = $("#stop");
  const dlJson = $("#dl-json");
  const dlCsv = $("#dl-csv");

  let currentJob = null;
  let evtSource = null;
  let target = 0;
  let received = 0;

  function setStatus(text, kind = "") {
    statusEl.className = "status " + kind;
    statusEl.textContent = text;
  }
  function setProgress(p) { bar.style.width = Math.min(100, p * 100) + "%"; }

  function esc(s) {
    if (s === null || s === undefined) return "";
    return String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"})[c]);
  }

  function shortUrl(u) {
    if (!u) return "";
    try {
      const url = new URL(u.startsWith("http") ? u : "https://" + u);
      return url.hostname.replace(/^www\./, "");
    } catch { return u; }
  }

  function emailCell(lead) {
    const list = lead.emails || (lead.email ? [lead.email] : []);
    if (!list.length) return "";
    const first = list[0];
    const link = `<a class="email-link" href="mailto:${esc(first)}">${esc(first)}</a>`;
    const extra = list.length > 1 ? `<span class="email-extra" title="${esc(list.slice(1).join(", "))}">+${list.length - 1}</span>` : "";
    return link + extra;
  }

  function websiteCell(lead) {
    if (!lead.website) return "";
    const href = lead.website.startsWith("http") ? lead.website : "https://" + lead.website;
    return `<a class="maps-link" href="${esc(href)}" target="_blank" rel="noopener" title="${esc(href)}">${esc(shortUrl(lead.website))}</a>`;
  }

  function addRow(lead, idx) {
    const tr = document.createElement("tr");
    tr.className = "tag-new";
    const mapsLink = lead.url ? `<a class="maps-link" href="${esc(lead.url)}" target="_blank" rel="noopener">open</a>` : "";
    tr.innerHTML = `
      <td>${idx}</td>
      <td class="title">${esc(lead.title || "")}</td>
      <td class="rating">${lead.totalScore ?? ""}</td>
      <td>${lead.reviewsCount ?? ""}</td>
      <td>${esc(lead.categoryName || "")}</td>
      <td>${esc(lead.phone || "")}</td>
      <td class="email">${emailCell(lead)}</td>
      <td>${websiteCell(lead)}</td>
      <td>${esc(lead.city || "")}</td>
      <td>${esc(lead.state || "")}</td>
      <td>${esc(lead.postalCode || "")}</td>
      <td class="addr">${esc(lead.fullAddress || "")}</td>
      <td>${mapsLink}</td>
    `;
    tbody.appendChild(tr);
  }

  async function startScrape(e) {
    e.preventDefault();
    if (currentJob) return;
    tbody.innerHTML = "";
    received = 0;

    const keyword = $("#keyword").value.trim();
    const location = $("#location").value.trim();
    const max_results = parseInt($("#max_results").value || "1000", 10);
    const fetch_emails = $("#fetch_emails").checked;
    const auto_grid = $("#auto_grid").checked;
    const restrict_to_location = $("#restrict_to_location").checked;
    const gridRaw = $("#grid_size").value.trim();
    const grid_size = gridRaw ? parseInt(gridRaw, 10) : null;
    if (!keyword) return;
    target = max_results;

    goBtn.disabled = true;
    stopBtn.disabled = false;
    setStatus("Starting scraper…", "running");
    setProgress(0);
    resultsWrap.hidden = false;
    countEl.textContent = "0";

    let res;
    try {
      res = await fetch("/api/scrape", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          keyword, location, max_results, fetch_emails,
          auto_grid, restrict_to_location, grid_size,
        }),
      });
    } catch (err) {
      setStatus("Could not reach the API: " + err.message, "error");
      goBtn.disabled = false; stopBtn.disabled = true;
      return;
    }
    if (!res.ok) {
      const t = await res.text();
      setStatus("Server error: " + t, "error");
      goBtn.disabled = false; stopBtn.disabled = true;
      return;
    }
    const info = await res.json();
    const { job_id, keywords, locations, auto_grid: agOn } = info;
    currentJob = job_id;
    const mode = agOn ? `auto-grid scan` : `text search`;
    setStatus(`Starting ${mode} (${keywords.length} keyword${keywords.length>1?"s":""} × ${locations.length} location${locations.length>1?"s":""})…`, "running");
    dlJson.href = `/api/jobs/${job_id}/export?fmt=json`;
    dlCsv.href  = `/api/jobs/${job_id}/export?fmt=csv`;
    let lastStatus = "";

    evtSource = new EventSource(`/api/jobs/${job_id}/stream`);
    evtSource.addEventListener("lead", (ev) => {
      const lead = JSON.parse(ev.data);
      received++;
      addRow(lead, received);
      countEl.textContent = received;
      setProgress(received / target);
      const tail = lastStatus ? ` · ${lastStatus}` : "";
      setStatus(`Scraped ${received} / ${target}${tail}`, "running");
    });
    evtSource.addEventListener("status", (ev) => {
      lastStatus = JSON.parse(ev.data);
      const tail = lastStatus ? ` · ${lastStatus}` : "";
      setStatus(`Scraped ${received} / ${target}${tail}`, "running");
    });
    evtSource.addEventListener("done", (ev) => {
      const di = JSON.parse(ev.data);
      finalize(di.status === "cancelled" ? "Cancelled." : `Done. ${di.count} leads scraped.`,
               di.status === "error" ? "error" : (di.status === "cancelled" ? "" : "done"));
    });
    evtSource.addEventListener("error", () => { /* SSE auto-reconnects */ });
  }

  function finalize(msg, kind) {
    setStatus(msg, kind);
    setProgress(1);
    if (evtSource) { evtSource.close(); evtSource = null; }
    currentJob = null;
    goBtn.disabled = false;
    stopBtn.disabled = true;
  }

  async function cancel() {
    if (!currentJob) return;
    try { await fetch(`/api/jobs/${currentJob}/cancel`, {method: "POST"}); } catch {}
    setStatus("Cancelling…", "running");
  }

  // Resume a previously-submitted job when arriving with ?job=<id>.
  // This is how a user comes back from the dashboard, or shares a job URL
  // with someone else on the same server.
  async function resumeJob(jobId) {
    let state;
    try {
      const r = await fetch(`/api/jobs/${encodeURIComponent(jobId)}`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      state = await r.json();
    } catch (err) {
      setStatus(`Could not load job ${jobId}: ${err.message}`, "error");
      return;
    }

    currentJob = jobId;
    target = state.max_results || 0;
    resultsWrap.hidden = false;
    countEl.textContent = state.results_count || 0;
    received = 0;
    tbody.innerHTML = "";
    for (const lead of (state.results || [])) {
      received++;
      addRow(lead, received);
    }
    countEl.textContent = received;
    setProgress(target ? received / target : 1);
    dlJson.href = `/api/jobs/${jobId}/export?fmt=json`;
    dlCsv.href  = `/api/jobs/${jobId}/export?fmt=csv`;

    if (state.status === "running" || state.status === "pending") {
      goBtn.disabled = true;
      stopBtn.disabled = false;
      setStatus(`Resuming live stream for job ${jobId.slice(0,8)}… (${received} / ${target} so far)`, "running");
      // Stream picks up where we left off; the server replays past leads then
      // streams new ones. To avoid double-rendering replayed leads, only
      // append leads whose URL we haven't already added.
      const seen = new Set((state.results || []).map(l => l.url).filter(Boolean));
      evtSource = new EventSource(`/api/jobs/${jobId}/stream`);
      let lastStatus = "";
      evtSource.addEventListener("lead", (ev) => {
        const lead = JSON.parse(ev.data);
        if (lead.url && seen.has(lead.url)) return;
        if (lead.url) seen.add(lead.url);
        received++;
        addRow(lead, received);
        countEl.textContent = received;
        setProgress(target ? received / target : 1);
        const tail = lastStatus ? ` · ${lastStatus}` : "";
        setStatus(`Scraped ${received} / ${target}${tail}`, "running");
      });
      evtSource.addEventListener("status", (ev) => {
        lastStatus = JSON.parse(ev.data);
        const tail = lastStatus ? ` · ${lastStatus}` : "";
        setStatus(`Scraped ${received} / ${target}${tail}`, "running");
      });
      evtSource.addEventListener("done", (ev) => {
        const di = JSON.parse(ev.data);
        finalize(di.status === "cancelled" ? "Cancelled." : `Done. ${di.count} leads scraped.`,
                 di.status === "error" ? "error" : (di.status === "cancelled" ? "" : "done"));
      });
      evtSource.addEventListener("error", () => { /* SSE auto-reconnects */ });
    } else {
      // Already-finished job. Just show static results.
      goBtn.disabled = false;
      stopBtn.disabled = true;
      const kind = state.status === "error" ? "error" : (state.status === "cancelled" || state.status === "interrupted" ? "" : "done");
      const msg = state.status === "error" ? `Error: ${state.error || "unknown"}`
                : state.status === "cancelled" ? `Cancelled with ${received} leads.`
                : state.status === "interrupted" ? `Interrupted by a server restart with ${received} leads.`
                : `Done. ${received} leads.`;
      setStatus(msg, kind);
      setProgress(1);
    }
  }

  form.addEventListener("submit", startScrape);
  stopBtn.addEventListener("click", cancel);

  // Auto-resume on load if ?job=<id> is in the URL.
  const params = new URLSearchParams(window.location.search);
  const resumeId = params.get("job");
  if (resumeId) {
    resumeJob(resumeId);
  }
})();
