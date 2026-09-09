/* History page — incident timeline + per-incident detail. */
import { escapeHtml } from './ui/format.js';
// apiFetch + isAdminLike live in alarm.js; this import closes a cycle
// (alarm.js imports HistoryPage). Safe: both are only used inside
// methods, never at module-eval time. Moves to net.js / auth.js later.
import { apiFetch, isAdminLike } from './alarm.js';

export class HistoryPage {
  constructor(monitor) {
    this.monitor = monitor;
    this.data = [];
    this.severityFilter = 'all';
    this.statusFilter = 'all';
    this.jobFilter = 'all';
    this.dateRange = 'month';
    this.sortBy = 'last_seen';
    this.searchQ = '';
    this.clearedBefore = parseFloat(localStorage.getItem('historyClearedBefore') || '0');
    this._loaded = false;
    this._loadAbortController = null;
    this._visibleCount = HistoryPage.PAGE_SIZE;
    this._tickInterval = null;
    this._hasFiring = false;

    this.tableEl = document.getElementById('historyFullTable');
    this.badge = document.getElementById('historyBadge');
    this.metaEl = document.getElementById('historyMeta');
    this.searchEl = document.getElementById('historySearch');
    this.jobSelectEl = document.getElementById('historyJobFilter');
    this.rangeSelectEl = document.getElementById('historyRangeSelect');
    this.sortSelectEl = document.getElementById('historySortSelect');
    this.pagerEl = document.getElementById('historyPager');
    this.pagerLabelEl = document.getElementById('historyPagerLabel');

    this.statTotal = document.getElementById('histTotal');
    this.statMonth = document.getElementById('histThisMonth');
    this.statCritical = document.getElementById('histCritical');
    this.statWarning = document.getElementById('histWarning');

    this._bindEvents();
  }

  _bindEvents() {
    document.querySelectorAll('[data-hist-filter]').forEach(btn => {
      btn.addEventListener('click', () => {
        this.severityFilter = btn.dataset.histFilter;
        document.querySelectorAll('[data-hist-filter]').forEach(b =>
          b.classList.toggle('filter-btn-active', b.dataset.histFilter === this.severityFilter)
        );
        this._resetPaging();
        this._render();
      });
    });

    document.querySelectorAll('[data-hist-status]').forEach(btn => {
      btn.addEventListener('click', () => {
        this.statusFilter = btn.dataset.histStatus;
        document.querySelectorAll('[data-hist-status]').forEach(b =>
          b.classList.toggle('filter-btn-active', b.dataset.histStatus === this.statusFilter)
        );
        this._resetPaging();
        this._render();
      });
    });

    this.searchEl.addEventListener('input', () => {
      this.searchQ = this.searchEl.value.toLowerCase();
      this._resetPaging();
      this._render();
    });

    this.jobSelectEl.addEventListener('change', () => {
      this.jobFilter = this.jobSelectEl.value;
      this._resetPaging();
      this._render();
    });

    // Date range scopes the stat cards too (task: "don't leave the summary
    // cards hardcoded to this month") — everything downstream of load()
    // recomputes off it.
    this.rangeSelectEl.addEventListener('change', () => {
      this.dateRange = this.rangeSelectEl.value;
      this._resetPaging();
      this._updateStats();
      this._render();
    });

    this.sortSelectEl.addEventListener('change', () => {
      this.sortBy = this.sortSelectEl.value;
      this._render();
    });

    document.getElementById('exportHistory').addEventListener('click', () => this._exportCSV());

    document.getElementById('clearHistory').addEventListener('click', () => {
      this.clearedBefore = Date.now() / 1000;
      localStorage.setItem('historyClearedBefore', this.clearedBefore);
      this._resetPaging();
      this._updateStats();
      this._render();
    });

    document.getElementById('historyLoadMore').addEventListener('click', () => {
      this._visibleCount += HistoryPage.PAGE_SIZE;
      this._render();
    });

    // Delegated + keyboard-reachable (a plain click-only div is invisible to
    // D-pad/remote nav) — opens the existing target detail drawer (task #5)
    // instead of an inline expand: one detail surface per host, not two.
    this.tableEl.addEventListener('click', e => {
      const resolveBtn = e.target.closest('[data-resolve-key]');
      if (resolveBtn) {
        e.stopPropagation();
        this._resolveIncident(resolveBtn.dataset.resolveKey);
        return;
      }
      const row = e.target.closest('[data-row-key]');
      if (row) this._openRowDrawer(row.dataset.rowKey);
    });
    this.tableEl.addEventListener('keydown', e => {
      if (e.key !== 'Enter' && e.key !== ' ') return;
      const row = e.target.closest('[data-row-key]');
      if (!row) return;
      e.preventDefault();
      this._openRowDrawer(row.dataset.rowKey);
    });
  }

