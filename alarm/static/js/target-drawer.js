/* Target-detail side drawer: its tabs, the per-target availability bars,
 * recent events, probe summary, maintenance + dependency panels, and the
 * response-time sparkline + history chart. Installed onto
 * InstancesPage.prototype (see availability.js for the why). */
import { calculateNiceScale, buildMSGradientDefs } from './ui/charts.js';
import { escapeHtml, slowThresholdMs, DATE_LOCALE } from './ui/format.js';
import { apiFetch } from './net.js';

class _DrawerMethods {
  _updateDrawerUptime(target) {
    const sinceEl = document.getElementById('drawerUptimeSince');
    if (sinceEl) {
      if (this.isRealtime) {
        sinceEl.textContent = 'Live snapshot';
      } else {
        const totalMin = Math.max(0, Math.round(this.periodMinutes));
        const d = Math.floor(totalMin / 1440);
        const h = Math.floor((totalMin % 1440) / 60);
        const m = totalMin % 60;
        sinceEl.textContent = `Since ${d}d ${h}h ${m}m`;
      }
    }

    const uptimeEl = document.getElementById('drawerUptimeVal');
    if (!uptimeEl) return;
    if (this.isRealtime) {
      uptimeEl.textContent = target.health === 'up' ? '100.00%' : '0.00%';
      return;
    }
    const pct = this.availabilityMap[target.instance];
    uptimeEl.textContent = (typeof pct === 'number') ? `${pct.toFixed(2)}%` : 'No data';
  }

  _switchModalTab(tabName) {
    const nav = document.getElementById('modalTabsNav');
    if (!nav) return;
    const tabs = nav.querySelectorAll('[data-tab]');
    tabs.forEach(t => {
      const active = t.dataset.tab === tabName;
      t.classList.toggle('active', active);
      t.setAttribute('aria-selected', active);
    });

    const panes = document.querySelectorAll('.modal-tab-pane');
    panes.forEach(p => {
      const isTarget = p.id.toLowerCase() === `tabpane${tabName.toLowerCase()}`;
      p.classList.toggle('hidden', !isTarget);
      p.style.display = isTarget ? 'flex' : 'none';
    });

    if (tabName === 'overview' && Array.isArray(this._rawSparklinePoints)) {
      requestAnimationFrame(() => this._renderSparkline(this._rawSparklinePoints));
    } else if (tabName === 'history' && Array.isArray(this._rawSparklinePoints)) {
      requestAnimationFrame(() => this._renderHistoryChart(this._rawSparklinePoints));
    }
  }

  _renderDrawerAvailabilityBars(target, points = [], events = [], fetchedRangeStart = null) {
    const container = document.getElementById('drawerAvailabilityBars');
    const timeLabelsEl = document.getElementById('drawerAvailabilityTimeLabels');
    if (!container) return;

    const now_ts = Math.floor(Date.now() / 1000);
    const isUp = target?.health === 'up';
    // Bootstrap-only estimate (drawer opened, history fetch not back yet). Once
    // real per-slot events for the selected range are in (fetchedRangeStart is
    // set), each hour is judged on its OWN data — never smeared with whatever
    // aggregate % happens to belong to the currently selected range, which is
    // what made switching 1h/24h/7d/30d repaint the same 24 real hours with a
    // different, unrelated number.
    const targetAvail = (fetchedRangeStart === null && target && target.instance && typeof this.availabilityMap[target.instance] === 'number')
      ? this.availabilityMap[target.instance]
      : null;

    let barsHtml = '';
    const slotsCount = 24;

    for (let i = 0; i < slotsCount; i++) {
      const slotStart = now_ts - (24 - i) * 3600;
      const slotEnd = now_ts - (23 - i) * 3600;

      let slotDownSec = 0;
      let hasEventCoverage = false;

      if (Array.isArray(events) && events.length > 0) {
        events.forEach(ev => {
          if (ev.status === 'OFFLINE') {
            const evStart = ev.start_ts;
            const evEnd = ev.ongoing ? now_ts : (ev.end_ts || now_ts);
            const oStart = Math.max(slotStart, evStart);
            const oEnd = Math.min(slotEnd, evEnd);
            if (oEnd > oStart) {
              slotDownSec += (oEnd - oStart);
              hasEventCoverage = true;
            }
          }
        });
      }

      // A slot the selected range never actually queried (e.g. a 1h range
      // leaves the other 23 of these 24 real hours unfetched) has no evidence
      // either way — mark it unknown instead of guessing from an aggregate
      // that belongs to a different window.
      const isOutsideFetchedRange = fetchedRangeStart !== null && slotEnd <= fetchedRangeStart;
      const haveRealData = fetchedRangeStart !== null;

      if (!hasEventCoverage && !haveRealData) {
        // Bootstrap-only guess (drawer just opened, real events not back yet).
        if (targetAvail === 0.0 || (!isUp && (!target?.downSince || target.downSince <= slotStart))) {
          slotDownSec = 3600;
        } else if (!isUp && target?.downSince && target.downSince < slotEnd) {
          slotDownSec = Math.max(0, slotEnd - Math.max(slotStart, target.downSince));
        } else if (targetAvail !== null && targetAvail < 100.0) {
          slotDownSec = Math.round(3600 * (1.0 - targetAvail / 100.0));
        }
      }
      // else if haveRealData: the query already covered this slot and found
      // no OFFLINE event in it — that silence is itself proof of uptime, so
      // it stays at the default 100%, never repainted by a different range's
      // aggregate percentage.

      let uptimePct = isOutsideFetchedRange ? null : 100;
      if (slotDownSec > 0) {
        uptimePct = Math.max(0, Math.min(100, Math.round(((3600 - slotDownSec) / 3600) * 100)));
      }

      let slotPts = Array.isArray(points) ? points.filter(p => p[0] >= slotStart && p[0] < slotEnd) : [];
      let avgLat = slotPts.length > 0 ? (slotPts.reduce((a, b) => a + b[1], 0) / slotPts.length) : (isUp ? target?.responseTimeMs || 0 : 0);

      let barColor = '#22C55E';
      let barHeight = '100%';
      let statusText = `${uptimePct}% Up`;

      if (uptimePct === null) {
        barColor = 'var(--border)';
        barHeight = '15%';
        statusText = 'No data (outside selected range)';
      } else if (uptimePct < 10) {
        barColor = '#EF4444';
        barHeight = '25%';
        statusText = 'Down (0% Up)';
      } else if (uptimePct < 95) {
        barColor = '#F59E0B';
        barHeight = `${Math.max(30, uptimePct)}%`;
        statusText = `${uptimePct}% Up (Partial Outage)`;
      } else if (avgLat > 500) {
        barColor = '#F59E0B';
        barHeight = '80%';
        statusText = `Slow (${avgLat.toFixed(1)}ms avg)`;
      }

      const slotDate = new Date(slotStart * 1000);
      const timeStr = slotDate.toLocaleTimeString(DATE_LOCALE, { hour: '2-digit', minute: '2-digit' });

      barsHtml += `<div title="${timeStr} • ${statusText}" style="flex:1; height:${barHeight}; background:${barColor}; border-radius:var(--r-xs, 4px); transition:all 0.2s ease;"></div>`;
    }

    container.innerHTML = barsHtml;

    if (timeLabelsEl) {
      const markers = [0, 6, 12, 18, 24];
      const labels = markers.map(hAgo => {
        const dObj = new Date((now_ts - (24 - hAgo) * 3600) * 1000);
        return dObj.toLocaleTimeString(DATE_LOCALE, { hour: '2-digit', minute: '2-digit' });
      });
      timeLabelsEl.innerHTML = labels.map(l => `<span>${l}</span>`).join('');
    }
  }

  _renderDrawerRecentEvents(events) {
    const container = document.getElementById('drawerRecentEventsList');
    if (!container) return;

    if (!Array.isArray(events) || events.length === 0) {
      container.innerHTML = '<div class="de-empty" style="font-size:12px; color:var(--text-secondary);">No incident event logs</div>';
      return;
    }

    const recent = events.slice(0, 3);
    container.innerHTML = recent.map(ev => {
      const isOnline = ev.status === 'ONLINE';
      const iconSvg = isOnline
        ? '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#22C55E" stroke-width="2"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>'
        : '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#EF4444" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>';
      
      const title = isOnline ? 'Up' : 'Down';
      const desc = isOnline ? 'Probe successful' : 'Timeout / No response';
      const dObj = new Date(ev.start_ts * 1000);
      const timeStr = dObj.toLocaleTimeString(DATE_LOCALE, { hour: '2-digit', minute: '2-digit', second: '2-digit' });
      const agoStr = this._relTime(ev.start_ts * 1000);

      return `
        <div style="display:flex; align-items:flex-start; justify-content:space-between; padding:6px 8px; background:var(--surface-hover); border:1px solid var(--border); border-radius:var(--r-sm); font-size:var(--fs-xs, 11px);">
          <div style="display:flex; align-items:center; gap:8px;">
            ${iconSvg}
            <div>
              <div style="font-weight:700; color:var(--text-primary);">${title}</div>
              <div style="color:var(--text-secondary); font-size:var(--fs-2xs, 10px);">${desc}</div>
            </div>
          </div>
          <div style="text-align:right;">
            <div style="color:var(--text-primary); font-family:var(--font-mono);">${timeStr}</div>
            <div style="color:var(--text-muted); font-size:var(--fs-2xs, 10px);">${agoStr}</div>
          </div>
        </div>`;
    }).join('');
  }

