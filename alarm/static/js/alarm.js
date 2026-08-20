/**
 * InfraWatch — Server Monitor Dashboard
 * Multi-page navigation: Dashboard · Instances · Logs · History
 * ─────────────────────────────────────────────────────────────
 * Endpoints:
 *   GET /status      → { status, alerts, updated }
 *   GET /history     → [ { name, severity, instance, time, summary } ]
 *   GET /logs        → [ { time, event, name, severity, instance, summary } ]
 *   GET /instances   → { ok, targets: [ { instance, job, health, lastScrape, … } ] }
 *   GET /health      → { ok }
 *   POST /webhook    → (Alertmanager payload)
 */

const JOB_DEFAULT_LS_KEY = 'infrawatch.defaultJob';

/* ════════════════════════════════════════════════════════════════════════════
   ROUTER
   ════════════════════════════════════════════════════════════════════════════ */
class Router {
  constructor(pages) {
    this.pages = pages;   // { pageId: PageObject }
    this.current = null;
  }

  go(pageId) {
    if (this.current === pageId) return;
    this.current = pageId;

    // Swap visible page panels
    document.querySelectorAll('.page-content').forEach(el => {
      el.hidden = el.id !== `page-${pageId}`;
    });

    // Swap active nav (sidebar + mobile)
    document.querySelectorAll('.nav-item, .mobile-nav-item').forEach(btn => {
      const active = btn.dataset.page === pageId;
      btn.classList.toggle('nav-item-active', active && btn.classList.contains('nav-item'));
      btn.classList.toggle('mobile-nav-active', active && btn.classList.contains('mobile-nav-item'));
      if (active) {
        btn.setAttribute('aria-current', 'page');
      } else {
        btn.removeAttribute('aria-current');
      }
    });

    // Update topbar title
    const titles = {
      dashboard: 'System Status',
      instances: 'Instances',
      logs: 'Alert Logs',
      history: 'Incident History',
    };
    const subs = {
      dashboard: 'System monitoring active',
      instances: 'Live scrape target status from Prometheus',
      logs: 'Real-time alert event stream',
      history: 'Full incident log',
    };
    document.getElementById('pageTitle').textContent = titles[pageId] || pageId;
    document.getElementById('subtitle').textContent = subs[pageId] || '';

    // Notify the page it was activated
    if (this.pages[pageId]?.onActivate) this.pages[pageId].onActivate();
  }
}


/* ════════════════════════════════════════════════════════════════════════════
   INSTANCES PAGE
   ════════════════════════════════════════════════════════════════════════════ */
class InstancesPage {
  constructor(monitor) {
    this.monitor = monitor;
    this.data = [];
    this.previousStates = {};
    this.downStartTimes = {};
    this.activeStatus = 'all';   // 'all' | 'up' | 'down' | 'slow'
    this.activeSort = 'default';  // 'default' | 'name_asc' | 'name_desc' | 'job_asc' | 'job_desc' | 'latency_desc' | 'latency_asc'
    this.selectedJob = 'all';     // 'all' or specific job string
    this._defaultJobRestored = false; // guards one-time startup restore of infrawatch.defaultJob
    this.searchQ = '';
    this.selectedTarget = null;
    this.isAcknowledged = false;
    this.acknowledgedDownInstances = new Set();

    this.table = document.getElementById('instancesBody');
    this.countBadge = document.getElementById('instanceCount');

    // Pagination (TV wallboard — max 80 cards/page, see _render()/_renderCards())
    this.pageSize = 80;
    this.currentPage = 1;
    this._totalPages = 1;
    this._sortedRows = [];
    this.autoRotate = false;
    this._autoRotateTimer = null;
    this._autoRotateResumeTimer = null;
    this.paginationBar = document.getElementById('hostPagination');
    this.pageInfoEl = document.getElementById('hpInfo');
    this.prevPageBtn = document.getElementById('hpPrevBtn');
    this.nextPageBtn = document.getElementById('hpNextBtn');
    this.pagesEl = document.getElementById('hpPages');
    this.autoRotateBtn = document.getElementById('hpAutoRotateBtn');
    this.errorEl = document.getElementById('instancesError');
    this.errorMsg = document.getElementById('instancesErrorMsg');
    this.searchEl = document.getElementById('instanceSearch');
    this.chipGroup = document.getElementById('statusFilterChips');

    // Summary cards
    this.statTotal = document.getElementById('instTotal');
    this.statUp = document.getElementById('instUp');
    this.statDown = document.getElementById('instDown');
    this.statUptime = document.getElementById('instOverallUptime');
    this.healthBadge = document.getElementById('globalHealthBadge');
    this.lastProbe = document.getElementById('lastProbeTime');

    // Toast
    this.eventBanner = document.getElementById('eventAlertBanner');
    this.eventBannerText = document.getElementById('eventBannerText');
    this.closeEventBannerBtn = document.getElementById('closeEventBanner');

    // Drawer
    this.sideDrawer = document.getElementById('sideDrawer');
    this.sideDrawerOverlay = document.getElementById('sideDrawerOverlay');
    this.closeDrawerBtn = document.getElementById('closeDrawerBtn');

    this.pollInterval = null;
    this.currentInterval = 5000;

    // Historical / period-based availability (drives the summary cards)
    this.periodMinutes = 1440;      // 24h default
    this.periodEnd = null;          // epoch seconds; null = window ends "now"
    this.periodLabel = '24h';
    this.isRealtime = false;        // true => summary cards mirror the live /instances poll instead of a historical aggregate
    this.availabilityMap = {};      // instance -> availability_pct (for drawer)
    this.availabilityBreakdown = null; // last full /api/availability response
    this.availabilityPollInterval = null;

    this.availabilityLabel = document.getElementById('availabilityLabel');
    this.rangeChipsGroup = document.getElementById('rangeFilterChips');
    this.customRangePopover = document.getElementById('customRangePopover');
    this.customRangeFromEl = document.getElementById('customRangeFrom');
    this.customRangeToEl = document.getElementById('customRangeTo');
    this.customRangeErrorEl = document.getElementById('customRangeError');

    this.availabilityDetailBtn = document.getElementById('availabilityDetailBtn');
    this.availabilityBreakdownModal = document.getElementById('availabilityBreakdownModal');

    // In-flight request cancellation (AbortController), retry backoff state, and
    // a cheap "did the data actually change" signature to skip unnecessary
    // re-renders — see load()/loadAvailability()/_render().
    this._loadAbortController = null;
    this._availAbortController = null;
    this._instanceFailCount = 0;
    this._availFailCount = 0;
    this._retryTimers = { instances: null, availability: null };
    this._lastDataSignature = null;
    this._searchDebounceTimer = null;

    // Monotonic sequence number for /api/availability requests: guards
    // against a stale/superseded response overwriting newer state even in
    // the edge case where fetch resolves instead of rejecting on an aborted
    // signal (timing is browser-dependent).
    this._availRequestSeq = 0;

    this._bindEvents();
  }

