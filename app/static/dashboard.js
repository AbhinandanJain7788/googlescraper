// Dashboard: list every job, refresh every 5s, allow open/download/delete.
// Pure vanilla — no build step. Keep dependencies at zero so Railway free tier
// doesn't waste minutes on `npm install`.

const tbody = document.getElementById('tbody');
const refreshBtn = document.getElementById('refresh');
const deleteAllBtn = document.getElementById('delete-all');
const refreshInfo = document.getElementById('refresh-info');

const POLL_MS = 5000;
let pollTimer = null;
let inFlight = false;

function fmtTime(t) {
  if (!t) return '';
  const d = new Date(t * 1000);
  // Locale-aware short date+time; reads as "May 17, 7:42 PM"
  return d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
}

function fmtDuration(start, end) {
  if (!start) return '';
  const stop = end || (Date.now() / 1000);
  const sec = Math.max(0, Math.round(stop - start));
  if (sec < 60) return `${sec}s`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ${sec % 60}s`;
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  return `${h}h ${m}m`;
}

function summariseList(arr, limit = 2) {
  if (!arr || arr.length === 0) return '';
  if (arr.length <= limit) return arr.join(', ');
  return `${arr.slice(0, limit).join(', ')} +${arr.length - limit} more`;
}

function escapeHtml(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function renderRow(job) {
  const isLive = job.status === 'running' || job.status === 'pending';
  const pct = job.max_results > 0
    ? Math.min(100, Math.round((job.results_count / job.max_results) * 100))
    : 0;

  const kw = summariseList(job.keywords || [], 2);
  const loc = summariseList(job.locations || [], 1);
  const shortId = (job.id || '').slice(0, 8);
  const err = job.error ? `<div class="small" style="color: var(--red); margin-top: 2px;">${escapeHtml(job.error)}</div>` : '';

  const progress = isLive
    ? `<div class="progress-mini"><div style="width:${pct}%"></div></div>`
    : '';

  // Download buttons only meaningful when the job has results.
  const dlButtons = (job.results_count > 0)
    ? `
      <a href="/api/jobs/${encodeURIComponent(job.id)}/export?fmt=csv">CSV</a>
      <a href="/api/jobs/${encodeURIComponent(job.id)}/export?fmt=json">JSON</a>
    `
    : '';

  return `
    <tr data-id="${escapeHtml(job.id)}">
      <td>
        <code>${escapeHtml(shortId)}</code>
        <div class="small">${escapeHtml(job.id || '')}</div>
      </td>
      <td>
        <div><strong>${escapeHtml(kw)}</strong></div>
        <div class="small">${escapeHtml(loc) || '<em>no location</em>'}</div>
      </td>
      <td>
        <span class="badge ${escapeHtml(job.status || 'pending')}">${escapeHtml(job.status || 'pending')}</span>
        ${err}
      </td>
      <td>
        <strong>${job.results_count || 0}</strong>
        <span class="small">/ ${job.max_results || 0}</span>
        ${progress}
      </td>
      <td>${escapeHtml(fmtTime(job.started_at))}<div class="small">${escapeHtml(fmtDuration(job.started_at, job.finished_at))}</div></td>
      <td>${escapeHtml(fmtTime(job.finished_at))}</td>
      <td class="actions">
        <a href="/?job=${encodeURIComponent(job.id)}">Open</a>
        ${dlButtons}
        <button class="danger" data-action="delete" data-id="${escapeHtml(job.id)}">Delete</button>
      </td>
    </tr>
  `;
}

async function loadJobs() {
  if (inFlight) return;
  inFlight = true;
  try {
    const r = await fetch('/api/jobs', { cache: 'no-store' });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const data = await r.json();
    const jobs = data.jobs || [];
    if (jobs.length === 0) {
      tbody.innerHTML = '<tr><td colspan="7" class="empty">No jobs yet. Start one from the scraper page.</td></tr>';
    } else {
      tbody.innerHTML = jobs.map(renderRow).join('');
    }
    refreshInfo.textContent = `Updated ${new Date().toLocaleTimeString()} • auto-refresh every 5s`;
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="7" class="empty">Failed to load jobs: ${escapeHtml(String(err))}</td></tr>`;
  } finally {
    inFlight = false;
  }
}

async function deleteJob(id) {
  if (!confirm('Delete this job and its CSV/JSON files? This cannot be undone.')) return;
  try {
    const r = await fetch(`/api/jobs/${encodeURIComponent(id)}`, { method: 'DELETE' });
    if (!r.ok) {
      const text = await r.text();
      alert(`Delete failed: ${r.status} ${text}`);
      return;
    }
    await loadJobs();
  } catch (err) {
    alert(`Delete failed: ${err}`);
  }
}

async function deleteAll() {
  if (!confirm('Delete ALL jobs (running ones get cancelled). This cannot be undone.')) return;
  try {
    const r = await fetch('/api/jobs?confirm=true', { method: 'DELETE' });
    if (!r.ok) {
      const text = await r.text();
      alert(`Delete-all failed: ${r.status} ${text}`);
      return;
    }
    await loadJobs();
  } catch (err) {
    alert(`Delete-all failed: ${err}`);
  }
}

tbody.addEventListener('click', (e) => {
  const btn = e.target.closest('button[data-action="delete"]');
  if (btn) deleteJob(btn.dataset.id);
});

refreshBtn.addEventListener('click', loadJobs);
deleteAllBtn.addEventListener('click', deleteAll);

// Pause polling when tab is hidden so we don't burn Railway's free tier on
// background polling no one is looking at.
document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  } else {
    loadJobs();
    if (!pollTimer) pollTimer = setInterval(loadJobs, POLL_MS);
  }
});

loadJobs();
pollTimer = setInterval(loadJobs, POLL_MS);