  _renderDrawerProbeSummary(target, events) {
    const elTotal = document.getElementById('spSummaryTotalProbes');
    const elSuccess = document.getElementById('spSummarySuccess');
    const elFailed = document.getElementById('spSummaryFailed');
    const elMttr = document.getElementById('spSummaryMttr');
    const elLongest = document.getElementById('spSummaryLongestOutage');
    const elLastOutage = document.getElementById('spSummaryLastOutage');

    if (!target) return;

    const rangeText = this._rangeDisplay();
    const rangeLabelEl = document.getElementById('spSummaryRangeLabel');
    if (rangeLabelEl) rangeLabelEl.textContent = `(${rangeText})`;

    // No entry (target filtered out, or this /api/availability response
    // hasn't landed yet) means "we don't know" — never fabricate 100%
    // coverage/availability to fill the gap.
    const entry = this.availabilityBreakdown?.entries?.find(e => e.id === target.instance || e.name === target.instance);
    // Use the SLA (maintenance-excluded) trio so Total == Success + Failed and
    // Success% == the availability figure even when planned maintenance is
    // carved out — the raw uptime/downtime minutes are on a different
    // denominator than availability_pct and made the three lines contradict
    // each other (audit M4). == raw when there are no maintenance windows.
    const _n = (v, fb = null) => (typeof v === 'number' ? v : fb);
    const obsMin = entry
      ? _n(entry.sla_observed_minutes, _n(entry.observed_minutes, _n(entry.coverage_minutes)))
      : null;
    const downMin = entry
      ? _n(entry.sla_downtime_minutes, _n(entry.downtime_minutes, 0))
      : null;
    const upMin = (obsMin !== null && downMin !== null) ? Math.max(0, obsMin - downMin) : null;
    const covMin = obsMin;
    const winMin = Math.max(1, Math.round(this.periodMinutes || 1440));
    const covPct = obsMin !== null ? Math.max(0, Math.min(100, (obsMin / winMin) * 100)) : null;
    const availPct = entry && typeof entry.availability_pct === 'number' ? entry.availability_pct : null;
    const maintExclMin = entry ? _n(entry.maintenance_excluded_minutes, 0) : 0;

    const slaBadgeEl = document.getElementById('spSummarySlaBadge');
    if (slaBadgeEl) {
      const sla = this._slaBadgeInfo(entry || {});
      slaBadgeEl.textContent = sla.label;
      slaBadgeEl.className = `sla-badge ${sla.cls}`;
    }

    let downEvents = Array.isArray(events) ? events.filter(e => e.status === 'OFFLINE') : [];
    let failedCount = entry?.incidents || downEvents.length || (target.health !== 'up' ? 1 : 0);

    const fmtDur = m => (typeof m !== 'number') ? '—' : (m < 60 ? `${m.toFixed(1)}m` : `${(m / 60).toFixed(1)}h`);
    const plannedNote = maintExclMin > 0 ? `; ${fmtDur(maintExclMin)} planned excl.` : '';

    if (elTotal) elTotal.textContent = covMin !== null ? `${fmtDur(covMin)} (${covPct !== null ? covPct.toFixed(1) : '—'}% of range${plannedNote})` : '—';
    if (elSuccess) elSuccess.textContent = upMin !== null ? `${fmtDur(upMin)} (${availPct !== null ? availPct.toFixed(2) + '%' : '—'})` : '—';
    if (elFailed) elFailed.textContent = downMin !== null ? `${fmtDur(downMin)} (${failedCount} incident${failedCount === 1 ? '' : 's'})` : '—';

    const haveDowntimeData = downMin !== null || downEvents.length > 0;
    let totalDownSec = downEvents.reduce((acc, e) => acc + (e.duration_seconds || 0), 0);
    let mttrSec = downEvents.length > 0 ? Math.round(totalDownSec / downEvents.length) : ((downMin || 0) > 0 && failedCount > 0 ? Math.round((downMin * 60) / failedCount) : 0);
    let maxDownSec = downEvents.length > 0 ? Math.max(...downEvents.map(e => e.duration_seconds || 0)) : Math.round((downMin || 0) * 60);

    const fmtSec = s => s > 0 ? (s < 60 ? `${s}s` : (s < 3600 ? `${(s / 60).toFixed(1)}m` : `${(s / 3600).toFixed(1)}h`)) : '0s';

    if (elMttr) elMttr.textContent = haveDowntimeData ? fmtSec(mttrSec) : '—';
    if (elLongest) elLongest.textContent = !haveDowntimeData ? '—' : (maxDownSec > 0 ? fmtSec(maxDownSec) : 'None');

    if (elLastOutage) {
      if (downEvents.length > 0) {
        const lastEv = downEvents[0];
        const dObj = new Date(lastEv.start_ts * 1000);
        elLastOutage.textContent = dObj.toLocaleTimeString(DATE_LOCALE, { hour: '2-digit', minute: '2-digit', second: '2-digit' });
      } else if (!haveDowntimeData) {
        elLastOutage.textContent = '—';
      } else if (downMin > 0) {
        elLastOutage.textContent = 'Past Outage';
      } else {
        elLastOutage.textContent = 'None';
      }
    }
  }