  _resetPaging() {
    this._visibleCount = HistoryPage.PAGE_SIZE;
  }

  onActivate() {
    if (!this._loaded) this._renderLoading();
    this.load();
    this._startTicking();
  }

  onDeactivate() {
    this._stopTicking();
  }

  // Only ticks (re-renders once a second) while a still-open incident is
  // visible, so a live "down Xm Ys" age keeps counting — the same idea as
  // LogsPage._startPolling, reused here because History can legitimately
  // show a not-yet-resolved incident (TargetDown fired, hasn't cleared).
  _startTicking() {
    this._stopTicking();
    this._tickInterval = setInterval(() => {
      if (this._hasFiring) this._render();
    }, 1000);
  }

  _stopTicking() {
    if (this._tickInterval) { clearInterval(this._tickInterval); this._tickInterval = null; }
  }

  async load() {
    if (this._loadAbortController) this._loadAbortController.abort();
    const controller = new AbortController();
    this._loadAbortController = controller;

    try {
      const res = await fetch('/history', { signal: controller.signal });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      this.data = data;
      this._loaded = true;
      this._syncJobOptions();
      this._updateStats();
      this._render();
    } catch (e) {
      if (e.name === 'AbortError') return;
      console.error('[History] load failed:', e);
      if (!this._loaded) this._renderError(e.message);
    } finally {
      if (this._loadAbortController === controller) this._loadAbortController = null;
    }
  }

  // Job dropdown only ever offers jobs that actually appear in the data —
  // same idea as InstancesPage's job select, applied to the history dataset.
  _syncJobOptions() {
    const jobs = [...new Set(this.data.map(r => r.job).filter(Boolean))].sort();
    const existing = new Set(Array.from(this.jobSelectEl.options).map(o => o.value));
    jobs.forEach(j => {
      if (!existing.has(j)) {
        const opt = document.createElement('option');
        opt.value = j;
        opt.textContent = j;
        this.jobSelectEl.appendChild(opt);
      }
    });
  }

  _renderLoading() {
    this.tableEl.innerHTML = `
      <div class="empty-state">
        <div class="es-text">Loading incident history…</div>
      </div>`;
  }

  _renderError(msg) {
    this.tableEl.innerHTML = `
      <div class="empty-state">
        <div class="es-text" style="color:var(--critical, #EF4444);">Failed to load incident history — ${this._esc(msg || 'network error')}</div>
      </div>`;
  }

  _rangeStartEpoch() {
    const now = new Date();
    if (this.dateRange === '7d') return Date.now() / 1000 - 7 * 86400;
    if (this.dateRange === '30d') return Date.now() / 1000 - 30 * 86400;
    if (this.dateRange === 'all') return 0;
    return new Date(now.getFullYear(), now.getMonth(), 1).getTime() / 1000; // 'month' (default)
  }

  // Incidents still in view after "Clear" — a real, undoable cut-off shared
  // by the stat cards, meta caption and table alike.
  _visibleData() {
    return this.clearedBefore ? this.data.filter(r => (r.time || 0) > this.clearedBefore) : this.data;
  }

  // Cleared + date-range only. The stat row summarizes "this time window",
  // independent of the severity/status/job/search filters below — those
  // narrow what the table itself lists (see _render()'s historyMeta caption),
  // they don't change what the summary cards mean.
  //
  // Filters on ACTIVITY END, not start: a still-Ongoing incident's activity
  // "ends" right now, so it always passes any of these range presets (they
  // all run up to the present) regardless of how long ago it first started —
  // an outage that's been down for 2 months must not vanish from the
  // default "This Month" view just because it predates this month. A
  // resolved incident is judged by when it actually resolved.
  _rangedData() {
    const since = this._rangeStartEpoch();
    return this._visibleData().filter(r => {
      const activityEnd = this._isOngoing(r) ? (Date.now() / 1000) : (r.resolved_time || r.time || 0);
      return activityEnd >= since;
    });
  }

