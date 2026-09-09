/* Availability: polling + cache, the period / custom-range controls,
 * the availability sub-nav, the data-quality audit, and the availability
 * breakdown modal + trend. Installed onto InstancesPage.prototype so the
 * methods keep their original `this` and every existing call site works
 * unchanged. */
import { apiFetch } from './net.js';

class _AvailabilityMethods {
  startAvailabilityPolling() {
    if (this.availabilityPollInterval) clearInterval(this.availabilityPollInterval);
    this.availabilityPollInterval = setInterval(() => this.loadAvailability(false), 15000);
  }

  stopAvailabilityPolling() {
    if (this.availabilityPollInterval) {
      clearInterval(this.availabilityPollInterval);
      this.availabilityPollInterval = null;
    }
    this._clearRetry('availability');
    if (this._availAbortController) {
      this._availAbortController.abort();
      this._availAbortController = null;
    }
  }

  _getAvailCacheKey(minutes = this.periodMinutes, job = this.selectedJob, end = this.periodEnd) {
    const jobKey = (job && job !== 'all') ? job : 'all';
    const minKey = Math.round(minutes || 1440);
    const endKey = end ? String(end) : 'live';
    return `${jobKey}:${minKey}:${endKey}`;
  }

  // Minutes from 00:00 UTC on the 1st of the current month to now. A plain
  // fixed window like every other range — just calendar-aligned so it lines up
  // with how uptime is reported and billed.
  _monthToDateMinutes() {
    const now = new Date();
    const monthStart = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), 1);
    return Math.max(1, Math.round((Date.now() - monthStart) / 60000));
  }

  _updateAvailLoadingUI(isLoading) {
    const updatingBadge = document.getElementById('availUpdatingBadge');
    if (updatingBadge) updatingBadge.classList.toggle('hidden', !isLoading);
  }

  // The headline % is honest math over whatever was observed, but for a young
  // Prometheus/DB a "7d"/"30d" window can be built from a few hours of samples.
  // Same rule the breakdown modal's own warning uses (_renderAvailabilityBreakdown).
  _availabilityCoverageIsLimited(data) {
    if (!data) return false;
    const covPct = typeof data.coverage_percent === 'number' ? data.coverage_percent : null;
    const status = data.data_status;
    return status === 'INSUFFICIENT_DATA' || status === 'PARTIAL' || (covPct !== null && covPct < 50);
  }

  _applyAvailabilityData(data, source = 'loadAvailability') {
    if (!data) return;
    this.availabilityBreakdown = data;
    this.availabilityMap = data.targets || {};

    // Availability card (Card 5): Updated to reflect the requested historical window
    const overall = (typeof data.overall === 'number') ? data.overall : null;
    const limited = overall !== null && this._availabilityCoverageIsLimited(data);
    if (this.statUptime) {
      // Mark the card when the window is mostly unobserved — otherwise the big
      // number implies full-window confidence it doesn't have. The Detail modal
      // carries the full telemetry audit (audit M2).
      this.statUptime.textContent = overall !== null ? `${overall.toFixed(2)}%${limited ? ' *' : ''}` : '—';
      this.statUptime.classList.toggle('is-limited-data', limited);
      if (limited) {
        const covPct = typeof data.coverage_percent === 'number' ? data.coverage_percent : null;
        this.statUptime.title = covPct !== null
          ? `Limited data — only ${covPct.toFixed(1)}% of this window was observed. Open Detail for the telemetry audit.`
          : 'Limited data for this window. Open Detail for the telemetry audit.';
      } else {
        this.statUptime.removeAttribute('title');
      }
    }

    // Ensure range label is synchronized with the response data
    const respMinutes = Math.round(data.period_minutes || this.periodMinutes);
    let label = '24h';
    if (respMinutes === 60) label = '1h';
    else if (respMinutes === 10080) label = '7d';
    else if (respMinutes === 43200) label = '30d';
    if (this.periodLabel === 'custom') label = 'Custom';
    else if (this.periodLabel === 'mtd') label = 'Month to date';

    if (this.availabilityLabel) this.availabilityLabel.textContent = `Availability (${label})${limited ? ' · limited data' : ''}`;

    // Refresh drawer uptime figure if a host is currently open
    if (this.selectedTarget) this._updateDrawerUptime(this.selectedTarget);

    // Refresh breakdown modal content if it's currently open
    if (this.availabilityBreakdownModal && !this.availabilityBreakdownModal.classList.contains('hidden')) {
      this._renderAvailabilityBreakdown(`_applyAvailabilityData:${source}`);
    }
  }

  async loadAvailability(force = false) {
    // Realtime mode is driven entirely by the live /instances poll (see
    // _updateStats()) — no historical Prometheus aggregate to fetch here.
    if (this.isRealtime) return;

    const cacheKey = this._getAvailCacheKey();
    const now = Date.now();
    const cached = this._availCache.get(cacheKey);
    const hasFreshCache = cached && (now - cached.timestamp < this._availCacheTTL);

    // 1. Instant Cache Render: If valid cached data exists, apply immediately with 0ms delay!
    if (cached && cached.data) {
      this._applyAvailabilityData(cached.data, 'cache_hit');
      if (!force && hasFreshCache) {
        this._updateAvailLoadingUI(false);
        return;
      }
    }

    // force=true (manual refresh, custom range submit) supersedes in-flight requests.
    // Switching presets aborts stale in-flight requests to save network/backend bandwidth.
    // Only skip when the in-flight request is for the SAME key (e.g. the 15s
    // poll firing while an identical fetch is still pending) — an in-flight
    // request for a different job/period/end must always be aborted and
    // replaced, otherwise switching e.g. All Jobs -> blackbox-ping-internal
    // while the All Jobs response is still in flight drops the new request
    // and lets the stale All Jobs data land (and overwrite Card 5) once it
    // finally resolves.
    if (this._availAbortController) {
      if (!force && this._availLoading && this._availInFlightKey === cacheKey) {
        return;
      }
      this._availAbortController.abort();
    }
    const controller = new AbortController();
    this._availAbortController = controller;
    this._availInFlightKey = cacheKey;
    const seq = ++this._availRequestSeq;
    const isStale = () => seq !== this._availRequestSeq;

    this._availLoading = true;
    const hasMatchingData = this.availabilityBreakdown && Math.round(this.availabilityBreakdown.period_minutes || 0) === Math.round(this.periodMinutes);
    this._updateAvailLoadingUI(!hasMatchingData);

    const rangeText = this._rangeDisplay();

    if (this.availabilityLabel) this.availabilityLabel.textContent = `Availability (${rangeText})`;
    const drawerUptimeLabel = document.getElementById('drawerUptimeLabel');
    if (drawerUptimeLabel) drawerUptimeLabel.textContent = `Uptime (${rangeText})`;
    const drawerSparklineRangeEl = document.getElementById('drawerSparklineRange');
    if (drawerSparklineRangeEl) drawerSparklineRangeEl.textContent = `(${rangeText})`;

    try {
      let url = `/api/availability?minutes=${Math.round(this.periodMinutes)}`;
      if (this.selectedJob && this.selectedJob !== 'all') {
        url += `&job=${encodeURIComponent(this.selectedJob)}`;
      }
      if (this.periodEnd) url += `&end=${this.periodEnd}`;

      const res = await fetch(url, { signal: controller.signal });
      const data = await res.json();

      if (isStale()) {
        return;
      }

      if (!data.ok) {
        this._availFailCount = Math.min(this._availFailCount + 1, 6);
        this._scheduleRetry('availability');
        return;
      }
      this._availFailCount = 0;
      this._clearRetry('availability');

      // Store response in client-side memory cache
      this._availCache.set(cacheKey, { timestamp: Date.now(), data });
      this._applyAvailabilityData(data, 'network_response');
    } catch (e) {
      if (e.name === 'AbortError' || isStale()) {
        return;
      }
      console.warn('[InfraWatch] Availability fetch failed:', e);
      this._availFailCount = Math.min(this._availFailCount + 1, 6);
      this._scheduleRetry('availability');
    } finally {
      if (this._availAbortController === controller) {
        this._availAbortController = null;
        this._availInFlightKey = null;
        this._availLoading = false;
        this._updateAvailLoadingUI(false);
      }
    }
  }

  _setActiveRangeChip(range) {
    if (!this.rangeChipsGroup) return;
    this.rangeChipsGroup.querySelectorAll('.chip').forEach(b => {
      b.classList.toggle('chip-active', b.dataset.range === range);
    });
  }

  // Human label for the active range, used in every "Availability (…)" caption.
  _rangeDisplay() {
    if (this.periodLabel === 'custom') return 'Custom';
    if (this.periodLabel === 'mtd') return 'Month to date';
    return this.periodLabel || '24h';
  }

  _toggleCustomRangePopover() {
    if (!this.customRangePopover) return;
    const isHidden = this.customRangePopover.classList.contains('hidden');
    if (isHidden) this._openCustomRangePopover();
    else this._closeCustomRangePopover();
  }

  _openCustomRangePopover() {
    if (!this.customRangePopover) return;
    if (this.customRangeErrorEl) this.customRangeErrorEl.classList.add('hidden');

    // Pre-fill with the currently active window (or last 24h by default)
    if (this.customRangeFromEl && !this.customRangeFromEl.value) {
      const end = this.periodEnd ? new Date(this.periodEnd * 1000) : new Date();
      const start = new Date(end.getTime() - this.periodMinutes * 60000);
      this.customRangeFromEl.value = this._toDatetimeLocalValue(start);
      this.customRangeToEl.value = this._toDatetimeLocalValue(end);
    }
    this.customRangePopover.classList.remove('hidden');
  }

  _closeCustomRangePopover() {
    if (!this.customRangePopover) return;
    this.customRangePopover.classList.add('hidden');
    // Revert chip highlight to whatever range is actually active
    if (this.periodLabel !== 'custom') this._setActiveRangeChip(this.periodLabel);
  }

  _toDatetimeLocalValue(date) {
    const pad = n => String(n).padStart(2, '0');
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
  }

  _applyCustomRange() {
    const fromVal = this.customRangeFromEl ? this.customRangeFromEl.value : '';
    const toVal = this.customRangeToEl ? this.customRangeToEl.value : '';
    const showError = msg => {
      if (this.customRangeErrorEl) {
        this.customRangeErrorEl.textContent = msg;
        this.customRangeErrorEl.classList.remove('hidden');
      }
    };

    if (!fromVal || !toVal) {
      showError('Please select start and end date & time');
      return;
    }

    const fromDate = new Date(fromVal);
    const toDate = new Date(toVal);

    if (isNaN(fromDate.getTime()) || isNaN(toDate.getTime())) {
      showError('Please select a valid date & time');
      return;
    }

    const minutes = (toDate.getTime() - fromDate.getTime()) / 60000;

    if (!(minutes > 0)) {
      showError('"To" date must be after "From" date');
      return;
    }
    if (minutes > 90 * 1440) {
      showError('Maximum range is 90 days');
      return;
    }
    if (toDate.getTime() > Date.now() + 60000) {
      showError('Range cannot be in the future');
      return;
    }

    this.isRealtime = false;
    if (this.availabilityDetailBtn) this.availabilityDetailBtn.style.display = '';
    this.periodMinutes = minutes;
    this.periodEnd = Math.floor(toDate.getTime() / 1000);
    this.periodLabel = 'custom';

    this._setActiveRangeChip('custom');
    this.customRangePopover.classList.add('hidden');
    this.loadAvailability();
    if (this.selectedTarget) {
      this.loadTargetHistory(this.selectedTarget.instance);
    }
  }

  /* ── Availability Breakdown sub-nav (Overview & Ranking <-> Data Quality
     & Telemetry Audit) ── */
  _initAvailSubnav() {
    const btnRanking = document.getElementById('btnSubnavRanking');
    const btnAudit = document.getElementById('btnSubnavAudit');
    const paneRanking = document.getElementById('paneAvailRanking');
    const paneAudit = document.getElementById('paneAvailAudit');
    const btnReturn = document.getElementById('btnReturnToRanking');
    const warnBtn = document.getElementById('metricFleetDataWarning');
    if (!btnRanking || !btnAudit || !paneRanking || !paneAudit) return;

    const showAvailTab = (tab) => {
      const isAudit = tab === 'audit';
      paneRanking.classList.toggle('hidden', isAudit);
      paneAudit.classList.toggle('hidden', !isAudit);
      btnRanking.classList.toggle('is-active', !isAudit);
      btnAudit.classList.toggle('is-active', isAudit);
      btnRanking.setAttribute('aria-selected', String(!isAudit));
      btnAudit.setAttribute('aria-selected', String(isAudit));
      // Roving tabindex: only the active tab is a Tab stop (WAI-ARIA APG).
      btnRanking.tabIndex = isAudit ? -1 : 0;
      btnAudit.tabIndex = isAudit ? 0 : -1;
      if (isAudit) this._renderTelemetryAudit();
    };
    this._showAvailTab = showAvailTab;

    btnRanking.addEventListener('click', () => showAvailTab('ranking'));
    btnAudit.addEventListener('click', () => showAvailTab('audit'));
    if (btnReturn) btnReturn.addEventListener('click', () => showAvailTab('ranking'));

    // Arrow / Home / End key navigation between the two tabs (APG tab pattern);
    // moving to a tab also activates it.
    const onTabKey = (e) => {
      if (e.key === 'ArrowRight' || e.key === 'ArrowDown' || e.key === 'End') {
        e.preventDefault();
        showAvailTab('audit');
        btnAudit.focus();
      } else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp' || e.key === 'Home') {
        e.preventDefault();
        showAvailTab('ranking');
        btnRanking.focus();
      }
    };
    btnRanking.addEventListener('keydown', onTabKey);
    btnAudit.addEventListener('keydown', onTabKey);
    // The fleet card's "Limited data" warning links straight to the audit
    // view that explains why — same idea as its "View Telemetry Audit ➔"
    // label already promises.
    if (warnBtn) warnBtn.addEventListener('click', () => showAvailTab('audit'));
  }

  /* ── Availability / SLA settings popover (gear icon) — Node Exporter
     correlation toggle. GET/POST /api/settings/availability. ── */
  _initAvailSettingsPopover() {
    const wrap = document.getElementById('availSettingsDd');
    const btn = document.getElementById('availSettingsBtn');
    const popover = document.getElementById('availSettingsPopover');
    const checkbox = document.getElementById('useNodeExporterCheckbox');
    if (!wrap || !btn || !popover) return;

    const open = () => {
      popover.classList.remove('hidden');
      btn.setAttribute('aria-expanded', 'true');
      this._loadAvailabilitySettings();
    };
    const close = () => {
      if (popover.classList.contains('hidden')) return;
      popover.classList.add('hidden');
      btn.setAttribute('aria-expanded', 'false');
    };
    this._closeAvailSettingsPopover = close;

    btn.addEventListener('click', e => {
      e.stopPropagation();
      if (popover.classList.contains('hidden')) open(); else close();
    });

    if (checkbox) {
      checkbox.addEventListener('change', () => this._saveAvailabilitySettings(checkbox.checked));
    }

    // Same click-outside/Escape convention as the Default Job popover.
    document.addEventListener('click', e => {
      if (!wrap.contains(e.target)) close();
    });
    document.addEventListener('keydown', e => {
      if (e.key === 'Escape' && !popover.classList.contains('hidden')) {
        // Swallow the Escape here so the window-level BACK interceptor doesn't
        // also close the whole Availability Breakdown modal underneath.
        e.stopPropagation();
        close();
        btn.focus();
      }
    });
  }

  async _loadAvailabilitySettings() {
    const checkbox = document.getElementById('useNodeExporterCheckbox');
    if (!checkbox) return;
    try {
      const res = await apiFetch('/api/settings/availability');
      const data = await res.json();
      if (data.ok) checkbox.checked = !!data.use_node_exporter_correlation;
    } catch (e) {
      // leave checkbox showing whatever it last had — a stale read is
      // better than an error toast for a settings popover nobody's saving yet
    }
  }

  async _saveAvailabilitySettings(enabled) {
    const checkbox = document.getElementById('useNodeExporterCheckbox');
    try {
      const res = await apiFetch('/api/settings/availability', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ use_node_exporter_correlation: enabled })
      });
      const data = await res.json();
      if (!data.ok) throw new Error(data.error || 'Failed to save');
      this._triggerEventToast(enabled ? 'Node Exporter correlation enabled.' : 'Node Exporter correlation disabled.');
    } catch (e) {
      if (checkbox) checkbox.checked = !enabled; // revert the optimistic UI toggle
      this._triggerEventToast('Failed to save availability settings.');
    }
  }

  /* ── Data Quality & Telemetry Audit pane ── */
  _initAuditFilters() {
    const chips = Array.from(document.querySelectorAll('.audit-chip[data-filter]'));
    chips.forEach(chip => {
      chip.addEventListener('click', () => {
        this._auditFilter = chip.dataset.filter;
        chips.forEach(c => c.classList.toggle('is-active', c === chip));
        this._renderAuditTable();
      });
    });
    const searchInput = document.getElementById('auditSearchInput');
    if (searchInput) {
      searchInput.addEventListener('input', () => {
        this._auditSearchQ = searchInput.value;
        this._renderAuditTable();
      });
    }
  }

  // Renders the whole audit pane from the SAME /api/availability response
  // already fetched for the Overview & Ranking pane (this.availabilityBreakdown)
  // — telemetry_audit/sla/entries[].sla_budget are all already in it, no
  // second request needed. Runs on every refresh regardless of which
  // sub-tab is showing, same as the rest of _renderAvailabilityBreakdown —
  // the "LIMITED DATA" sub-nav badge needs to stay current even before the
  // operator has clicked into the tab.
  _renderTelemetryAudit() {
    // Always kept in sync with the latest data (not gated on the pane being
    // visible) — the "LIMITED DATA" sub-nav badge needs to reflect current
    // status even before the operator has clicked into the tab.
    const data = this.availabilityBreakdown;
    if (!data) return;

    const audit = data.telemetry_audit || {};
    const sla = data.sla || {};
    const fmtPct = v => typeof v === 'number' ? `${v.toFixed(2)}%` : '—';
    const fmtDur = s => this._formatDowntimeDuration(s || 0).replace(' downtime', '');

    // SLA Availability card — fleet_aggregate is already the
    // maintenance-excluded figure (see fleet_availability.py), matching
    // this card's "excl. planned maintenance" label exactly.
    const fleetAvail = typeof data.fleet_aggregate?.value === 'number' ? data.fleet_aggregate.value : null;
    const slaCard = document.getElementById('auditSlaCard');
    const slaValueEl = document.getElementById('auditSlaValue');
    const slaBadgeEl = document.getElementById('auditSlaBadge');
    const slaTargetEl = document.getElementById('auditSlaTarget');
    const slaMaintEl = document.getElementById('auditSlaMaint');

    if (slaValueEl) slaValueEl.textContent = fmtPct(fleetAvail);
    if (slaTargetEl) slaTargetEl.textContent = `target ${typeof sla.target_pct === 'number' ? sla.target_pct.toFixed(1) : '—'}%`;

    let slaState = 'is-mid', slaLabel = 'INSUFFICIENT DATA';
    if (sla.has_data) {
      slaState = sla.window?.breached ? 'is-bad' : 'is-ok';
      slaLabel = sla.window?.breached ? 'NON-COMPLIANT' : 'COMPLIANT';
    }
    if (slaCard) slaCard.className = `audit-sla-card ${slaState}`;
    if (slaBadgeEl) { slaBadgeEl.textContent = slaLabel; slaBadgeEl.className = `audit-sla-badge ${slaState}`; }

    const maintSec = data.maintenance_excluded_seconds || 0;
    if (slaMaintEl) {
      // #auditSlaMaint uses the bare native `hidden` attribute in markup
      // (no "hidden" class, unlike e.g. #auditSubnavBadge) — toggle the
      // property, not classList, or this would silently never show.
      slaMaintEl.hidden = maintSec <= 0;
      if (maintSec > 0) slaMaintEl.textContent = `${fmtDur(maintSec)} excluded (maintenance)`;
    }

    // Downtime Budget card
    const budgetFillEl = document.getElementById('auditBudgetFill');
    const budgetStateEl = document.getElementById('auditBudgetState');
    const budgetUsedEl = document.getElementById('auditBudgetUsed');
    const budgetProjEl = document.getElementById('auditBudgetProjection');

    if (sla.has_data && sla.window) {
      const usedPct = Math.max(0, sla.window.used_percent || 0);
      const state = sla.window.breached ? 'is-bad' : (usedPct >= 75 ? 'is-mid' : 'is-ok');
      if (budgetFillEl) { budgetFillEl.style.width = `${Math.min(100, usedPct)}%`; budgetFillEl.className = `audit-budget-fill ${state}`; }
      if (budgetStateEl) { budgetStateEl.textContent = sla.window.breached ? 'BREACHED' : `${usedPct.toFixed(0)}% USED`; budgetStateEl.className = `audit-budget-state ${state}`; }
      if (budgetUsedEl) budgetUsedEl.textContent = `${fmtDur(sla.window.observed_downtime_seconds)} used of ${fmtDur(sla.window.allowed_downtime_seconds)} allowed`;
      if (budgetProjEl) {
        budgetProjEl.textContent = sla.projected
          ? (sla.projected.breach
            ? `⚠ Projected to breach in ${sla.projected.days}d at current rate`
            : `On track for ${sla.projected.days}d projection`)
          : '';
      }
    } else {
      if (budgetFillEl) { budgetFillEl.style.width = '0%'; budgetFillEl.className = 'audit-budget-fill'; }
      if (budgetStateEl) { budgetStateEl.textContent = '—'; budgetStateEl.className = 'audit-budget-state'; }
      if (budgetUsedEl) budgetUsedEl.textContent = '— used of —';
      if (budgetProjEl) budgetProjEl.textContent = '';
    }

    // Coverage source one-liner
    const statusEl = document.getElementById('auditStatus');
    const statusTextEl = document.getElementById('auditStatusText');
    const statusSrcEl = document.getElementById('auditStatusSrc');
    const confLevel = audit.confidence_level || 'LOW';
    const statusState = confLevel === 'HIGH' ? 'is-ok' : (confLevel === 'MODERATE' ? 'is-warn' : 'is-bad');
    if (statusEl) statusEl.className = `audit-status ${statusState}`;
    if (statusTextEl) statusTextEl.textContent = audit.root_cause_hint || audit.recommendation || 'No telemetry audit data available for this window.';
    if (statusSrcEl) statusSrcEl.textContent = audit.storage?.source ? `source: ${audit.storage.source}` : '';

    // Coverage split cards (Observed / Unmonitored / Planned maintenance)
    const coverageSec = audit.coverage_seconds ?? data.coverage_seconds ?? 0;
    const missingSec = audit.missing_seconds ?? data.missing_seconds ?? 0;
    const winSec = audit.requested_window_seconds || data.requested_window_seconds || (coverageSec + missingSec) || 1;
    // Compact window label for the sub-line: "24h" reads cleaner than
    // fmtDur's "24h 00m" when the window is a whole number of hours.
    const winLabel = winSec % 3600 === 0 ? `${winSec / 3600}h` : fmtDur(winSec);
    const covPct = typeof audit.coverage_percent === 'number' ? audit.coverage_percent : (data.coverage_percent || 0);
    const missPct = typeof audit.missing_percent === 'number' ? audit.missing_percent : Math.max(0, 100 - covPct);

    const setCoverageCard = (valId, subId, sec, pct, subText) => {
      const valEl = document.getElementById(valId);
      const subEl = document.getElementById(subId);
      if (valEl) valEl.textContent = fmtDur(sec);
      if (subEl) subEl.textContent = subText || `${pct.toFixed(1)}% of ${winLabel}`;
    };
    setCoverageCard('auditMetricObserved', 'auditMetricObservedPct', coverageSec, covPct);
    setCoverageCard('auditMetricMissing', 'auditMetricMissingPct', missingSec, missPct);

    const maintCard = document.getElementById('auditMaintCard');
    const auditGrid = document.getElementById('auditGrid');
    // Headline = wall-clock planned-maintenance time scheduled inside the
    // selected range (what the operator expects to see — "4m", not "4s").
    // Sub-line = how much of that has actually left the SLA denominator so
    // far; it lags the headline while the last minutes of telemetry are
    // still being aggregated, then catches up. Both are fleet totals.
    const maintSchedSec = data.maintenance_scheduled_seconds || 0;
    if (maintSchedSec > 0 || maintSec > 0) {
      if (maintCard) maintCard.hidden = false;
      if (auditGrid) auditGrid.classList.add('has-maint');
      const mVal = document.getElementById('auditMetricMaint');
      const mSub = document.getElementById('auditMetricMaintPct');
      // Always minutes:seconds, even for a value under 60s — fmtDur alone
      // would render the carved figure as a bare "4s" and hide that it is
      // 4s *of 4 minutes scheduled* (the rest is telemetry not yet
      // aggregated). Showing both makes the lag self-evident.
      const fmtMS = s => { s = Math.max(0, Math.round(s)); return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, '0')}s`; };
      if (mVal) mVal.textContent = fmtDur(maintSchedSec || maintSec);
      if (mSub) mSub.textContent = `${fmtMS(maintSec)} of ${fmtMS(maintSchedSec)} excl. from SLA so far`;
    } else {
      if (maintCard) maintCard.hidden = true;
      if (auditGrid) auditGrid.classList.remove('has-maint');
    }

    // Sub-nav "LIMITED DATA" badge
    const subnavBadge = document.getElementById('auditSubnavBadge');
    if (subnavBadge) {
      const isLimited = confLevel === 'LOW' || data.data_status === 'INSUFFICIENT_DATA' || data.data_status === 'PARTIAL';
      subnavBadge.classList.toggle('hidden', !isLimited);
    }

    this._renderAuditTable();
  }

  _renderAuditTable() {
    const tbody = document.getElementById('auditTableBody');
    const countEl = document.getElementById('auditTableHostCount');
    if (!tbody) return;
    const entries = this.availabilityBreakdown?.entries || [];
    const filter = this._auditFilter || 'all';
    const q = (this._auditSearchQ || '').trim().toLowerCase();

    let rows = entries.filter(e => {
      const cov = e.coverage_pct ?? e.coverage_percent ?? 0;
      if (q && !((e.name || e.id || '')).toLowerCase().includes(q)) return false;
      if (filter === 'breach') return e.sla_status === 'NON_COMPLIANT';
      if (filter === 'limited') return cov < 50;
      if (filter === 'optimal') return cov >= 95;
      return true;
    });

    if (countEl) countEl.textContent = `${rows.length} host${rows.length !== 1 ? 's' : ''}`;

    if (rows.length === 0) {
      tbody.innerHTML = `<tr><td colspan="5" class="audit-empty-row">No hosts match this filter</td></tr>`;
      return;
    }

    // Worst coverage first — an audit view exists to surface problems, not
    // to repeat the Overview pane's default ordering.
    rows.sort((a, b) => (a.coverage_pct ?? a.coverage_percent ?? 0) - (b.coverage_pct ?? b.coverage_percent ?? 0));

    tbody.innerHTML = rows.map(e => {
      const cov = e.coverage_pct ?? e.coverage_percent ?? 0;
      const isLim = cov < 50;
      // Per-host "why is coverage low" — hint + recommendation from the backend
      // telemetry audit, so a limited row explains itself on hover.
      const covReason = [e.root_cause_hint, e.recommendation].filter(Boolean).join(' — ')
        || `${cov.toFixed(1)}% of this window was observed for ${e.name || e.id || 'this host'}.`;
      const covTitle = ` title="${this._esc(covReason)}"`;
      const sla = this._slaBadgeInfo(e);
      const slaCls = sla.cls === 'alt-ok' ? 'text-ok' : (sla.cls === 'alt-warning' ? 'text-bad' : 'text-dim');
      const budget = e.sla_budget || {};
      const targetPct = typeof e.sla_target_pct === 'number' ? e.sla_target_pct : budget.target_pct;
      const downtimeSec = e.sla_downtime_seconds ?? e.downtime_seconds ?? 0;
      // Below the SLA coverage gate there isn't enough observed time to judge
      // the error budget — an 86s/24h allowance measured against 20min of
      // data would read "Breached" on almost anything. Show n/a instead.
      // (undefined sla_eligible = legacy entry, keep prior behaviour.)
      const slaEligible = e.sla_eligible !== false;
      const budgetLeftTxt = !slaEligible ? 'n/a'
        : budget.has_data
          ? (budget.window?.breached ? 'Breached' : this._formatDowntimeDuration(budget.window?.remaining_seconds || 0).replace(' downtime', ''))
          : '—';
      const budgetCls = (!slaEligible || !budget.has_data) ? 'text-dim'
        : (budget.window?.breached ? 'text-bad' : ((budget.window?.used_percent || 0) >= 75 ? 'text-mid' : 'text-ok'));
      const budgetTitle = !slaEligible ? ' title="Coverage below the SLA threshold — not enough data to score the error budget"' : '';

      return `<tr>
        <td>
          <span class="audit-host-name">${this._esc(e.name || e.id || '—')}</span>
          <span class="audit-host-job">${this._esc(e.job || '—')}</span>
        </td>
        <td${covTitle}>
          <div class="audit-cov-cell">
            <span class="audit-cov-pct${isLim ? ' is-lim' : ''}">${cov.toFixed(1)}%</span>
            <div class="audit-cov-bar-wrap"><div class="audit-cov-bar-fill${isLim ? ' bar-limited' : ''}" style="width:${Math.max(0, Math.min(100, cov))}%;"></div></div>
          </div>
        </td>
        <td class="ta-r mono">
          <span class="${slaCls}">${typeof e.availability_pct === 'number' ? e.availability_pct.toFixed(2) + '%' : '—'}</span>
          <span class="audit-tgt"> / ${typeof targetPct === 'number' ? targetPct.toFixed(1) : '—'}%</span>
        </td>
        <td class="ta-r mono">${this._formatDowntimeDuration(downtimeSec).replace(' downtime', '')}</td>
        <td class="ta-r mono"${budgetTitle}><span class="${budgetCls}">${budgetLeftTxt}</span></td>
      </tr>`;
    }).join('');
  }

  /* ── Availability breakdown modal (Historical service health) ── */
  _openAvailabilityBreakdown() {
    if (!this.availabilityBreakdownModal) return;
    // Remember what had focus so it can be restored on close (WCAG 2.4.3).
    this._preBreakdownFocusEl = document.activeElement;
    this.availabilityBreakdownModal.classList.remove('hidden');
    document.body.classList.add('modal-open');
    if (this._untrapBreakdown) this._untrapBreakdown();
    this._untrapBreakdown = window.trapModalFocus(this.availabilityBreakdownModal);

    // Move focus into the dialog. The close button is always present and is a
    // safe first stop (Enter on it just re-closes).
    const closeBtn = document.getElementById('closeAvailabilityBreakdown');
    if (closeBtn) setTimeout(() => closeBtn.focus(), 0);

    const modalRangeSelect = document.getElementById('modalRangeSelect');
    if (modalRangeSelect) {
      const curMins = Math.round(this.periodMinutes);
      const matchingOpt = Array.from(modalRangeSelect.options).find(o => Math.round(parseFloat(o.value)) === curMins);
      if (matchingOpt) {
        modalRangeSelect.value = matchingOpt.value;
      }
    }
    
    const currentMins = Math.round(this.periodMinutes);
    const cacheKey = this._getAvailCacheKey();
    const cached = this._availCache.get(cacheKey);

    if (cached && cached.data && Math.round(cached.data.period_minutes || 0) === currentMins) {
      // Modal is already unhidden above, so _applyAvailabilityData's own
      // modal-open check already triggers a render — a second explicit call
      // here just re-renders the same data.
      this._applyAvailabilityData(cached.data, '_openAvailabilityBreakdown');
    } else {
      this.loadAvailability(false);
    }
  }

  _closeAvailabilityBreakdown() {
    if (this._untrapBreakdown) { this._untrapBreakdown(); this._untrapBreakdown = null; }
    if (this.availabilityBreakdownModal) this.availabilityBreakdownModal.classList.add('hidden');

    const availSortMenu = document.getElementById('availSortMenu');
    const availSortTrigger = document.getElementById('availSortTrigger');
    availSortMenu?.classList.add('hidden');
    availSortTrigger?.setAttribute('aria-expanded', 'false');

    if (!document.querySelector('.modal-backdrop:not(.hidden):not(#availabilityBreakdownModal)')) {
      document.body.classList.remove('modal-open');
    }

    // Return focus to whatever opened the modal (usually the Detail button).
    if (this._preBreakdownFocusEl && document.contains(this._preBreakdownFocusEl)) {
      this._preBreakdownFocusEl.focus();
    }
    this._preBreakdownFocusEl = null;
  }

  // Format downtime in seconds to human-readable format: "12h 18m downtime", "4m 20s downtime", "0s downtime"
  _formatDowntimeDuration(seconds) {
    if (!seconds || seconds <= 0) return '0s downtime';
    if (seconds < 60) return `${Math.round(seconds)}s downtime`;
    if (seconds < 3600) {
      const m = Math.floor(seconds / 60);
      const s = Math.round(seconds % 60);
      return s > 0 ? `${m}m ${s}s downtime` : `${m}m downtime`;
    }
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    return `${h}h ${m.toString().padStart(2, '0')}m downtime`;
  }

  // Get clean friendly role/job badge for a host
  _getHostRoleLabel(target, entry) {
    if (target && target.labels && target.labels.role) return target.labels.role;
    if (target && target.labels && target.labels.job && target.labels.job !== 'blackbox') return target.labels.job;
    const job = (target && target.job) || (entry && entry.job) || '';
    if (job === 'blackbox-ping-internal' || job === 'ping') return 'ICMP Ping';
    if (job === 'blackbox_http' || job === 'http' || (target && target.isWeb)) return 'Web Server';
    if (job === 'custom') return 'Custom Target';
    if (job) return job;
    return 'Server';
  }

  // Single source of truth for SLA status (used by detail drawer)
  _slaBadgeInfo(entry) {
    const status = entry && entry.sla_status;
    if (status === 'COMPLIANT') return { cls: 'alt-ok', label: 'COMPLIANT' };
    if (status === 'NON_COMPLIANT') return { cls: 'alt-warning', label: 'NON-COMPLIANT' };
    if (status === 'INSUFFICIENT_DATA') return { cls: 'alt-insufficient', label: 'INSUFFICIENT DATA' };
    const cov = typeof entry?.coverage_percent === 'number' ? entry.coverage_percent
      : (typeof entry?.coverage_pct === 'number' ? entry.coverage_pct : null);
    const avail = typeof entry?.availability_pct === 'number' ? entry.availability_pct : null;
    if (avail === null || cov === null || cov < 50) return { cls: 'alt-insufficient', label: 'INSUFFICIENT DATA' };
    return avail >= 99.9 ? { cls: 'alt-ok', label: 'COMPLIANT' } : { cls: 'alt-warning', label: 'NON-COMPLIANT' };
  }

  // Human duration label for an offset of `sec` seconds before "now".
  _trendOffsetLabel(sec) {
    if (sec <= 60) return 'now';
    if (sec < 3600) return `-${Math.round(sec / 60)}m`;
    if (sec < 86400 * 2) return `-${Math.round(sec / 3600)}h`;
    return `-${Math.round(sec / 86400)}d`;
  }

  // Absolute timestamp label for the hover tooltip — time-of-day for short
  // windows, calendar date for multi-day ones.
  _trendTsLabel(ts, windowSec) {
    const d = new Date(ts * 1000);
    const pad = n => String(n).padStart(2, '0');
    if (windowSec <= 86400 * 2) {
      return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
    }
    const mon = d.toLocaleString(undefined, { month: 'short' });
    return `${mon} ${d.getDate()}, ${pad(d.getHours())}:00`;
  }

  /* ── Availability Trend chart (hourly fleet availability) ──
     Draws data.trend ([{ts, availability_pct}], newest last) as an SVG line +
     area into #avbTrendPlot, over the SAME window the breakdown is scored for
     (data.trend_start_ts .. data.trend_end_ts) — 24h/7d/30d/MTD/custom, not a
     frozen 24h. Fewer than 2 points -> keep the placeholder. The y-axis
     auto-zooms to the data (100% pinned at top). A pointer overlay adds a
     crosshair + tooltip on hover. ── */
  _renderAvailabilityTrend(data) {
    const plot = document.getElementById('avbTrendPlot');
    if (!plot) return;
    const section = plot.closest('.avb-trend-section');
    const emptyEl = plot.querySelector('.avb-trend-empty');
    const gridLabels = plot.querySelectorAll('.avb-trend-grid span i');
    const subEl = section && section.querySelector('.modal-title-sub');
    const xAxisSpans = section ? section.querySelectorAll('.avb-trend-xaxis span') : [];
    let wrap = document.getElementById('avbTrendSvgWrap');
    let hover = document.getElementById('avbTrendHover');

    const pts = Array.isArray(data && data.trend)
      ? data.trend.filter(p => p && typeof p.ts === 'number' && typeof p.availability_pct === 'number')
      : [];

    const endTs = typeof data.trend_end_ts === 'number' ? data.trend_end_ts : (Date.now() / 1000);
    const startTs = typeof data.trend_start_ts === 'number' && data.trend_start_ts < endTs
      ? data.trend_start_ts
      : endTs - 86400;
    const windowSec = Math.max(1, endTs - startTs);

    // Caption reflects the active range (drives from periodLabel, same source
    // as every other "Availability (…)" label in the modal).
    if (subEl) {
      const rangeTxt = this.periodLabel === 'mtd' ? 'month to date'
        : this.periodLabel === 'custom' ? 'selected range'
          : `last ${this.periodLabel || '24h'}`;
      subEl.textContent = `· ${rangeTxt}`;
    }
    // 5 evenly spaced x-axis ticks across the real window.
    if (xAxisSpans.length === 5) {
      for (let i = 0; i < 5; i++) {
        xAxisSpans[i].textContent = this._trendOffsetLabel(windowSec * (1 - i / 4));
      }
    }

    const resetGrid = () => {
      if (gridLabels.length === 3) {
        gridLabels[0].textContent = '100%';
        gridLabels[1].textContent = '50%';
        gridLabels[2].textContent = '0%';
      }
    };

    if (pts.length < 2) {
      if (wrap) wrap.remove();
      if (hover) hover.remove();
      if (emptyEl) emptyEl.classList.remove('hidden');
      resetGrid();
      return;
    }
    pts.sort((a, b) => a.ts - b.ts);

    if (emptyEl) emptyEl.classList.add('hidden');

    // y-domain: 100% pinned at the top, lower bound snapped below the worst
    // slot (never above 95, never below 0) so a near-flat healthy line still
    // shows shape without lying about the scale.
    const worst = Math.min(...pts.map(p => p.availability_pct));
    const yMax = 100;
    let yMin = Math.max(0, Math.floor((worst - 2) / 5) * 5);
    if (yMin >= yMax) yMin = yMax - 5;
    const yMid = Math.round((yMin + yMax) / 2);
    if (gridLabels.length === 3) {
      gridLabels[0].textContent = `${yMax}%`;
      gridLabels[1].textContent = `${yMid}%`;
      gridLabels[2].textContent = `${yMin}%`;
    }

    const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
    const xOf = ts => clamp(((ts - startTs) / windowSec) * 100, 0, 100);
    const yOf = v => clamp(((yMax - v) / (yMax - yMin)) * 100, 0, 100);
    const coords = pts.map(p => [xOf(p.ts), yOf(p.availability_pct)]);

    const lineD = coords.map(([x, y], i) => `${i === 0 ? 'M' : 'L'}${x.toFixed(2)} ${y.toFixed(2)}`).join(' ');
    const areaD = `${lineD} L${coords[coords.length - 1][0].toFixed(2)} 100 L${coords[0][0].toFixed(2)} 100 Z`;

    if (!wrap) {
      // Build the <svg> as a string inside an HTML <div> — same pattern as
      // _renderSparkline; avoids relying on SVGElement.innerHTML. Appended
      // after .avb-trend-grid so the line paints over the grid lines.
      wrap = document.createElement('div');
      wrap.id = 'avbTrendSvgWrap';
      wrap.className = 'avb-trend-svg-wrap';
      wrap.setAttribute('aria-hidden', 'true');
      plot.appendChild(wrap);
    }
    wrap.innerHTML =
      `<svg class="avb-trend-svg" viewBox="0 0 100 100" preserveAspectRatio="none">` +
      `<path class="avb-trend-area" d="${areaD}"></path>` +
      `<path class="avb-trend-line" d="${lineD}" vector-effect="non-scaling-stroke"></path>` +
      `</svg>`;

    // ── Interactive hover overlay: crosshair + dot + tooltip on the nearest
    // slot. Overlay + listeners are created once and reused; the point data
    // lives on the instance so the persistent handler always sees fresh data.
    this._trendHoverState = { coords, pts, windowSec };
    if (!hover) {
      hover = document.createElement('div');
      hover.id = 'avbTrendHover';
      hover.className = 'avb-trend-hover';
      hover.innerHTML =
        '<div class="avb-trend-cross"></div>' +
        '<div class="avb-trend-dot"></div>' +
        '<div class="avb-trend-tip"></div>';
      plot.appendChild(hover);

      const move = e => {
        const st = this._trendHoverState;
        if (!st || st.coords.length < 2) return;
        const rect = hover.getBoundingClientRect();
        if (!rect.width) return;
        const xp = clamp(((e.clientX - rect.left) / rect.width) * 100, 0, 100);
        let best = 0, bd = Infinity;
        for (let i = 0; i < st.coords.length; i++) {
          const d = Math.abs(st.coords[i][0] - xp);
          if (d < bd) { bd = d; best = i; }
        }
        const [cx, cy] = st.coords[best];
        const p = st.pts[best];
        const cross = hover.querySelector('.avb-trend-cross');
        const dot = hover.querySelector('.avb-trend-dot');
        const tip = hover.querySelector('.avb-trend-tip');
        cross.style.left = `${cx}%`;
        dot.style.left = `${cx}%`;
        dot.style.top = `${cy}%`;
        tip.textContent = `${this._trendTsLabel(p.ts, st.windowSec)} · ${p.availability_pct.toFixed(2)}%`;
        tip.style.left = `${cx}%`;
        tip.style.top = `${cy}%`;
        tip.classList.toggle('flip-x', cx > 62);
        tip.classList.toggle('flip-y', cy < 22);
        hover.classList.add('active');
      };
      hover.addEventListener('pointermove', move);
      hover.addEventListener('pointerleave', () => hover.classList.remove('active'));
    }
  }

  _renderAvailabilityBreakdown(source = 'direct') {
    const data = this.availabilityBreakdown;
    const expectedMins = Math.round(this.periodMinutes);
    const isMatchingData = data && Math.round(data.period_minutes || 0) === expectedMins;

    this._updateAvailLoadingUI(this._availLoading && !isMatchingData);

    // Render the audit pane + trend chart FIRST — the ranking code below has
    // several early returns (no entries, healthy fleet -> empty attention list)
    // that would otherwise skip these and leave stale content after a poll.
    this._renderTelemetryAudit();
    this._renderAvailabilityTrend(data);

    // 1. Fleet Availability (Card 1)
    const fleetAvail = (data?.fleet_aggregate && typeof data.fleet_aggregate.value === 'number')
      ? data.fleet_aggregate.value
      : (typeof data?.overall === 'number' ? data.overall : null);

    const aggEl = document.getElementById('metricFleetAggregate');
    const legendUpEl = document.getElementById('legendUptimePct');
    const legendDownEl = document.getElementById('legendDowntimePct');
    // Revamp visuals (presentation only, driven by the same fleet_aggregate value)
    const ringEl = document.getElementById('avbFleetRing');       // donut ring on the Fleet card
    const splitUpEl = document.getElementById('avbHealthyUpBar');  // uptime/downtime split bar on the Healthy Hosts card
    const splitDownEl = document.getElementById('avbHealthyDownBar');
    const RING_CIRCUMFERENCE = 2 * Math.PI * 26; // r = 26 (see .avb-ring markup)

    // Drive the ring via inline style props (strokeDasharray + strokeDashoffset),
    // not setAttribute: an inline style reliably wins the cascade, and the
    // dasharray must be re-asserted here so it exactly matches the computed
    // circumference (the markup's rounded "163.36" left a hairline gap at 100%).
    const setRing = pct => {
      if (!ringEl) return;
      ringEl.style.strokeDasharray = `${RING_CIRCUMFERENCE}`;
      ringEl.style.strokeDashoffset = `${RING_CIRCUMFERENCE * (1 - Math.max(0, Math.min(100, pct)) / 100)}`;
    };

    if (fleetAvail !== null) {
      const clamped = Math.max(0, Math.min(100, fleetAvail));
      const upPctStr = `${fleetAvail.toFixed(2)}%`;
      const downPct = Math.max(0, 100 - fleetAvail);
      const downPctStr = `${downPct.toFixed(2)}%`;

      if (aggEl) aggEl.textContent = upPctStr;
      if (legendUpEl) legendUpEl.textContent = upPctStr;
      if (legendDownEl) legendDownEl.textContent = downPctStr;
      setRing(clamped);
      if (splitUpEl) splitUpEl.style.width = `${clamped.toFixed(2)}%`;
      if (splitDownEl) splitDownEl.style.width = `${(100 - clamped).toFixed(2)}%`;
    } else {
      if (aggEl) aggEl.textContent = '—';
      if (legendUpEl) legendUpEl.textContent = '—';
      if (legendDownEl) legendDownEl.textContent = '—';
      setRing(0);
      if (splitUpEl) splitUpEl.style.width = '0%';
      if (splitDownEl) splitDownEl.style.width = '0%';
    }

    // Fleet-level data confidence warning — the headline % is real math over
    // whatever got observed, but for a young Prometheus/DB (retention just
    // started, SQLite not backfilled yet) a "7d"/"30d" window can be built
    // from a few hours of actual samples. Surface that instead of letting
    // the big number imply full-window confidence it doesn't have.
    const warnEl = document.getElementById('metricFleetDataWarning');
    const warnTextEl = document.getElementById('metricFleetDataWarningText');
    if (warnEl) {
      const covPct = typeof data?.coverage_percent === 'number' ? data.coverage_percent : null;
      const status = data?.data_status;
      const isLimited = status === 'INSUFFICIENT_DATA' || status === 'PARTIAL' || (covPct !== null && covPct < 50);
      if (fleetAvail !== null && isLimited && covPct !== null) {
        // Set only the text span's content — warnEl is a <button> with its
        // own icon span (ahw-icon, rendered separately) and a "View
        // telemetry audit" action span alongside this one; overwriting the
        // whole button's textContent used to wipe both of those out every
        // refresh, and prefixing this string with its own "⚠" used to draw
        // the warning glyph twice.
        if (warnTextEl) warnTextEl.textContent = `Limited data — only ${covPct.toFixed(1)}% of this window observed`;
        warnEl.hidden = false;
      } else {
        warnEl.hidden = true;
      }
    }

    // 2. Healthy Hosts (Card 2)
    // Denominator = every monitored host (server_count), NOT just the ones with
    // telemetry: a host we have no data for is not demonstrably "healthy", and
    // "41 / 50" next to "View all (51)" / "51 hosts" elsewhere reads as a bug.
    // The % is recomputed from the same two numbers so the headline and the
    // sub-label can never disagree.
    const healthyCount = data?.health_ratio?.healthy_count ?? data?.healthy_hosts_count ?? null;
    const totalCount = data?.health_ratio?.server_count
      ?? data?.health_ratio?.total_count
      ?? data?.counts?.total
      ?? (data?.entries ? data.entries.length : null);

    const healthEl = document.getElementById('metricHealthRatio');
    const healthyCountEl = document.getElementById('metricHealthyHostsCount');

    if (healthyCount !== null && typeof totalCount === 'number' && totalCount > 0) {
      const healthyPct = (healthyCount / totalCount) * 100;
      if (healthEl) healthEl.textContent = `${healthyCount} / ${totalCount}`;
      if (healthyCountEl) healthyCountEl.textContent = `${healthyPct.toFixed(2)}% healthy`;
    } else {
      if (healthEl) healthEl.textContent = '—';
      if (healthyCountEl) healthyCountEl.textContent = '— healthy';
    }

    // Keep hidden secondary metrics updated for test/DOM compatibility
    const avgEl = document.getElementById('metricFleetAverage');
    if (avgEl) avgEl.textContent = (data?.fleet_average?.value !== null && typeof data?.fleet_average?.value === 'number') ? `${data.fleet_average.value.toFixed(2)}%` : '—';
    const slaEl = document.getElementById('metricSlaCompliance');
    if (slaEl) slaEl.textContent = (data?.sla_compliance_ratio?.value !== null && typeof data?.sla_compliance_ratio?.value === 'number') ? `${data.sla_compliance_ratio.value.toFixed(2)}%` : '—';
    const covRatioEl = document.getElementById('metricCoverageRatio');
    if (covRatioEl) covRatioEl.textContent = (data?.coverage_ratio?.value !== null && typeof data?.coverage_ratio?.value === 'number') ? `${data.coverage_ratio.value.toFixed(2)}%` : '—';

    // 3. Hosts Requiring Attention
    const listEl = document.getElementById('hostsRequiringAttentionList');
    if (!listEl) return;

    if (!data && this._availLoading) {
      listEl.innerHTML = '<div class="de-empty" style="padding: 24px; text-align: center; color: var(--text-secondary);"><span class="avail-updating-spinner" style="margin-right:8px;"></span> Loading host availability data...</div>';
      return;
    }

    const rawEntries = (data && Array.isArray(data.entries)) ? data.entries
      : (data && data.per_server && Array.isArray(data.per_server.values) ? data.per_server.values : []);

    if (rawEntries.length === 0) {
      listEl.innerHTML = '<div class="de-empty" style="padding: 24px; text-align: center; color: var(--text-secondary);">No monitored hosts found.</div>';
      return;
    }

    // Normalizing and preparing entries
    const processedHosts = rawEntries.map(e => {
      const avail = typeof e.availability_pct === 'number' ? e.availability_pct : null;
      const downMin = typeof e.downtime_minutes === 'number' ? e.downtime_minutes : 0;
      const downSec = typeof e.downtime_seconds === 'number' ? e.downtime_seconds : (downMin * 60);
      const inc = parseInt(e.incidents || e.incident_count || 0, 10) || 0;
      const covPct = typeof e.coverage_percent === 'number' ? e.coverage_percent : (typeof e.coverage_pct === 'number' ? e.coverage_pct : 0);
      const obsSec = typeof e.observed_seconds === 'number' ? e.observed_seconds : ((e.observed_minutes || e.coverage_minutes || 0) * 60);

      const isNoData = obsSec <= 0 || avail === null;
      const isLimitedData = !isNoData && (covPct < 50);

      return {
        id: e.id || e.name,
        name: e.name || e.id || '—',
        job: e.job || '',
        availability: avail,
        downtimeDurationSeconds: downSec,
        incidentCount: inc,
        coveragePercent: covPct,
        observedDurationSeconds: obsSec,
        isNoData: isNoData,
        isLimitedData: isLimitedData,
        rawEntry: e
      };
    });

    const sortMode = this._breakdownSort || 'avail_asc';

    // Synchronize custom dropdown trigger label & active items
    const sortLabels = {
      avail_asc: 'Lowest Availability',
      incidents_desc: 'Most Incidents',
      downtime_desc: 'Longest Downtime',
      avail_desc: 'Highest Availability',
      name_asc: 'Host Name (A-Z)',
      name_desc: 'Host Name (Z-A)'
    };

    const triggerTextEl = document.getElementById('availSortTriggerText');
    if (triggerTextEl) {
      triggerTextEl.textContent = sortLabels[sortMode] || 'Lowest Availability';
    }

    const sortMenuEl = document.getElementById('availSortMenu');
    if (sortMenuEl) {
      sortMenuEl.querySelectorAll('.avail-sort-item').forEach(item => {
        const isSelected = item.dataset.value === sortMode;
        item.classList.toggle('is-active', isSelected);
        item.setAttribute('aria-selected', String(isSelected));
      });
    }

    const subtitleEl = document.getElementById('availAttentionSubtitle');
    if (subtitleEl) {
      const subtitleMap = {
        avail_asc: 'Sorted by lowest availability (ascending)',
        avail_desc: 'Sorted by highest availability (descending)',
        incidents_desc: 'Sorted by most incidents (descending)',
        downtime_desc: 'Sorted by longest downtime (descending)',
        name_asc: 'Sorted by host name (A to Z)',
        name_desc: 'Sorted by host name (Z to A)'
      };
      subtitleEl.textContent = subtitleMap[sortMode] || 'Sorted by availability (ascending)';
    }

    const arrowName = document.getElementById('sortArrowName');
    const arrowAvail = document.getElementById('sortArrowAvail');
    const arrowImpact = document.getElementById('sortArrowImpact');
    const btnName = document.querySelector('.ath-sort-btn[data-sort-key="name"]');
    const btnAvail = document.querySelector('.ath-sort-btn[data-sort-key="avail"]');
    const btnImpact = document.querySelector('.ath-sort-btn[data-sort-key="impact"]');

    if (arrowName) arrowName.textContent = '';
    if (arrowAvail) arrowAvail.textContent = '';
    if (arrowImpact) arrowImpact.textContent = '';
    btnName?.classList.remove('ath-sorted');
    btnAvail?.classList.remove('ath-sorted');
    btnImpact?.classList.remove('ath-sorted');

    if (sortMode === 'name_asc' || sortMode === 'name_desc') {
      btnName?.classList.add('ath-sorted');
      if (arrowName) arrowName.textContent = sortMode === 'name_asc' ? '▲' : '▼';
    } else if (sortMode === 'avail_asc' || sortMode === 'avail_desc') {
      btnAvail?.classList.add('ath-sorted');
      if (arrowAvail) arrowAvail.textContent = sortMode === 'avail_asc' ? '▲' : '▼';
    } else if (sortMode === 'incidents_desc' || sortMode === 'downtime_desc') {
      btnImpact?.classList.add('ath-sorted');
      if (arrowImpact) arrowImpact.textContent = '▼';
    }

    // Dynamic Multi-criteria Sorting
    processedHosts.sort((a, b) => {
      if (sortMode === 'incidents_desc') {
        if (b.incidentCount !== a.incidentCount) return b.incidentCount - a.incidentCount;
        if (b.downtimeDurationSeconds !== a.downtimeDurationSeconds) return b.downtimeDurationSeconds - a.downtimeDurationSeconds;
        const availA = a.availability !== null ? a.availability : 999;
        const availB = b.availability !== null ? b.availability : 999;
        return availA - availB;
      }
      if (sortMode === 'downtime_desc') {
        if (b.downtimeDurationSeconds !== a.downtimeDurationSeconds) return b.downtimeDurationSeconds - a.downtimeDurationSeconds;
        if (b.incidentCount !== a.incidentCount) return b.incidentCount - a.incidentCount;
        const availA = a.availability !== null ? a.availability : 999;
        const availB = b.availability !== null ? b.availability : 999;
        return availA - availB;
      }
      if (sortMode === 'avail_desc') {
        const availA = a.availability !== null ? a.availability : -1;
        const availB = b.availability !== null ? b.availability : -1;
        if (availA !== availB) return availB - availA;
        if (a.downtimeDurationSeconds !== b.downtimeDurationSeconds) return a.downtimeDurationSeconds - b.downtimeDurationSeconds;
        return a.incidentCount - b.incidentCount;
      }
      if (sortMode === 'name_asc') {
        return (a.name || '').localeCompare(b.name || '', undefined, { numeric: true, sensitivity: 'base' });
      }
      if (sortMode === 'name_desc') {
        return (b.name || '').localeCompare(a.name || '', undefined, { numeric: true, sensitivity: 'base' });
      }
      // Default: 'avail_asc' (Lowest availability first)
      const availA = a.availability !== null ? a.availability : 999;
      const availB = b.availability !== null ? b.availability : 999;
      if (availA !== availB) return availA - availB;
      if (b.downtimeDurationSeconds !== a.downtimeDurationSeconds) return b.downtimeDurationSeconds - a.downtimeDurationSeconds;
      return b.incidentCount - a.incidentCount;
    });

    // Update toggle button text with host counts
    const lbl = document.getElementById('viewAllHostsLabel');
    if (lbl) {
      lbl.textContent = this._showAllHostsInBreakdown ? 'Show top hosts' : `View all (${processedHosts.length})`;
    }

    // Filter to the hosts that actually match the current "attention" lens.
    // If none match, priorityHosts is left empty on purpose so the empty
    // state below renders instead of falling back to showing healthy hosts
    // under a "Hosts Requiring Attention" heading. The "avail_desc" /
    // "name_asc" / "name_desc" modes are plain rankings and keep every host.
    let priorityHosts = processedHosts;
    if (sortMode === 'incidents_desc') {
      priorityHosts = processedHosts.filter(h => h.incidentCount > 0);
    } else if (sortMode === 'downtime_desc') {
      priorityHosts = processedHosts.filter(h => h.downtimeDurationSeconds > 0);
    } else if (sortMode === 'avail_asc') {
      priorityHosts = processedHosts.filter(h => (h.availability !== null && h.availability < 100) || h.downtimeDurationSeconds > 0 || h.incidentCount > 0 || h.isNoData);
    }

    const displayList = this._showAllHostsInBreakdown
      ? processedHosts
      : priorityHosts.slice(0, 5);

    if (displayList.length === 0) {
      const allNoData = processedHosts.length > 0 && processedHosts.every(h => h.isNoData);
      let emptyMsg;
      if (allNoData) {
        emptyMsg = 'No telemetry data recorded for monitored hosts in this time range.';
      } else if (sortMode === 'incidents_desc') {
        emptyMsg = 'No incidents recorded for any monitored host in this time range.';
      } else if (sortMode === 'downtime_desc') {
        emptyMsg = 'No downtime recorded for any monitored host in this time range.';
      } else {
        emptyMsg = 'All monitored hosts currently have 100% availability with zero recorded downtime.';
      }
      listEl.innerHTML = `<div class="de-empty" style="padding: 24px; text-align: center; color: var(--text-secondary);">${emptyMsg}</div>`;
      return;
    }

    // O(1) lookup instead of an O(N) find() per host
    const dataByInstance = new Map(this.data.map(t => [t.instance, t]));
    const rowsHtml = displayList.map(h => {
      const target = dataByInstance.get(h.id) || dataByInstance.get(h.name);
      const roleLabel = this._getHostRoleLabel(target, h);

      // Severity styling — a plain color dot carries the status; the
      // availability % text/color and badges already say what it means, so
      // the dot doesn't need its own glyph on top of that.
      let sevClass = 'ara-sev-good';
      let pctClass = 'pct-good';
      let barClass = 'bar-fill-good';

      if (h.isNoData) {
        sevClass = 'ara-sev-warning';
        pctClass = 'pct-muted';
        barClass = 'bar-fill-warning';
      } else if (h.availability < 60) {
        sevClass = 'ara-sev-critical';
        pctClass = 'pct-critical';
        barClass = 'bar-fill-critical';
      } else if (h.availability < 80) {
        sevClass = 'ara-sev-orange';
        pctClass = 'pct-orange';
        barClass = 'bar-fill-orange';
      } else if (h.availability < 95) {
        sevClass = 'ara-sev-warning';
        pctClass = 'pct-warning';
        barClass = 'bar-fill-warning';
      }

      const availText = h.isNoData ? '—' : `${h.availability.toFixed(2)}%`;
      const barWidth = h.isNoData ? 0 : Math.max(0, Math.min(100, h.availability));
      const downtimeText = this._formatDowntimeDuration(h.downtimeDurationSeconds);
      const incidentsText = `${h.incidentCount} incident${h.incidentCount === 1 ? '' : 's'}`;

      let badgeHtml = '';
      if (h.isNoData) {
        badgeHtml = '<span class="ara-nodata-badge">NO DATA</span>';
      } else if (h.isLimitedData) {
        badgeHtml = '<span class="ara-limited-badge" title="Observed duration is < 50% of the selected window">LIMITED DATA</span>';
      }

      return `
        <div class="ara-row" data-instance="${this._esc(h.id)}" role="button" tabindex="0" title="Click to view host details">
          <!-- Col 1: Host / IP -->
          <div class="ara-host-col">
            <span class="ara-severity-dot ${sevClass}" aria-hidden="true"></span>
            <div class="ara-host-meta">
              <span class="ara-hostname">${this._esc(h.name)}</span>
              <div class="ara-badges-row">
                <span class="ara-job-badge">${this._esc(roleLabel)}</span>
                ${badgeHtml}
              </div>
            </div>
          </div>

          <!-- Col 2: Availability & Bar -->
          <div class="ara-avail-col">
            <span class="ara-avail-pct ${pctClass}">${availText}</span>
            <div class="ara-bar-container">
              <div class="ara-bar-track">
                <div class="ara-bar-fill ${barClass}" style="width: ${barWidth}%;"></div>
              </div>
            </div>
          </div>

          <!-- Col 3: Impact -->
          <div class="ara-impact-col">
            <div class="ara-impact-info">
              <div class="ara-impact-texts">
                <span class="ara-incidents-text">${incidentsText}</span>
                <span class="ara-downtime-text">${downtimeText}</span>
              </div>
            </div>
            <svg class="ara-chevron" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
              <polyline points="6 3 11 8 6 13"></polyline>
            </svg>
          </div>
        </div>`;
    }).join('');

    listEl.innerHTML = rowsHtml;
  }
}

export function installAvailability(Cls) {
  for (const k of Object.getOwnPropertyNames(_AvailabilityMethods.prototype)) {
    if (k !== 'constructor') Cls.prototype[k] = _AvailabilityMethods.prototype[k];
  }
}