  /* ── Target Side Drawer ────────────────────────── */
  _openDrawer(target) {
    // Remember the host card that had focus so it can be restored on close.
    this._preDrawerFocusEl = document.activeElement?.closest('.host-card') || null;

    this.selectedTarget = target;
    const isUp = target.health === 'up';
    const isSlow = isUp && target.responseTimeMs > slowThresholdMs(target);
    const isDown = !isUp;
    const now = Date.now();

    this._switchModalTab('overview');

    // Abort any in-flight history request from previously selected target
    if (this._historyAbortController) {
      this._historyAbortController.abort();
      this._historyAbortController = null;
    }

    // Reset drawer state & zoom range to prevent data bleed across targets
    this._sparklineZoomRange = null;
    this._rawSparklinePoints = [];

    // IP + job
    const titleEl = document.getElementById('drawerTargetTitle');
    if (titleEl) titleEl.textContent = target.instance;
    const infoIpEl = document.getElementById('drawerInfoIp');
    if (infoIpEl) infoIpEl.textContent = target.instance;
    const jobEl = document.getElementById('drawerJobBadge');
    if (jobEl) jobEl.textContent = target.job || '—';
    const infoJobEl = document.getElementById('drawerInfoJob');
    if (infoJobEl) infoJobEl.textContent = target.job || '—';

    // Protocol / Module — prefer the real blackbox module label if Prometheus
    // reports one, else derive dynamically from target labels or job name
    const protocolEl = document.getElementById('drawerInfoProtocol');
    const moduleEl = document.getElementById('drawerInfoModule');
    const realModule = target.labels?.module;
    const jobLower = (target.job || '').toLowerCase();
    let protocolLabel = '—';
    let moduleLabel = realModule || '—';

    if (realModule) {
      if (realModule.includes('icmp') || realModule.includes('ping')) {
        protocolLabel = 'ICMP';
      } else if (realModule.includes('http') || realModule.includes('https')) {
        protocolLabel = 'HTTP';
      } else if (realModule.includes('tcp')) {
        protocolLabel = 'TCP';
      } else if (realModule.includes('dns')) {
        protocolLabel = 'DNS';
      } else {
        protocolLabel = realModule.toUpperCase();
      }
    } else if (jobLower.includes('ping') || jobLower === 'icmp') {
      protocolLabel = 'ICMP';
      moduleLabel = 'icmp';
    } else if (jobLower.includes('http') || jobLower === 'custom' || target.isWeb) {
      protocolLabel = 'HTTP';
      moduleLabel = 'http_2xx';
    } else if (jobLower.includes('node') || jobLower.includes('exporter')) {
      protocolLabel = 'HTTP (Metrics)';
      moduleLabel = 'node_exporter';
    } else if (target.job) {
      protocolLabel = target.job;
    }
    if (protocolEl) protocolEl.textContent = protocolLabel;
    if (moduleEl) moduleEl.textContent = moduleLabel;

    // Status dot
    const dot = document.getElementById('drawerStatusDot');
    if (dot) {
      dot.className = 'drawer-dot ' + (isDown ? 'dot-down' : (isSlow ? 'dot-slow' : 'dot-up'));
    }

    // Status pill & status text
    const pill = document.getElementById('drawerStatusPill');
    const statusTextEl = document.getElementById('drawerStatusText');
    const label = isDown ? 'Offline' : (isSlow ? 'Slow' : 'Online');
    const cls = isDown ? 'dsp-down' : (isSlow ? 'dsp-slow' : 'dsp-up');
    if (pill) {
      pill.textContent = label;
      pill.className = `drawer-status-pill ${cls}`;
    }
    if (statusTextEl) {
      statusTextEl.textContent = isDown ? 'OFFLINE' : (isSlow ? 'SLOW' : 'ONLINE');
      statusTextEl.style.color = isDown ? '#EF4444' : (isSlow ? '#F59E0B' : '#22C55E');
    }

    // Last check / Aging
    const lastCheckEl = document.getElementById('drawerLastCheck');
    if (lastCheckEl) {
      if (isDown) {
        let downMs = 0;
        if (target.downSince && target.downSince > 0) {
          downMs = Math.max(0, now - (target.downSince * 1000));
        } else if (this.downStartTimes[target.instance]) {
          downMs = Math.max(0, now - this.downStartTimes[target.instance]);
        }
        lastCheckEl.textContent = `Down for ${this._fmtDownAging(downMs)}`;
      } else {
        lastCheckEl.textContent = target.lastScrape ? this._relTime(target.lastScrape) : 'Just now';
      }
    }

    // Metrics
    const latEl = document.getElementById('drawerLatency');
    if (latEl) {
      if (isDown) {
        let downMs = 0;
        if (target.downSince && target.downSince > 0) {
          downMs = Math.max(0, now - (target.downSince * 1000));
        } else if (this.downStartTimes[target.instance]) {
          downMs = Math.max(0, now - this.downStartTimes[target.instance]);
        }
        latEl.textContent = `Down ${this._fmtDownAging(downMs)}`;
      } else {
        latEl.textContent = (target.responseTimeMs != null) ? `${target.responseTimeMs} ms` : '—';
      }
    }

    const probeEl = document.getElementById('drawerHttpCode');
    if (probeEl) {
      probeEl.textContent = target.httpStatusCode ? String(target.httpStatusCode) : (isDown ? (target.failureCategory || 'FAIL') : 'OK');
    }

    // Category from the backend classifier (classify_scrape_failure) prefixed
    // onto the raw Prometheus error — raw text is never dropped, just labeled.
    const errorRowEl = document.getElementById('drawerErrorRow');
    if (errorRowEl) {
      const cat = target.failureCategory;
      const raw = target.failureDetail || target.lastError;
      if (isDown && (raw || (cat && cat !== 'Unknown'))) {
        errorRowEl.textContent = (cat && cat !== 'Unknown' && raw && raw !== cat) ? `${cat} — ${raw}` : (raw || cat);
        errorRowEl.classList.remove('hidden');
      } else {
        errorRowEl.textContent = '';
        errorRowEl.classList.add('hidden');
      }
    }

    this._updateDrawerUptime(target);

    const probeStateEl = document.getElementById('drawerProbeState');
    const probeDetailEl = document.getElementById('drawerProbeDetail');
    if (probeStateEl) {
      probeStateEl.textContent = isDown ? 'FAILED' : 'OK';
      probeStateEl.style.color = isDown ? '#EF4444' : '#22C55E';
    }
    if (probeDetailEl) {
      if (isDown) {
        probeDetailEl.textContent = target.scrapeFailureCategory || (target.lastError ? 'Scrape Error' : 'No response');
      } else {
        probeDetailEl.textContent = target.httpStatusCode ? `HTTP ${target.httpStatusCode}` : 'Probe successful';
      }
    }

    // Scrape URL (Rec #5)
    const linkEl = document.getElementById('drawerTargetUrlLink');
    if (linkEl) {
      const u = target.scrapeUrl || target.instance;
      const hrefUrl = u.startsWith('http://') || u.startsWith('https://') ? u : `http://${u}`;
      linkEl.textContent = u;
      linkEl.href = hrefUrl;
      linkEl.target = '_blank';
    }

    // Last/Next Scrape — real timestamp from Prometheus, "next" is an
    // estimate off this dashboard's own poll cadence (this.currentInterval),
    // not a fabricated Prometheus schedule.
    const lastScrapeEl = document.getElementById('drawerLastScrape');
    if (lastScrapeEl) lastScrapeEl.textContent = target.lastScrape ? this._relTime(target.lastScrape) : '—';
    const nextScrapeEl = document.getElementById('drawerNextScrape');
    if (nextScrapeEl) {
      if (target.lastScrape) {
        const nextMs = new Date(target.lastScrape).getTime() + this.currentInterval;
        const diffSec = Math.round((nextMs - Date.now()) / 1000);
        nextScrapeEl.textContent = diffSec > 0 ? `~${diffSec}s from now` : 'Due now';
      } else {
        nextScrapeEl.textContent = '—';
      }
    }

    // Fetch real-time Uptime & Downtime event history from Prometheus
    this.loadTargetHistory(target.instance);

    this._renderDrawerMaintenance(target);
    this._renderDrawerDependency(target);
    this._renderDrawerAvailabilityBars(target);
    this._renderDrawerProbeSummary(target, []);

    // Open
    if (this.sideDrawerOverlay) {
      this.sideDrawerOverlay.classList.remove('hidden');
      requestAnimationFrame(() => this.sideDrawerOverlay.classList.add('visible'));
    }
    if (this.sideDrawer) this.sideDrawer.classList.add('drawer-open');
    if (this._untrapDrawer) this._untrapDrawer();
    if (this.sideDrawer) this._untrapDrawer = window.trapModalFocus(this.sideDrawer);

    // Focus management
    setTimeout(() => {
      const closeBtn = document.getElementById('closeDrawerBtn');
      if (closeBtn) closeBtn.focus();
    }, 320);
  }

  // Toggles the drawer between "schedule maintenance" and "maintenance
  // active" (with a live countdown) depending on the target's current state.
  _renderDrawerMaintenance(target) {
    const activePanel = document.getElementById('drawerMaintenanceActive');
    const form = document.getElementById('drawerMaintenanceForm');
    if (!activePanel || !form) return;

    if (target.maintenance) {
      activePanel.classList.remove('hidden');
      form.classList.add('hidden');
      const countdownEl = document.getElementById('drawerMaintCountdown');
      if (countdownEl) {
        const remainMs = target.maintenanceUntil ? Math.max(0, target.maintenanceUntil * 1000 - Date.now()) : 0;
        countdownEl.textContent = this._fmtDownAging(remainMs);
      }
      const reasonEl = document.getElementById('drawerMaintReason');
      if (reasonEl) reasonEl.textContent = target.maintenanceReason || 'No reason given';
    } else {
      activePanel.classList.add('hidden');
      form.classList.remove('hidden');
    }
  }