  _updateStats() {
    const base = this._rangedData();
    const now = new Date();
    // "This Month" = incidents whose ACTIVITY falls in the current calendar
    // month, judged the same way _rangedData() judges its range (activity end,
    // so a still-ongoing incident counts) and against the same local
    // month-start anchor _rangeStartEpoch() uses — previously this bucketed on
    // the incident's START time via getMonth(), a second, differently-defined
    // filter that disagreed with the range filter near month boundaries and
    // shifted by the viewer's UTC offset (audit m7).
    const monthStart = new Date(now.getFullYear(), now.getMonth(), 1).getTime() / 1000;
    const nowSec = Date.now() / 1000;
    const month = base.filter(i => {
      const activityEnd = this._isOngoing(i) ? nowSec : (i.resolved_time || i.time || 0);
      return activityEnd >= monthStart;
    }).length;
    const crit = base.filter(i => (i.severity || '').toLowerCase() === 'critical').length;
    const warn = base.filter(i => (i.severity || '').toLowerCase() === 'warning').length;

    this.statTotal.textContent = base.length;
    this.statMonth.textContent = month;
    this.statCritical.textContent = crit;
    this.statWarning.textContent = warn;
  }

  _isOngoing(inc) {
    return (inc.status || 'firing').toLowerCase() !== 'resolved';
  }

  // "Total Down" is a cumulative figure across every occurrence of this
  // incident (task #3: "durasi agregat"), not just the current/latest one —
  // total_down_seconds is the sum banked server-side on each past resolve;
  // an Ongoing incident adds its still-running current session on top.
  _downSeconds(inc) {
    const banked = typeof inc.total_down_seconds === 'number' ? inc.total_down_seconds : (inc.duration_seconds || 0);
    if (this._isOngoing(inc)) return banked + Math.max(0, Date.now() / 1000 - (inc.time || 0));
    return banked;
  }

  _filteredSorted() {
    let rows = this._rangedData();
    if (this.severityFilter !== 'all') {
      rows = rows.filter(r => (r.severity || '').toLowerCase() === this.severityFilter);
    }
    if (this.statusFilter !== 'all') {
      const wantOngoing = this.statusFilter === 'ongoing';
      rows = rows.filter(r => this._isOngoing(r) === wantOngoing);
    }
    if (this.jobFilter !== 'all') {
      rows = rows.filter(r => (r.job || '') === this.jobFilter);
    }
    if (this.searchQ) {
      rows = rows.filter(r =>
        (r.name || '').toLowerCase().includes(this.searchQ) ||
        (r.instance || '').toLowerCase().includes(this.searchQ) ||
        (r.summary || '').toLowerCase().includes(this.searchQ)
      );
    }

    const sorted = rows.slice();
    if (this.sortBy === 'total_down') {
      sorted.sort((a, b) => this._downSeconds(b) - this._downSeconds(a));
    } else if (this.sortBy === 'occurrences') {
      sorted.sort((a, b) => (b.occurrences || 1) - (a.occurrences || 1));
    } else {
      sorted.sort((a, b) => (b.resolved_time || b.time || 0) - (a.resolved_time || a.time || 0));
    }
    return sorted;
  }

  _render() {
    const rows = this._filteredSorted();
    // Reacts to every active filter (severity/status/job/search/range) —
    // the fixed grand total lives on the "Total Incidents" stat card instead
    // (task: stop showing the same number twice).
    this.metaEl.textContent = `${rows.length} incident${rows.length !== 1 ? 's' : ''} (filtered)`;
    this.badge.textContent = rows.length;
    this._hasFiring = rows.some(r => this._isOngoing(r));

    if (rows.length === 0) {
      // Distinguish "cleared" (a real, undoable state) from "no match"
      // (adjust your filter/search) — same empty table otherwise reads as broken.
      const clearedAll = this.clearedBefore > 0 && this.data.length > 0 &&
        this.data.every(r => (r.time || 0) <= this.clearedBefore);
      this.tableEl.innerHTML = clearedAll ? `
        <div class="empty-state">
          <div class="es-icon" aria-hidden="true">
            <svg width="28" height="28" viewBox="0 0 28 28" fill="none">
              <path d="M6 22V8a2 2 0 012-2h12a2 2 0 012 2v14" stroke="currentColor" stroke-width="1.5"/>
              <path d="M3 22h22M10 11h8M10 15h5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
            </svg>
          </div>
          <div class="es-text">History cleared — new incidents will appear here</div>
          <button class="btn btn-secondary btn-sm" id="undoHistoryClear" type="button">Show cleared incidents</button>
        </div>` : `
        <div class="empty-state">
          <div class="es-icon" aria-hidden="true">
            <svg width="28" height="28" viewBox="0 0 28 28" fill="none">
              <path d="M6 22V8a2 2 0 012-2h12a2 2 0 012 2v14" stroke="currentColor" stroke-width="1.5"/>
              <path d="M3 22h22M10 11h8M10 15h5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
            </svg>
          </div>
          <div class="es-text">No incidents match your filter</div>
        </div>`;
      if (clearedAll) {
        document.getElementById('undoHistoryClear').addEventListener('click', () => {
          this.clearedBefore = 0;
          localStorage.removeItem('historyClearedBefore');
          this._updateStats();
          this._render();
        });
      }
      this.pagerEl.hidden = true;
      return;
    }

    // Pagination: only `_visibleCount` rows ever hit the DOM (task #11 — a
    // fleet with 100s of incidents shouldn't render them all at once). A
    // "Show more" bump is the lazy version of a virtual-scroll list; add
    // one if this table routinely needs to show thousands at a time.
    const page = rows.slice(0, this._visibleCount);

    const focused = document.activeElement;
    const focusedKey = (focused && this.tableEl.contains(focused)) ? focused.dataset.rowKey : null;

    this.tableEl.innerHTML = page.map(inc => `<div class="history-entry">${this._renderRow(inc)}</div>`).join('');

    if (focusedKey) {
      const el = this.tableEl.querySelector(`[data-row-key="${CSS.escape(focusedKey)}"]`);
      if (el) el.focus();
    }

    if (rows.length > page.length) {
      this.pagerEl.hidden = false;
      this.pagerLabelEl.textContent = `Showing ${page.length} of ${rows.length}`;
    } else {
      this.pagerEl.hidden = true;
    }
  }