  _bindEvents() {
    // Refresh button
    const refreshBtn = document.getElementById('refreshInstances');
    if (refreshBtn) {
      refreshBtn.addEventListener('click', () => {
        refreshBtn.classList.add('spinning');
        this.load().finally(() => refreshBtn.classList.remove('spinning'));
      });
    }

    // Interactive Summary Card Filtering (Rec #3)
    const summaryCards = document.querySelectorAll('.summary-card');
    if (summaryCards && summaryCards.length >= 4) {
      const statusMap = ['all', 'up', 'slow', 'down'];
      summaryCards.forEach((card, idx) => {
        if (idx < 4) {
          card.addEventListener('click', () => {
            this.activeStatus = statusMap[idx];
            if (this.chipGroup) {
              this.chipGroup.querySelectorAll('.chip').forEach(b => {
                b.classList.toggle('chip-active', b.dataset.status === this.activeStatus);
              });
            }
            this._lastDataSignature = null;
            this._render();
          });
        }
      });
    }

    // Search
    const clearSearchBtn = document.getElementById('clearSearchBtn');
    if (this.searchEl) {
      this.searchEl.addEventListener('input', () => {
        if (clearSearchBtn) {
          clearSearchBtn.classList.toggle('hidden', !this.searchEl.value.trim());
        }
        if (this._searchDebounceTimer) clearTimeout(this._searchDebounceTimer);
        this._searchDebounceTimer = setTimeout(() => {
          this._searchDebounceTimer = null;
          this.searchQ = this.searchEl.value.toLowerCase().trim();
          this._lastDataSignature = null;
          this._render();
        }, 180);
      });
    }

    if (clearSearchBtn && this.searchEl) {
      clearSearchBtn.addEventListener('click', () => {
        this.searchEl.value = '';
        this.searchQ = '';
        clearSearchBtn.classList.add('hidden');
        this._lastDataSignature = null;
        this._render();
        this.searchEl.focus();
      });
    }

    document.addEventListener('keydown', e => {
      if (e.key === 'Escape' && document.activeElement === this.searchEl) {
        this.searchEl.value = '';
        this.searchQ = '';
        if (clearSearchBtn) clearSearchBtn.classList.add('hidden');
        this._lastDataSignature = null;
        this._render();
        this.searchEl.blur();
      }
    });

    // Status filter chips
    if (this.chipGroup) {
      this.chipGroup.addEventListener('click', e => {
        const btn = e.target.closest('[data-status]');
        if (!btn) return;
        this.activeStatus = btn.dataset.status;
        this.chipGroup.querySelectorAll('.chip').forEach(b => {
          b.classList.toggle('chip-active', b.dataset.status === this.activeStatus);
        });
        this._lastDataSignature = null;
        this._render();
      });
    }

    // Modal & Target management
    const openBtn = document.getElementById('openAddTargetModalBtn');
    if (openBtn) openBtn.addEventListener('click', () => this._openModal());

    const closeBtn = document.getElementById('closeAddTargetModal');
    if (closeBtn) closeBtn.addEventListener('click', () => this._closeModal());

    const cancelBtn = document.getElementById('cancelAddTargetBtn');
    if (cancelBtn) cancelBtn.addEventListener('click', () => this._closeModal());

    const form = document.getElementById('addTargetForm');
    if (form) form.addEventListener('submit', (e) => this._submitAddTarget(e));

    const drawerDelBtn = document.getElementById('drawerDeleteBtn');
    if (drawerDelBtn) {
      drawerDelBtn.addEventListener('click', async () => {
        if (this.selectedTarget) {
          const ok = await this._deleteTarget(this.selectedTarget.instance);
          if (ok) this._closeDrawer();
        }
      });
    }

    // Maintenance Mode (Phase 9)
    const maintForm = document.getElementById('drawerMaintenanceForm');
    if (maintForm) {
      maintForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        if (!this.selectedTarget) return;
        const minutes = parseInt(document.getElementById('drawerMaintDuration')?.value, 10) || 60;
        const reason = document.getElementById('drawerMaintReasonInput')?.value.trim() || '';
        await this._startMaintenance(this.selectedTarget.instance, minutes, reason);
      });
    }
    const maintEndBtn = document.getElementById('drawerMaintEndBtn');
    if (maintEndBtn) {
      maintEndBtn.addEventListener('click', async () => {
        if (!this.selectedTarget) return;
        await this._endMaintenance(this.selectedTarget.maintenanceId, this.selectedTarget.instance);
      });
    }

    // Alert Correlation / Dependency (Phase 12)
    const depForm = document.getElementById('drawerDependencyForm');
    if (depForm) {
      depForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        if (!this.selectedTarget) return;
        const parent = document.getElementById('drawerDependencyParentSelect')?.value;
        if (!parent) return;
        await this._setDependency(this.selectedTarget.instance, parent);
      });
    }
    const depRemoveBtn = document.getElementById('drawerDependencyRemoveBtn');
    if (depRemoveBtn) {
      depRemoveBtn.addEventListener('click', async () => {
        if (!this.selectedTarget) return;
        await this._removeDependency(this.selectedTarget.dependencyId, this.selectedTarget.instance);
      });
    }

    // Acknowledge Alarm button
    const ackBtn = document.getElementById('ackAlarmBtn');
    if (ackBtn) {
      ackBtn.addEventListener('click', () => {
        const currentDownList = this.data.filter(t => t.health !== 'up').map(t => t.instance);
        this.acknowledgedDownInstances = new Set(currentDownList);
        this.isAcknowledged = true;
        if (this.monitor && typeof this.monitor.stopAlarm === 'function') {
          this.monitor.stopAlarm();
        }
        this._updateStats();
        this._triggerEventToast('Alarm Acknowledged by NOC operator');
      });
    }

    // Interval select
    const intervalSelect = document.getElementById('scrapeIntervalSelect');
    if (intervalSelect) {
      intervalSelect.addEventListener('change', e => {
        this.currentInterval = parseInt(e.target.value, 10) || 5000;
        this.startPolling(this.currentInterval);
        this.load();
      });
    }

    // Sort select filter
    const sortSelect = document.getElementById('sortSelect');
    if (sortSelect) {
      sortSelect.addEventListener('change', e => {
        this.activeSort = e.target.value;
        this._lastDataSignature = null;
        this._render();
      });
    }

    // Job select filter
    const jobSelect = document.getElementById('jobSelect');
    if (jobSelect) {
      jobSelect.addEventListener('change', e => {
        this.selectedJob = e.target.value;
        this._lastDataSignature = null;
        this.load();
        this.loadAvailability();
        this._updateJobDefaultUI();
      });
    }

    // Job custom dropdown + Default Job gear/popover (UI layer only; reuses
    // the jobSelect 'change' listener above for any actual filter change,
    // no separate filter logic).
    this._initJobFilterUI();

    // Period / time-range chips (24h / 7d / 30d / Custom Range)
    if (this.rangeChipsGroup) {
      this.rangeChipsGroup.addEventListener('click', e => {
        const btn = e.target.closest('[data-range]');
        if (!btn) return;
        const range = btn.dataset.range;

        if (range === 'custom') {
          this._toggleCustomRangePopover();
          return;
        }

        this._closeCustomRangePopover();
        this._setActiveRangeChip(range);

        if (range === 'realtime') {
          this.isRealtime = true;
          this.periodLabel = 'realtime';
          this.periodEnd = null;
          if (this.availabilityLabel) this.availabilityLabel.textContent = 'Availability (Realtime)';
          const drawerUptimeLabel = document.getElementById('drawerUptimeLabel');
          if (drawerUptimeLabel) drawerUptimeLabel.textContent = 'Uptime (Realtime)';
          const breakdownRangeEl = document.getElementById('availabilityBreakdownRange');
          if (breakdownRangeEl) breakdownRangeEl.textContent = '(Realtime)';
          const drawerSparklineRangeEl = document.getElementById('drawerSparklineRange');
          if (drawerSparklineRangeEl) drawerSparklineRangeEl.textContent = '(Realtime)';
          // Fleet-aggregate/lowest-availability breakdown is inherently a
          // time-weighted historical metric — doesn't apply to an instant
          // snapshot, so hide the Detail button rather than show stale data.
          if (this.availabilityDetailBtn) this.availabilityDetailBtn.style.display = 'none';
          // Reflect whatever the last live poll already fetched immediately,
          // instead of waiting for the next 5s/10s/30s tick.
          this._updateStats();
          if (this.selectedTarget) {
            this.loadTargetHistory(this.selectedTarget.instance);
          }
          return;
        }

        this.isRealtime = false;
        if (this.availabilityDetailBtn) this.availabilityDetailBtn.style.display = '';
        const presets = { '24h': 1440, '7d': 10080, '30d': 43200 };
        this.periodMinutes = presets[range] || 1440;
        this.periodEnd = null;
        this.periodLabel = range;
        this.loadAvailability();
        if (this.selectedTarget) {
          this.loadTargetHistory(this.selectedTarget.instance);
        }
      });
    }

    const customApplyBtn = document.getElementById('customRangeApplyBtn');
    if (customApplyBtn) {
      customApplyBtn.addEventListener('click', () => this._applyCustomRange());
    }
    const customCancelBtn = document.getElementById('customRangeCancelBtn');
    if (customCancelBtn) {
      customCancelBtn.addEventListener('click', () => this._closeCustomRangePopover());
    }

    // Close custom-range popover when clicking outside it
    document.addEventListener('click', e => {
      if (!this.customRangePopover || this.customRangePopover.classList.contains('hidden')) return;
      const wrap = e.target.closest('.range-select-wrap');
      if (!wrap) this._closeCustomRangePopover();
    });

    // Availability breakdown modal (Detail button on the Availability card)
    if (this.availabilityDetailBtn) {
      this.availabilityDetailBtn.addEventListener('click', () => this._openAvailabilityBreakdown());
    }
    const closeBreakdownBtn = document.getElementById('closeAvailabilityBreakdown');
    if (closeBreakdownBtn) {
      closeBreakdownBtn.addEventListener('click', () => this._closeAvailabilityBreakdown());
    }
    if (this.availabilityBreakdownModal) {
      this.availabilityBreakdownModal.addEventListener('click', e => {
        if (e.target === this.availabilityBreakdownModal) this._closeAvailabilityBreakdown();
      });
    }

    // Toast dismiss
    if (this.closeEventBannerBtn) {
      this.closeEventBannerBtn.addEventListener('click', () =>
        this.eventBanner?.classList.add('hidden')
      );
    }

    // Drawer close
    if (this.closeDrawerBtn) {
      this.closeDrawerBtn.addEventListener('click', () => this._closeDrawer());
    }
    if (this.sideDrawerOverlay) {
      this.sideDrawerOverlay.addEventListener('click', () => this._closeDrawer());
    }

    // Semi-Fullscreen Modal Tabs & Control Listeners
    const tabsNav = document.getElementById('modalTabsNav');
    if (tabsNav) {
      tabsNav.addEventListener('click', e => {
        const btn = e.target.closest('[data-tab]');
        if (!btn) return;
        this._switchModalTab(btn.dataset.tab);
      });
    }

    const drawerRefreshBtn = document.getElementById('drawerRefreshBtn');
    if (drawerRefreshBtn) {
      drawerRefreshBtn.addEventListener('click', () => {
        if (this.selectedTarget) {
          this._openDrawer(this.selectedTarget);
          this.loadAvailability();
        }
      });
    }

    const modalCloseBottomBtn = document.getElementById('modalCloseBottomBtn');
    if (modalCloseBottomBtn) {
      modalCloseBottomBtn.addEventListener('click', () => this._closeDrawer());
    }

    // Sparkline Time Range selector buttons (5m, 15m, 1h, 6h, 24h, 7d)
    const spRangeGroup = document.getElementById('spRangeGroup');
    if (spRangeGroup) {
      spRangeGroup.addEventListener('click', e => {
        const btn = e.target.closest('[data-range]');
        if (!btn) return;
        const range = btn.dataset.range;
        const presets = { '5m': 5, '15m': 15, '1h': 60, '6h': 360, '24h': 1440, '7d': 10080 };
        this.periodMinutes = presets[range] || 1440;
        this.periodLabel = range;
        this.periodEnd = null;
        this._sparklineZoomRange = null;
        
        spRangeGroup.querySelectorAll('.sp-range-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');

        const resetBtn = document.getElementById('sparklineResetZoomBtn');
        if (resetBtn) resetBtn.style.display = 'none';

        if (this.selectedTarget) {
          this.loadTargetHistory(this.selectedTarget.instance);
        }
      });
    }

    // Sparkline Reset Zoom button
    const spResetBtn = document.getElementById('sparklineResetZoomBtn');
    if (spResetBtn) {
      spResetBtn.addEventListener('click', () => {
        this._sparklineZoomRange = null;
        spResetBtn.style.display = 'none';
        if (Array.isArray(this._rawSparklinePoints)) {
          this._renderSparkline(this._rawSparklinePoints, true);
        }
      });
    }

    const btnViewAllEvents = document.getElementById('btnViewAllEvents');
    if (btnViewAllEvents) {
      btnViewAllEvents.addEventListener('click', () => this._switchModalTab('events'));
    }

    // Card click => open drawer; delegated keydown => D-pad nav + Enter/Space to open
    // (Escape/Back is handled globally — see the window-level BACK interceptor below.)
    if (this.table) {
      this.table.addEventListener('click', e => {
        const card = e.target.closest('.host-card');
        if (card) {
          const inst = card.dataset.instance;
          const target = this.data.find(t => t.instance === inst);
          if (target) this._openDrawer(target);
        }
      });
      this.table.addEventListener('keydown', e => this._onHostGridKeydown(e));
    }

    // Pagination controls
    if (this.prevPageBtn) {
      this.prevPageBtn.addEventListener('click', () => this._goToPage(this.currentPage - 1, true));
    }
    if (this.nextPageBtn) {
      this.nextPageBtn.addEventListener('click', () => this._goToPage(this.currentPage + 1, true));
    }
    if (this.pagesEl) {
      this.pagesEl.addEventListener('click', e => {
        const btn = e.target.closest('[data-page]');
        if (!btn) return;
        this._goToPage(parseInt(btn.dataset.page, 10), true);
      });
    }
    if (this.autoRotateBtn) {
      this.autoRotateBtn.addEventListener('click', () => {
        this.autoRotate = !this.autoRotate;
        this.autoRotateBtn.textContent = `Auto Rotate: ${this.autoRotate ? 'ON' : 'OFF'}`;
        this.autoRotateBtn.classList.toggle('hp-autorotate-on', this.autoRotate);
        this.autoRotateBtn.setAttribute('aria-pressed', String(this.autoRotate));
        if (this.autoRotate) this._startAutoRotate();
        else this._stopAutoRotate();
      });
    }
  }

  onActivate() {
    this.load();
    this.loadAvailability();
    this.startPolling(this.currentInterval);
    this.startAvailabilityPolling();
    this.startDownCounterTicker();
    if (this.autoRotate) this._startAutoRotate();
  }

  onDeactivate() {
    this.stopPolling();
    this.stopAvailabilityPolling();
    this.stopDownCounterTicker();
    this._stopAutoRotate();
  }

  /* ── Pagination (TV wallboard: max 80 cards/page) ── */
  _goToPage(page, manual) {
    const target = Math.max(1, Math.min(this._totalPages, page));
    if (manual) this._registerPageInteraction();
    if (target === this.currentPage) return;
    this.currentPage = target;
    this._renderPageWithTransition();
  }

  _renderPageWithTransition() {
    const rows = this._sortedRows || [];
    const startIdx = (this.currentPage - 1) * this.pageSize;
    const pageRows = rows.slice(startIdx, startIdx + this.pageSize);
    this._updatePaginationUI(rows.length, startIdx, pageRows.length);
    if (!this.table) return;
    // Fade transition (200-300ms) between pages — full DOM rebuild is expected
    // here since the card set genuinely changes, unlike the poll-time diff in _renderCards().
    this.table.classList.add('hg-fade-out');
    setTimeout(() => {
      this._renderCards(pageRows);
      this.table.classList.remove('hg-fade-out');
    }, 220);
  }

  _updatePaginationUI(total, startIdx, pageCount) {
    if (this.pageInfoEl) {
      this.pageInfoEl.textContent = total === 0
        ? 'Showing 0 of 0 hosts'
        : `Showing ${startIdx + 1}–${startIdx + pageCount} of ${total} hosts`;
    }
    if (this.paginationBar) this.paginationBar.classList.toggle('hidden', this._totalPages <= 1);
    if (this.prevPageBtn) this.prevPageBtn.disabled = this.currentPage <= 1;
    if (this.nextPageBtn) this.nextPageBtn.disabled = this.currentPage >= this._totalPages;
    if (this.pagesEl) {
      this.pagesEl.innerHTML = this._buildPageList(this._totalPages, this.currentPage).map(p =>
        p === '…'
          ? `<span class="hp-ellipsis">…</span>`
          : `<button class="hp-page-btn${p === this.currentPage ? ' hp-page-active' : ''}" data-page="${p}" type="button">${p}</button>`
      ).join('');
    }
  }

  _buildPageList(total, current) {
    const keep = new Set([1, total, current, current - 1, current + 1].filter(p => p >= 1 && p <= total));
    const sorted = Array.from(keep).sort((a, b) => a - b);
    const pages = [];
    let prev = 0;
    sorted.forEach(p => {
      if (prev && p - prev > 1) pages.push('…');
      pages.push(p);
      prev = p;
    });
    return pages;
  }

  /* ── Auto Rotate (TV mode) ── */
  _startAutoRotate() {
    this._clearAutoRotateTimers();
    this._autoRotateTimer = setInterval(() => {
      this.currentPage = this.currentPage >= this._totalPages ? 1 : this.currentPage + 1;
      this._renderPageWithTransition();
    }, 8000);
  }

  _stopAutoRotate() {
    this._clearAutoRotateTimers();
  }

  _clearAutoRotateTimers() {
    if (this._autoRotateTimer) { clearInterval(this._autoRotateTimer); this._autoRotateTimer = null; }
    if (this._autoRotateResumeTimer) { clearTimeout(this._autoRotateResumeTimer); this._autoRotateResumeTimer = null; }
  }

  // Manual page change pauses rotation, resumes after 30s of no interaction.
  _registerPageInteraction() {
    if (!this.autoRotate) return;
    if (this._autoRotateTimer) { clearInterval(this._autoRotateTimer); this._autoRotateTimer = null; }
    if (this._autoRotateResumeTimer) clearTimeout(this._autoRotateResumeTimer);
    this._autoRotateResumeTimer = setTimeout(() => {
      this._autoRotateResumeTimer = null;
      if (this.autoRotate) this._startAutoRotate();
    }, 30000);
  }

  startDownCounterTicker() {
    this.stopDownCounterTicker();
    this.downCounterInterval = setInterval(() => this._tickDownCounters(), 1000);
  }

  stopDownCounterTicker() {
    if (this.downCounterInterval) {
      clearInterval(this.downCounterInterval);
      this.downCounterInterval = null;
    }
  }

  _tickDownCounters() {
    if (!this.data || this.data.length === 0) return;
    const now = Date.now();

    if (this.table) {
      const cards = this.table.querySelectorAll('.host-card.hc-down');
      cards.forEach(card => {
        const inst = card.dataset.instance;
        const target = this.data.find(t => t.instance === inst);
        if (!target) return;

        let downMs = 0;
        if (target.downSince && target.downSince > 0) {
          downMs = Math.max(0, now - (target.downSince * 1000));
        } else {
          if (!this.downStartTimes[inst]) this.downStartTimes[inst] = now;
          downMs = Math.max(0, now - this.downStartTimes[inst]);
        }

        const latEl = card.querySelector('.hc-latency');
        if (latEl) {
          latEl.textContent = this._downLabel(target, this._fmtDownAging(downMs));
        }
      });

      const maintCards = this.table.querySelectorAll('.host-card.hc-maintenance');
      maintCards.forEach(card => {
        const inst = card.dataset.instance;
        const target = this.data.find(t => t.instance === inst);
        if (!target || !target.maintenanceUntil) return;
        const remainMs = Math.max(0, target.maintenanceUntil * 1000 - now);
        const latEl = card.querySelector('.hc-latency');
        if (latEl) latEl.textContent = `Maint ${this._fmtDownAging(remainMs)} left`;
      });
    }

    if (this.selectedTarget && this.selectedTarget.health !== 'up') {
      const drawerLatEl = document.getElementById('drawerLatency');
      if (drawerLatEl) {
        let downMs = 0;
        if (this.selectedTarget.downSince && this.selectedTarget.downSince > 0) {
          downMs = Math.max(0, now - (this.selectedTarget.downSince * 1000));
        } else {
          if (!this.downStartTimes[this.selectedTarget.instance]) this.downStartTimes[this.selectedTarget.instance] = now;
          downMs = Math.max(0, now - this.downStartTimes[this.selectedTarget.instance]);
        }
        drawerLatEl.textContent = `Down ${this._fmtDownAging(downMs)}`;
      }

      const ongoingLogEl = document.querySelector('#drawerLogsList .ongoing-duration-val[data-ongoing="true"]');
      if (ongoingLogEl) {
        const startTs = parseInt(ongoingLogEl.dataset.startTs, 10);
        if (startTs && !isNaN(startTs)) {
          const durationMs = Math.max(0, now - (startTs * 1000));
          ongoingLogEl.textContent = this._fmtDownAging(durationMs);
        }
      }
    }

    if (this.selectedTarget && this.selectedTarget.maintenance) {
      this._renderDrawerMaintenance(this.selectedTarget);
    }

    if (this.availabilityBreakdownModal && !this.availabilityBreakdownModal.classList.contains('hidden')) {
      this._renderAvailabilityBreakdown();
    }
  }

  /* ── Historical availability (past N days) ─────── */
  startAvailabilityPolling() {
    // Only clear the previous timer — see startPolling() above for why this
    // must not also abort the in-flight loadAvailability() call onActivate()
    // just made.
    if (this.availabilityPollInterval) clearInterval(this.availabilityPollInterval);
    // Poll availability every 15s from Prometheus historical PromQL range
    // queries. force:false — a wide range (7d/30d) can legitimately take
    // longer than 15s to answer; forcing an abort+restart on every tick
    // would kill the previous request before it ever lands, leaving the UI
    // stuck on stale/loading data forever. Skip the tick instead and let
    // the in-flight request finish.
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

  async loadAvailability(force = true) {
    // Realtime mode is driven entirely by the live /instances poll (see
    // _updateStats()) — no historical Prometheus aggregate to fetch here.
    if (this.isRealtime) return;

    // force=true (range/job/custom-range change, manual refresh, initial
    // load): a newer, more relevant request supersedes whatever's in
    // flight — cancel it and start fresh.
    // force=false (the 15s background poll tick): only start a new request
    // if the previous one has already finished. Never abort a still-valid
    // in-flight request just because a poll tick fired.
    if (this._availAbortController) {
      if (!force) return;
      this._availAbortController.abort();
    }
    const controller = new AbortController();
    this._availAbortController = controller;
    // Belt-and-suspenders stale guard: even if a response resolves instead
    // of rejecting after its controller was aborted (timing edge case), a
    // mismatched sequence number means a newer request owns the UI now.
    const seq = ++this._availRequestSeq;
    const isStale = () => seq !== this._availRequestSeq;

    const rangeText = this.periodLabel === 'custom' ? 'Custom' : this.periodLabel;

    if (this.availabilityLabel) this.availabilityLabel.textContent = `Availability (${rangeText})`;
    const drawerUptimeLabel = document.getElementById('drawerUptimeLabel');
    if (drawerUptimeLabel) drawerUptimeLabel.textContent = `Uptime (${rangeText})`;
    const breakdownRangeEl = document.getElementById('availabilityBreakdownRange');
    if (breakdownRangeEl) breakdownRangeEl.textContent = `(${rangeText})`;
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
      if (isStale()) return;

      if (!data.ok) {
        this._availFailCount = Math.min(this._availFailCount + 1, 6);
        this._scheduleRetry('availability');
        // Non-blocking: this.availabilityBreakdown (last-good SLA data) and
        // live monitoring are left exactly as they were — never zeroed out.
        return;
      }
      this._availFailCount = 0;
      this._clearRetry('availability');

      this.availabilityBreakdown = data;
      this.availabilityMap = data.targets || {};

      // Summary row: Total / Online / Warning / Offline / Availability — all
      // scoped to the selected period (owned exclusively by this call; live
      // /instances polling no longer writes to these DOM nodes).
      const counts = data.counts || {};
      if (this.statTotal) this.statTotal.textContent = counts.total ?? '—';
      if (this.statUp) this.statUp.textContent = counts.online ?? '—';
      const statSlow = document.getElementById('instSlow');
      if (statSlow) statSlow.textContent = counts.warning ?? '—';
      if (this.statDown) this.statDown.textContent = counts.offline ?? '—';

      const overall = (typeof data.overall === 'number') ? data.overall : null;
      if (this.statUptime) {
        this.statUptime.textContent = overall !== null ? `${overall.toFixed(2)}%` : '—';
      }

      // Refresh drawer uptime figure if a host is currently open
      if (this.selectedTarget) this._updateDrawerUptime(this.selectedTarget);

      // Refresh breakdown modal content if it's currently open
      if (this.availabilityBreakdownModal && !this.availabilityBreakdownModal.classList.contains('hidden')) {
        this._renderAvailabilityBreakdown();
      }
    } catch (e) {
      if (e.name === 'AbortError' || isStale()) return;
      // Keep last known values; availability is a secondary, best-effort metric
      console.warn('[InfraWatch] Availability fetch failed:', e);
      this._availFailCount = Math.min(this._availFailCount + 1, 6);
      this._scheduleRetry('availability');
    } finally {
      if (this._availAbortController === controller) this._availAbortController = null;
    }
  }

  _updateDrawerUptime(target) {
    const uptimeEl = document.getElementById('drawerUptimeVal');
    if (!uptimeEl) return;
    if (this.isRealtime) {
      uptimeEl.textContent = target.health === 'up' ? '100.00%' : '0.00%';
      return;
    }
    const pct = this.availabilityMap[target.instance];
    uptimeEl.textContent = (typeof pct === 'number') ? `${pct.toFixed(2)}%` : 'No data';
  }

  /* ── Period / range selector (toolbar) ─────────── */
  _setActiveRangeChip(range) {
    if (!this.rangeChipsGroup) return;
    this.rangeChipsGroup.querySelectorAll('.chip').forEach(b => {
      b.classList.toggle('chip-active', b.dataset.range === range);
    });
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
      showError('Pilih tanggal & waktu mulai dan akhir');
      return;
    }

    const fromDate = new Date(fromVal);
    const toDate = new Date(toVal);

    if (isNaN(fromDate.getTime()) || isNaN(toDate.getTime())) {
      showError('Pilih tanggal & waktu yang valid');
      return;
    }

    const minutes = (toDate.getTime() - fromDate.getTime()) / 60000;

    if (!(minutes > 0)) {
      showError('Rentang "Sampai" harus setelah "Dari"');
      return;
    }
    if (minutes > 90 * 1440) {
      showError('Rentang maksimum 90 hari');
      return;
    }
    if (toDate.getTime() > Date.now() + 60000) {
      showError('Rentang tidak boleh di masa depan');
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

  /* ── Availability breakdown modal (4 metrics, kept separate) ──── */
  _openAvailabilityBreakdown() {
    if (!this.availabilityBreakdownModal) return;
    this.availabilityBreakdownModal.classList.remove('hidden');
    if (this._untrapBreakdown) this._untrapBreakdown();
    this._untrapBreakdown = window.trapModalFocus(this.availabilityBreakdownModal);
    
    const currentMins = Math.round(this.periodMinutes);
    if (!this.availabilityBreakdown || Math.round(this.availabilityBreakdown.period_minutes || 0) !== currentMins) {
      this.loadAvailability();
    } else {
      this._renderAvailabilityBreakdown();
    }
  }

  _closeAvailabilityBreakdown() {
    if (this._untrapBreakdown) { this._untrapBreakdown(); this._untrapBreakdown = null; }
    if (this.availabilityBreakdownModal) this.availabilityBreakdownModal.classList.add('hidden');
  }

  // Single source of truth for SLA status: read the backend's own
  // COMPLIANT/NON_COMPLIANT/INSUFFICIENT_DATA verdict (which already
  // accounts for coverage, not just the raw availability number) instead of
  // re-deriving a pct-only threshold in the UI. A target with barely any
  // observed coverage must never render as compliant just because the
  // little data it has happens to look good.
  _slaBadgeInfo(entry) {
    const status = entry && entry.sla_status;
    if (status === 'COMPLIANT') return { cls: 'alt-ok', label: 'COMPLIANT' };
    if (status === 'NON_COMPLIANT') return { cls: 'alt-warning', label: 'NON-COMPLIANT' };
    if (status === 'INSUFFICIENT_DATA') return { cls: 'alt-insufficient', label: 'INSUFFICIENT DATA' };
    // Backend didn't send a verdict (older payload shape) — fall back to the
    // same eligibility rule the backend uses (coverage-gated), never a bare
    // pct threshold that could paint a low-coverage target green.
    const cov = typeof entry?.coverage_percent === 'number' ? entry.coverage_percent
      : (typeof entry?.coverage_pct === 'number' ? entry.coverage_pct : null);
    const avail = typeof entry?.availability_pct === 'number' ? entry.availability_pct : null;
    if (avail === null || cov === null || cov < 50) return { cls: 'alt-insufficient', label: 'INSUFFICIENT DATA' };
    return avail >= 99.9 ? { cls: 'alt-ok', label: 'COMPLIANT' } : { cls: 'alt-warning', label: 'NON-COMPLIANT' };
  }

  _renderAvailabilityBreakdown() {
    const data = this.availabilityBreakdown;
    const expectedMins = Math.round(this.periodMinutes);
    const isMatchingData = data && Math.round(data.period_minutes || 0) === expectedMins;

    const fmtPct = m => (isMatchingData && m && typeof m.value === 'number') ? `${m.value.toFixed(2)}%` : '—';

    const aggEl = document.getElementById('metricFleetAggregate');
    if (aggEl) aggEl.textContent = fmtPct(data?.fleet_aggregate);

    const avgEl = document.getElementById('metricFleetAverage');
    if (avgEl) avgEl.textContent = fmtPct(data?.fleet_average);

    const slaEl = document.getElementById('metricSlaCompliance');
    if (slaEl) slaEl.textContent = fmtPct(data?.sla_compliance_ratio || data?.sla_compliance);

    const healthEl = document.getElementById('metricHealthRatio');
    if (healthEl) healthEl.textContent = fmtPct(data?.health_ratio);

    const covRatioEl = document.getElementById('metricCoverageRatio');
    if (covRatioEl) covRatioEl.textContent = fmtPct(data?.coverage_ratio);

    const analytics = isMatchingData ? data?.analytics : null;
    const meanOutageEl = document.getElementById('metricMeanOutage');
    if (meanOutageEl) {
      const m = analytics && analytics.mean_outage_minutes;
      meanOutageEl.textContent = (typeof m === 'number') ? (m < 60 ? `${m.toFixed(1)}m` : `${(m / 60).toFixed(1)}h`) : '—';
    }

    const unstableTableEl = document.getElementById('mostUnstableTable');
    if (unstableTableEl) {
      const list = analytics && Array.isArray(analytics.most_unstable) ? analytics.most_unstable : [];
      if (!isMatchingData) {
        unstableTableEl.innerHTML = '<div class="de-empty">Memuat data histori...</div>';
      } else if (list.length === 0) {
        unstableTableEl.innerHTML = '<div class="de-empty">No incidents recorded</div>';
      } else {
        unstableTableEl.innerHTML = list.map((entry, i) => {
          const pct = typeof entry.availability_pct === 'number' ? entry.availability_pct.toFixed(2) + '%' : '—';
          const sla = this._slaBadgeInfo(entry);
          return `
            <div class="alt-row alt-row-ranked">
              <span class="alt-rank">#${i + 1}</span>
              <span class="alt-host">${this._esc(entry.name || entry.id || '—')}</span>
              <span class="alt-incidents">${entry.incidents} incident${entry.incidents === 1 ? '' : 's'}</span>
              <span class="sla-badge ${sla.cls}">${sla.label}</span>
              <span class="alt-pct ${sla.cls}">${pct}</span>
            </div>`;
        }).join('');
      }
    }

    const tableEl = document.getElementById('availabilityLowestTable');
    if (!tableEl) return;

    if (!isMatchingData) {
      tableEl.innerHTML = '<div class="de-empty">Memuat data histori server...</div>';
      return;
    }

    const lowest = (data && Array.isArray(data.lowest_availability)) ? data.lowest_availability : [];
    if (lowest.length === 0) {
      tableEl.innerHTML = '<div class="de-empty">All monitored servers currently have 100% availability.</div>';
      return;
    }

    tableEl.innerHTML = lowest.map(entry => {
      const pct = entry.availability_pct;
      const sla = this._slaBadgeInfo(entry);
      return `
        <div class="alt-row">
          <span class="alt-host">${this._esc(entry.name || entry.id || '—')}</span>
          <span class="sla-badge ${sla.cls}">${sla.label}</span>
          <span class="alt-pct ${sla.cls}">${typeof pct === 'number' ? pct.toFixed(2) + '%' : '—'}</span>
        </div>`;
    }).join('');

    const logTableEl = document.getElementById('availabilityIncidentLogTable');
    if (logTableEl) {
      const allEntries = (data && Array.isArray(data.entries)) ? data.entries : (data && data.summary && data.summary.per_server && Array.isArray(data.summary.per_server.values) ? data.summary.per_server.values : lowest);
      
      if (!allEntries || allEntries.length === 0) {
        logTableEl.innerHTML = '<div class="de-empty">No downtime incidents recorded</div>';
      } else {
        const nowMs = Date.now();
        const periodSec = (this.periodMinutes || 1440) * 60;

        logTableEl.innerHTML = allEntries.map(entry => {
          let pct = typeof entry.availability_pct === 'number' ? entry.availability_pct : 100;
          let incidents = entry.incidents || 0;
          let downMin = entry.downtime_minutes || 0;
          let downSec = Math.round(downMin * 60);

          const target = this.data.find(t => t.instance === (entry.name || entry.id));
          if (target && target.health !== 'up') {
            let liveDownSec = 0;
            if (target.downSince && target.downSince > 0) {
              liveDownSec = Math.max(0, (nowMs - target.downSince * 1000) / 1000);
            } else if (this.downStartTimes[target.instance]) {
              liveDownSec = Math.max(0, (nowMs - this.downStartTimes[target.instance]) / 1000);
            }
            if (liveDownSec > 0) {
              downSec = Math.max(downSec, Math.round(liveDownSec));
              downMin = downSec / 60.0;
              const livePct = Math.max(0, Math.min(100, ((periodSec - downSec) / periodSec) * 100));
              pct = Math.min(pct, livePct);
            }
          }

          const downText = downSec > 0 ? (downSec < 60 ? `${downSec}s` : `${downMin.toFixed(1)}m`) : '0s';
          // SLA badge comes from the backend's own historical verdict
          // (entry.sla_status) — never from `pct`, which above is blended
          // with the live ongoing-outage extension. Live status and
          // historical SLA status stay on separate signals.
          const sla = this._slaBadgeInfo(entry);

          return `
            <div class="alt-row" style="display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 0.4rem 0.75rem; padding: 0.5rem 0.75rem; border-bottom: 1px solid var(--border-subtle, rgba(255,255,255,0.05)); font-size: 0.85rem;">
              <div style="font-weight: 500; font-family: var(--font-mono, monospace); color: var(--fg-main); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; max-width: 100%;">${this._esc(entry.name || entry.id || '—')}</div>
              <div style="display: flex; flex-wrap: wrap; gap: 0.5rem 1rem; align-items: center;">
                <span style="color: var(--fg-muted); font-size: 0.8rem; white-space: nowrap;">Incidents: <strong style="color: ${incidents > 0 ? 'var(--critical)' : 'var(--success)'};">${incidents}</strong></span>
                <span style="color: var(--fg-muted); font-size: 0.8rem; white-space: nowrap;">Downtime: <strong style="color: ${downSec > 0 ? 'var(--critical)' : 'var(--success)'};">${downText}</strong></span>
                <span class="sla-badge ${sla.cls}">${sla.label}</span>
                <span class="alt-pct ${sla.cls}">${typeof pct === 'number' ? pct.toFixed(2) + '%' : '—'}</span>
              </div>
            </div>`;
        }).join('');
      }
    }
  }

  startPolling(ms) {
    // Only clear the previous timer here — not the full stopPolling(), which
    // also cancels any in-flight fetch. onActivate() calls load() and then
    // startPolling() in the same tick, so aborting here would kill the
    // request it just kicked off, leaving the grid on its skeleton loader
    // until the next tick instead of showing data as soon as it arrives.
    if (this.pollInterval) clearInterval(this.pollInterval);
    this.pollInterval = setInterval(() => this.load(), ms);
  }

  stopPolling() {
    if (this.pollInterval) {
      clearInterval(this.pollInterval);
      this.pollInterval = null;
    }
    this._clearRetry('instances');
    if (this._loadAbortController) {
      this._loadAbortController.abort();
      this._loadAbortController = null;
    }
  }

  /* ── Retry backoff helpers (shared by load()/loadAvailability()) ── */
  _clearRetry(kind) {
    if (this._retryTimers[kind]) {
      clearTimeout(this._retryTimers[kind]);
      this._retryTimers[kind] = null;
    }
  }

  _scheduleRetry(kind) {
    if (this._retryTimers[kind]) return; // a retry is already queued
    if (kind === 'instances' && this.pollInterval) {
      clearInterval(this.pollInterval);
      this.pollInterval = null;
    } else if (kind === 'availability' && this.availabilityPollInterval) {
      clearInterval(this.availabilityPollInterval);
      this.availabilityPollInterval = null;
    }
    const failCount = kind === 'instances' ? this._instanceFailCount : this._availFailCount;
    const delay = Math.min(1000 * (2 ** Math.max(0, failCount - 1)), 30000);
    this._retryTimers[kind] = setTimeout(() => {
      this._retryTimers[kind] = null;
      if (kind === 'instances') {
        this.load();
        this.startPolling(this.currentInterval);
      } else {
        this.loadAvailability();
        this.startAvailabilityPolling();
      }
    }, delay);
  }

  _computeDataSignature(targets) {
    let sig = `${this.activeSort || 'default'}|${this.selectedJob || 'all'}|${this.activeStatus || 'all'}|${this.searchQ || ''};`;
    for (let i = 0; i < targets.length; i++) {
      const t = targets[i];
      sig += t.instance + '|' + t.job + '|' + t.health + '|' + t.responseTimeMs + '|' + t.downSince + '|' + t.maintenance + '|' + t.suppressedBy + '|' + t.failureCategory + ';';
    }
    return sig;
  }

  async load() {
    // Cancel any still-in-flight /instances request (overlapping poll tick,
    // rapid filter change, endpoint switch, manual refresh) instead of letting
    // stale responses race with fresh ones.
    if (this._loadAbortController) this._loadAbortController.abort();
    const controller = new AbortController();
    this._loadAbortController = controller;

    try {
      let url = '/instances';
      if (this.selectedJob && this.selectedJob !== 'all') {
        url += `?job=${encodeURIComponent(this.selectedJob)}`;
      }
      const res = await fetch(url, { signal: controller.signal });
      const data = await res.json();

      if (!data.ok) {
        this._showError(data.error || 'Cannot reach Prometheus engine');
        this._instanceFailCount = Math.min(this._instanceFailCount + 1, 6);
        this._scheduleRetry('instances');
        return;
      }
      if (this.errorEl) this.errorEl.classList.add('hidden');
      this._instanceFailCount = 0;
      this._clearRetry('instances');

      // Update available jobs in dropdown if present
      if (Array.isArray(data.available_jobs) && document.getElementById('jobSelect')) {
        const jobSelect = document.getElementById('jobSelect');
        const existingOptions = Array.from(jobSelect.options).map(o => o.value);

        data.available_jobs.forEach(j => {
          if (!existingOptions.includes(j)) {
            const opt = document.createElement('option');
            opt.value = j;
            opt.textContent = `Job: ${j}`;
            jobSelect.appendChild(opt);
          }
        });

        this._syncJobDropdownOptions();
        this._restoreDefaultJob(jobSelect);
      }

      const newTargets = data.targets || [];
      this._checkStateTransitions(newTargets);
      this.data = newTargets;
      this._updateStats();

      // Skip the (relatively) expensive filter/sort/DOM-diff pass when the
      // fleet's health/latency data is byte-identical to the last render —
      // common at steady state, and the main win at thousands of targets.
      const sig = this._computeDataSignature(newTargets);
      if (sig !== this._lastDataSignature) {
        this._lastDataSignature = sig;
        this._render();
      }
      if (this.metaEl) this.metaEl.textContent = `Updated ${new Date().toLocaleTimeString()}`;
    } catch (e) {
      if (e.name === 'AbortError') return;
      this._showError(e.message);
      this._instanceFailCount = Math.min(this._instanceFailCount + 1, 6);
      this._scheduleRetry('instances');
    } finally {
      if (this._loadAbortController === controller) this._loadAbortController = null;
    }
  }

  // Job Filter custom dropdown + Default Job gear/popover — one controller,
  // since both float off the same wrap and share outside-click/Escape
  // handling. #jobSelect (native, hidden via CSS) stays the single state
  // holder: every existing load()/loadAvailability()/_restoreDefaultJob
  // codepath keeps reading jobSelect.value/.options and listening for
  // 'change' on it unmodified — this controller only ever drives that same
  // element and never re-implements filtering.
  _initJobFilterUI() {
    const wrap = document.getElementById('jobSelectWrap');
    const jobSelect = document.getElementById('jobSelect');
    const ddTrigger = document.getElementById('jobDdTrigger');
    const ddMenu = document.getElementById('jobDdMenu');
    const ddLabel = document.getElementById('jobDdLabel');
    const gearBtn = document.getElementById('jobDefaultSettingsBtn');
    const popover = document.getElementById('jobDefaultPopover');
    const setRow = document.getElementById('jdpSetRow');
    const setCheckbox = document.getElementById('jdpSetCheckbox');
    const resetBtn = document.getElementById('jdpResetBtn');
    const badge = document.getElementById('jobDefaultBadge');
    if (!wrap || !jobSelect || !ddTrigger || !ddMenu || !gearBtn || !popover) return;

    this._jobFilterEls = { wrap, jobSelect, ddTrigger, ddMenu, ddLabel, gearBtn, popover, setRow, setCheckbox, resetBtn, badge };

    // ── Custom dropdown (UI layer only — see _selectJobDdOption) ──
    ddTrigger.addEventListener('click', e => {
      e.stopPropagation();
      if (ddMenu.classList.contains('hidden')) this._openJobDropdown();
      else this._closeJobDropdown();
    });
    ddTrigger.addEventListener('keydown', e => {
      if (e.key === 'ArrowDown' || e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        this._openJobDropdown();
      }
    });
    ddMenu.addEventListener('click', e => {
      const li = e.target.closest('.job-dd-option');
      if (li) this._selectJobDdOption(li);
    });
    ddMenu.addEventListener('keydown', e => this._onJobDdMenuKeydown(e));

    // ── Default Job gear + popover ──
    gearBtn.addEventListener('click', e => {
      e.stopPropagation();
      if (popover.classList.contains('hidden')) this._openJobDefaultPopover();
      else this._closeJobDefaultPopover();
    });

    if (setCheckbox) {
      setCheckbox.addEventListener('change', () => {
        if (!setCheckbox.checked) return; // clearing the default only happens via Reset
        try { localStorage.setItem(JOB_DEFAULT_LS_KEY, this.selectedJob); } catch (e) { }
        this._triggerEventToast('Default Job saved.');
        this._updateJobDefaultUI();
        this._closeJobDefaultPopover();
      });
    }

    if (resetBtn) {
      resetBtn.addEventListener('click', () => {
        try { localStorage.removeItem(JOB_DEFAULT_LS_KEY); } catch (e) { }
        if (jobSelect.value !== 'all') {
          jobSelect.value = 'all';
          jobSelect.dispatchEvent(new Event('change')); // reuses the one filter code path
        } else {
          this._updateJobDefaultUI();
        }
        this._triggerEventToast('Default Job cleared.');
        this._closeJobDefaultPopover();
      });
    }

    // Click outside / Escape closes whichever of the two floats is open —
    // neither is a modal, both stay lightweight popovers.
    document.addEventListener('click', e => {
      if (wrap.contains(e.target)) return;
      this._closeJobDropdown();
      this._closeJobDefaultPopover();
    });
    document.addEventListener('keydown', e => {
      if (e.key !== 'Escape') return;
      if (!ddMenu.classList.contains('hidden')) { this._closeJobDropdown(); ddTrigger.focus(); }
      if (!popover.classList.contains('hidden')) this._closeJobDefaultPopover();
    });

    this._syncJobDropdownSelection();
    this._updateJobDefaultUI();
  }

  /* ── Custom Job dropdown — pure UI, drives #jobSelect + its 'change' ── */
  _openJobDropdown() {
    const els = this._jobFilterEls;
    if (!els) return;
    this._closeJobDefaultPopover();
    els.ddMenu.classList.remove('hidden');
    els.ddTrigger.setAttribute('aria-expanded', 'true');
    const current = els.ddMenu.querySelector('[aria-selected="true"]') || els.ddMenu.firstElementChild;
    this._setActiveJobDdOption(current);
    els.ddMenu.focus();
  }

  _closeJobDropdown() {
    const els = this._jobFilterEls;
    if (!els || els.ddMenu.classList.contains('hidden')) return;
    els.ddMenu.classList.add('hidden');
    els.ddTrigger.setAttribute('aria-expanded', 'false');
  }

  _setActiveJobDdOption(li) {
    const els = this._jobFilterEls;
    if (!els || !li) return;
    els.ddMenu.querySelectorAll('.job-dd-option-active').forEach(el => el.classList.remove('job-dd-option-active'));
    li.classList.add('job-dd-option-active');
    li.scrollIntoView({ block: 'nearest' });
  }

  _onJobDdMenuKeydown(e) {
    const els = this._jobFilterEls;
    if (!els) return;
    const options = Array.from(els.ddMenu.children);
    if (!options.length) return;
    const activeIdx = Math.max(0, options.findIndex(li => li.classList.contains('job-dd-option-active')));

    switch (e.key) {
      case 'ArrowDown':
        e.preventDefault();
        this._setActiveJobDdOption(options[Math.min(options.length - 1, activeIdx + 1)]);
        break;
      case 'ArrowUp':
        e.preventDefault();
        this._setActiveJobDdOption(options[Math.max(0, activeIdx - 1)]);
        break;
      case 'Home':
        e.preventDefault();
        this._setActiveJobDdOption(options[0]);
        break;
      case 'End':
        e.preventDefault();
        this._setActiveJobDdOption(options[options.length - 1]);
        break;
      case 'Enter':
      case ' ':
        e.preventDefault();
        this._selectJobDdOption(options[activeIdx]);
        break;
      case 'Escape':
        e.preventDefault();
        this._closeJobDropdown();
        els.ddTrigger.focus();
        break;
      case 'Tab':
        this._closeJobDropdown();
        break;
    }
  }

  // The only place a dropdown click/keypress turns into a filter change —
  // sets the hidden native select's value and dispatches 'change' on it,
  // which the existing jobSelect listener in _bindEvents picks up exactly
  // as it did when that select was visible. No parallel filtering logic.
  _selectJobDdOption(li) {
    const els = this._jobFilterEls;
    if (!els || !li) return;
    if (els.jobSelect.value !== li.dataset.value) {
      els.jobSelect.value = li.dataset.value;
      els.jobSelect.dispatchEvent(new Event('change'));
    }
    this._closeJobDropdown();
    els.ddTrigger.focus();
  }

  // Rebuilds the <li> option list from the hidden <select>'s <option>s
  // (itself populated by load()'s available_jobs merge) — never a second
  // source of truth for what jobs exist.
  _syncJobDropdownOptions() {
    const els = this._jobFilterEls;
    if (!els) return;
    const existing = new Set(Array.from(els.ddMenu.children).map(li => li.dataset.value));
    Array.from(els.jobSelect.options).forEach(opt => {
      if (existing.has(opt.value)) return;
      const li = document.createElement('li');
      li.setAttribute('role', 'option');
      li.className = 'job-dd-option';
      li.dataset.value = opt.value;
      li.textContent = opt.textContent;
      els.ddMenu.appendChild(li);
    });
    this._syncJobDropdownSelection();
  }

  // Reflects #jobSelect's current value into the trigger label and the
  // menu's aria-selected state.
  _syncJobDropdownSelection() {
    const els = this._jobFilterEls;
    if (!els) return;
    const selectedOpt = els.jobSelect.options[els.jobSelect.selectedIndex];
    if (els.ddLabel) els.ddLabel.textContent = selectedOpt ? selectedOpt.textContent : 'Semua Job';
    Array.from(els.ddMenu.children).forEach(li => {
      li.setAttribute('aria-selected', String(li.dataset.value === els.jobSelect.value));
    });
  }

  /* ── Default Job gear popover ── */
  _openJobDefaultPopover() {
    const els = this._jobFilterEls;
    if (!els) return;
    this._closeJobDropdown();
    this._updateJobDefaultUI();
    els.popover.classList.remove('hidden');
    els.gearBtn.setAttribute('aria-expanded', 'true');
  }

  _closeJobDefaultPopover() {
    const els = this._jobFilterEls;
    if (!els || els.popover.classList.contains('hidden')) return;
    els.popover.classList.add('hidden');
    els.gearBtn.setAttribute('aria-expanded', 'false');
  }

  // Reflects whether the currently selected Job is the saved default:
  // toggles the trigger's "Default" badge and shows/hides the "Set as
  // Default" row in the popover (hidden when already default, per spec).
  // Also keeps the dropdown label/selection in sync, since both change
  // together whenever selectedJob changes.
  _updateJobDefaultUI() {
    const els = this._jobFilterEls;
    if (!els) return;
    this._syncJobDropdownSelection();

    let stored = null;
    try { stored = localStorage.getItem(JOB_DEFAULT_LS_KEY); } catch (e) { }
    const isDefault = !!stored && stored === this.selectedJob;

    if (els.badge) els.badge.classList.toggle('hidden', !isDefault);
    if (els.setRow) els.setRow.classList.toggle('hidden', isDefault);
    if (els.setCheckbox) els.setCheckbox.checked = isDefault;
  }

  // Startup restore of a saved default Job — runs once, first successful
  // load() only (jobSelect must already be populated). Fires the same
  // 'change' event the manual dropdown handler listens on (see _bindEvents)
  // instead of duplicating the load()/loadAvailability() filter logic.
  _restoreDefaultJob(jobSelect) {
    if (this._defaultJobRestored) return;
    this._defaultJobRestored = true;

    let stored = null;
    try { stored = localStorage.getItem(JOB_DEFAULT_LS_KEY); } catch (e) { }
    if (!stored) return;

    const exists = Array.from(jobSelect.options).some(o => o.value === stored);
    if (!exists) {
      try { localStorage.setItem(JOB_DEFAULT_LS_KEY, 'all'); } catch (e) { }
      return;
    }

    if (jobSelect.value !== stored) {
      jobSelect.value = stored;
      jobSelect.dispatchEvent(new Event('change')); // also triggers _updateJobDefaultUI via the change listener
    } else {
      this._updateJobDefaultUI();
    }
  }

  _checkStateTransitions(newTargets) {
    const now = Date.now();
    newTargets.forEach(t => {
      const prev = this.previousStates[t.instance];
      const curr = t.health || 'unknown';
      if (curr !== 'up') {
        if (t.downSince && t.downSince > 0) {
          this.downStartTimes[t.instance] = t.downSince * 1000;
        } else if (!this.downStartTimes[t.instance]) {
          this.downStartTimes[t.instance] = now;
        }
      } else {
        delete this.downStartTimes[t.instance];
      }

      if (prev && prev !== curr) {
        const label = curr === 'up' ? 'back online' : 'went offline';
        this._triggerEventToast(`${t.instance} ${label}`);
      }
      this.previousStates[t.instance] = curr;
    });
  }

  _fmtDownAging(ms) {
    if (!ms || ms <= 0) return '0s';
    const sec = Math.floor(ms / 1000);
    if (sec < 60) return `${sec}s`;
    if (sec < 3600) {
      const m = Math.floor(sec / 60);
      const s = sec % 60;
      return `${m}m ${s}s`;
    }
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    return `${h}h ${m}m`;
  }

  // Single place the wallboard card's down-state text is composed — reused by
  // _tickDownCounters() (per-second update) and both _renderCards() branches,
  // so the classifier's category (from /instances' failureCategory, backed by
  // classify_scrape_failure() in app.py) never has to be re-derived client-side.
  _downLabel(t, agingStr) {
    if (t.suppressedBy) return `↳ via ${t.suppressedBy}`;
    const cat = t.failureCategory;
    return (cat && cat !== 'Unknown') ? `${cat} · ${agingStr}` : `Down ${agingStr}`;
  }

  _triggerEventToast(msg) {
    if (!this.eventBanner || !this.eventBannerText) return;
    this.eventBannerText.textContent = msg;
    this.eventBanner.classList.remove('hidden');
    setTimeout(() => this.eventBanner?.classList.add('hidden'), 6000);
  }

  _showError(msg) {
    if (this.errorEl) this.errorEl.classList.remove('hidden');
    if (this.errorMsg) this.errorMsg.textContent = msg || 'Engine Prometheus tidak dapat dijangkau';
    // Preserve existing table layout and host cards so previously loaded data remains visible!
    if (this.table && (!this.data || this.data.length === 0)) {
      this.table.innerHTML = `
        <div class="empty-state">
          <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
          <span>Menunggu data Prometheus — ${this._esc(msg)}</span>
        </div>`;
    }
  }

  _updateStats() {
    // NOTE: total/up/down/slow here are the LIVE snapshot from /instances,
    // used to drive the alarm/ack/health-badge logic below (which must
    // always react to what's happening right now, regardless of the
    // selected history period). Normally the Total/Online/Warning/Offline/
    // Availability numbers on the summary cards are owned exclusively by
    // loadAvailability() and reflect the historical period selected in the
    // toolbar range chips — except in Realtime mode, where this same live
    // snapshot also drives those cards directly (see block below).
    const total = this.data.length;
    const up = this.data.filter(t => t.health === 'up').length;
    const down = total - up;
    const slow = this.data.filter(t => t.health === 'up' && t.responseTimeMs > 500).length;

    // Alarm-eligible downs — excludes hosts under an active maintenance
    // window (Phase 9). `down`/`statDown` above stay factual (a maintenance
    // host that's actually down still counts there); this narrower list
    // drives everything that would otherwise page/annoy an operator for
    // planned work: the ack button, the "NEW outage" toast, the health
    // pill, and the audio alarm.
    const alarmableDown = this.data.filter(t => t.health !== 'up' && !t.maintenance);

    // Realtime mode OR initial fast fill (eliminates '—' skeleton lines on page load)
    if (this.isRealtime || (this.statTotal && (this.statTotal.textContent === '—' || !this.statTotal.textContent.trim()))) {
      const onlineHealthy = up - slow;
      if (this.statTotal) this.statTotal.textContent = total;
      if (this.statUp) this.statUp.textContent = onlineHealthy;
      const statSlow = document.getElementById('instSlow');
      if (statSlow) statSlow.textContent = slow;
      if (this.statDown) this.statDown.textContent = down;
      if (this.statUptime && (this.statUptime.textContent === '—' || !this.statUptime.textContent.trim())) {
        const pct = total > 0 ? (up / total) * 100 : 0;
        this.statUptime.textContent = `${pct.toFixed(2)}%`;
      }
      // Keep an open drawer's uptime figure live too.
      if (this.selectedTarget) {
        const fresh = this.data.find(t => t.instance === this.selectedTarget.instance) || this.selectedTarget;
        this._updateDrawerUptime(fresh);
      }
    }

    // Last probe time
    if (this.lastProbe) {
      this.lastProbe.textContent = `Updated ${new Date().toLocaleTimeString()}`;
    }

    // Acknowledge Button & Global health badge
    const ackBtn = document.getElementById('ackAlarmBtn');
    const ackLabel = document.getElementById('ackBtnLabel');

    const currentDownList = alarmableDown.map(t => t.instance);
    const hasNewDownTarget = currentDownList.some(inst => !this.acknowledgedDownInstances.has(inst));

    // Wallboard "critical spotlight" (Phase 14) — jump to a newly-down host
    // once, the moment it appears, instead of re-jumping every poll tick.
    if (this.monitor && typeof this.monitor.spotlightHost === 'function') {
      const freshlyDown = currentDownList.filter(inst => !this._spotlightedInstances.has(inst));
      currentDownList.forEach(inst => this._spotlightedInstances.add(inst));
      if (freshlyDown.length > 0) this.monitor.spotlightHost(freshlyDown[0]);
    }
    if (currentDownList.length === 0) this._spotlightedInstances.clear();

    if (this.isAcknowledged && hasNewDownTarget) {
      this.isAcknowledged = false;
      if (this.monitor && typeof this.monitor.resetOutageAlarm === 'function') {
        this.monitor.resetOutageAlarm();
      }
      const newDownList = currentDownList.filter(inst => !this.acknowledgedDownInstances.has(inst));
      const downListToShow = newDownList.length > 0 ? newDownList : currentDownList;
      const ipStr = downListToShow.length > 3
        ? `${downListToShow.slice(0, 3).join(', ')} (+${downListToShow.length - 3} others)`
        : downListToShow.join(', ');
      this._triggerEventToast(`NEW outage detected on ${ipStr}! Alarm re-triggered.`);
    }

    if (alarmableDown.length > 0 || slow > 0) {
      if (ackBtn) {
        ackBtn.classList.remove('hidden');
        if (this.isAcknowledged) {
          ackBtn.className = 'ack-alarm-btn ack-done';
          if (ackLabel) ackLabel.textContent = '✓ Acknowledged';
        } else {
          ackBtn.className = 'ack-alarm-btn ack-alert';
          if (ackLabel) ackLabel.textContent = 'Acknowledge Alarm';
        }
      }
    } else {
      // All clear => Hide acknowledge button and reset ack state
      this.isAcknowledged = false;
      this.acknowledgedDownInstances.clear();
      if (this.monitor && typeof this.monitor.resetOutageAlarm === 'function') {
        this.monitor.resetOutageAlarm();
      }
      if (ackBtn) ackBtn.classList.add('hidden');
    }

    // Global health badge & Alarm Trigger
    if (alarmableDown.length > 0) {
      if (this.healthBadge) {
        this.healthBadge.className = 'status-pill pill-critical';
        this.healthBadge.innerHTML = '<span class="pill-dot"></span><span class="pill-label">Critical</span>';
      }
      if (!this.isAcknowledged && this.monitor && typeof this.monitor.playAlarm === 'function') {
        this.monitor.playAlarm();
      }
    } else {
      if (this.healthBadge) {
        if (slow > 0) {
          this.healthBadge.className = 'status-pill pill-degraded';
          this.healthBadge.innerHTML = '<span class="pill-dot"></span><span class="pill-label">Degraded</span>';
        } else {
          this.healthBadge.className = 'status-pill pill-healthy';
          this.healthBadge.innerHTML = '<span class="pill-dot"></span><span class="pill-label">Healthy</span>';
        }
      }
      if (this.monitor && typeof this.monitor.stopAlarm === 'function') {
        this.monitor.stopAlarm();
      }
    }

    // Live indicator
    const liveDot = document.getElementById('liveDot');
    if (liveDot) liveDot.style.background = alarmableDown.length > 0 ? 'var(--critical)' : 'var(--success)';

    // statusMeta (topnav)
    const meta = document.getElementById('statusMeta');
    if (meta) meta.textContent = new Date().toLocaleTimeString();

    // Maintenance nav badge — derived from the /instances payload already
    // fetched above, no extra request.
    const maintBadge = document.getElementById('maintenanceNavBadge');
    if (maintBadge) {
      const activeCount = this.data.filter(t => t.maintenance).length;
      maintBadge.textContent = activeCount;
      maintBadge.classList.toggle('hidden', activeCount === 0);
    }
  }

  _render() {
    let rows = this.data;

    // Status filter (Warning threshold = 500ms)
    if (this.activeStatus === 'up') {
      rows = rows.filter(t => t.health === 'up' && !(t.responseTimeMs > 500));
    } else if (this.activeStatus === 'down') {
      rows = rows.filter(t => t.health !== 'up');
    } else if (this.activeStatus === 'slow') {
      rows = rows.filter(t => t.health === 'up' && t.responseTimeMs > 500);
    }

    // Search filter
    if (this.searchQ) {
      rows = rows.filter(t => t.instance.toLowerCase().includes(this.searchQ));
    }

    if (this.countBadge) this.countBadge.textContent = rows.length;
    if (!this.table) return;

    if (rows.length === 0) {
      this.table.innerHTML = `
        <div class="empty-state">
          <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="11" cy="11" r="8"/><path d="M21 21l-4.35-4.35"/></svg>
          <span>No hosts match the current filter</span>
        </div>`;
      return;
    }

    // Sort options: default (Prioritas: Down Pertama), name_asc, name_desc, job_asc, job_desc, latency_desc, latency_asc
    rows = [...rows].sort((a, b) => {
      // Down hosts are always pinned first, regardless of sort mode, so an
      // outage never scrolls off-screen on a filtered/sorted wallboard view.
      // Maintenance-down is expected, not an outage, so it doesn't jump the queue.
      const aDown = (a.health !== 'up' && !a.maintenance) ? 1 : 0;
      const bDown = (b.health !== 'up' && !b.maintenance) ? 1 : 0;
      if (aDown !== bDown) return bDown - aDown;

      const sortMode = this.activeSort || 'default';
      if (sortMode === 'name_asc') {
        return a.instance.localeCompare(b.instance, undefined, { numeric: true, sensitivity: 'base' });
      } else if (sortMode === 'name_desc') {
        return b.instance.localeCompare(a.instance, undefined, { numeric: true, sensitivity: 'base' });
      } else if (sortMode === 'job_asc') {
        const cmp = (a.job || '').localeCompare(b.job || '', undefined, { numeric: true, sensitivity: 'base' });
        if (cmp !== 0) return cmp;
        return a.instance.localeCompare(b.instance, undefined, { numeric: true, sensitivity: 'base' });
      } else if (sortMode === 'job_desc') {
        const cmp = (b.job || '').localeCompare(a.job || '', undefined, { numeric: true, sensitivity: 'base' });
        if (cmp !== 0) return cmp;
        return a.instance.localeCompare(b.instance, undefined, { numeric: true, sensitivity: 'base' });
      } else if (sortMode === 'latency_desc') {
        const al = a.health !== 'up' ? 999999 : (a.responseTimeMs || 0);
        const bl = b.health !== 'up' ? 999999 : (b.responseTimeMs || 0);
        if (al !== bl) return bl - al;
        return a.instance.localeCompare(b.instance, undefined, { numeric: true, sensitivity: 'base' });
      } else if (sortMode === 'latency_asc') {
        const al = a.health !== 'up' ? 999999 : (a.responseTimeMs || 0);
        const bl = b.health !== 'up' ? 999999 : (b.responseTimeMs || 0);
        if (al !== bl) return al - bl;
        return a.instance.localeCompare(b.instance, undefined, { numeric: true, sensitivity: 'base' });
      } else {
        // default: Prioritas (Down Pertama, lalu slow >500ms, lalu online)
        const ao = a.health !== 'up' ? 2 : (a.responseTimeMs > 500 ? 1 : 0);
        const bo = b.health !== 'up' ? 2 : (b.responseTimeMs > 500 ? 1 : 0);
        if (ao !== bo) return bo - ao;
        return a.instance.localeCompare(b.instance, undefined, { numeric: true, sensitivity: 'base' });
      }
    });

    this._sortedRows = rows;

    // Paginate — max 80 cards/page for the TV wallboard (see PAGINATION
    // REQUIREMENTS). Clamp instead of resetting to page 1 so polling/live
    // updates never yank the operator off the page they're viewing.
    this._totalPages = Math.max(1, Math.ceil(rows.length / this.pageSize));
    this.currentPage = Math.min(Math.max(1, this.currentPage), this._totalPages);
    const startIdx = (this.currentPage - 1) * this.pageSize;
    const pageRows = rows.slice(startIdx, startIdx + this.pageSize);
    this._updatePaginationUI(rows.length, startIdx, pageRows.length);

    this._renderCards(pageRows);
  }

  // Renders exactly the given (already paginated) rows into the grid, with
  // an in-place DOM diff to avoid flicker on same-page poll refreshes.
  _renderCards(rows) {
    const now = Date.now();
    const existingDomCards = Array.from(this.table.querySelectorAll('.host-card'));
    const existingInstances = existingDomCards.map(c => c.dataset.instance);
    const newInstances = rows.map(r => r.instance);

    // Smart In-Place Update to completely eliminate refresh flicker!
    const structureMatches = existingDomCards.length === rows.length &&
      existingInstances.every((inst, idx) => inst === newInstances[idx]);

    if (structureMatches) {
      rows.forEach((t, i) => {
        const card = existingDomCards[i];
        const isUp = t.health === 'up';
        const isSlow = isUp && t.responseTimeMs > 500;
        const isDown = !isUp;

        const stateClass = t.maintenance ? 'hc-maintenance' : (isDown ? 'hc-down' : (isSlow ? 'hc-slow' : 'hc-up'));
        const isAcked = isDown && !t.maintenance && this.acknowledgedDownInstances.has(t.instance);
        const fullClass = `host-card ${stateClass}${t.suppressedBy ? ' hc-suppressed' : ''}${isAcked ? ' hc-acked' : ''}`;
        if (card.className !== fullClass) {
          card.className = fullClass;
        }

        let latencyText = '';
        if (t.maintenance) {
          const remainMs = t.maintenanceUntil ? Math.max(0, t.maintenanceUntil * 1000 - now) : 0;
          latencyText = `Maint ${this._fmtDownAging(remainMs)} left`;
        } else if (isDown) {
          let downMs = 0;
          if (t.downSince && t.downSince > 0) {
            downMs = Math.max(0, now - (t.downSince * 1000));
          } else if (this.downStartTimes[t.instance]) {
            downMs = Math.max(0, now - this.downStartTimes[t.instance]);
          }
          latencyText = this._downLabel(t, this._fmtDownAging(downMs));
        } else {
          latencyText = t.responseTimeMs ? `${t.responseTimeMs} ms` : '< 1 ms';
        }

        const ipEl = card.querySelector('.hc-ip') || card.children[0];
        if (ipEl && ipEl.textContent !== t.instance) ipEl.textContent = t.instance;

        const latEl = card.querySelector('.hc-latency') || card.children[1];
        if (latEl && latEl.textContent !== latencyText) latEl.textContent = latencyText;

        const jobEl = card.querySelector('.hc-job');
        if (jobEl) {
          const jobText = t.job || 'blackbox';
          if (jobEl.textContent !== jobText) jobEl.textContent = jobText;
        }
      });
    } else {
      const focusedCard = document.activeElement ? document.activeElement.closest('.host-card') : null;
      const focusedInst = focusedCard ? focusedCard.dataset.instance : null;

      // Re-render only when structure/filter changes
      this.table.innerHTML = rows.map((t, i) => {
        const isUp = t.health === 'up';
        const isSlow = isUp && t.responseTimeMs > 500;
        const isDown = !isUp;

        const stateClass = t.maintenance ? 'hc-maintenance' : (isDown ? 'hc-down' : (isSlow ? 'hc-slow' : 'hc-up'));
        const isAcked = isDown && !t.maintenance && this.acknowledgedDownInstances.has(t.instance);
        const fullClass = `host-card ${stateClass}${t.suppressedBy ? ' hc-suppressed' : ''}${isAcked ? ' hc-acked' : ''}`;
        let latencyText = '';
        if (t.maintenance) {
          const remainMs = t.maintenanceUntil ? Math.max(0, t.maintenanceUntil * 1000 - now) : 0;
          latencyText = `Maint ${this._fmtDownAging(remainMs)} left`;
        } else if (isDown) {
          let downMs = 0;
          if (t.downSince && t.downSince > 0) {
            downMs = Math.max(0, now - (t.downSince * 1000));
          } else if (this.downStartTimes[t.instance]) {
            downMs = Math.max(0, now - this.downStartTimes[t.instance]);
          }
          latencyText = this._downLabel(t, this._fmtDownAging(downMs));
        } else {
          latencyText = t.responseTimeMs ? `${t.responseTimeMs} ms` : '< 1 ms';
        }

        return `<div class="${fullClass}"
                     data-instance="${this._esc(t.instance)}"
                     role="listitem"
                     tabindex="0"
                     aria-label="${this._esc(t.instance)} — ${t.maintenance ? 'Under maintenance' : (isDown ? (t.suppressedBy ? `Offline, correlated with ${t.suppressedBy}` : 'Offline') : (isSlow ? 'Slow' : 'Online'))}"
                     title="Click to view details or delete target">
          <div class="hc-ip">${this._esc(t.instance)}</div>
          <div class="hc-latency">${this._esc(latencyText)}</div>
        </div>`;
      }).join('');

      if (focusedInst) {
        const newFocusedCard = Array.from(this.table.querySelectorAll('.host-card')).find(c => c.dataset.instance === focusedInst);
        if (newFocusedCard) newFocusedCard.focus();
      }
    }
  }

  // Delegated D-pad/keyboard navigation for host cards — bound once on
  // #instancesBody instead of per-card, so it survives re-renders and
  // doesn't accumulate a listener per host.
  _onHostGridKeydown(e) {
    const card = e.target.closest('.host-card');
    if (!card || !this.table.contains(card)) return;

    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      const inst = card.dataset.instance;
      const target = this.data.find(t => t.instance === inst);
      if (target) this._openDrawer(target);
      return;
    }

    const arrowKeys = ['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'];
    if (!arrowKeys.includes(e.key)) return;
    e.preventDefault();

    const allCards = Array.from(this.table.children).filter(el => el.classList.contains('host-card'));
    const idx = allCards.indexOf(card);
    if (idx === -1) return;

    // Resolved column count from the CSS grid (auto-fit), so this stays
    // correct across breakpoints without hardcoding a column number.
    const cols = getComputedStyle(this.table).gridTemplateColumns.split(' ').length || 1;
    let nextIdx = idx;
    if (e.key === 'ArrowRight') nextIdx = idx + 1;
    else if (e.key === 'ArrowLeft') nextIdx = idx - 1;
    else if (e.key === 'ArrowDown') nextIdx = idx + cols;
    else if (e.key === 'ArrowUp') nextIdx = idx - cols;

    nextIdx = Math.max(0, Math.min(allCards.length - 1, nextIdx));
    allCards[nextIdx]?.focus();
  }

  _switchModalTab(tabName) {
    const nav = document.getElementById('modalTabsNav');
    if (!nav) return;
    const tabs = nav.querySelectorAll('[data-tab]');
    tabs.forEach(t => {
      t.classList.toggle('active', t.dataset.tab === tabName);
    });

    const panes = document.querySelectorAll('.modal-tab-pane');
    panes.forEach(p => {
      const isTarget = p.id.toLowerCase() === `tabpane${tabName.toLowerCase()}`;
      p.classList.toggle('hidden', !isTarget);
      p.style.display = isTarget ? 'flex' : 'none';
    });
  }

  _renderDrawerAvailabilityBars(target, points = [], events = []) {
    const container = document.getElementById('drawerAvailabilityBars');
    const timeLabelsEl = document.getElementById('drawerAvailabilityTimeLabels');
    if (!container) return;

    const now_ts = Math.floor(Date.now() / 1000);
    const isUp = target?.health === 'up';

    let barsHtml = '';
    const slotsCount = 24;

    for (let i = 0; i < slotsCount; i++) {
      const slotStart = now_ts - (24 - i) * 3600;
      const slotEnd = now_ts - (23 - i) * 3600;

      let slotDownSec = 0;
      if (Array.isArray(events)) {
        events.forEach(ev => {
          if (ev.status === 'OFFLINE') {
            const evStart = ev.start_ts;
            const evEnd = ev.ongoing ? now_ts : (ev.end_ts || now_ts);
            const oStart = Math.max(slotStart, evStart);
            const oEnd = Math.min(slotEnd, evEnd);
            if (oEnd > oStart) {
              slotDownSec += (oEnd - oStart);
            }
          }
        });
      }

      if (!isUp && i === 23 && slotDownSec === 0) {
        slotDownSec = 3600;
      }

      let uptimePct = 100;
      if (slotDownSec > 0) {
        uptimePct = Math.max(0, Math.min(100, Math.round(((3600 - slotDownSec) / 3600) * 100)));
      }

      let slotPts = Array.isArray(points) ? points.filter(p => p[0] >= slotStart && p[0] < slotEnd) : [];
      let avgLat = slotPts.length > 0 ? (slotPts.reduce((a, b) => a + b[1], 0) / slotPts.length) : (isUp ? target?.responseTimeMs || 0 : 0);

      let barColor = '#22C55E';
      let barHeight = '100%';
      let statusText = `${uptimePct}% Up`;

      if (uptimePct < 10) {
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
      const timeStr = slotDate.toLocaleTimeString('id-ID', { hour: '2-digit', minute: '2-digit' });

      barsHtml += `<div title="${timeStr} • ${statusText}" style="flex:1; height:${barHeight}; background:${barColor}; border-radius:2px; transition:all 0.2s ease;"></div>`;
    }

    container.innerHTML = barsHtml;

    if (timeLabelsEl) {
      const markers = [0, 6, 12, 18, 24];
      const labels = markers.map(hAgo => {
        const dObj = new Date((now_ts - (24 - hAgo) * 3600) * 1000);
        return dObj.toLocaleTimeString('id-ID', { hour: '2-digit', minute: '2-digit' });
      });
      timeLabelsEl.innerHTML = labels.map(l => `<span>${l}</span>`).join('');
    }
  }

  _renderDrawerRecentEvents(events) {
    const container = document.getElementById('drawerRecentEventsList');
    if (!container) return;

    if (!Array.isArray(events) || events.length === 0) {
      container.innerHTML = '<div class="de-empty" style="font-size:12px; color:var(--text-secondary);">Tidak ada log event insiden</div>';
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
      const timeStr = dObj.toLocaleTimeString('id-ID', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
      const agoStr = this._relTime(ev.start_ts * 1000);

      return `
        <div style="display:flex; align-items:flex-start; justify-content:space-between; padding:6px 8px; background:rgba(15,23,42,0.6); border:1px solid var(--border); border-radius:6px; font-size:11px;">
          <div style="display:flex; align-items:center; gap:8px;">
            ${iconSvg}
            <div>
              <div style="font-weight:700; color:var(--text-primary);">${title}</div>
              <div style="color:var(--text-secondary); font-size:10px;">${desc}</div>
            </div>
          </div>
          <div style="text-align:right;">
            <div style="color:var(--text-primary); font-family:var(--font-mono);">${timeStr}</div>
            <div style="color:var(--text-muted); font-size:10px;">${agoStr}</div>
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

    const rangeText = this.periodLabel === 'custom' ? 'Custom' : (this.periodLabel || '24h');
    const rangeLabelEl = document.getElementById('spSummaryRangeLabel');
    if (rangeLabelEl) rangeLabelEl.textContent = `(${rangeText})`;

    // No entry (target filtered out, or this /api/availability response
    // hasn't landed yet) means "we don't know" — never fabricate 100%
    // coverage/availability to fill the gap.
    const entry = this.availabilityBreakdown?.entries?.find(e => e.id === target.instance || e.name === target.instance);
    const covMin = entry ? entry.coverage_minutes : null;
    const upMin = entry ? entry.uptime_minutes : null;
    const downMin = entry ? entry.downtime_minutes : null;
    const covPct = entry && typeof entry.coverage_pct === 'number' ? entry.coverage_pct
      : (entry && typeof entry.coverage_percent === 'number' ? entry.coverage_percent : null);
    const availPct = entry && typeof entry.availability_pct === 'number' ? entry.availability_pct : null;

    const slaBadgeEl = document.getElementById('spSummarySlaBadge');
    if (slaBadgeEl) {
      const sla = this._slaBadgeInfo(entry || {});
      slaBadgeEl.textContent = sla.label;
      slaBadgeEl.className = `sla-badge ${sla.cls}`;
    }

    let downEvents = Array.isArray(events) ? events.filter(e => e.status === 'OFFLINE') : [];
    let failedCount = entry?.incidents || downEvents.length || (target.health !== 'up' ? 1 : 0);

    const fmtDur = m => (typeof m !== 'number') ? '—' : (m < 60 ? `${m.toFixed(1)}m` : `${(m / 60).toFixed(1)}h`);

    if (elTotal) elTotal.textContent = covMin !== null ? `${fmtDur(covMin)} (${covPct !== null ? covPct.toFixed(1) : '—'}% observed)` : '—';
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
        elLastOutage.textContent = dObj.toLocaleTimeString('id-ID', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
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
    const isSlow = isUp && target.responseTimeMs > 500;
    const isDown = !isUp;
    const now = Date.now();

    this._switchModalTab('overview');

    // IP + job
    const titleEl = document.getElementById('drawerTargetTitle');
    if (titleEl) titleEl.textContent = target.instance;
    const infoIpEl = document.getElementById('drawerInfoIp');
    if (infoIpEl) infoIpEl.textContent = target.instance;
    const jobEl = document.getElementById('drawerJobBadge');
    if (jobEl) jobEl.textContent = target.job || 'blackbox-ping-internal';
    const infoJobEl = document.getElementById('drawerInfoJob');
    if (infoJobEl) infoJobEl.textContent = target.job || 'blackbox-ping-internal';

    // Status dot
    const dot = document.getElementById('drawerStatusDot');
    if (dot) {
      dot.className = 'drawer-dot ' + (isDown ? 'dot-down' : (isSlow ? 'dot-slow' : 'dot-up'));
    }

    // Status pill & status text
    const pill = document.getElementById('drawerStatusPill');
    const statusTextEl = document.getElementById('drawerStatusText');
    const label = isDown ? 'Offline' : (isSlow ? 'Slow (>500ms)' : 'Online');
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
        latEl.textContent = target.responseTimeMs ? `${target.responseTimeMs} ms` : (isUp ? '< 1 ms' : '—');
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

    const lossEl = document.getElementById('drawerPacketLoss');
    if (lossEl) lossEl.textContent = isDown ? '100%' : '0%';

    // Scrape URL (Rec #5)
    const linkEl = document.getElementById('drawerTargetUrlLink');
    if (linkEl) {
      const u = target.scrapeUrl || target.instance;
      const hrefUrl = u.startsWith('http://') || u.startsWith('https://') ? u : `http://${u}`;
      linkEl.textContent = u;
      linkEl.href = hrefUrl;
      linkEl.target = '_blank';
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
    const res = await fetch('/api/maintenance', {
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
    const res = await fetch(`/api/maintenance/${encodeURIComponent(maintenanceId)}`, { method: 'DELETE' });
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
    const res = await fetch('/api/dependencies', {
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
    const res = await fetch(`/api/dependencies/${encodeURIComponent(dependencyId)}`, { method: 'DELETE' });
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
        const res = await fetch(`/api/maintenance/${encodeURIComponent(btn.dataset.endId)}`, { method: 'DELETE' });
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

  _calculateNiceScale(minVal, maxVal, maxTicks = 4) {
    let min = 0;
    if (minVal < 0) min = minVal;
    
    let max = Math.max(min + 0.1, maxVal);
    let rawMax = max > 0 ? max * 1.15 : 1.0;
    let range = rawMax - min;
    
    let rawStep = range / maxTicks;
    let exponent = Math.floor(Math.log10(rawStep));
    let fraction = rawStep / Math.pow(10, exponent);
    
    let niceFraction;
    if (fraction < 1.25) niceFraction = 1;
    else if (fraction < 2.5) niceFraction = 2;
    else if (fraction < 3.75) niceFraction = 3;
    else if (fraction < 7.5) niceFraction = 5;
    else niceFraction = 10;
    
    let step = niceFraction * Math.pow(10, exponent);
    if (step <= 0) step = 1;

    let niceMin = Math.floor(min / step) * step;
    if (niceMin < 0 && min >= 0) niceMin = 0;
    
    let niceMax = Math.ceil(rawMax / step) * step;
    while (niceMax < max) {
      niceMax += step;
    }

    let ticks = [];
    for (let tick = niceMin; tick <= niceMax + (step * 0.0001); tick += step) {
      const cleanTick = Math.round(tick * 10000) / 10000;
      ticks.push(cleanTick);
    }

    const formatTick = (val) => {
      if (val >= 1000) {
        return val.toLocaleString('en-US', { maximumFractionDigits: 0 });
      }
      if (step >= 10) {
        return val.toFixed(2);
      }
      if (step >= 1) {
        return val.toFixed(2);
      }
      if (step >= 0.1) {
        return val.toFixed(2);
      }
      return val.toFixed(3);
    };

    return {
      niceMin,
      niceMax,
      step,
      ticks,
      formatTick
    };
  }

  _buildMSGradientDefs(rangeMin, rangeMax, gradIdLine, gradIdArea) {
    const rangeSpan = Math.max(0.1, rangeMax - rangeMin);
    
    const getOffsetPct = (msVal) => {
      const ratio = (msVal - rangeMin) / rangeSpan;
      return Math.max(0, Math.min(100, ratio * 100)).toFixed(1);
    };

    const off100 = getOffsetPct(100);
    const off300 = getOffsetPct(300);
    const off500 = getOffsetPct(500);

    const topColor = rangeMax > 500 ? '#EF4444' : (rangeMax > 200 ? '#F59E0B' : '#22C55E');
    const topOpacity = rangeMax > 500 ? '0.30' : '0.18';

    return `
      <defs>
        <linearGradient id="${gradIdLine}" x1="0" y1="1" x2="0" y2="0">
          <stop offset="0%" stop-color="#22C55E"/>
          <stop offset="${off100}%" stop-color="#22C55E"/>
          <stop offset="${off300}%" stop-color="#F59E0B"/>
          <stop offset="${off500}%" stop-color="#EF4444"/>
          <stop offset="100%" stop-color="${topColor}"/>
        </linearGradient>
        <linearGradient id="${gradIdArea}" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="${topColor}" stop-opacity="${topOpacity}"/>
          <stop offset="60%" stop-color="#22C55E" stop-opacity="0.08"/>
          <stop offset="100%" stop-color="#22C55E" stop-opacity="0.0"/>
        </linearGradient>
      </defs>`;
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
      wrap.innerHTML = '<div class="de-empty" style="padding:15px; font-size:12px; color:var(--text-secondary); text-align:center;">Tidak ada data tren response time untuk rentang ini</div>';
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
    const niceScale = this._calculateNiceScale(minL, maxL, 4);
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
      leftLabels.push(`<span style="position:absolute; right:0; top:${topPct}%; transform:translateY(-50%); font-size:10.5px; font-family:var(--font-mono); color:var(--text-secondary); opacity:0.85; white-space:nowrap;">${formattedVal}</span>`);
      rightLabels.push(`<span style="position:absolute; left:0; top:${topPct}%; transform:translateY(-50%); font-size:10.5px; font-family:var(--font-mono); color:var(--text-secondary); opacity:0.85; white-space:nowrap;">${formattedVal}</span>`);
    });

    // Time axis label formatting helper
    const fmtTime = (ts) => {
      const d = new Date(ts * 1000);
      const isLongRange = (tN - t0) > 86400; // >24h
      if (isLongRange) {
        return d.toLocaleDateString('id-ID', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' });
      }
      return d.toLocaleTimeString('id-ID', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    };

    const t0Str = fmtTime(t0);
    const tMidStr = fmtTime(t0 + (tN - t0) / 2);
    const tNStr = fmtTime(tN);

    const defsHtml = this._buildMSGradientDefs(rangeMin, rangeMax, 'sgLineGrad', 'sgAreaGrad');

    // Build SVG & Adaptive ms Meter Overlay HTML
    wrap.innerHTML = `
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:4px; font-size:11px; font-weight:600; color:var(--text-secondary); opacity:0.8; padding:0 2px;">
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
      <div class="sparkline-x-axis" style="display:flex; justify-content:space-between; font-size:10px; font-family:var(--font-mono); color:var(--text-secondary); margin-top:8px; padding:6px 54px 0 54px; border-top:1px dashed rgba(255,255,255,0.08);">
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
      const timeLabel = dObj.toLocaleTimeString('id-ID', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
      const statusText = closest.val > 500 ? 'SLOW' : (closest.val === 0 ? 'DOWN' : 'UP');

      tooltip.innerHTML = `
        <div style="font-weight:600; color:var(--text-secondary); margin-bottom:2px; font-size:10px;">Time: <span style="color:#fff;">${timeLabel}</span></div>
        <div style="font-weight:600; color:var(--text-secondary); margin-bottom:2px; font-size:10px;">Response Time: <span style="color:${valColor}; font-weight:700;">${closest.val.toFixed(1)} ms</span></div>
        <div style="font-weight:600; color:var(--text-secondary); font-size:10px;">Status: <span style="color:${valColor}; font-weight:700;">${statusText}</span></div>
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
      if (elList) elList.innerHTML = '<div class="de-empty" style="padding:10px; font-size:12px;">Tidak ada data poin latensi</div>';
      wrap.innerHTML = '<div class="de-empty" style="padding:25px; font-size:12px; color:var(--text-secondary); text-align:center;">Tidak ada histori latensi untuk rentang ini</div>';
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
        const timeStr = dObj.toLocaleTimeString('id-ID', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
        const dateStr = dObj.toLocaleDateString('id-ID', { day: '2-digit', month: 'short' });
        const latVal = p[1];
        const isSlow = latVal > 500;
        const color = isSlow ? '#F59E0B' : '#22C55E';
        return `
          <div style="display:flex; justify-content:space-between; align-items:center; padding:4px 8px; background:rgba(15,23,42,0.6); border:1px solid var(--border); border-radius:4px; font-size:11px; font-family:var(--font-mono);">
            <span style="color:var(--text-secondary);">${dateStr} ${timeStr}</span>
            <span style="font-weight:700; color:${color};">${latVal.toFixed(1)} ms</span>
          </div>`;
      }).join('');
    }

    // Dynamic Adaptive Nice Scale Math
    const niceScale = this._calculateNiceScale(minL, maxL, 4);
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
      leftLabels.push(`<span style="position:absolute; right:0; top:${topPct}%; transform:translateY(-50%); font-size:10.5px; font-family:var(--font-mono); color:var(--text-secondary); opacity:0.85; white-space:nowrap;">${formattedVal}</span>`);
      rightLabels.push(`<span style="position:absolute; left:0; top:${topPct}%; transform:translateY(-50%); font-size:10.5px; font-family:var(--font-mono); color:var(--text-secondary); opacity:0.85; white-space:nowrap;">${formattedVal}</span>`);
    });

    // Time axis label formatting helper
    const fmtTime = (ts) => {
      const d = new Date(ts * 1000);
      const isLongRange = (tN - t0) > 86400; // >24h
      if (isLongRange) {
        return d.toLocaleDateString('id-ID', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' });
      }
      return d.toLocaleTimeString('id-ID', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    };

    const t0Str = fmtTime(t0);
    const tMidStr = fmtTime(t0 + (tN - t0) / 2);
    const tNStr = fmtTime(tN);

    const defsHistHtml = this._buildMSGradientDefs(rangeMin, rangeMax, 'sgHistLineGrad', 'sgHistGrad');

    wrap.innerHTML = `
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:4px; font-size:11px; font-weight:600; color:var(--text-secondary); opacity:0.8; padding:0 2px;">
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
      <div class="sparkline-x-axis" style="display:flex; justify-content:space-between; font-size:10px; font-family:var(--font-mono); color:var(--text-secondary); margin-top:8px; padding:6px 54px 0 54px; border-top:1px dashed rgba(255,255,255,0.08);">
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
      const timeLabel = dObj.toLocaleTimeString('id-ID', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
      const statusText = closest.val > 500 ? 'SLOW' : (closest.val === 0 ? 'DOWN' : 'UP');

      tooltip.innerHTML = `
        <div style="font-weight:600; color:var(--text-secondary); margin-bottom:2px; font-size:10px;">Time: <span style="color:#fff;">${timeLabel}</span></div>
        <div style="font-weight:600; color:var(--text-secondary); margin-bottom:2px; font-size:10px;">Response Time: <span style="color:${valColor}; font-weight:700;">${closest.val.toFixed(1)} ms</span></div>
        <div style="font-weight:600; color:var(--text-secondary); font-size:10px;">Status: <span style="color:${valColor}; font-weight:700;">${statusText}</span></div>
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

    const rangeText = this.periodLabel === 'custom' ? 'Custom' : (this.periodLabel || '24h');
    const drawerSparklineRangeEl = document.getElementById('drawerSparklineRange');
    if (drawerSparklineRangeEl) drawerSparklineRangeEl.textContent = `(${rangeText})`;
    const historyRangeTag = document.getElementById('historyRangeTag');
    if (historyRangeTag) historyRangeTag.textContent = `Histori (${rangeText})`;

    const logsList = document.getElementById('drawerLogsList');
    const eventsBadge = document.getElementById('eventsCountBadge');
    if (!logsList) return;
    logsList.innerHTML = '<div class="de-empty" style="padding:10px; font-size:12px; color:var(--text-secondary);">Memuat log histori Prometheus...</div>';

    try {
      const minutes = Math.round(this.periodMinutes || 1440);
      let historyUrl = `/api/target-history?target=${encodeURIComponent(targetInstance)}&minutes=${minutes}`;
      if (this.periodEnd) historyUrl += `&end=${this.periodEnd}`;
      const res = await fetch(historyUrl, { signal: controller.signal });
      const data = await res.json();
      
      if (data.ok && Array.isArray(data.latency_points) && data.latency_points.length > 1) {
        this._renderSparkline(data.latency_points);
        this._renderHistoryChart(data.latency_points);
      } else {
        this._renderSparkline([]);
        this._renderHistoryChart([]);
      }

      this._renderDrawerAvailabilityBars(this.selectedTarget, data.latency_points || [], data.events || []);

      if (!data.ok || !Array.isArray(data.events) || data.events.length === 0) {
        logsList.innerHTML = '<div style="padding: 12px; background: rgba(34, 197, 94, 0.1); border: 1px solid rgba(34, 197, 94, 0.2); border-radius: 8px; font-size: 12px; color: #22C55E; display: flex; align-items: center; gap: 8px;"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg> <span>Target 100% Online — Tidak ada insiden downtime pada rentang ini</span></div>';
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
        const dateStr = dateObj.toLocaleDateString('id-ID', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', second: '2-digit' });
        const durationStr = this._fmtDownAging(ev.duration_seconds * 1000);
        const ongoingBadge = ev.ongoing ? '<span style="font-size:10px; background:rgba(56,189,248,0.15); color:#38BDF8; padding:1px 5px; border-radius:3px; margin-left:6px; font-weight:500;">Berjalan</span>' : '';

        const summaryText = ev.summary && !ev.summary.startsWith('Target ONLINE') && !ev.summary.startsWith('Target OFFLINE')
          ? `<div style="font-size:10px; color:var(--text-muted); margin-top:1px;">${ev.summary}</div>`
          : '';

        return `
          <div class="de-row" style="display:flex; align-items:center; justify-content:space-between; padding:8px 10px; background:var(--surface); border:1px solid var(--border); border-radius:var(--r-sm); margin-bottom:6px; font-size:12px;">
            <div style="display:flex; align-items:center; gap:8px;">
              <span style="width:8px; height:8px; border-radius:50%; background:${dotBg}; display:inline-block; flex-shrink:0;"></span>
              <div>
                <div style="font-weight:600; color:${statusColor}; display:flex; align-items:center; gap:6px;">
                  ${statusText} ${ongoingBadge}
                </div>
                <div style="font-size:11px; color:var(--text-secondary); margin-top:2px;">${dateStr}</div>
                ${summaryText}
              </div>
            </div>
            <div style="text-align:right;">
              <span class="ongoing-duration-val" data-start-ts="${ev.start_ts}" data-ongoing="${ev.ongoing ? 'true' : 'false'}" style="font-family:var(--font-mono); font-weight:600; color:var(--text-primary); font-size:12px;">${durationStr}</span>
              <div style="font-size:10px; color:var(--text-muted);">Durasi Status</div>
            </div>
          </div>
        `;
      }).join('');
    } catch (e) {
      if (e.name === 'AbortError') return;
      logsList.innerHTML = '<div class="de-empty" style="padding:10px; font-size:12px; color:#EF4444;">Gagal memuat log histori</div>';
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
      select.innerHTML = '<option value="" disabled selected>Memuat daftar target Prometheus...</option>';
      try {
        const res = await fetch('/api/prometheus-targets');
        const data = await res.json();
        if (data.ok && Array.isArray(data.targets)) {
          if (data.targets.length === 0) {
            select.innerHTML = '<option value="" disabled selected>Tidak ada target Prometheus yang ditemukan</option>';
            return;
          }

          select.innerHTML = `
            <option value="" disabled selected>-- Pilih Target Prometheus (${data.targets.length} Target) --</option>
            ${data.targets.map(t => {
            const statusTag = t.isDeleted ? '[Di-hapus / Non-aktif]' : '[Aktif]';
            return `<option value="${this._esc(t.instance)}">${this._esc(t.instance)} ${statusTag}</option>`;
          }).join('')}
          `;
          setTimeout(() => select.focus(), 50);
        } else {
          select.innerHTML = '<option value="" disabled selected>Gagal memuat target Prometheus</option>';
        }
      } catch (ex) {
        select.innerHTML = '<option value="" disabled selected>Gagal terhubung ke Prometheus API</option>';
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
        err.textContent = 'Silakan pilih target dari daftar Prometheus';
        err.classList.remove('hidden');
      }
      return;
    }

    if (err) err.classList.add('hidden');
    if (submitBtn) submitBtn.disabled = true;

    try {
      const res = await fetch('/api/targets', {
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
      title: 'Hapus Target Monitoring',
      message: `Apakah Anda yakin ingin menghapus target "${url}" dari daftar monitoring?`,
      confirmText: 'Ya, Hapus',
      cancelText: 'Batal',
      isDanger: true
    });

    if (!confirmed) return false;

    try {
      const res = await fetch('/api/targets', {
        method: 'DELETE',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url: url })
      });
      const data = await res.json();
      if (data.ok) {
        this.load();
        return true;
      }
      this._triggerEventToast(data.error || `Gagal menghapus target "${url}"`);
      return false;
    } catch (ex) {
      console.warn('[InfraWatch] Failed to delete target:', ex);
      this._triggerEventToast(`Gagal menghapus target "${url}"`);
      return false;
    }
  }

  _relTime(iso) {
    if (!iso || iso === '—' || iso === 'null' || iso === 'undefined') return '—';
    try {
      const parsed = new Date(iso);
      if (isNaN(parsed.getTime())) return '—';
      const diff = Math.floor((Date.now() - parsed.getTime()) / 1000);
      if (diff < 0) return 'Just now';
      if (diff < 5) return 'Just now';
      if (diff < 60) return `${diff}s ago`;
      if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
      return `${Math.floor(diff / 3600)}h ago`;
    } catch { return '—'; }
  }

  _esc(s) {
    return String(s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
}


/* ════════════════════════════════════════════════════════════════════════════
   LOGS PAGE
   ════════════════════════════════════════════════════════════════════════════ */
class LogsPage {
  constructor(monitor) {
    this.monitor = monitor;
    this.data = [];
    this.filter = 'all';
    this.searchQ = '';
    this.isLive = true;
    this.interval = null;
    this.seenCount = 0;  // for NEW badge
    this.clearedBefore = parseFloat(localStorage.getItem('logsClearedBefore') || '0');
    this._loaded = false;
    this._loadAbortController = null;

    this.stream = document.getElementById('logStream');
    this.searchEl = document.getElementById('logSearch');
    this.navBadge = document.getElementById('logsBadge');

    this._bindEvents();
  }

  _bindEvents() {
    document.querySelectorAll('[data-log-filter]').forEach(btn => {
      btn.addEventListener('click', () => {
        this.filter = btn.dataset.logFilter;
        document.querySelectorAll('[data-log-filter]').forEach(b =>
          b.classList.toggle('filter-btn-active', b.dataset.logFilter === this.filter)
        );
        this._render();
      });
    });

    this.searchEl.addEventListener('input', () => {
      this.searchQ = this.searchEl.value.toLowerCase();
      this._render();
    });

    document.getElementById('logLiveToggle').addEventListener('change', e => {
      this.isLive = e.target.checked;
      if (this.isLive) this.load();
    });

    document.getElementById('clearLogs').addEventListener('click', () => {
      this.clearedBefore = Date.now() / 1000;
      localStorage.setItem('logsClearedBefore', this.clearedBefore);
      this._render();
    });
  }

  onActivate() {
    this.isActive = true;
    this.navBadge.classList.add('hidden');
    if (!this._loaded) this._renderLoading();
    this.load();
    this._startPolling(5000);
  }

  onDeactivate() {
    this.isActive = false;
    this._startPolling(30000);
  }

  _startPolling(intervalMs = 5000) {
    this._stopPolling();
    this.interval = setInterval(() => {
      if (this.isLive) this.load();
    }, intervalMs);
  }

  _stopPolling() {
    if (this.interval) { clearInterval(this.interval); this.interval = null; }
  }

  async load() {
    if (this._loadAbortController) this._loadAbortController.abort();
    const controller = new AbortController();
    this._loadAbortController = controller;

    try {
      const res = await fetch('/logs?limit=100', { signal: controller.signal });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const logs = await res.json();

      const prevCount = this.data.length;
      this.data = logs;
      this._loaded = true;

      // Show NEW badge only when the operator isn't already looking at this feed
      if (!this.isActive && prevCount > 0 && logs.length > prevCount) {
        const diff = logs.length - prevCount;
        this.navBadge.textContent = `+${diff}`;
        this.navBadge.classList.remove('hidden');
        const mobileBadge = document.getElementById('mobileLogsBadge');
        if (mobileBadge) {
          mobileBadge.textContent = `+${diff}`;
          mobileBadge.hidden = false;
        }
        // Hide after 4s
        setTimeout(() => {
          this.navBadge.classList.add('hidden');
          if (mobileBadge) mobileBadge.hidden = true;
        }, 4000);
      }

      this._render();
    } catch (e) {
      if (e.name === 'AbortError') return;
      console.error('[Logs] load failed:', e);
      if (!this._loaded) this._renderError(e.message);
    } finally {
      if (this._loadAbortController === controller) this._loadAbortController = null;
    }
  }

  _renderLoading() {
    this.stream.innerHTML = `
      <div class="empty-state">
        <div class="es-text">Loading alert logs…</div>
      </div>`;
  }

  _renderError(msg) {
    this.stream.innerHTML = `
      <div class="empty-state">
        <div class="es-text" style="color:var(--critical, #EF4444);">Failed to load alert logs — ${this._esc(msg || 'network error')}</div>
      </div>`;
  }

  _render() {
    let rows = this.data;

    if (this.clearedBefore) {
      rows = rows.filter(r => (r.time || 0) > this.clearedBefore);
    }
    if (this.filter !== 'all') {
      rows = rows.filter(r => r.event === this.filter);
    }
    if (this.searchQ) {
      rows = rows.filter(r =>
        (r.name || '').toLowerCase().includes(this.searchQ) ||
        (r.instance || '').toLowerCase().includes(this.searchQ) ||
        (r.summary || '').toLowerCase().includes(this.searchQ)
      );
    }

    if (rows.length === 0) {
      this.stream.innerHTML = `
        <div class="empty-state">
          <div class="es-icon" aria-hidden="true">
            <svg width="32" height="32" viewBox="0 0 32 32" fill="none">
              <path d="M4 26V8a2 2 0 012-2h20a2 2 0 012 2v18" stroke="currentColor" stroke-width="1.5"/>
              <path d="M2 26h28M10 13h12M10 18h8" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
            </svg>
          </div>
          <div class="es-text">No log entries yet — waiting for webhooks…</div>
        </div>`;
      return;
    }

    this.stream.innerHTML = rows.map((r, i) => {
      const ev = r.event || 'unknown';
      const sev = (r.severity || 'info').toLowerCase();
      const meta = ev === 'resolved'
        ? (typeof r.duration_seconds === 'number' ? this._fmtDur(r.duration_seconds) : '—')
        : (typeof r.latency_ms === 'number' ? `${r.latency_ms}ms` : '—');
      return `
        <div class="log-entry log-entry-${ev}" style="animation-delay:${Math.min(i, 20) * 0.02}s">
          <span class="log-time">${this._fmt(r.time)}</span>
          <span class="log-col-status"><span class="status-dot status-dot-${ev}"></span><span class="log-event log-event-${ev}">${ev}</span></span>
          <span class="log-name">${this._esc(r.name || '—')}</span>
          <span class="log-inst" title="${this._esc(r.job || '')}">${this._esc(r.instance || '—')}${r.job ? ` <small class="log-job">${this._esc(r.job)}</small>` : ''}</span>
          <span class="log-sev log-sev-${sev}">${sev}</span>
          <span class="log-meta" title="${ev === 'resolved' ? 'Duration' : 'Latency'}">${this._esc(meta)}</span>
          <span class="log-msg" title="${this._esc(r.summary || '')}">${this._esc(r.summary || '—')}</span>
        </div>`;
    }).join('');
  }

  _fmt(ts) {
    if (!ts) return '—';
    const d = new Date(ts * 1000);
    return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}:${String(d.getSeconds()).padStart(2, '0')}`;
  }

  _fmtDur(s) {
    if (typeof s !== 'number' || isNaN(s)) return '—';
    if (s < 60) return `${Math.round(s)}s`;
    if (s < 3600) return `${Math.round(s / 60)}m`;
    return `${(s / 3600).toFixed(1)}h`;
  }

  _esc(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
}


/* ════════════════════════════════════════════════════════════════════════════
   HISTORY PAGE
   ════════════════════════════════════════════════════════════════════════════ */
class HistoryPage {
  constructor(monitor) {
    this.monitor = monitor;
    this.data = [];
    this.filter = 'all';
    this.searchQ = '';
    this.clearedBefore = parseFloat(localStorage.getItem('historyClearedBefore') || '0');
    this._loaded = false;
    this._loadAbortController = null;

    this.tableEl = document.getElementById('historyFullTable');
    this.badge = document.getElementById('historyBadge');
    this.metaEl = document.getElementById('historyMeta');
    this.searchEl = document.getElementById('historySearch');

    this.statTotal = document.getElementById('histTotal');
    this.statMonth = document.getElementById('histThisMonth');
    this.statCritical = document.getElementById('histCritical');
    this.statWarning = document.getElementById('histWarning');

    this._bindEvents();
  }

  _bindEvents() {
    document.querySelectorAll('[data-hist-filter]').forEach(btn => {
      btn.addEventListener('click', () => {
        this.filter = btn.dataset.histFilter;
        document.querySelectorAll('[data-hist-filter]').forEach(b =>
          b.classList.toggle('filter-btn-active', b.dataset.histFilter === this.filter)
        );
        this._render();
      });
    });

    this.searchEl.addEventListener('input', () => {
      this.searchQ = this.searchEl.value.toLowerCase();
      this._render();
    });

    document.getElementById('exportHistory').addEventListener('click', () => this._exportCSV());

    document.getElementById('clearHistory').addEventListener('click', () => {
      this.clearedBefore = Date.now() / 1000;
      localStorage.setItem('historyClearedBefore', this.clearedBefore);
      this._updateStats();
      this._render();
    });

    // Click a row to expand its recovery-sequence detail (fired/resolved/
    // duration/receiver) — reuses the already-loaded incident data, no
    // extra request and no second incident store.
    this.tableEl.addEventListener('click', e => {
      const row = e.target.closest('.history-row-full');
      if (!row) return;
      const detail = row.nextElementSibling;
      if (detail && detail.classList.contains('history-row-detail')) {
        detail.classList.toggle('hidden');
      }
    });
  }

  onActivate() {
    if (!this._loaded) this._renderLoading();
    this.load();
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

  // Incidents still in view after "Clear" — the stat cards, meta caption
  // and table all reset together off this same cut-off, so nothing shows
  // a stale total once the list has been cleared.
  _visibleData() {
    return this.clearedBefore ? this.data.filter(r => (r.time || 0) > this.clearedBefore) : this.data;
  }

  _updateStats() {
    const base = this._visibleData();
    const now = new Date();
    const month = base.filter(i => {
      const d = new Date(i.time * 1000);
      return d.getMonth() === now.getMonth() && d.getFullYear() === now.getFullYear();
    }).length;
    const crit = base.filter(i => (i.severity || '').toLowerCase() === 'critical').length;
    const warn = base.filter(i => (i.severity || '').toLowerCase() === 'warning').length;

    this.statTotal.textContent = base.length;
    this.statMonth.textContent = month;
    this.statCritical.textContent = crit;
    this.statWarning.textContent = warn;
  }

  _render() {
    const base = this._visibleData();
    this.metaEl.textContent = `${base.length} incident${base.length !== 1 ? 's' : ''} total`;

    let rows = base;
    if (this.filter !== 'all') {
      rows = rows.filter(r => (r.severity || '').toLowerCase() === this.filter);
    }
    if (this.searchQ) {
      rows = rows.filter(r =>
        (r.name || '').toLowerCase().includes(this.searchQ) ||
        (r.instance || '').toLowerCase().includes(this.searchQ) ||
        (r.summary || '').toLowerCase().includes(this.searchQ)
      );
    }

    this.badge.textContent = rows.length;

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
      return;
    }

    this.tableEl.innerHTML = rows.map((inc, i) => {
      const sev = (inc.severity || 'critical').toLowerCase();
      const st = (inc.status || 'firing').toLowerCase();
      const stLabel = st === 'resolved' ? 'Resolved' : 'Open';
      const alt = i % 2 === 1 ? ' history-row-alt' : '';
      return `
        <div class="history-row history-row-full${alt}" style="animation-delay:${Math.min(i, 30) * 0.025}s">
          <div class="history-time">${this._fmt(inc.time)}</div>
          <div class="history-col-status"><span class="status-dot status-dot-${st}"></span><span class="history-status history-status-${st}">${stLabel}</span></div>
          <div class="history-name">${this._esc(inc.name || 'Unknown')}</div>
          <div class="history-instance" title="${this._esc(inc.job || '')}">${this._esc(inc.instance || '—')}${inc.job ? ` <small class="log-job">${this._esc(inc.job)}</small>` : ''}</div>
          <div><span class="history-sev ${sev}">${sev}</span></div>
          <div class="history-col-duration">${this._fmtDuration(inc)}</div>
          <div class="history-col-msg" title="${this._esc(inc.summary || '')}">${this._esc(inc.summary || '—')}</div>
        </div>
        <div class="history-row-detail hidden">
          <div class="hrd-sequence">
            <div class="hrd-point">
              <span class="hrd-point-label">Fired</span>
              <span class="hrd-point-time">${this._fmt(inc.time)}</span>
            </div>
            <div class="hrd-arrow" aria-hidden="true">&#8594;</div>
            <div class="hrd-point">
              <span class="hrd-point-label">Resolved</span>
              <span class="hrd-point-time">${inc.resolved_time ? this._fmt(inc.resolved_time) : (st === 'resolved' ? '—' : 'still open')}</span>
            </div>
            <div class="hrd-duration ${st === 'resolved' ? '' : 'hrd-duration-open'}">
              <span class="hrd-duration-label">Duration</span>
              <span class="hrd-duration-value">${this._fmtDuration(inc)}</span>
            </div>
          </div>
          <div class="hrd-meta">Receiver: ${this._esc(inc.receiver || '—')}</div>
        </div>`;
    }).join('');
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

  _fmtDuration(inc) {
    const s = inc.duration_seconds;
    if (typeof s !== 'number' || isNaN(s)) return '—';
    if (s < 60) return `${Math.round(s)}s`;
    if (s < 3600) return `${Math.round(s / 60)}m`;
    return `${(s / 3600).toFixed(1)}h`;
  }

  _exportCSV() {
    const header = 'Time,Alert,Instance,Job,Severity,Status,ResolvedTime,DurationSeconds,Summary\n';
    const rows = this.data.map(r =>
      [
        this._fmt(r.time), r.name, r.instance, r.job, r.severity,
        r.status || 'firing',
        r.resolved_time ? this._fmt(r.resolved_time) : '',
        typeof r.duration_seconds === 'number' ? Math.round(r.duration_seconds) : '',
        r.summary
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

  _esc(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }
}


/* ════════════════════════════════════════════════════════════════════════════
   MAIN MONITOR (Dashboard + coordination)
   ════════════════════════════════════════════════════════════════════════════ */
class ServerMonitor {
  constructor() {
    this.isMuted = false;
    this.isInitialized = false;
    this.statusCheckInterval = null;
    this.historyCheckInterval = null;

    // ── DOM refs ──────────────────────────────────
    this.dashboard = document.getElementById('dashboard');

    this.heroCard = document.getElementById('heroCard');
    this.beaconRing = document.getElementById('beaconRing');
    this.beaconCore = document.getElementById('beaconCore');
    this.statusText = document.getElementById('statusText');
    this.heroDesc = document.getElementById('heroDesc');
    this.statusMeta = document.getElementById('statusMeta');

    this.alertCount = document.getElementById('alertCount');
    this.uptimeDays = document.getElementById('uptimeDays');
    this.incidentCount = document.getElementById('incidentCount');
    this.alertBar = document.getElementById('alertBar');
    this.incidentBar = document.getElementById('incidentBar');
    this.uptimeBar = document.getElementById('uptimeBar');

    this.alertsList = document.getElementById('alertsList');
    this.alertBadge = document.getElementById('alertBadge');

    this.historyTable = document.getElementById('historyTable');
    this.historyCount = document.getElementById('historyCount');

    this.soundToggle = document.getElementById('soundToggle');
    this.soundLabel = document.getElementById('soundLabel');
    this.testBtn = document.getElementById('testBtn');
    this.audioWarning = document.getElementById('audioWarning');
    this.alarmAudio = document.getElementById('alarmAudio');

    this.netIndicator = document.getElementById('netIndicator');
    this.healthIndicator = document.getElementById('healthIndicator');
    this.subtitle = document.getElementById('subtitle');

    // ── Sub-pages ─────────────────────────────────
    this.instancesPage = new InstancesPage(this);
    this.logsPage = new LogsPage(this);
    this.historyPage = new HistoryPage(this);

    // ── Router ────────────────────────────────────
    this.router = new Router({
      dashboard: this.instancesPage,
      instances: this.instancesPage,
    });

    this._bindLogsModal();
    this._bindSelfHealthModal();
    this._bindEvents();
    this.initialize();
  }

  /* ── Event binding ─────────────────────────────── */
  _bindEvents() {
    const enterBtn = document.getElementById('enterDashboardBtn');
    const splashOverlay = document.getElementById('splashOverlay');

    if (enterBtn && splashOverlay) {
      enterBtn.addEventListener('click', () => {
        this.unlockAudio();
        this.resetOutageAlarm();
        splashOverlay.classList.add('splash-hidden');
        try { localStorage.setItem('iw-audio-unlocked', 'true'); } catch (e) { }
        const downCount = this.instancesPage?.data?.filter(t => t.health !== 'up' && !t.maintenance)?.length || 0;
        if (downCount > 0) {
          this.playAlarm();
        }
      });
    }

    const soundBtn = document.getElementById('soundToggleBtn');
    if (soundBtn) {
      soundBtn.addEventListener('click', () => {
        this.toggleSound(this.isMuted); // toggles state
      });
    }

    // Auto-unlock audio on user's first click or keypress anywhere
    const unlock = () => {
      this.unlockAudio();
      document.removeEventListener('click', unlock);
      document.removeEventListener('keydown', unlock);
    };
    document.addEventListener('click', unlock);
    document.addEventListener('keydown', unlock);
  }

  initialize() {
    if (this.isInitialized) return;
    this.isInitialized = true;

    // Auto-bypass splash overlay if audio consent was previously recorded or running in kiosk mode
    const splashOverlay = document.getElementById('splashOverlay');
    if (splashOverlay && (localStorage.getItem('iw-audio-unlocked') === 'true' || (this.audioCtx && this.audioCtx.state === 'running'))) {
      splashOverlay.classList.add('splash-hidden');
      this.unlockAudio();
    }

    this.initEndpointManager();
    this.instancesPage.onActivate();
    this.checkStatus();
    if (!this.statusCheckInterval) {
      this.statusCheckInterval = setInterval(() => this.checkStatus(), 6000);
    }

    // Poll logs continuously (not just while the modal is open) so the
    // "+N new" nav badge can fire even when the operator is elsewhere —
    // slower cadence in the background, tightened to 5s once the modal opens.
    this.logsPage._startPolling(30000);
    this.logsPage.load();
    this._checkSelfHealth();
    setInterval(() => this._checkSelfHealth(), 20000);

    // Kiosk / TV Standby lifecycle management: pause/resume cleanly on wake
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible') {
        // Immediate clean sync on wake without timer accumulation
        this.checkStatus();
        this.instancesPage.load();
        this.instancesPage.loadAvailability();
        this._checkSelfHealth();
      }
    });
  }

  // Phase 13 self-monitoring — reuses /health rather than a second endpoint.
  // Automatic polling is unchanged (still every 20s from initialize()) and
  // /health is still the only data source — this just also feeds the modal
  // below instead of a hover-only tooltip.
  async _checkSelfHealth() {
    const dot = document.getElementById('selfHealthDot');
    const btn = document.getElementById('selfHealthBtn');
    if (!dot || !btn) return;
    try {
      const res = await fetch('/health');
      const data = await res.json();
      this._lastHealthData = data;
      this._lastHealthSuccessAt = new Date();
      dot.style.background = data.ok ? 'var(--success)' : 'var(--critical)';
      btn.title = data.ok ? 'InfraWatch self-status: all systems OK (click for detail)' : 'InfraWatch self-status: degraded (click for detail)';
    } catch (e) {
      this._lastHealthData = null;
      dot.style.background = 'var(--critical)';
      btn.title = 'InfraWatch self-status: unreachable (click for detail)';
    }
    this._renderSelfHealthModal();
  }

  _bindSelfHealthModal() {
    const modal = document.getElementById('selfHealthModal');
    const btn = document.getElementById('selfHealthBtn');
    const closeBtn = document.getElementById('closeSelfHealthModal');
    if (!modal || !btn) return;

    const open = () => {
      modal.classList.remove('hidden');
      if (this._untrapSelfHealth) this._untrapSelfHealth();
      this._untrapSelfHealth = window.trapModalFocus(modal);
      this._checkSelfHealth(); // refresh on open rather than showing a stale snapshot
    };
    const close = () => {
      if (this._untrapSelfHealth) { this._untrapSelfHealth(); this._untrapSelfHealth = null; }
      modal.classList.add('hidden');
    };

    btn.addEventListener('click', open);
    if (closeBtn) closeBtn.addEventListener('click', close);
    modal.addEventListener('click', e => { if (e.target === modal) close(); });
  }

  _renderSelfHealthModal() {
    const list = document.getElementById('selfHealthList');
    const updatedEl = document.getElementById('selfHealthUpdated');
    if (!list) return;

    const data = this._lastHealthData;
    const rows = [
      ['Prometheus', 'prometheus'],
      ['Monitoring API', 'monitoring_api'],
      ['Alarm Service', 'alarm_service'],
      ['Storage', 'storage'],
    ];
    const c = (data && data.components) || {};
    list.innerHTML = rows.map(([label, key]) => {
      const ok = !!(c[key] && c[key].ok);
      const dotColor = data ? (ok ? 'var(--success)' : 'var(--critical)') : 'var(--text-muted)';
      return `
        <div class="dil-row">
          <span class="dil-label">
            <span style="display:inline-block; width:7px; height:7px; border-radius:50%; background:${dotColor}; margin-right:7px;"></span>${label}
          </span>
          <span class="dil-value" style="color: ${data ? (ok ? 'var(--success)' : 'var(--critical)') : 'var(--text-muted)'};">${data ? (ok ? 'OK' : 'DOWN') : 'Unknown'}</span>
        </div>`;
    }).join('');

    if (updatedEl) {
      updatedEl.textContent = this._lastHealthSuccessAt
        ? `Last successful update: ${this._lastHealthSuccessAt.toLocaleTimeString()}`
        : 'Last successful update: never (endpoint unreachable)';
    }
  }



  /* ── Alert Logs / Incident History modal ───────── */
  _bindLogsModal() {
    const modal = document.getElementById('logsModal');
    const openBtn = document.getElementById('openLogsModalBtn');
    const closeBtn = document.getElementById('closeLogsModal');
    const tabs = {
      logs: { btn: document.getElementById('logsTabBtn'), panel: document.getElementById('logsTabPanel') },
      history: { btn: document.getElementById('historyTabBtn'), panel: document.getElementById('historyTabPanel') },
      maintenance: { btn: document.getElementById('maintenanceTabBtn'), panel: document.getElementById('maintenanceTabPanel') },
    };
    if (!modal || !openBtn) return;

    const showTab = (tab) => {
      Object.entries(tabs).forEach(([key, { btn, panel }]) => {
        const active = key === tab;
        if (panel) panel.classList.toggle('hidden', !active);
        if (btn) {
          btn.classList.toggle('chip-active', active);
          btn.setAttribute('aria-selected', String(active));
        }
      });
      if (tab === 'logs') this.logsPage.onActivate();
      else if (tab === 'history') this.historyPage.onActivate();
      else if (tab === 'maintenance') this.instancesPage._maintenanceManagerOnActivate();
    };

    Object.entries(tabs).forEach(([key, { btn }]) => {
      if (btn) btn.addEventListener('click', () => showTab(key));
    });

    const openModal = () => {
      modal.classList.remove('hidden');
      if (this._untrapLogs) this._untrapLogs();
      this._untrapLogs = window.trapModalFocus(modal);
      showTab('logs');
    };
    const closeModal = () => {
      if (this._untrapLogs) { this._untrapLogs(); this._untrapLogs = null; }
      modal.classList.add('hidden');
      this.logsPage.onDeactivate();
    };

    openBtn.addEventListener('click', openModal);
    if (closeBtn) closeBtn.addEventListener('click', closeModal);
    modal.addEventListener('click', e => { if (e.target === modal) closeModal(); });
  }

  /* ── Endpoint Manager ──────────────────────────── */
  async initEndpointManager() {
    const endpointSelect = document.getElementById('endpointSelect');
    const openBtn = document.getElementById('openEndpointModalBtn');
    const closeBtn = document.getElementById('closeEndpointModalBtn');
    const modal = document.getElementById('endpointModal');
    const addForm = document.getElementById('addEndpointForm');
    const urlInput = document.getElementById('endpointUrlInput');
    const errorEl = document.getElementById('addEndpointError');
    const listContainer = document.getElementById('endpointListContainer');

    const fetchEndpoints = async () => {
      try {
        const res = await fetch('/api/endpoints');
        const data = await res.json();
        if (!data.ok) return;

        // Populate topbar select dropdown
        if (endpointSelect) {
          endpointSelect.innerHTML = '';
          data.endpoints.forEach(ep => {
            const opt = document.createElement('option');
            opt.value = ep.url;
            opt.selected = ep.active;
            const displayUrl = ep.url.replace(/^https?:\/\//, '');
            opt.textContent = `Prometheus: ${displayUrl}${ep.active ? ' (Active)' : ''}`;
            endpointSelect.appendChild(opt);
          });
        }

        // Populate modal list
        if (listContainer) {
          listContainer.innerHTML = '';
          data.endpoints.forEach(ep => {
            const row = document.createElement('div');
            row.style.cssText = 'display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:8px; padding:8px 10px; background:var(--surface); border:1px solid var(--border); border-radius:var(--r-sm); font-size:12px; margin-bottom:6px;';
            const statusDot = ep.online ? '<span style="color:#22C55E; margin-right:6px;">● Online</span>' : '<span style="color:#EF4444; margin-right:6px;">● Offline</span>';
            const activeBadge = ep.active ? '<span style="background:var(--accent-bg); color:var(--accent); padding:2px 6px; border-radius:4px; font-size:10px; font-weight:600; margin-left:6px;">ACTIVE</span>' : '';
            
            row.innerHTML = `
              <div style="display:flex; align-items:center; overflow:hidden; flex:1; min-width:0;">
                ${statusDot}
                <span style="font-family:var(--font-mono); font-weight:500; text-overflow:ellipsis; overflow:hidden; white-space:nowrap; color:var(--text-primary); min-width:0; flex:1;">${ep.url}</span>
                ${activeBadge}
              </div>
              <div style="display:flex; gap:6px; flex-shrink:0; margin-left:10px; flex-wrap:wrap; justify-content:flex-end;">
                ${!ep.active ? `<button class="btn btn-secondary btn-sm select-ep-btn" data-url="${ep.url}" style="padding:2px 8px; font-size:11px;">Pilih</button>` : ''}
                ${data.endpoints.length > 1 ? `<button class="btn btn-danger btn-sm del-ep-btn" data-url="${ep.url}" style="padding:2px 8px; font-size:11px; background:rgba(239,68,68,0.15); color:#EF4444; border:1px solid rgba(239,68,68,0.3);">Hapus</button>` : ''}
              </div>
            `;
            listContainer.appendChild(row);
          });

          // Bind Select buttons
          listContainer.querySelectorAll('.select-ep-btn').forEach(btn => {
            btn.addEventListener('click', async (e) => {
              const targetUrl = e.currentTarget.dataset.url;
              await selectEndpoint(targetUrl);
            });
          });

          // Bind Delete buttons
          listContainer.querySelectorAll('.del-ep-btn').forEach(btn => {
            btn.addEventListener('click', async (e) => {
              const targetUrl = e.currentTarget.dataset.url;
              const confirmed = await window.showConfirmDialog({
                title: 'Hapus Endpoint Prometheus',
                message: `Apakah Anda yakin ingin menghapus endpoint "${targetUrl}"?`,
                confirmText: 'Hapus Endpoint',
                cancelText: 'Batal',
                isDanger: true
              });
              if (confirmed) {
                await deleteEndpoint(targetUrl);
              }
            });
          });
        }
      } catch (e) {
        console.warn('[EndpointManager] Failed to load endpoints:', e);
      }
    };

    const selectEndpoint = async (url) => {
      try {
        const res = await fetch('/api/endpoints/select', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ url })
        });
        const data = await res.json();
        if (data.ok) {
          await fetchEndpoints();
          this.instancesPage.load();
          this.instancesPage.loadAvailability();
        }
      } catch (e) { }
    };

    const deleteEndpoint = async (url) => {
      try {
        const res = await fetch('/api/endpoints', {
          method: 'DELETE',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ url })
        });
        const data = await res.json();
        if (data.ok) {
          await fetchEndpoints();
          this.instancesPage.load();
        } else if (errorEl) {
          errorEl.textContent = data.error || 'Gagal menghapus endpoint';
          errorEl.classList.remove('hidden');
        }
      } catch (e) { }
    };

    // Event listeners
    if (endpointSelect) {
      endpointSelect.addEventListener('change', (e) => {
        selectEndpoint(e.target.value);
      });
    }

    if (openBtn && modal) {
      openBtn.addEventListener('click', () => {
        modal.classList.remove('hidden');
        if (this._untrapEndpoint) this._untrapEndpoint();
        this._untrapEndpoint = window.trapModalFocus(modal);
        fetchEndpoints();
      });
    }

    if (closeBtn && modal) {
      closeBtn.addEventListener('click', () => {
        if (this._untrapEndpoint) { this._untrapEndpoint(); this._untrapEndpoint = null; }
        modal.classList.add('hidden');
      });
    }

    if (addForm) {
      addForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        if (errorEl) errorEl.classList.add('hidden');
        const url = urlInput.value.trim();
        if (!url) return;

        try {
          const res = await fetch('/api/endpoints', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url, set_active: true })
          });
          const data = await res.json();
          if (data.ok) {
            urlInput.value = '';
            if (modal) modal.classList.add('hidden');
            await fetchEndpoints();
            this.instancesPage.load();
            this.instancesPage.loadAvailability();
          } else if (errorEl) {
            errorEl.textContent = data.error || 'Gagal menambah endpoint';
            errorEl.classList.remove('hidden');
          }
        } catch (e) {
          if (errorEl) {
            errorEl.textContent = 'Gagal terhubung ke server';
            errorEl.classList.remove('hidden');
          }
        }
      });
    }

    // Initial load
    fetchEndpoints();
  }


  /* ── onActivate (dashboard page) ───────────────── */
  onActivate() { /* already polling */ }

  /* ── Time helpers ──────────────────────────────── */
  formatTimeAgo(ts) {
    const diff = Math.max(0, Math.floor(Date.now() / 1000) - ts);
    if (diff < 5) return 'Just now';
    if (diff < 60) return `${diff}s ago`;
    if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
    return `${Math.floor(diff / 86400)}d ago`;
  }

  formatTime(ts) {
    const d = new Date(ts * 1000);
    const hh = String(d.getHours()).padStart(2, '0');
    const mm = String(d.getMinutes()).padStart(2, '0');
    const dd = String(d.getDate()).padStart(2, '0');
    const mo = String(d.getMonth() + 1).padStart(2, '0');
    return `${hh}:${mm} · ${dd}/${mo}`;
  }

  /* ── Status fetch ──────────────────────────────── */
  async checkStatus() {
    try {
      const res = await fetch('/status');
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      const statusState = data.status || 'NORMAL';
      // Alarm trigger is exclusively handled by InstancesPage._updateStats to avoid double playback
    } catch (err) {
      console.warn('[InfraWatch] Status check failed:', err);
    }
  }

  /* ── Hero section update ───────────────────────── */
  _updateHero(statusState, alerts) {
    const isCritical = statusState === 'CRITICAL';
    const isWarning = statusState === 'WARNING';
    const label = isCritical ? 'CRITICAL' : (isWarning ? 'WARNING' : 'NORMAL');

    if (this.statusText) {
      this.statusText.textContent = label;
      this.statusText.classList.toggle('critical', isCritical);
      this.statusText.classList.toggle('warning', isWarning);
    }

    if (this.heroCard) {
      this.heroCard.classList.toggle('critical', isCritical);
      this.heroCard.classList.toggle('warning', isWarning);
    }

    if (this.beaconCore) {
      this.beaconCore.classList.toggle('critical', isCritical);
      this.beaconCore.classList.toggle('warning', isWarning);
    }

    if (this.beaconRing) {
      this.beaconRing.classList.toggle('critical', isCritical);
      this.beaconRing.classList.toggle('warning', isWarning);
    }

    if (this.heroDesc) {
      this.heroDesc.textContent = isCritical
        ? `${alerts.length} active critical alert${alerts.length !== 1 ? 's' : ''} — immediate attention required`
        : (isWarning
          ? `${alerts.length} warning alert${alerts.length !== 1 ? 's' : ''} — investigation recommended`
          : 'All systems operational');
    }

    if (this.alertsList) this._renderAlerts(alerts);
    if (this.alertCount) this.alertCount.textContent = String(alerts.length).padStart(2, '0');
    if (this.alertBadge) {
      this.alertBadge.textContent = alerts.length;
      this.alertBadge.classList.toggle('badge-hidden', alerts.length === 0);
    }

    // Sound control: Sound alarm on Critical alerts
    if (isCritical && !this.isMuted) {
      this.playAlarm();
    } else {
      this.stopAlarm();
    }
  }

  /* ── Alert rendering ───────────────────────────── */
  _renderAlerts(alerts) {
    if (!this.alertsList) return;
    if (alerts.length === 0) {
      this.alertsList.innerHTML = `
        <div class="empty-state">
          <div class="es-icon" aria-hidden="true">
            <svg width="32" height="32" viewBox="0 0 32 32" fill="none">
              <circle cx="16" cy="16" r="13" stroke="currentColor" stroke-width="1.5"/>
              <path d="M11 16l4 4 6-7" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
            </svg>
          </div>
          <div class="es-text">No active alerts — everything is quiet</div>
        </div>`;
      return;
    }

    this.alertsList.innerHTML = alerts.map((a) => {
      const name = this._esc(a.name || 'Unknown Alert');
      const inst = this._esc(a.instance || '');
      const summary = this._esc(a.summary || '');
      const sev = (a.severity || 'critical').toLowerCase();
      const timeStr = a.time ? `Triggered ${this.formatTimeAgo(a.time)}` : '';

      return `
        <div class="alert-item">
          <div class="alert-icon" aria-hidden="true">▲</div>
          <div class="alert-content">
            <div class="alert-title">${name}${inst ? ` · <span style="font-weight:400;color:var(--text-2)">${inst}</span>` : ''}</div>
            ${summary ? `<div class="alert-desc">${summary}</div>` : ''}
            <div class="alert-meta">
              ${timeStr ? `<span class="alert-time">${timeStr}</span>` : ''}
              <span class="alert-sev ${sev}">${sev}</span>
            </div>
          </div>
        </div>`;
    }).join('');
  }

  /* ── History fetch (for dashboard mini-panel) ── */
  async loadHistory() {
    try {
      const res = await fetch('/history');
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const incidents = await res.json();

      const uptimeDays = this._calcUptimeDays(incidents);
      if (this.uptimeDays) {
        this.uptimeDays.textContent = uptimeDays !== null ? String(uptimeDays).padStart(3, '0') : '—';
      }
      if (this.uptimeBar) {
        this.uptimeBar.style.width = uptimeDays !== null ? `${Math.min(100, (uptimeDays / 30) * 100)}%` : '0%';
      }

      const monthCount = this._countMonthIncidents(incidents);
      if (this.incidentCount) {
        this.incidentCount.textContent = String(monthCount).padStart(2, '0');
      }

      if (this.incidentBar) {
        this.incidentBar.style.width = `${Math.min(100, (monthCount / 20) * 100)}%`;
      }

      if (this.historyCount) this.historyCount.textContent = incidents.length;
      if (this.historyTable) this._renderMiniHistory(incidents);

      if (this.historyPage) {
        this.historyPage.data = incidents;
        if (typeof this.historyPage._updateStats === 'function') this.historyPage._updateStats();
      }
    } catch (err) {
      console.error('[InfraWatch] History load failed:', err);
    }
  }

  /* ── Mini history (dashboard) ─────────────────── */
  _renderMiniHistory(incidents) {
    if (incidents.length === 0) {
      this.historyTable.innerHTML = `
        <div class="empty-state">
          <div class="es-icon" aria-hidden="true">
            <svg width="28" height="28" viewBox="0 0 28 28" fill="none">
              <path d="M6 22V8a2 2 0 012-2h12a2 2 0 012 2v14" stroke="currentColor" stroke-width="1.5"/>
              <path d="M3 22h22M10 11h8M10 15h5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
            </svg>
          </div>
          <div class="es-text">No incidents recorded yet</div>
        </div>`;
      return;
    }

    this.historyTable.innerHTML = incidents.slice(0, 10).map((inc) => {
      const sev = (inc.severity || 'critical').toLowerCase();
      return `
        <div class="history-row">
          <div class="history-time">${this.formatTime(inc.time)}</div>
          <div class="history-name">${this._esc(inc.name || 'Unknown')}</div>
          <div class="history-instance">${this._esc(inc.instance || '—')}</div>
          <div><span class="history-sev ${sev}">${sev}</span></div>
        </div>`;
    }).join('');
  }

  /* ── Metric helpers ────────────────────────────── */
  _updateMetricAlerts(count) {
    if (this.alertBar) {
      this.alertBar.style.width = `${Math.min(100, (count / 10) * 100)}%`;
    }
  }

  _calcUptimeDays(incidents) {
    if (!incidents || incidents.length === 0) return null;
    const newest = incidents[0];
    if (!newest?.time) return null;
    return Math.floor((Date.now() / 1000 - newest.time) / 86400);
  }

  _countMonthIncidents(incidents) {
    if (!incidents) return 0;
    const now = new Date();
    return incidents.filter((i) => {
      const d = new Date(i.time * 1000);
      return d.getMonth() === now.getMonth() && d.getFullYear() === now.getFullYear();
    }).length;
  }

  /* ── Indicators ────────────────────────────────── */
  _setNetIndicator(ok) {
    this.netIndicator.classList.toggle('success', ok);
    this.netIndicator.classList.toggle('error', !ok);
    this.netIndicator.setAttribute('aria-label', `Network: ${ok ? 'connected' : 'disconnected'}`);
  }

  _setHealthIndicator(statusState) {
    const isNormal = statusState === 'NORMAL' || statusState === true;
    const isWarning = statusState === 'WARNING';
    this.healthIndicator.classList.toggle('success', isNormal);
    this.healthIndicator.classList.toggle('warning', isWarning);
    this.healthIndicator.classList.toggle('error', !isNormal && !isWarning);
    this.healthIndicator.setAttribute('aria-label', `Health: ${statusState}`);
  }

  /* ── Sound control ─────────────────────────────── */
  /* ── Sound control & Web Audio Synth Fallback ────────────────────────────────── */
  _getAudioContext() {
    if (!this.audioCtx) {
      const AudioCtx = window.AudioContext || window.webkitAudioContext;
      if (AudioCtx) this.audioCtx = new AudioCtx();
    }
    if (this.audioCtx && this.audioCtx.state === 'suspended') {
      this.audioCtx.resume().catch(() => { });
    }
    return this.audioCtx;
  }

  _startSynthBeep() {
    this._stopSynthBeep();
    try {
      const ctx = this._getAudioContext();
      if (!ctx) return;

      this.synthOsc = ctx.createOscillator();
      this.synthGain = ctx.createGain();

      this.synthOsc.type = 'sawtooth';
      this.synthOsc.frequency.setValueAtTime(880, ctx.currentTime); // A5
      this.synthOsc.frequency.exponentialRampToValueAtTime(440, ctx.currentTime + 0.4);

      this.synthGain.gain.setValueAtTime(0.15, ctx.currentTime);

      this.synthOsc.connect(this.synthGain);
      this.synthGain.connect(ctx.destination);

      this.synthOsc.start();

      // Loop modulating frequency for alarm effect
      this.synthTimer = setInterval(() => {
        if (!this.synthOsc || !this.audioCtx || this.audioCtx.state !== 'running') return;
        try {
          const now = this.audioCtx.currentTime;
          this.synthOsc.frequency.setValueAtTime(880, now);
          this.synthOsc.frequency.exponentialRampToValueAtTime(440, now + 0.3);
        } catch (e) { }
      }, 450);
    } catch (e) {
      console.warn('[Audio] Web Audio synth fallback failed:', e);
    }
  }

  _stopSynthBeep() {
    if (this.synthTimer) {
      clearInterval(this.synthTimer);
      this.synthTimer = null;
    }
    if (this.synthOsc) {
      try {
        this.synthOsc.stop();
        this.synthOsc.disconnect();
      } catch (e) { }
      this.synthOsc = null;
    }
  }

  toggleSound(enabled) {
    this.isMuted = !enabled;
    const soundOn = document.getElementById('soundIconOn');
    const soundOff = document.getElementById('soundIconOff');
    if (soundOn) soundOn.style.display = enabled ? '' : 'none';
    if (soundOff) soundOff.style.display = enabled ? 'none' : '';

    if (this.isMuted) {
      this.stopAlarm();
    } else {
      const downCount = this.instancesPage?.data?.filter(t => t.health !== 'up')?.length || 0;
      if (downCount > 0) {
        this.playAlarm();
      }
    }
  }

  unlockAudio() {
    const ctx = this._getAudioContext();
    if (ctx && ctx.state === 'suspended') {
      ctx.resume().catch(() => { });
    }
    if (this.alarmAudio) {
      this.alarmAudio.muted = false;
      this.alarmAudio.volume = 1.0;
    }
    try { localStorage.setItem('iw-audio-unlocked', 'true'); } catch (e) { }
    console.log('[InfraWatch] Audio context & element unlocked cleanly');
  }

  playAlarm() {
    if (this.isMuted) return;

    if (this.isPlayingAlarm || this.hasPlayedForCurrentOutage) {
      return;
    }

    this.isPlayingAlarm = true;
    this.hasPlayedForCurrentOutage = true;

    if (this.alarmAudio) {
      this.alarmAudio.loop = true;
      this.alarmAudio.muted = false;
      this.alarmAudio.currentTime = 0;
      const p = this.alarmAudio.play();
      if (p !== undefined) {
        p.then(() => {
          console.log('[InfraWatch] Single MP3 alarm playing cleanly');
        }).catch((e) => {
          console.warn('[InfraWatch] HTML5 Audio play error, trying synth fallback:', e);
          this._startSynthBeep();
        });
      }
    } else {
      this._startSynthBeep();
    }

    // Automatically stop sound after 1 minute (60,000 ms)
    if (this.alarmTimeout) clearTimeout(this.alarmTimeout);
    this.alarmTimeout = setTimeout(() => {
      console.log('[InfraWatch] 1 minute alarm limit reached. Stopping audio.');
      this.stopAlarmAudioOnly();
    }, 60000);
  }

  stopAlarmAudioOnly() {
    this.isPlayingAlarm = false;
    if (this.alarmAudio) {
      try {
        this.alarmAudio.pause();
        this.alarmAudio.currentTime = 0;
      } catch (e) { }
    }
    this._stopSynthBeep();
  }

  stopAlarm() {
    if (this.alarmTimeout) {
      clearTimeout(this.alarmTimeout);
      this.alarmTimeout = null;
    }
    this.stopAlarmAudioOnly();
  }

  resetOutageAlarm() {
    this.hasPlayedForCurrentOutage = false;
    this.stopAlarm();
  }

  testAlarm() {
    // If user clicked test alarm while sound is off, auto turn sound on
    if (this.isMuted) {
      this.soundToggle.checked = true;
      this.toggleSound(true);
    }

    this.unlockAudio();
    this.alarmAudio.muted = false;
    this.alarmAudio.loop = false;
    this.alarmAudio.currentTime = 0;

    const p = this.alarmAudio.play();
    if (p !== undefined) {
      p.then(() => {
        this.audioWarning.classList.add('hidden');
      }).catch(() => {
        // Use Web Audio Synth for test beep
        this._startSynthBeep();
        setTimeout(() => this._stopSynthBeep(), 1500);
      });
    }
  }

  /* ── Escape HTML ───────────────────────────────── */
  _esc(str) {
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  /* ── Cleanup ───────────────────────────────────── */
  destroy() {
    clearInterval(this.statusCheckInterval);
    clearInterval(this.historyCheckInterval);
    this.instancesPage.onDeactivate();
  }
}



// ── Bootstrap ────────────────────────────────────────
const _boot = () => { window.monitor = new ServerMonitor(); };

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', _boot);
} else {
  _boot();
}

window.addEventListener('beforeunload', () => window.monitor?.destroy());

/* ── Universal TV Remote BACK Button Interceptor ──────────
   D-pad remotes report Back as Escape, "GoBack", or Backspace depending on
   the device/browser. Dismiss whatever's open (a modal or the side drawer)
   via its own close button — reusing existing close logic — rather than
   duplicating each dialog's teardown here. ──────────────────────────── */
window.addEventListener('keydown', e => {
  if (e.key !== 'Escape' && e.key !== 'GoBack' && e.key !== 'Backspace') return;

  const activeTag = document.activeElement?.tagName.toLowerCase();
  const isTextInput = activeTag === 'input' || activeTag === 'textarea';
  if (e.key === 'Backspace' && isTextInput) return; // let text editing behave normally

  const openModal = document.querySelector('.modal-backdrop:not(.hidden), .modal-overlay:not(.hidden)');
  const drawerOpen = document.getElementById('sideDrawer')?.classList.contains('drawer-open');
  if (!openModal && !drawerOpen) return;

  e.preventDefault();
  if (openModal) {
    openModal.querySelector('.modal-close')?.click();
  } else {
    document.getElementById('closeDrawerBtn')?.click();
  }
});

/* ── Modal Focus Trap Helper (WCAG 2.1 SC 2.4.3) ──────── */
window.trapModalFocus = function(modalEl) {
  if (!modalEl) return () => {};
  const handler = function(e) {
    if (e.key !== 'Tab') return;
    const focusable = Array.from(modalEl.querySelectorAll('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])')).filter(el => !el.disabled && el.offsetWidth > 0 && el.offsetHeight > 0);
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];

    if (e.shiftKey) {
      if (document.activeElement === first || !modalEl.contains(document.activeElement)) {
        last.focus();
        e.preventDefault();
      }
    } else {
      if (document.activeElement === last || !modalEl.contains(document.activeElement)) {
        first.focus();
        e.preventDefault();
      }
    }
  };

  modalEl.addEventListener('keydown', handler);
  return () => modalEl.removeEventListener('keydown', handler);
};

/* ── Modern Confirmation Dialog Helper ─────────────── */
window.showConfirmDialog = function({ title, message, confirmText = 'Ya, Hapus', cancelText = 'Batal', isDanger = true }) {
  return new Promise((resolve) => {
    const modal = document.getElementById('confirmModal');
    const titleEl = document.getElementById('confirmModalTitle');
    const msgEl = document.getElementById('confirmModalMessage');
    const cancelBtn = document.getElementById('confirmCancelBtn');
    const actionBtn = document.getElementById('confirmActionBtn');
    const closeBtn = document.getElementById('closeConfirmModalBtn');

    if (!modal) {
      resolve(window.confirm(message));
      return;
    }

    if (titleEl) titleEl.textContent = title || 'Konfirmasi Action';
    if (msgEl) msgEl.textContent = message || 'Apakah Anda yakin?';
    if (cancelBtn) cancelBtn.textContent = cancelText;
    if (actionBtn) {
      actionBtn.textContent = confirmText;
      actionBtn.style.background = isDanger ? '#EF4444' : 'var(--accent)';
    }

    modal.classList.remove('hidden');
    const untrap = window.trapModalFocus(modal);
    setTimeout(() => { if (cancelBtn) cancelBtn.focus(); }, 50);

    const cleanup = (result) => {
      untrap();
      modal.classList.add('hidden');
      if (cancelBtn) cancelBtn.removeEventListener('click', onCancel);
      if (actionBtn) actionBtn.removeEventListener('click', onConfirm);
      if (closeBtn) closeBtn.removeEventListener('click', onCancel);
      resolve(result);
    };

    const onCancel = () => cleanup(false);
    const onConfirm = () => cleanup(true);

    if (cancelBtn) cancelBtn.addEventListener('click', onCancel);
    if (actionBtn) actionBtn.addEventListener('click', onConfirm);
    if (closeBtn) closeBtn.addEventListener('click', onCancel);
  });
};