  async _startMaintenance(instance, minutes, reason) {
    const now = Math.floor(Date.now() / 1000);
    const res = await apiFetch('/api/maintenance', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ target: instance, scope: 'instance', reason, start: now, end: now + minutes * 60 })
    });
    if (res.ok) {
      const { window: mw } = await res.json();
      // Patch the known result directly instead of awaiting a reload — load()
      // shares one AbortController with the periodic poll, so an in-flight
      // poll tick can silently abort this call's fetch and leave the drawer
      // reading pre-mutation data even though the write already succeeded.
      const target = this.data.find(t => t.instance === instance);
      if (target && mw) {
        Object.assign(target, { maintenance: true, maintenanceId: mw.id, maintenanceUntil: mw.end, maintenanceReason: mw.reason });
        if (this.selectedTarget && this.selectedTarget.instance === instance) this.selectedTarget = target;
        this._renderDrawerMaintenance(target);
      }
      this._lastDataSignature = null;
      this.load(); // background refresh so the rest of the grid (badges, other cards) catches up too
    }
    return res.ok;
  }

  async _endMaintenance(maintenanceId, instance) {
    if (!maintenanceId) return false;
    const res = await apiFetch(`/api/maintenance/${encodeURIComponent(maintenanceId)}`, { method: 'DELETE' });
    if (res.ok) {
      const target = this.data.find(t => t.instance === instance);
      if (target) {
        Object.assign(target, { maintenance: false, maintenanceId: null, maintenanceUntil: null, maintenanceReason: '' });
        if (this.selectedTarget && this.selectedTarget.instance === instance) this.selectedTarget = target;
        this._renderDrawerMaintenance(target);
      }
      this._lastDataSignature = null;
      this.load();
    }
    return res.ok;
  }

  // Toggles the drawer between "link to a parent" and "already linked"
  // (Phase 12 alert correlation).
  _renderDrawerDependency(target) {
    const activePanel = document.getElementById('drawerDependencyActive');
    const form = document.getElementById('drawerDependencyForm');
    const select = document.getElementById('drawerDependencyParentSelect');
    if (!activePanel || !form || !select) return;

    if (target.dependsOn) {
      activePanel.classList.remove('hidden');
      form.classList.add('hidden');
      const parentEl = document.getElementById('drawerDependencyParent');
      if (parentEl) parentEl.textContent = target.dependsOn;
    } else {
      activePanel.classList.add('hidden');
      form.classList.remove('hidden');
      const current = select.value;
      select.innerHTML = '<option value="">Select parent host…</option>' +
        this.data
          .filter(t => t.instance !== target.instance)
          .map(t => `<option value="${this._esc(t.instance)}">${this._esc(t.instance)}</option>`)
          .join('');
      select.value = current;
    }
  }

  async _setDependency(child, parent) {
    const res = await apiFetch('/api/dependencies', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ child, parent })
    });
    if (res.ok) {
      const { dependency } = await res.json();
      const target = this.data.find(t => t.instance === child);
      if (target && dependency) {
        Object.assign(target, { dependsOn: dependency.parent, dependencyId: dependency.id });
        if (this.selectedTarget && this.selectedTarget.instance === child) this.selectedTarget = target;
        this._renderDrawerDependency(target);
      }
      this._lastDataSignature = null;
      this.load(); // background refresh — picks up suppressedBy once the poll recomputes it server-side
    }
    return res.ok;
  }

  async _removeDependency(dependencyId, child) {
    if (!dependencyId) return false;
    const res = await apiFetch(`/api/dependencies/${encodeURIComponent(dependencyId)}`, { method: 'DELETE' });
    if (res.ok) {
      const target = this.data.find(t => t.instance === child);
      if (target) {
        Object.assign(target, { dependsOn: null, dependencyId: null, suppressedBy: null });
        if (this.selectedTarget && this.selectedTarget.instance === child) this.selectedTarget = target;
        this._renderDrawerDependency(target);
      }
      this._lastDataSignature = null;
      this.load();
    }
    return res.ok;
  }

  /* ── Maintenance Manager (Phase 9 Revise 1) ───────────────────────────
     Central view over every window from the same /api/maintenance the
     drawer already uses — no second store, no polling of its own (it
     refreshes on tab-open and after any action taken inside it). The
     drawer keeps the ability to *start* a window on its own host; this is
     purely for seeing/ending everything at once without hunting through
     individual hosts. */
  async _maintenanceManagerOnActivate() {
    const table = document.getElementById('maintManagerTable');
    const meta = document.getElementById('maintManagerMeta');
    if (!table) return;
    if (!this._maintManagerBound) {
      this._maintManagerBound = true;
      table.addEventListener('click', async e => {
        const btn = e.target.closest('[data-end-id]');
        if (!btn) return;
        btn.disabled = true;
        const res = await apiFetch(`/api/maintenance/${encodeURIComponent(btn.dataset.endId)}`, { method: 'DELETE' });
        if (res.ok) {
          this._lastDataSignature = null;
          await this.load(); // so any affected host card/badge updates too
          this._maintenanceManagerOnActivate();
        } else {
          btn.disabled = false;
        }
      });
    }

    let windows = [];
    try {
      const res = await fetch('/api/maintenance');
      const data = await res.json();
      windows = Array.isArray(data.windows) ? data.windows : [];
    } catch (e) { /* leave table showing its previous state */ }

    const now = Date.now() / 1000;
    const active = windows.filter(w => w.active).sort((a, b) => a.end - b.end);
    const upcoming = windows.filter(w => !w.active && w.start > now).sort((a, b) => a.start - b.start);

    if (meta) meta.textContent = `${active.length} active, ${upcoming.length} upcoming`;

    if (active.length === 0 && upcoming.length === 0) {
      table.innerHTML = '<div class="de-empty">No maintenance windows scheduled</div>';
      return;
    }

    const row = (w, isActive) => {
      const label = isActive
        ? `Ends in ${this._fmtDownAging(Math.max(0, w.end * 1000 - Date.now()))}`
        : `Starts in ${this._fmtDownAging(Math.max(0, w.start * 1000 - Date.now()))}`;
      return `
        <div class="maint-row">
          <span class="maint-target" title="${this._esc(w.scope === 'job' ? 'Entire job/group' : 'Single host')}">${this._esc(w.target)}</span>
          <span class="maint-reason">${this._esc(w.reason || 'No reason given')}</span>
          <span class="maint-window ${isActive ? 'maint-window-active' : ''}">${label}</span>
          <button class="btn btn-sm btn-secondary" data-end-id="${this._esc(w.id)}" type="button">${isActive ? 'End Early' : 'Cancel'}</button>
        </div>`;
    };

    table.innerHTML = [
      active.length ? `<div class="maint-group-label">Active</div>${active.map(w => row(w, true)).join('')}` : '',
      upcoming.length ? `<div class="maint-group-label">Upcoming</div>${upcoming.map(w => row(w, false)).join('')}` : '',
    ].join('');
  }

  _renderSparkline(points, isInternalCall = false) {
    const wrap = document.getElementById('drawerSparkline');
    const badge = document.getElementById('drawerSparklineBadge');
    const resetBtn = document.getElementById('sparklineResetZoomBtn');
    const statNow = document.getElementById('spStatNow');
    const statAvg = document.getElementById('spStatAvg');
    const statP95 = document.getElementById('spStatP95');
    const statMax = document.getElementById('spStatMax');

    if (!wrap) return;

    if (!isInternalCall) {
      this._rawSparklinePoints = Array.isArray(points) ? points : [];
      if (!this._sparklineZoomRange) {
        if (resetBtn) resetBtn.style.display = 'none';
      }
    }

    if (!Array.isArray(points) || points.length < 2) {
      if (statNow) statNow.textContent = '—';
      if (statAvg) statAvg.textContent = '—';
      if (statP95) statP95.textContent = '—';
      if (statMax) statMax.textContent = '—';
      if (badge) badge.style.display = 'none';
      if (resetBtn) resetBtn.style.display = 'none';
      wrap.innerHTML = '<div class="de-empty" style="padding:15px; font-size:12px; color:var(--text-secondary); text-align:center;">No response time trend data for this range</div>';
      return;
    }

    // Apply zoom filtering if active
    let activePoints = points;
    if (this._sparklineZoomRange) {
      const { tMin, tMax } = this._sparklineZoomRange;
      const filtered = points.filter(p => p[0] >= tMin && p[0] <= tMax);
      if (filtered.length >= 2) {
        activePoints = filtered;
        if (resetBtn) resetBtn.style.display = 'inline-flex';
      } else {
        this._sparklineZoomRange = null;
        if (resetBtn) resetBtn.style.display = 'none';
      }
    }

    const lats = activePoints.map(p => p[1]);
    const nowVal = lats[lats.length - 1];
    const minL = Math.min(...lats);
    const maxL = Math.max(...lats);
    const avgL = lats.reduce((a, b) => a + b, 0) / lats.length;

    const sortedLats = [...lats].sort((a, b) => a - b);
    const p95Idx = Math.min(sortedLats.length - 1, Math.floor(sortedLats.length * 0.95));
    const p95L = sortedLats[p95Idx];

    // Update Header Summary Cards strictly based on visible activePoints
    if (statNow) statNow.textContent = `${nowVal.toFixed(1)} ms`;
    if (statAvg) statAvg.textContent = `${avgL.toFixed(1)} ms`;
    if (statP95) statP95.textContent = `${p95L.toFixed(1)} ms`;
    if (statMax) statMax.textContent = `${maxL.toFixed(1)} ms`;

    // Status Badge & Accent Color based on thresholds
    let accentColor = 'var(--accent)';
    let badgeLabel = 'Normal (<200ms)';
    let badgeBg = 'rgba(34, 197, 94, 0.15)';
    let badgeFg = '#22C55E';

    if (maxL > 1000) {
      accentColor = '#EF4444';
      badgeLabel = 'Spike (>1000ms)';
      badgeBg = 'rgba(239, 68, 68, 0.15)';
      badgeFg = '#EF4444';
    } else if (p95L > 200 || maxL > 300) {
      accentColor = '#F59E0B';
      badgeLabel = 'Degraded (>200ms)';
      badgeBg = 'rgba(245, 158, 11, 0.15)';
      badgeFg = '#F59E0B';
    }

    if (badge) {
      badge.textContent = badgeLabel;
      badge.style.background = badgeBg;
      badge.style.color = badgeFg;
      badge.style.display = 'inline-block';
    }

    // Dynamic Adaptive Nice Scale Math
    const niceScale = calculateNiceScale(minL, maxL, 4);
    const rangeMin = niceScale.niceMin;
    const rangeMax = niceScale.niceMax;
    const rangeSpan = Math.max(0.001, rangeMax - rangeMin);

    const width = 600;
    const height = 130;
    const padTop = 10;
    const padBottom = 10;
    const drawHeight = height - padTop - padBottom;

    const t0 = activePoints[0][0];
    const tN = activePoints[activePoints.length - 1][0];
    const dt = Math.max(1, tN - t0);

    const pts = activePoints.map(p => {
      const x = (((p[0] - t0) / dt) * width).toFixed(1);
      const yRatio = (p[1] - rangeMin) / rangeSpan;
      const y = (height - padBottom - (yRatio * drawHeight)).toFixed(1);
      return { x: parseFloat(x), y: parseFloat(y), t: p[0], val: p[1] };
    });

    const lineD = 'M' + pts.map(p => `${p.x},${p.y}`).join(' L');
    const bottomY = (height - padBottom - (((0 - rangeMin) / rangeSpan) * drawHeight)).toFixed(1);
    const areaD = `${lineD} L${width},${bottomY} L0,${bottomY}Z`;

    // Grid lines & Y-axis labels matching dynamic tick positions
    const gridLines = [];
    const leftLabels = [];
    const rightLabels = [];

    niceScale.ticks.forEach(t => {
      const tRatio = (t - rangeMin) / rangeSpan;
      const tY = height - padBottom - (tRatio * drawHeight);
      const topPct = ((tY / height) * 100).toFixed(2);
      const formattedVal = niceScale.formatTick(t);

      gridLines.push(`<line x1="0" y1="${tY.toFixed(1)}" x2="${width}" y2="${tY.toFixed(1)}" stroke="rgba(255,255,255,0.07)" stroke-dasharray="3,3" stroke-width="1"/>`);
      leftLabels.push(`<span style="position:absolute; right:0; top:${topPct}%; transform:translateY(-50%); font-size:var(--fs-xs, 11px); font-family:var(--font-mono); color:var(--text-secondary); opacity:0.85; white-space:nowrap;">${formattedVal}</span>`);
      rightLabels.push(`<span style="position:absolute; left:0; top:${topPct}%; transform:translateY(-50%); font-size:var(--fs-xs, 11px); font-family:var(--font-mono); color:var(--text-secondary); opacity:0.85; white-space:nowrap;">${formattedVal}</span>`);
    });

    // Time axis label formatting helper
    const fmtTime = (ts) => {
      const d = new Date(ts * 1000);
      const isLongRange = (tN - t0) > 86400; // >24h
      if (isLongRange) {
        return d.toLocaleDateString(DATE_LOCALE, { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' });
      }
      return d.toLocaleTimeString(DATE_LOCALE, { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    };

    const t0Str = fmtTime(t0);
    const tMidStr = fmtTime(t0 + (tN - t0) / 2);
    const tNStr = fmtTime(tN);

    const defsHtml = buildMSGradientDefs(rangeMin, rangeMax, 'sgLineGrad', 'sgAreaGrad');

    // Build SVG & Adaptive ms Meter Overlay HTML
    wrap.innerHTML = `
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:4px; font-size:var(--fs-xs, 11px); font-weight:600; color:var(--text-secondary); opacity:0.8; padding:0 2px;">
        <span>ms</span>
        <span>ms</span>
      </div>
      <div style="display:flex; gap:10px; position:relative; align-items:stretch;">
        <div style="position:relative; width:44px; flex-shrink:0; pointer-events:none;">
          ${leftLabels.join('')}
        </div>
        <div class="sparkline-svg-wrap" id="sparklineSvgWrap" style="flex:1; position:relative; height:130px; cursor:crosshair;">
          <svg class="sparkline" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none">
            ${defsHtml}
            ${gridLines.join('')}
            <path class="spark-area" d="${areaD}" fill="url(#sgAreaGrad)"/>
            <path class="spark-line" d="${lineD}" fill="none" stroke="url(#sgLineGrad)" stroke-width="1.8" vector-effect="non-scaling-stroke" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
          <div class="sparkline-selection-box" id="spSelectBox" style="display:none;"></div>
          <div class="sparkline-tracker" id="spTracker" style="display:none;"></div>
          <div class="sparkline-dot" id="spDot" style="display:none; background:${accentColor}; box-shadow:0 0 8px ${accentColor};"></div>
          <div class="sparkline-tooltip" id="spTooltip" style="display:none;"></div>
        </div>
        <div style="position:relative; width:44px; flex-shrink:0; pointer-events:none;">
          ${rightLabels.join('')}
        </div>
      </div>
      <div class="sparkline-x-axis" style="display:flex; justify-content:space-between; font-size:var(--fs-2xs, 10px); font-family:var(--font-mono); color:var(--text-secondary); margin-top:8px; padding:6px 54px 0 54px; border-top:1px dashed var(--border);">
        <span>${t0Str}</span>
        <span>${tMidStr}</span>
        <span>${tNStr}</span>
      </div>`;

    // Attach Interactive Tooltip & Drag-to-Zoom / Pan Handlers
    const svgWrap = wrap.querySelector('#sparklineSvgWrap');
    const selectBox = wrap.querySelector('#spSelectBox');
    const tracker = wrap.querySelector('#spTracker');
    const dot = wrap.querySelector('#spDot');
    const tooltip = wrap.querySelector('#spTooltip');

    if (!svgWrap || !tracker || !dot || !tooltip) return;

    let isMouseDown = false;
    let dragStartX = 0;
    let isDraggingZoom = false;

    const onPointerDown = (e) => {
      isMouseDown = true;
      const rect = svgWrap.getBoundingClientRect();
      const clientX = e.touches ? e.touches[0].clientX : e.clientX;
      dragStartX = clientX - rect.left;
      isDraggingZoom = false;
    };

    const onPointerMove = (e) => {
      const rect = svgWrap.getBoundingClientRect();
      const clientX = e.touches ? e.touches[0].clientX : e.clientX;
      const currentX = Math.max(0, Math.min(rect.width, clientX - rect.left));

      if (isMouseDown) {
        const deltaX = Math.abs(currentX - dragStartX);
        if (deltaX > 6) {
          isDraggingZoom = true;
          const leftX = Math.min(dragStartX, currentX);
          const boxW = Math.abs(currentX - dragStartX);
          selectBox.style.left = `${leftX}px`;
          selectBox.style.width = `${boxW}px`;
          selectBox.style.display = 'block';

          tracker.style.display = 'none';
          dot.style.display = 'none';
          tooltip.style.display = 'none';
          return;
        }
      }

      if (clientX < rect.left || clientX > rect.right) {
        onPointerLeave();
        return;
      }

      const relX = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
      const targetT = t0 + relX * (tN - t0);

      // Binary search for nearest point by timestamp
      let low = 0;
      let high = pts.length - 1;
      let closest = pts[0];
      let minDiff = Math.abs(pts[0].t - targetT);

      while (low <= high) {
        const mid = (low + high) >> 1;
        const diff = Math.abs(pts[mid].t - targetT);
        if (diff < minDiff) {
          minDiff = diff;
          closest = pts[mid];
        }
        if (pts[mid].t < targetT) low = mid + 1;
        else high = mid - 1;
      }

      const pointX = (closest.x / width) * rect.width;
      const pointY = (closest.y / height) * rect.height;

      tracker.style.left = `${pointX}px`;
      tracker.style.top = `${padTop}px`;
      tracker.style.height = `${drawHeight}px`;
      tracker.style.display = 'block';

      dot.style.left = `${pointX}px`;
      dot.style.top = `${pointY}px`;
      dot.style.display = 'block';

      const valColor = closest.val > 500 ? '#EF4444' : (closest.val > 200 ? '#F59E0B' : '#22C55E');
      dot.style.background = valColor;
      dot.style.boxShadow = `0 0 8px ${valColor}`;

      const dObj = new Date(closest.t * 1000);
      const timeLabel = dObj.toLocaleTimeString(DATE_LOCALE, { hour: '2-digit', minute: '2-digit', second: '2-digit' });
      const statusText = closest.val > 500 ? 'SLOW' : (closest.val === 0 ? 'DOWN' : 'UP');

      tooltip.innerHTML = `
        <div style="font-weight:600; color:var(--text-secondary); margin-bottom:2px; font-size:var(--fs-2xs, 10px);">Time: <span style="color:var(--text-primary);">${timeLabel}</span></div>
        <div style="font-weight:600; color:var(--text-secondary); margin-bottom:2px; font-size:var(--fs-2xs, 10px);">Response Time: <span style="color:${valColor}; font-weight:700;">${closest.val.toFixed(1)} ms</span></div>
        <div style="font-weight:600; color:var(--text-secondary); font-size:var(--fs-2xs, 10px);">Status: <span style="color:${valColor}; font-weight:700;">${statusText}</span></div>
      `;
      
      const clampX = Math.max(45, Math.min(rect.width - 45, pointX));
      tooltip.style.left = `${clampX}px`;
      tooltip.style.top = `${Math.max(20, pointY)}px`;
      tooltip.style.display = 'block';
    };

    const onPointerUp = (e) => {
      if (isMouseDown && isDraggingZoom) {
        const rect = svgWrap.getBoundingClientRect();
        const clientX = e.changedTouches ? e.changedTouches[0].clientX : e.clientX;
        const currentX = Math.max(0, Math.min(rect.width, clientX - rect.left));
        
        const minX = Math.min(dragStartX, currentX);
        const maxX = Math.max(dragStartX, currentX);

        const relMinX = Math.max(0, Math.min(1, minX / rect.width));
        const relMaxX = Math.max(0, Math.min(1, maxX / rect.width));

        const zoomTMin = t0 + relMinX * (tN - t0);
        const zoomTMax = t0 + relMaxX * (tN - t0);

        if (zoomTMax - zoomTMin > 3) {
          this._sparklineZoomRange = { tMin: zoomTMin, tMax: zoomTMax };
          if (resetBtn) resetBtn.style.display = 'inline-flex';
          this._renderSparkline(this._rawSparklinePoints, true);
        }
      }
      isMouseDown = false;
      isDraggingZoom = false;
      if (selectBox) selectBox.style.display = 'none';
    };

    const onPointerLeave = () => {
      isMouseDown = false;
      isDraggingZoom = false;
      if (selectBox) selectBox.style.display = 'none';
      tracker.style.display = 'none';
      dot.style.display = 'none';
      tooltip.style.display = 'none';
    };

    svgWrap.addEventListener('mousedown', onPointerDown);
    svgWrap.addEventListener('mousemove', onPointerMove);
    svgWrap.addEventListener('mouseup', onPointerUp);
    svgWrap.addEventListener('mouseleave', onPointerLeave);

    svgWrap.addEventListener('touchstart', onPointerDown, { passive: true });
    svgWrap.addEventListener('touchmove', onPointerMove, { passive: true });
    svgWrap.addEventListener('touchend', onPointerUp, { passive: true });
  }

  _renderHistoryChart(points) {
    const wrap = document.getElementById('drawerHistoryChart');
    const elMin = document.getElementById('histMinVal');
    const elAvg = document.getElementById('histAvgVal');
    const elP95 = document.getElementById('histP95Val');
    const elMax = document.getElementById('histMaxVal');
    const elList = document.getElementById('drawerHistoryDatapointsList');

    if (!wrap) return;
    if (!Array.isArray(points) || points.length < 2) {
      if (elMin) elMin.textContent = '—';
      if (elAvg) elAvg.textContent = '—';
      if (elP95) elP95.textContent = '—';
      if (elMax) elMax.textContent = '—';
      if (elList) elList.innerHTML = '<div class="de-empty" style="padding:10px; font-size:12px;">No latency datapoints</div>';
      wrap.innerHTML = '<div class="de-empty" style="padding:25px; font-size:12px; color:var(--text-secondary); text-align:center;">No latency history for this range</div>';
      return;
    }

    const lats = points.map(p => p[1]);
    const minL = Math.min(...lats);
    const maxL = Math.max(...lats);
    const avgL = lats.reduce((a, b) => a + b, 0) / lats.length;
    const sorted = [...lats].sort((a, b) => a - b);
    const p95L = sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * 0.95))];

    if (elMin) elMin.textContent = `${minL.toFixed(1)} ms`;
    if (elAvg) elAvg.textContent = `${avgL.toFixed(1)} ms`;
    if (elP95) elP95.textContent = `${p95L.toFixed(1)} ms`;
    if (elMax) elMax.textContent = `${maxL.toFixed(1)} ms`;

    // Render full datapoints list across selected range
    const histPointsCountBadge = document.getElementById('histPointsCountBadge');
    if (histPointsCountBadge) {
      histPointsCountBadge.textContent = `${points.length.toLocaleString()} points`;
    }

    if (elList) {
      const recentPts = [...points].reverse();
      elList.innerHTML = recentPts.map(p => {
        const dObj = new Date(p[0] * 1000);
        const timeStr = dObj.toLocaleTimeString(DATE_LOCALE, { hour: '2-digit', minute: '2-digit', second: '2-digit' });
        const dateStr = dObj.toLocaleDateString(DATE_LOCALE, { day: '2-digit', month: 'short' });
        const latVal = p[1];
        const isSlow = latVal > 500;
        const color = isSlow ? '#F59E0B' : '#22C55E';
        return `
          <div style="display:flex; justify-content:space-between; align-items:center; padding:4px 8px; background:var(--surface-hover); border:1px solid var(--border); border-radius:var(--r-sm); font-size:var(--fs-xs, 11px); font-family:var(--font-mono);">
            <span style="color:var(--text-secondary);">${dateStr} ${timeStr}</span>
            <span style="font-weight:700; color:${color};">${latVal.toFixed(1)} ms</span>
          </div>`;
      }).join('');
    }

    // Dynamic Adaptive Nice Scale Math
    const niceScale = calculateNiceScale(minL, maxL, 4);
    const rangeMin = niceScale.niceMin;
    const rangeMax = niceScale.niceMax;
    const rangeSpan = Math.max(0.001, rangeMax - rangeMin);

    const width = 600;
    const height = 140;
    const padTop = 10;
    const padBottom = 10;
    const drawHeight = height - padTop - padBottom;

    const t0 = points[0][0];
    const tN = points[points.length - 1][0];
    const dt = Math.max(1, tN - t0);

    const pts = points.map(p => {
      const x = (((p[0] - t0) / dt) * width).toFixed(1);
      const yRatio = (p[1] - rangeMin) / rangeSpan;
      const y = (height - padBottom - (yRatio * drawHeight)).toFixed(1);
      return { x: parseFloat(x), y: parseFloat(y), t: p[0], val: p[1] };
    });

    const lineD = 'M' + pts.map(p => `${p.x},${p.y}`).join(' L');
    const bottomY = (height - padBottom - (((0 - rangeMin) / rangeSpan) * drawHeight)).toFixed(1);
    const areaD = `${lineD} L${width},${bottomY} L0,${bottomY}Z`;

    // Grid lines & Y-axis labels matching dynamic tick positions
    const gridLines = [];
    const leftLabels = [];
    const rightLabels = [];

    niceScale.ticks.forEach(t => {
      const tRatio = (t - rangeMin) / rangeSpan;
      const tY = height - padBottom - (tRatio * drawHeight);
      const topPct = ((tY / height) * 100).toFixed(2);
      const formattedVal = niceScale.formatTick(t);

      gridLines.push(`<line x1="0" y1="${tY.toFixed(1)}" x2="${width}" y2="${tY.toFixed(1)}" stroke="rgba(255,255,255,0.07)" stroke-dasharray="3,3" stroke-width="1"/>`);
      leftLabels.push(`<span style="position:absolute; right:0; top:${topPct}%; transform:translateY(-50%); font-size:var(--fs-xs, 11px); font-family:var(--font-mono); color:var(--text-secondary); opacity:0.85; white-space:nowrap;">${formattedVal}</span>`);
      rightLabels.push(`<span style="position:absolute; left:0; top:${topPct}%; transform:translateY(-50%); font-size:var(--fs-xs, 11px); font-family:var(--font-mono); color:var(--text-secondary); opacity:0.85; white-space:nowrap;">${formattedVal}</span>`);
    });

    // Time axis label formatting helper
    const fmtTime = (ts) => {
      const d = new Date(ts * 1000);
      const isLongRange = (tN - t0) > 86400; // >24h
      if (isLongRange) {
        return d.toLocaleDateString(DATE_LOCALE, { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' });
      }
      return d.toLocaleTimeString(DATE_LOCALE, { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    };

    const t0Str = fmtTime(t0);
    const tMidStr = fmtTime(t0 + (tN - t0) / 2);
    const tNStr = fmtTime(tN);

    const defsHistHtml = buildMSGradientDefs(rangeMin, rangeMax, 'sgHistLineGrad', 'sgHistGrad');

    wrap.innerHTML = `
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:4px; font-size:var(--fs-xs, 11px); font-weight:600; color:var(--text-secondary); opacity:0.8; padding:0 2px;">
        <span>ms</span>
        <span>ms</span>
      </div>
      <div style="display:flex; gap:10px; position:relative; align-items:stretch;">
        <div style="position:relative; width:44px; flex-shrink:0; pointer-events:none;">
          ${leftLabels.join('')}
        </div>
        <div class="sparkline-svg-wrap" id="histSvgWrap" style="flex:1; position:relative; height:140px; cursor:crosshair;">
          <svg class="sparkline" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none">
            ${defsHistHtml}
            ${gridLines.join('')}
            <path class="spark-area" d="${areaD}" fill="url(#sgHistGrad)"/>
            <path class="spark-line" d="${lineD}" fill="none" stroke="url(#sgHistLineGrad)" stroke-width="1.8" vector-effect="non-scaling-stroke" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
          <div class="sparkline-tracker" id="histTracker" style="display:none;"></div>
          <div class="sparkline-dot" id="histDot" style="display:none; background:var(--accent);"></div>
          <div class="sparkline-tooltip" id="histTooltip" style="display:none;"></div>
        </div>
        <div style="position:relative; width:44px; flex-shrink:0; pointer-events:none;">
          ${rightLabels.join('')}
        </div>
      </div>
      <div class="sparkline-x-axis" style="display:flex; justify-content:space-between; font-size:var(--fs-2xs, 10px); font-family:var(--font-mono); color:var(--text-secondary); margin-top:8px; padding:6px 54px 0 54px; border-top:1px dashed var(--border);">
        <span>${t0Str}</span>
        <span>${tMidStr}</span>
        <span>${tNStr}</span>
      </div>`;

    const svgWrap = wrap.querySelector('#histSvgWrap');
    const tracker = wrap.querySelector('#histTracker');
    const dot = wrap.querySelector('#histDot');
    const tooltip = wrap.querySelector('#histTooltip');

    if (!svgWrap || !tracker || !dot || !tooltip) return;

    const onPointerMove = (e) => {
      const rect = svgWrap.getBoundingClientRect();
      const clientX = e.touches ? e.touches[0].clientX : e.clientX;
      if (clientX < rect.left || clientX > rect.right) {
        onPointerLeave();
        return;
      }

      const relX = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
      const targetT = t0 + relX * (tN - t0);

      let low = 0;
      let high = pts.length - 1;
      let closest = pts[0];
      let minDiff = Math.abs(pts[0].t - targetT);

      while (low <= high) {
        const mid = (low + high) >> 1;
        const diff = Math.abs(pts[mid].t - targetT);
        if (diff < minDiff) {
          minDiff = diff;
          closest = pts[mid];
        }
        if (pts[mid].t < targetT) low = mid + 1;
        else high = mid - 1;
      }

      const pointX = (closest.x / width) * rect.width;
      const pointY = (closest.y / height) * rect.height;

      tracker.style.left = `${pointX}px`;
      tracker.style.top = `${padTop}px`;
      tracker.style.height = `${drawHeight}px`;
      tracker.style.display = 'block';

      dot.style.left = `${pointX}px`;
      dot.style.top = `${pointY}px`;
      dot.style.display = 'block';

      const valColor = closest.val > 500 ? '#EF4444' : (closest.val > 200 ? '#F59E0B' : '#22C55E');
      dot.style.background = valColor;
      dot.style.boxShadow = `0 0 8px ${valColor}`;

      const dObj = new Date(closest.t * 1000);
      const timeLabel = dObj.toLocaleTimeString(DATE_LOCALE, { hour: '2-digit', minute: '2-digit', second: '2-digit' });
      const statusText = closest.val > 500 ? 'SLOW' : (closest.val === 0 ? 'DOWN' : 'UP');

      tooltip.innerHTML = `
        <div style="font-weight:600; color:var(--text-secondary); margin-bottom:2px; font-size:var(--fs-2xs, 10px);">Time: <span style="color:var(--text-primary);">${timeLabel}</span></div>
        <div style="font-weight:600; color:var(--text-secondary); margin-bottom:2px; font-size:var(--fs-2xs, 10px);">Response Time: <span style="color:${valColor}; font-weight:700;">${closest.val.toFixed(1)} ms</span></div>
        <div style="font-weight:600; color:var(--text-secondary); font-size:var(--fs-2xs, 10px);">Status: <span style="color:${valColor}; font-weight:700;">${statusText}</span></div>
      `;
      const clampX = Math.max(50, Math.min(rect.width - 50, pointX));
      tooltip.style.left = `${clampX}px`;
      tooltip.style.top = `${Math.max(20, pointY)}px`;
      tooltip.style.display = 'block';
    };

    const onPointerLeave = () => {
      tracker.style.display = 'none';
      dot.style.display = 'none';
      tooltip.style.display = 'none';
    };

    svgWrap.addEventListener('mousemove', onPointerMove);
    svgWrap.addEventListener('mouseleave', onPointerLeave);
  }

  async loadTargetHistory(targetInstance) {
    if (this._historyAbortController) this._historyAbortController.abort();
    const controller = new AbortController();
    this._historyAbortController = controller;
    const seq = ++this._targetHistorySeq;
    const isStale = () => seq !== this._targetHistorySeq || this.selectedTarget?.instance !== targetInstance;

    const rangeText = this._rangeDisplay();
    const drawerSparklineRangeEl = document.getElementById('drawerSparklineRange');
    if (drawerSparklineRangeEl) drawerSparklineRangeEl.textContent = `(${rangeText})`;
    const historyRangeTag = document.getElementById('historyRangeTag');
    if (historyRangeTag) historyRangeTag.textContent = `History (${rangeText})`;

    const logsList = document.getElementById('drawerLogsList');
    const eventsBadge = document.getElementById('eventsCountBadge');
    if (!logsList) return;
    logsList.innerHTML = '<div class="de-empty" style="padding:10px; font-size:12px; color:var(--text-secondary);"><span class="avail-updating-spinner" style="margin-right:6px;"></span> Loading Prometheus history logs...</div>';

    try {
      const minutes = Math.round(this.periodMinutes || 1440);
      const fetchedRangeEnd = this.periodEnd || Math.floor(Date.now() / 1000);
      const fetchedRangeStart = fetchedRangeEnd - minutes * 60;
      let historyUrl = `/api/target-history?target=${encodeURIComponent(targetInstance)}&minutes=${minutes}`;
      if (this.periodEnd) historyUrl += `&end=${this.periodEnd}`;
      const res = await fetch(historyUrl, { signal: controller.signal });
      const data = await res.json();
      if (isStale()) return;
      
      if (data.ok && Array.isArray(data.latency_points) && data.latency_points.length > 1) {
        this._renderSparkline(data.latency_points);
        this._renderHistoryChart(data.latency_points);
      } else {
        this._renderSparkline([]);
        this._renderHistoryChart([]);
      }

      this._renderDrawerAvailabilityBars(this.selectedTarget, data.latency_points || [], data.events || [], fetchedRangeStart);

      if (!data.ok) {
        // An actual backend error (bad request, Prometheus unreachable, …) —
        // don't dress it up as "no telemetry recorded", which reads as a
        // healthy-but-empty history (audit m9).
        logsList.innerHTML = `<div style="padding: 12px; background: rgba(245, 158, 11, 0.1); border: 1px solid rgba(245, 158, 11, 0.25); border-radius: 8px; font-size: 12px; color: #F59E0B; display: flex; align-items: center; gap: 8px;"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg> <span>${this._esc(data.error || 'History is temporarily unavailable')}</span></div>`;
        if (eventsBadge) eventsBadge.textContent = '—';
        this._renderDrawerRecentEvents([]);
        this._renderDrawerProbeSummary(this.selectedTarget, []);
        return;
      }

      if (!Array.isArray(data.events) || data.events.length === 0) {
        const isTargetDown = this.selectedTarget?.health === 'down' || this.selectedTarget?.effective_status === 'down';
        const hasNoPoints = !Array.isArray(data.latency_points) || data.latency_points.length === 0;

        if (isTargetDown) {
          logsList.innerHTML = '<div style="padding: 12px; background: rgba(239, 68, 68, 0.1); border: 1px solid rgba(239, 68, 68, 0.25); border-radius: 8px; font-size: 12px; color: #EF4444; display: flex; align-items: center; gap: 8px;"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg> <span>Target is currently OFFLINE / Unreachable</span></div>';
        } else if (hasNoPoints) {
          logsList.innerHTML = '<div style="padding: 12px; background: rgba(148, 163, 184, 0.08); border: 1px solid rgba(148, 163, 184, 0.2); border-radius: 8px; font-size: 12px; color: var(--text-secondary); display: flex; align-items: center; gap: 8px;"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="8"/></svg> <span>No telemetry data recorded in this range</span></div>';
        } else {
          logsList.innerHTML = '<div style="padding: 12px; background: rgba(34, 197, 94, 0.1); border: 1px solid rgba(34, 197, 94, 0.2); border-radius: 8px; font-size: 12px; color: #22C55E; display: flex; align-items: center; gap: 8px;"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg> <span>Target Online — No downtime incidents in this range</span></div>';
        }
        if (eventsBadge) eventsBadge.textContent = '0 events';
        this._renderDrawerRecentEvents([]);
        this._renderDrawerProbeSummary(this.selectedTarget, []);
        return;
      }

      if (eventsBadge) eventsBadge.textContent = `${data.events.length} events`;
      this._renderDrawerRecentEvents(data.events);
      this._renderDrawerProbeSummary(this.selectedTarget, data.events);

      logsList.innerHTML = data.events.map(ev => {
        const isOnline = ev.status === 'ONLINE';
        const dotBg = isOnline ? '#22C55E' : '#EF4444';
        const statusText = isOnline ? 'ONLINE' : 'OFFLINE';
        const statusColor = isOnline ? '#22C55E' : '#EF4444';
        
        const dateObj = new Date(ev.start_ts * 1000);
        const dateStr = dateObj.toLocaleDateString(undefined, { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', second: '2-digit' });
        const durationStr = this._fmtDownAging(ev.duration_seconds * 1000);
        const ongoingBadge = ev.ongoing ? '<span style="font-size:var(--fs-2xs, 10px); background:var(--accent-bg); color:var(--accent); padding:1px 5px; border-radius:var(--r-sm); margin-left:6px; font-weight:500;">Ongoing</span>' : '';

        const summaryText = ev.summary && !ev.summary.startsWith('Target ONLINE') && !ev.summary.startsWith('Target OFFLINE')
          ? `<div style="font-size:var(--fs-2xs, 10px); color:var(--text-muted); margin-top:1px;">${ev.summary}</div>`
          : '';

        return `
          <div class="de-row" style="display:flex; align-items:center; justify-content:space-between; padding:8px 10px; background:var(--surface); border:1px solid var(--border); border-radius:var(--r-sm); margin-bottom:6px; font-size:12px;">
            <div style="display:flex; align-items:center; gap:8px;">
              <span style="width:8px; height:8px; border-radius:50%; background:${dotBg}; display:inline-block; flex-shrink:0;"></span>
              <div>
                <div style="font-weight:600; color:${statusColor}; display:flex; align-items:center; gap:6px;">
                  ${statusText} ${ongoingBadge}
                </div>
                <div style="font-size:var(--fs-xs, 11px); color:var(--text-secondary); margin-top:2px;">${dateStr}</div>
                ${summaryText}
              </div>
            </div>
            <div style="text-align:right;">
              <span class="ongoing-duration-val" data-start-ts="${ev.start_ts}" data-ongoing="${ev.ongoing ? 'true' : 'false'}" style="font-family:var(--font-mono); font-weight:600; color:var(--text-primary); font-size:12px;">${durationStr}</span>
              <div style="font-size:var(--fs-2xs, 10px); color:var(--text-muted);">Status Duration</div>
            </div>
          </div>
        `;
      }).join('');
    } catch (e) {
      if (e.name === 'AbortError') return;
      logsList.innerHTML = '<div class="de-empty" style="padding:10px; font-size:12px; color:#EF4444;">Failed to load history logs</div>';
    } finally {
      if (this._historyAbortController === controller) this._historyAbortController = null;
    }
  }

  _closeDrawer() {
    if (this._historyAbortController) {
      this._historyAbortController.abort();
      this._historyAbortController = null;
    }
    this.selectedTarget = null;
    if (this._untrapDrawer) { this._untrapDrawer(); this._untrapDrawer = null; }
    if (this.sideDrawer) this.sideDrawer.classList.remove('drawer-open');
    if (this.sideDrawerOverlay) {
      this.sideDrawerOverlay.classList.remove('visible');
      setTimeout(() => this.sideDrawerOverlay?.classList.add('hidden'), 300);
    }

    // Restore D-pad focus to the host card that opened the drawer.
    if (this._preDrawerFocusEl && document.contains(this._preDrawerFocusEl)) {
      this._preDrawerFocusEl.focus();
    }
    this._preDrawerFocusEl = null;
  }

  async _openModal() {
    const modal = document.getElementById('addTargetModal');
    const select = document.getElementById('targetUrlSelect');
    const err = document.getElementById('addTargetError');

    if (err) err.classList.add('hidden');
    if (modal) {
      modal.classList.remove('hidden');
      if (this._untrapAddTarget) this._untrapAddTarget();
      this._untrapAddTarget = window.trapModalFocus(modal);
    }

    if (select) {
      select.innerHTML = '<option value="" disabled selected>Loading Prometheus target list...</option>';
      try {
        const res = await fetch('/api/prometheus-targets');
        const data = await res.json();
        if (data.ok && Array.isArray(data.targets)) {
          if (data.targets.length === 0) {
            select.innerHTML = '<option value="" disabled selected>No Prometheus targets found</option>';
            return;
          }

          select.innerHTML = `
            <option value="" disabled selected>-- Select Prometheus Target (${data.targets.length} Targets) --</option>
            ${data.targets.map(t => {
            const statusTag = t.isDeleted ? '[Deleted / Inactive]' : '[Active]';
            return `<option value="${this._esc(t.instance)}">${this._esc(t.instance)} ${statusTag}</option>`;
          }).join('')}
          `;
          setTimeout(() => select.focus(), 50);
        } else {
          select.innerHTML = '<option value="" disabled selected>Failed to load Prometheus targets</option>';
        }
      } catch (ex) {
        select.innerHTML = '<option value="" disabled selected>Failed to connect to Prometheus API</option>';
      }
    }
  }

  _closeModal() {
    if (this._untrapAddTarget) { this._untrapAddTarget(); this._untrapAddTarget = null; }
    const modal = document.getElementById('addTargetModal');
    if (modal) modal.classList.add('hidden');
  }

  async _submitAddTarget(e) {
    e.preventDefault();
    const select = document.getElementById('targetUrlSelect');
    const err = document.getElementById('addTargetError');
    const submitBtn = document.getElementById('submitAddTargetBtn');
    const url = select ? select.value.trim() : '';

    if (!url) {
      if (err) {
        err.textContent = 'Please select a target from the Prometheus list';
        err.classList.remove('hidden');
      }
      return;
    }

    if (err) err.classList.add('hidden');
    if (submitBtn) submitBtn.disabled = true;

    try {
      const res = await apiFetch('/api/targets', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url: url })
      });
      const data = await res.json();
      if (!data.ok) {
        if (err) {
          err.textContent = data.error || 'Failed to add target';
          err.classList.remove('hidden');
        }
        return;
      }
      this._closeModal();
      if (data.warning) this._triggerEventToast(data.warning);
      else if (data.message) this._triggerEventToast(data.message);
      this.load();
    } catch (ex) {
      if (err) {
        err.textContent = ex.message || 'Error communicating with server';
        err.classList.remove('hidden');
      }
    } finally {
      if (submitBtn) submitBtn.disabled = false;
    }
  }

  async _deleteTarget(url) {
    const confirmed = await window.showConfirmDialog({
      title: 'Delete Monitoring Target',
      message: `Are you sure you want to remove target "${url}" from monitoring?`,
      confirmText: 'Delete Target',
      cancelText: 'Cancel',
      isDanger: true
    });

    if (!confirmed) return false;

    try {
      const res = await apiFetch('/api/targets', {
        method: 'DELETE',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url: url })
      });
      const data = await res.json();
      if (data.ok) {
        this.load();
        return true;
      }
      this._triggerEventToast(data.error || `Failed to delete target "${url}"`);
      return false;
    } catch (ex) {
      console.warn('[InfraWatch] Failed to delete target:', ex);
      this._triggerEventToast(`Failed to delete target "${url}"`);
      return false;
    }
  }
}

export function installTargetDrawer(Cls) {
  for (const k of Object.getOwnPropertyNames(_DrawerMethods.prototype)) {
    if (k !== 'constructor') Cls.prototype[k] = _DrawerMethods.prototype[k];
  }
}