  _renderRow(inc) {
    const rawSev = (inc.severity || 'critical').toLowerCase();
    // Clamp to the known set — the value is stored from the Alertmanager
    // webhook and flows straight into a class name and text below.
    const sev = ['critical', 'warning', 'info'].includes(rawSev) ? rawSev : 'critical';
    const ongoing = this._isOngoing(inc);
    const rowKey = inc.key || `${inc.instance}|${inc.name}|${inc.time}`;
    const jobSub = [inc.job, inc.name].filter(Boolean).join(' · ');
    // Root cause sub-text (task #4) — lastError when this incident recorded
    // one (poller-sourced TargetDown outages do), else the summary text
    // already carries a classification like "unreachable (HTTP 503)".
    const errDetail = inc.last_error || inc.summary || '';
    // Cumulative across every occurrence (_downSeconds), not just the
    // current/latest one — see its comment for why duration_seconds alone
    // isn't "Total Down" once an incident has flapped more than once.
    const downtime = ongoing
      ? `${this.monitor.instancesPage._fmtDownAging(this._downSeconds(inc) * 1000)} (open)`
      : this._fmtDur(this._downSeconds(inc));
    // Who (and when) acknowledged this incident — persisted on the row so
    // it still shows after the live ack record is cleared on resolve.
    const ackLine = inc.acknowledged_by
      ? `<div class="history-ack">✓ Acked by ${this._esc(inc.acknowledged_by)} · ${this._fmt(inc.acknowledged_at)}</div>`
      : '';

    // Force-resolve control (audit F1) — only for an admin, only on a still
    // -Ongoing incident. Backstop for a stuck incident that no automatic path
    // can clear (host removed from Prometheus, so the poller never sees it
    // recover). Lives inside the Host cell so it needs no grid-column change.
    const canResolve = ongoing && isAdminLike(window.currentUser) && inc.key;
    const resolveLine = canResolve
      ? `<button type="button" class="history-resolve-btn" data-resolve-key="${this._esc(inc.key)}" title="Force-resolve this stuck incident">Force-resolve</button>`
      : '';

    // Severity color is a property of the alert (critical=red, warning=amber),
    // independent of open/resolved — that distinction lives on the Status
    // column and the row background (.history-row-active) instead.
    const sevClass = `history-sev-${sev}`;
    const statusClass = ongoing ? 'history-status-ongoing' : 'history-status-resolved';
    const statusLabel = ongoing ? 'Ongoing' : 'Resolved';

    return `<div class="history-row-full${ongoing ? ' history-row-active' : ''}" data-row-key="${this._esc(rowKey)}" tabindex="0" role="button" aria-label="Open details for ${this._esc(inc.instance || 'incident')}">
      <div class="history-chip">
        <div>
          <div class="history-host">${this._esc(inc.instance || '—')}</div>
          <div class="history-sub">${this._esc(jobSub || '—')}${errDetail ? ` — ${this._esc(errDetail)}` : ''}</div>
          ${ackLine}
          ${resolveLine}
        </div>
      </div>
      <span class="history-status ${statusClass}"><span class="history-status-dot"></span>${statusLabel}</span>
      <span class="history-sev ${sevClass}">${this._esc(sev.toUpperCase())}</span>
      <span class="history-occurrences">×${inc.occurrences || 1}</span>
      <span class="history-firstseen">${this._fmt(inc.first_seen || inc.time)}</span>
      <span class="history-downtime">${downtime}</span>
      <span class="history-lastseen">${this._fmt(inc.resolved_time || inc.time)}</span>
    </div>`;
  }

  // Row click -> the same target detail drawer InstancesPage already uses
  // (task #5), instead of a second, competing detail UI. Prefers the live
  // target object (has current health/labels/etc.); falls back to a minimal
  // one built from the incident row itself for a host that's since been
  // removed from monitoring.
  _openRowDrawer(rowKey) {
    const inc = this.data.find(r => (r.key || `${r.instance}|${r.name}|${r.time}`) === rowKey);
    if (!inc) return;
    const live = (this.monitor.instancesPage.data || []).find(t => t.instance === inc.instance);
    const target = live || {
      instance: inc.instance,
      job: inc.job || '',
      health: this._isOngoing(inc) ? 'down' : 'up',
      responseTimeMs: 0,
      httpStatusCode: inc.http_status_code,
      lastError: inc.last_error || inc.summary || '',
      labels: {},
      isWeb: false,
      downSince: this._isOngoing(inc) ? inc.time : null,
      active_alerts: []
    };
    this.monitor.instancesPage._openDrawer(target);
  }

  // Force-resolve a stuck incident via POST /api/alerts/resolve (audit F1).
  async _resolveIncident(key) {
    if (!key) return;
    const toast = (m) => this.monitor?.instancesPage?._triggerEventToast?.(m);
    const ok = await window.showConfirmDialog({
      title: 'Force-Resolve Incident',
      message: `Mark "${key}" as resolved? Use this only for a stuck incident that no longer reflects reality — e.g. a host removed from Prometheus, whose recovery the poller can never observe.`,
      confirmText: 'Force-resolve',
      cancelText: 'Cancel',
      isDanger: true
    });
    if (!ok) return;
    try {
      const res = await apiFetch('/api/alerts/resolve', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ key })
      });
      const data = await res.json().catch(() => ({}));
      if (res.ok && data.ok) {
        toast(data.changed ? 'Incident force-resolved.' : 'Incident was not firing — no change.');
        this.load();
        this.monitor?.instancesPage?.load?.();
      } else if (res.status === 403) {
        toast('Permission denied: admin required to resolve incidents.');
      } else {
        toast(data.error || 'Failed to resolve incident.');
      }
    } catch (ex) {
      console.warn('[InfraWatch] resolve incident failed:', ex);
      toast('Failed to resolve incident — see console.');
    }
  }

  _fmt(ts) {
    if (!ts) return '—';
    const d = new Date(ts * 1000);
    const hh = String(d.getHours()).padStart(2, '0');
    const mm = String(d.getMinutes()).padStart(2, '0');
    const dd = String(d.getDate()).padStart(2, '0');
    const mo = String(d.getMonth() + 1).padStart(2, '0');
    return `${hh}:${mm} · ${dd}/${mo}`;
  }

  _fmtDur(s) {
    if (typeof s !== 'number' || isNaN(s)) return '—';
    if (s < 60) return `${Math.round(s)}s`;
    if (s < 3600) return `${Math.round(s / 60)}m`;
    return `${(s / 3600).toFixed(1)}h`;
  }

  _exportCSV() {
    const header = 'FirstSeen,LastSeen,Alert,Instance,Job,Severity,Status,Occurrences,LastOccurrenceDurationSeconds,TotalDownSeconds,LastError\n';
    const rows = this.data.map(r =>
      [
        this._fmt(r.first_seen || r.time), this._fmt(r.resolved_time || r.time), r.name, r.instance, r.job, r.severity,
        r.status || 'firing',
        r.occurrences || 1,
        typeof r.duration_seconds === 'number' ? Math.round(r.duration_seconds) : '',
        Math.round(this._downSeconds(r)),
        r.last_error || r.summary
      ]
        .map(v => `"${String(v || '').replace(/"/g, '""')}"`)
        .join(',')
    ).join('\n');
    const blob = new Blob([header + rows], { type: 'text/csv' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `infrawatch-history-${Date.now()}.csv`;
    a.click();
    URL.revokeObjectURL(a.href);
  }

  _esc(s) { return escapeHtml(s); }
}

HistoryPage.PAGE_SIZE = 40;
