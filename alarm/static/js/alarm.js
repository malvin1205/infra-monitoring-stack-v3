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

import './ui/dialog.js'; // window.trapModalFocus, window.showConfirmDialog
import { escapeHtml, slowThresholdMs } from './ui/format.js';
import { calculateNiceScale, buildMSGradientDefs } from './ui/charts.js';
import { LogsPage } from './logs.js';
import { HistoryPage } from './history.js';
import { apiFetch } from './net.js';

const JOB_DEFAULT_LS_KEY = 'infrawatch.defaultJob';

// Default Job is per Prometheus endpoint — switching endpoints must land on
// the NEW endpoint's own saved default (or "All Jobs"), never carry the old
// one over. Stored as { "<endpoint-url>": "<job>" }. A legacy bare-string
// value (the single global default from before this was per-endpoint) is not
// valid JSON, so it reads back as {} and the user re-sets once per endpoint.
function _loadJobDefaults() {
  try {
    const val = JSON.parse(localStorage.getItem(JOB_DEFAULT_LS_KEY) || '{}');
    return (val && typeof val === 'object') ? val : {};
  } catch (e) { return {}; }
}
function getDefaultJob(endpointUrl) {
  return endpointUrl ? (_loadJobDefaults()[endpointUrl] || null) : null;
}
function setDefaultJob(endpointUrl, job) {
  if (!endpointUrl) return;
  const map = _loadJobDefaults();
  if (job && job !== 'all') map[endpointUrl] = job;
  else delete map[endpointUrl];
  try { localStorage.setItem(JOB_DEFAULT_LS_KEY, JSON.stringify(map)); } catch (e) { }
}

// ── Session Authentication & Current User State ─────────────────────────────
window.currentUser = null;
window.isSystemInitialized = true;

async function checkAuthStatus() {
  try {
    const res = await fetch('/api/auth/status', {
      headers: { 'X-Requested-With': 'XMLHttpRequest' },
      credentials: 'same-origin'
    });
    if (res.ok) {
      const data = await res.json();
      window.isSystemInitialized = data.initialized;
      window.currentUser = data.user;
      updateUserUI(data.user);
      if (!data.initialized) {
        showSetupModal();
      }
      return data;
    }
  } catch (e) {
    console.error('Failed to fetch auth status', e);
  }
  return null;
}

function showSetupModal() {
  const modal = document.getElementById('setupModal');
  if (modal) {
    modal.classList.remove('hidden');
    document.getElementById('setupUsernameInput')?.focus();
  }
}

function closeSetupModal() {
  const modal = document.getElementById('setupModal');
  if (modal) modal.classList.add('hidden');
}

function showLoginModal() {
  const modal = document.getElementById('loginModal');
  if (modal) {
    modal.classList.remove('hidden');
    document.getElementById('loginUsernameInput')?.focus();
  }
}

function closeLoginModal() {
  const modal = document.getElementById('loginModal');
  if (modal) modal.classList.add('hidden');
}

// 'owner' (the founding account) and 'admin' share the same UI privileges;
// what only the owner can do is enforced server-side in the user-mgmt routes.
export function isAdminLike(u) {
  return !!u && (u.role === 'admin' || u.role === 'owner');
}

function showUsersModal() {
  // Manage Users needs an admin/owner session. The header button is already
  // hidden for non-admins, but a stale click (session expired since page load)
  // or a direct call should route to login, not open a modal that only 401s.
  if (!isAdminLike(window.currentUser)) {
    showLoginModal();
    return;
  }
  const modal = document.getElementById('usersModal');
  if (modal) {
    modal.classList.remove('hidden');
    fetchUsersList();
    document.getElementById('newUsernameInput')?.focus();
  }
}

function closeUsersModal() {
  const modal = document.getElementById('usersModal');
  if (modal) modal.classList.add('hidden');
}

async function patchUser(userId, payload) {
  const res = await apiFetch(`/api/auth/users/${userId}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok || !data.ok) {
    alert(data.error || 'Failed to update user');
    return false;
  }
  return true;
}

function initUsersListActions() {
  const container = document.getElementById('usersListContainer');
  if (!container) return;

  container.addEventListener('change', async (e) => {
    const sel = e.target.closest('.user-role-select');
    if (!sel) return;
    const prevValue = sel.dataset.prev || (sel.value === 'admin' ? 'viewer' : 'admin');
    sel.disabled = true;
    const ok = await patchUser(sel.dataset.userId, { role: sel.value });
    sel.disabled = false;
    if (ok) { sel.dataset.prev = sel.value; }
    else { sel.value = prevValue; }
  });

  container.addEventListener('click', async (e) => {
    const toggleBtn = e.target.closest('.user-status-toggle');
    if (toggleBtn) {
      if (toggleBtn.disabled) return;
      const wasActive = toggleBtn.dataset.active === '1';
      toggleBtn.disabled = true;
      const ok = await patchUser(toggleBtn.dataset.userId, { is_active: !wasActive });
      toggleBtn.disabled = false;
      if (ok) fetchUsersList();
      return;
    }
    const resetBtn = e.target.closest('.user-reset-pw-btn');
    if (resetBtn) {
      if (resetBtn.disabled) return;
      const newPassword = prompt('New password (min 12 chars):');
      if (newPassword === null) return;
      if (newPassword.length < 12) { alert('Password must be at least 12 characters'); return; }
      resetBtn.disabled = true;
      const ok = await patchUser(resetBtn.dataset.userId, { password: newPassword });
      resetBtn.disabled = false;
      if (ok) alert('Password updated.');
    }
  });
}

async function fetchUsersList() {
  const container = document.getElementById('usersListContainer');
  if (!container) return;
  const createForm = document.getElementById('createUserForm');
  try {
    const res = await apiFetch('/api/auth/users');
    if (res.status === 401 || res.status === 403) {
      // apiFetch suppresses its auto login-modal for /api/auth/* URLs, so a
      // 401 here just rendered a bare "Unauthorized". Make it actionable and
      // hide the create form (pointless without admin access).
      if (createForm) createForm.classList.add('hidden');
      container.innerHTML = `<div style="padding: 16px; text-align: center; font-size: 12px; color: var(--text-secondary);">`
        + `Your session has expired or you are not signed in as an administrator.`
        + `<div style="margin-top: 10px;"><button type="button" id="usersModalLoginBtn" class="btn btn-primary btn-sm">Log In</button></div>`
        + `</div>`;
      document.getElementById('usersModalLoginBtn')?.addEventListener('click', () => { closeUsersModal(); showLoginModal(); });
      return;
    }
    if (createForm) createForm.classList.remove('hidden');
    const data = await res.json();
    if (res.ok && data.ok) {
      if (!data.users || data.users.length === 0) {
        container.innerHTML = '<div style="padding: 12px; text-align: center; color: var(--text-muted); font-size: 12px;">No users found.</div>';
        return;
      }
      const selfId = window.currentUser ? window.currentUser.id : null;
      const viewerIsOwner = window.currentUser && window.currentUser.role === 'owner';
      container.innerHTML = `
        <table style="width: 100%; border-collapse: collapse; font-size: 12px; text-align: left;">
          <thead>
            <tr style="border-bottom: 1px solid var(--border); color: var(--text-secondary); background: var(--bg-card);">
              <th style="padding: 8px 12px;">Username</th>
              <th style="padding: 8px 12px;">Display Name</th>
              <th style="padding: 8px 12px;">Role</th>
              <th style="padding: 8px 12px;">Status</th>
              <th style="padding: 8px 12px; text-align: right;">Actions</th>
            </tr>
          </thead>
          <tbody>
            ${data.users.map(u => {
              // The owner row is read-only to everyone but the owner: no role
              // change (role is permanent), and status/password locked for a
              // non-owner viewer. Mirrors the server-side guards.
              const isOwnerRow = u.role === 'owner';
              const rowLocked = isOwnerRow && !viewerIsOwner;
              const roleCell = isOwnerRow
                ? `<span class="user-role-pill role-owner">OWNER</span>`
                : `<select class="search-input user-role-select" data-user-id="${u.id}" style="font-size: 11px; padding: 3px 6px; border-radius: var(--r-sm);">
                    <option value="admin" ${u.role === 'admin' ? 'selected' : ''}>ADMIN</option>
                    <option value="viewer" ${u.role === 'viewer' ? 'selected' : ''}>VIEWER</option>
                  </select>`;
              return `
              <tr style="border-bottom: 1px solid var(--border-subtle);" data-user-row="${u.id}">
                <td style="padding: 8px 12px; font-weight: 600; color: var(--text-primary);">${escapeHtml(u.username)}${u.id === selfId ? ' <span style="color: var(--text-muted); font-weight: 400;">(you)</span>' : ''}</td>
                <td style="padding: 8px 12px; color: var(--text-secondary);">${escapeHtml(u.display_name) || '—'}</td>
                <td style="padding: 8px 12px;">${roleCell}</td>
                <td style="padding: 8px 12px;">
                  <button type="button" class="user-status-toggle" data-user-id="${u.id}" data-active="${u.is_active ? '1' : '0'}" ${rowLocked ? 'disabled' : ''} style="background: none; border: none; cursor: ${rowLocked ? 'not-allowed' : 'pointer'}; padding: 0; color: ${u.is_active ? 'var(--success)' : 'var(--critical)'}; font-weight: 500; font-size: 12px; opacity: ${rowLocked ? '0.5' : '1'};">
                    ${u.is_active ? '● Active' : '○ Inactive'}
                  </button>
                </td>
                <td style="padding: 8px 12px; text-align: right;">
                  <button type="button" class="btn btn-secondary user-reset-pw-btn" data-user-id="${u.id}" ${rowLocked ? 'disabled' : ''} style="padding: 3px 8px; font-size: 11px; ${rowLocked ? 'opacity: 0.5; cursor: not-allowed;' : ''}">Reset Password</button>
                </td>
              </tr>
            `;
            }).join('')}
          </tbody>
        </table>
      `;
    } else {
      container.innerHTML = `<div style="padding: 12px; text-align: center; color: var(--critical); font-size: 12px;">${data.error || 'Failed to load users'}</div>`;
    }
  } catch (err) {
    if (createForm) createForm.classList.remove('hidden');
    container.innerHTML = '<div style="padding: 12px; text-align: center; color: var(--critical); font-size: 12px;">Network error loading users</div>';
  }
}

function updateUserUI(user) {
  const avatar = document.getElementById('userAvatar');
  const nameLabel = document.getElementById('userNameLabel');
  const rolePill = document.getElementById('userRolePill');
  const dropdownName = document.getElementById('dropdownUserName');
  const dropdownRole = document.getElementById('dropdownUserRole');
  const loginBtn = document.getElementById('headerLoginBtn');
  const logoutBtn = document.getElementById('headerLogoutBtn');
  const manageUsersBtn = document.getElementById('headerManageUsersBtn');

  if (user && user.username) {
    const name = user.display_name || user.username;
    if (avatar) avatar.textContent = (name[0] || 'U').toUpperCase();
    if (nameLabel) nameLabel.textContent = name;
    if (rolePill) {
      rolePill.textContent = (user.role || 'viewer').toUpperCase();
      rolePill.className = `user-role-pill role-${user.role || 'viewer'}`;
    }
    if (dropdownName) dropdownName.textContent = name;
    if (dropdownRole) dropdownRole.textContent = user.role === 'owner' ? 'Owner' : user.role === 'admin' ? 'Administrator' : 'Read-Only Viewer';
    if (loginBtn) loginBtn.classList.add('hidden');
    if (logoutBtn) logoutBtn.classList.remove('hidden');
    if (manageUsersBtn) {
      if (isAdminLike(user)) manageUsersBtn.classList.remove('hidden');
      else manageUsersBtn.classList.add('hidden');
    }
  } else {
    if (avatar) avatar.textContent = 'G';
    if (nameLabel) nameLabel.textContent = 'Guest';
    if (rolePill) {
      rolePill.textContent = 'VIEWER';
      rolePill.className = 'user-role-pill role-viewer';
    }
    if (dropdownName) dropdownName.textContent = 'Guest Operator';
    if (dropdownRole) dropdownRole.textContent = 'Read-Only Viewer';
    if (loginBtn) loginBtn.classList.remove('hidden');
    if (logoutBtn) logoutBtn.classList.add('hidden');
    if (manageUsersBtn) manageUsersBtn.classList.add('hidden');
  }
}

// One place to change the date/time locale for every chart axis, drawer
// timestamp and log row. 'id-ID' renders 24h "HH.MM" and "DD Mmm"; switch to
// e.g. 'en-GB' for "HH:MM" if the wallboard audience is non-Indonesian.
const DATE_LOCALE = 'id-ID';

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
    this._activeEndpoint = null;  // current Prometheus URL — set by initEndpointManager; keys the per-endpoint Default Job
    this._defaultJobRestored = false; // guards the restore of this endpoint's Default Job; re-armed on every endpoint switch
    this.searchQ = '';
    this.selectedTarget = null;
    this.isAcknowledged = false;
    this.acknowledgedDownInstances = new Set();
    this._spotlightedInstances = new Set();

    this.table = document.getElementById('instancesBody');
    this.countBadge = document.getElementById('instanceCount');
    this._downCardElements = [];
    this._maintCardElements = [];

    // Pagination (TV wallboard) — pageSize is not a fixed number, it's
    // however many cards actually fit the grid viewport without shrinking
    // below a readable size. See _calculateGridCapacity()/_applyGridCapacity().
    this.pageSize = 40; // safe seed until the first real measurement lands
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
    this._showAllHostsInBreakdown = false;
    this._breakdownSort = 'avail_asc';

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
    this._availLoading = false;
    this._availCache = new Map(); // cacheKey -> { timestamp, data }
    this._availCacheTTL = 30000;  // 30s client-side cache for instant zero-latency range switching
    this._targetHistorySeq = 0;

    this._bindEvents();
    this._setupGridCapacityObserver();
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

    // Acknowledge Alarm button (Server-Side Global Incident State)
    const ackBtn = document.getElementById('ackAlarmBtn');
    if (ackBtn) {
      ackBtn.addEventListener('click', async () => {
        try {
          const res = await apiFetch('/api/alerts/ack', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({})
          });
          const data = await res.json();
          if (res.ok && data.ok) {
            this.isAcknowledged = true;
            if (this.monitor && typeof this.monitor.stopAlarm === 'function') {
              this.monitor.stopAlarm();
            }
            const ackUser = window.currentUser ? window.currentUser.username : 'operator';
            this._triggerEventToast(`Alarm acknowledged by ${ackUser}`);
            this.load();
          } else if (res.status === 403) {
            this._triggerEventToast('Permission denied: Viewer cannot acknowledge alerts.');
          }
        } catch (err) {
          console.error('Failed to acknowledge alerts', err);
        }
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
      jobSelect.addEventListener('change', async e => {
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

    // Skin every other native <select> in the app with the same dropdown.
    this._enhanceAllSelects();

    // Period / time-range chips (24h / 7d / 30d / Custom Range)
    if (this.rangeChipsGroup) {
      this.rangeChipsGroup.addEventListener('click', async e => {
        const btn = e.target.closest('[data-range]');
        if (!btn) return;
        const range = btn.dataset.range;

        if (range === 'custom') {
          this._toggleCustomRangePopover();
          return;
        }

        this._closeCustomRangePopover();
        this._setActiveRangeChip(range);

        if (range === 'mtd') {
          this.isRealtime = false;
          if (this.availabilityDetailBtn) this.availabilityDetailBtn.style.display = '';
          this.periodEnd = null;
          this.periodLabel = range;
          this.periodMinutes = this._monthToDateMinutes();
          const modalRangeSelectEl = document.getElementById('modalRangeSelect');
          if (modalRangeSelectEl) modalRangeSelectEl.value = range;
          this.loadAvailability(true);
          if (this.selectedTarget) this.loadTargetHistory(this.selectedTarget.instance);
          return;
        }

        if (range === 'realtime') {
          this.isRealtime = true;
          this.periodLabel = 'realtime';
          this.periodEnd = null;
          if (this.availabilityLabel) this.availabilityLabel.textContent = 'Availability (Realtime)';
          const drawerUptimeLabel = document.getElementById('drawerUptimeLabel');
          if (drawerUptimeLabel) drawerUptimeLabel.textContent = 'Uptime (Realtime)';
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

        const modalRangeSelect = document.getElementById('modalRangeSelect');
        if (modalRangeSelect) {
          const curMins = Math.round(this.periodMinutes);
          const opt = Array.from(modalRangeSelect.options).find(o => Math.round(parseFloat(o.value)) === curMins);
          if (opt) modalRangeSelect.value = opt.value;
        }

        this.loadAvailability(false);
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

    const modalRangeSelect = document.getElementById('modalRangeSelect');
    if (modalRangeSelect) {
      modalRangeSelect.addEventListener('change', async e => {
        if (e.target.value === 'mtd') {
          this.isRealtime = false;
          this.periodEnd = null;
          this.periodLabel = 'mtd';
          this.periodMinutes = this._monthToDateMinutes();
          this._setActiveRangeChip('mtd');
          this.loadAvailability(true);
          return;
        }
        const mins = parseFloat(e.target.value);
        if (!isNaN(mins) && mins > 0) {
          this.periodMinutes = mins;
          this.isRealtime = false;
          this.periodEnd = null;
          let label = '24h';
          if (mins === 10080) label = '7d';
          else if (mins === 43200) label = '30d';
          this.periodLabel = label;
          this._setActiveRangeChip(label);
          this.loadAvailability(false);
        }
      });
    }

    // Availability Breakdown modal: Overview<->Audit sub-nav, the gear-icon
    // Node Exporter correlation settings popover, and the per-host audit
    // table's filter chips/search — all previously present in the markup
    // with zero JS behind them.
    this._initAvailSubnav();
    this._initAvailSettingsPopover();
    this._initAuditFilters();

    // Custom Dropdown for Sorting Hosts in Availability Breakdown Modal
    const availSortTrigger = document.getElementById('availSortTrigger');
    const availSortMenu = document.getElementById('availSortMenu');
    
    if (availSortTrigger && availSortMenu) {
      const toggleSortMenu = (open) => {
        const isHidden = typeof open === 'boolean' ? !open : !availSortMenu.classList.contains('hidden');
        availSortMenu.classList.toggle('hidden', isHidden);
        availSortTrigger.setAttribute('aria-expanded', String(!isHidden));
        if (!isHidden) {
          const activeItem = availSortMenu.querySelector('.avail-sort-item.is-active') || availSortMenu.firstElementChild;
          activeItem?.focus();
        }
      };

      availSortTrigger.addEventListener('click', (e) => {
        e.stopPropagation();
        toggleSortMenu();
      });

      availSortTrigger.addEventListener('keydown', (e) => {
        if (e.key === 'ArrowDown' || e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          toggleSortMenu(true);
        }
      });

      availSortMenu.addEventListener('click', (e) => {
        const item = e.target.closest('.avail-sort-item');
        if (!item) return;
        const val = item.dataset.value;
        if (val) {
          this._breakdownSort = val;
          toggleSortMenu(false);
          availSortTrigger.focus();
          this._renderAvailabilityBreakdown('sortMenu:click');
        }
      });

      availSortMenu.addEventListener('keydown', (e) => {
        const items = Array.from(availSortMenu.querySelectorAll('.avail-sort-item'));
        const currentIndex = items.indexOf(document.activeElement);

        if (e.key === 'ArrowDown') {
          e.preventDefault();
          const nextIndex = (currentIndex + 1) % items.length;
          items[nextIndex]?.focus();
        } else if (e.key === 'ArrowUp') {
          e.preventDefault();
          const prevIndex = (currentIndex - 1 + items.length) % items.length;
          items[prevIndex]?.focus();
        } else if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          const active = document.activeElement?.closest('.avail-sort-item');
          if (active && active.dataset.value) {
            this._breakdownSort = active.dataset.value;
            toggleSortMenu(false);
            availSortTrigger.focus();
            this._renderAvailabilityBreakdown('sortMenu:enter');
          }
        } else if (e.key === 'Escape' || e.key === 'Tab') {
          // Close only this dropdown — don't let the keystroke bubble to the
          // window-level BACK/Escape interceptor, which would then also close
          // the whole Availability Breakdown modal.
          if (e.key === 'Escape') e.stopPropagation();
          toggleSortMenu(false);
          availSortTrigger.focus();
        }
      });

      document.addEventListener('click', (e) => {
        if (!availSortMenu.classList.contains('hidden') && !e.target.closest('#availSortContainer')) {
          toggleSortMenu(false);
        }
      });
    }

    const tableHeaderEl = document.querySelector('.avail-table-header');
    if (tableHeaderEl) {
      tableHeaderEl.addEventListener('click', e => {
        const btn = e.target.closest('.ath-sort-btn');
        if (!btn) return;
        const key = btn.dataset.sortKey;
        if (key === 'name') {
          this._breakdownSort = this._breakdownSort === 'name_asc' ? 'name_desc' : 'name_asc';
        } else if (key === 'avail') {
          this._breakdownSort = this._breakdownSort === 'avail_asc' ? 'avail_desc' : 'avail_asc';
        } else if (key === 'impact') {
          this._breakdownSort = this._breakdownSort === 'incidents_desc' ? 'downtime_desc' : 'incidents_desc';
        }
        this._renderAvailabilityBreakdown('tableHeader:sort');
      });
    }

    const btnViewAllHosts = document.getElementById('btnViewAllHosts');
    if (btnViewAllHosts) {
      btnViewAllHosts.addEventListener('click', () => {
        this._showAllHostsInBreakdown = !this._showAllHostsInBreakdown;
        this._renderAvailabilityBreakdown('btnViewAllHosts:toggle');
      });
    }

    // Delegated event handling for host rows in breakdown table
    const attentionListEl = document.getElementById('hostsRequiringAttentionList');
    if (attentionListEl) {
      const handleRowSelect = (el) => {
        const row = el.closest('.ara-row');
        if (!row) return;
        const inst = row.dataset.instance;
        if (inst) {
          const target = this.data.find(t => t.instance === inst) || { instance: inst, job: 'blackbox' };
          this._closeAvailabilityBreakdown();
          this._openDrawer(target);
        }
      };
      attentionListEl.addEventListener('click', e => handleRowSelect(e.target));
      attentionListEl.addEventListener('keydown', e => {
        if (e.key === 'Enter' || e.key === ' ') {
          const row = e.target.closest('.ara-row');
          if (row) {
            e.preventDefault();
            handleRowSelect(row);
          }
        }
      });
    }

    const btnLearnCalculations = document.getElementById('btnLearnCalculations');
    if (btnLearnCalculations) {
      btnLearnCalculations.addEventListener('click', () => {
        const panel = document.getElementById('calcExplanationPanel');
        if (panel) {
          const isHidden = panel.classList.toggle('hidden');
          btnLearnCalculations.setAttribute('aria-expanded', !isHidden);
        }
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

  /* ── Pagination (TV wallboard: adaptive cards/page, see _calculateGridCapacity) ── */
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

  /* ── Adaptive Grid Capacity ──────────────────────────────────────────
     pageSize is derived from the grid's actual laid-out size, not a
     hardcoded count. Column count comes straight from the browser's own
     resolved `grid-template-columns` (auto-fill pre-creates every column
     that fits, even with fewer cards than columns, so this is authoritative
     regardless of how many hosts are on the current page). Row count is
     the same available-height-over-min-card-height math the CSS itself
     uses for `grid-auto-rows: minmax(--hc-min-h, 1fr)`, so JS and CSS never
     disagree about how many rows actually fit. ── */
  _calculateGridCapacity() {
    const grid = this.table;
    const fallback = { columns: 1, rows: 1, pageSize: this.pageSize || 40 };
    if (!grid) return fallback;

    const width = grid.clientWidth;
    const height = grid.clientHeight;
    if (!width || !height) return fallback; // not laid out yet — keep prior value rather than guess

    const cs = getComputedStyle(grid);
    const columns = cs.gridTemplateColumns.split(' ').filter(Boolean).length || 1;

    const minCardH = parseFloat(cs.getPropertyValue('--hc-min-h')) || 56;
    const rowGap = parseFloat(cs.rowGap) || 0;
    const availHeight = height - (parseFloat(cs.paddingTop) || 0) - (parseFloat(cs.paddingBottom) || 0);
    const rows = Math.max(1, Math.floor((availHeight + rowGap) / (minCardH + rowGap)));

    return { columns, rows, pageSize: Math.max(1, columns * rows) };
  }

  // Re-measures grid capacity; if it actually changed (resize, zoom, DPI),
  // reflows pagination without losing hosts or stranding the user on an
  // empty page — keeps whichever host was first-visible in view instead of
  // jumping back to page 1.
  _applyGridCapacity() {
    const cap = this._calculateGridCapacity();
    if (cap.pageSize === this.pageSize) return;
    const firstVisibleIdx = (this.currentPage - 1) * this.pageSize;
    this.pageSize = cap.pageSize;
    this.currentPage = Math.floor(firstVisibleIdx / this.pageSize) + 1;
    // No data yet (first measurement lands before load() resolves) — the
    // updated pageSize is already in place for load()'s own _render() call,
    // don't churn the still-showing skeleton loader in the meantime.
    if (this.data && this.data.length) this._render();
  }

  _setupGridCapacityObserver() {
    if (!this.table || typeof ResizeObserver === 'undefined') return;
    let debounceTimer = null;
    this._gridResizeObserver = new ResizeObserver(() => {
      clearTimeout(debounceTimer);
      debounceTimer = setTimeout(() => this._applyGridCapacity(), 120);
    });
    this._gridResizeObserver.observe(this.table);
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
      if (this._downCardElements && this._downCardElements.length > 0) {
        this._downCardElements.forEach(({ inst, latEl }) => {
          const target = this.data.find(t => t.instance === inst);
          if (!target) return;

          let downMs = 0;
          if (target.downSince && target.downSince > 0) {
            downMs = Math.max(0, now - (target.downSince * 1000));
          } else {
            if (!this.downStartTimes[inst]) this.downStartTimes[inst] = now;
            downMs = Math.max(0, now - this.downStartTimes[inst]);
          }

          if (latEl) {
            latEl.textContent = this._downLabel(target, this._fmtDownAging(downMs));
          }
        });
      }

      if (this._maintCardElements && this._maintCardElements.length > 0) {
        this._maintCardElements.forEach(({ inst, latEl }) => {
          const target = this.data.find(t => t.instance === inst);
          if (!target || !target.maintenanceUntil) return;
          const remainMs = Math.max(0, target.maintenanceUntil * 1000 - now);
          if (latEl) latEl.textContent = `Maint ${this._fmtDownAging(remainMs)} left`;
        });
      }
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
  }

  /* ── Historical availability (past N days) ─────── */
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

  /* ── Period / range selector (toolbar) ─────────── */
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
      // is_alarmable/effective_status/active_alerts are backend-derived from
      // Alertmanager alerts, not just probe health — an alert can fire/clear
      // (e.g. a warning-severity CPU alert) with health/downSince/etc all
      // unchanged, which used to leave the signature identical and skip
      // _render(), so the top-level alarm banner could flip while every
      // per-target card silently stayed stale.
      const alertsSig = Array.isArray(t.active_alerts)
        ? t.active_alerts.map(a => `${a.name}:${a.severity}`).join(',')
        : '';
      sig += t.instance + '|' + t.job + '|' + t.health + '|' + t.responseTimeMs + '|' + t.downSince + '|' + t.maintenance + '|' + t.suppressedBy + '|' + t.failureCategory + '|' + t.is_alarmable + '|' + t.effective_status + '|' + alertsSig + ';';
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

      // Another client (another device/tab) may have repointed the server's
      // GLOBAL active Prometheus endpoint out from under us — the job filter
      // and topbar picker here are now stale, and a job absent on the new
      // endpoint renders an empty "no hosts match" grid. The poll response
      // names the URL actually served; if that isn't the one we think is
      // active, re-check the registry — then, only if the *registered*
      // active really moved (a transient fetch fallback to a secondary
      // endpoint also lands here and must NOT count), do locally what a
      // manual endpoint switch does and re-fetch against the new endpoint.
      if (data.prometheus_url && this._activeEndpoint && data.prometheus_url !== this._activeEndpoint) {
        const known = this._activeEndpoint;
        if (this.monitor && typeof this.monitor._syncEndpointsUI === 'function') {
          await this.monitor._syncEndpointsUI(); // refreshes this._activeEndpoint from /api/endpoints
        }
        if (this._activeEndpoint && this._activeEndpoint !== known) {
          this._resetJobFilter();
          this._triggerEventToast('Active Prometheus endpoint changed elsewhere — job filter reset.');
          this.loadAvailability(); // 24h % / trend are per endpoint+job too
          return this.load();
        }
      }

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
      this.serverPayload = data;
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
        setDefaultJob(this._activeEndpoint, this.selectedJob);
        this._triggerEventToast('Default Job saved.');
        this._updateJobDefaultUI();
        this._closeJobDefaultPopover();
      });
    }

    if (resetBtn) {
      resetBtn.addEventListener('click', () => {
        setDefaultJob(this._activeEndpoint, null);
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

  // Skins a native <select> with the same custom dropdown as the Job filter
  // (.job-dd-* : pill trigger, chevron, panel with blue hover). The <select>
  // stays in the DOM as the single source of truth — every existing reader/
  // writer of .value and every 'change' listener keeps working; picking here
  // sets .value and dispatches 'change' exactly as the native control would.
  // UI only, no business logic.
  _enhanceSelect(sel) {
    if (!sel || sel.dataset.ddEnhanced || !sel.parentNode) return;
    sel.dataset.ddEnhanced = '1';

    // A hidden required control blocks native form submission ("not
    // focusable") — these forms all validate in JS anyway (see
    // _submitAddTarget), so drop it.
    sel.removeAttribute('required');

    const block = sel.classList.contains('form-select');
    const wrap = document.createElement('span');
    wrap.className = 'dd' + (block ? ' dd-block' : '');
    sel.parentNode.insertBefore(wrap, sel.nextSibling);
    sel.classList.add('dd-native');
    wrap.appendChild(sel);

    const trigger = document.createElement('button');
    trigger.type = 'button';
    trigger.className = 'job-dd-trigger';
    trigger.setAttribute('aria-haspopup', 'listbox');
    trigger.setAttribute('aria-expanded', 'false');
    const al = sel.getAttribute('aria-label') || sel.getAttribute('title');
    if (al) trigger.setAttribute('aria-label', al);
    const label = document.createElement('span');
    label.className = 'job-dd-label';
    trigger.appendChild(label);
    trigger.insertAdjacentHTML('beforeend',
      '<svg aria-hidden="true" focusable="false" class="job-dd-caret" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>');
    const menu = document.createElement('ul');
    menu.className = 'job-dd-menu hidden';
    menu.setAttribute('role', 'listbox');
    menu.tabIndex = -1;
    wrap.appendChild(trigger);
    wrap.appendChild(menu);

    const enabled = () => Array.from(menu.children).filter(li => li.dataset.disabled !== '1');
    const setActive = li => {
      if (!li) return;
      menu.querySelectorAll('.job-dd-option-active').forEach(el => el.classList.remove('job-dd-option-active'));
      li.classList.add('job-dd-option-active');
      li.scrollIntoView({ block: 'nearest' });
    };
    const rebuild = () => {
      menu.innerHTML = '';
      Array.from(sel.options).forEach(o => {
        if (o.hidden) return;
        const li = document.createElement('li');
        li.className = 'job-dd-option';
        li.setAttribute('role', 'option');
        li.dataset.value = o.value;
        li.textContent = o.textContent;
        if (o.disabled) { li.dataset.disabled = '1'; li.setAttribute('aria-disabled', 'true'); }
        li.setAttribute('aria-selected', String(o.value === sel.value));
        menu.appendChild(li);
      });
      const cur = sel.options[sel.selectedIndex];
      label.textContent = cur ? cur.textContent : '';
      trigger.disabled = sel.disabled;
    };
    const open = () => {
      if (sel.disabled) return;
      rebuild();
      menu.classList.remove('hidden');
      trigger.setAttribute('aria-expanded', 'true');
      setActive(menu.querySelector('[aria-selected="true"]:not([aria-disabled])') || enabled()[0]);
      menu.focus();
    };
    const close = () => {
      if (menu.classList.contains('hidden')) return;
      menu.classList.add('hidden');
      trigger.setAttribute('aria-expanded', 'false');
    };
    const pick = li => {
      if (!li || li.dataset.disabled === '1') return;
      if (sel.value !== li.dataset.value) {
        sel.value = li.dataset.value;
        sel.dispatchEvent(new Event('change', { bubbles: true }));
      }
      rebuild();
      close();
      trigger.focus();
    };

    trigger.addEventListener('click', e => { e.stopPropagation(); if (menu.classList.contains('hidden')) open(); else close(); });
    trigger.addEventListener('keydown', e => {
      if (e.key === 'ArrowDown' || e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); }
    });
    menu.addEventListener('click', e => { const li = e.target.closest('.job-dd-option'); if (li) pick(li); });
    menu.addEventListener('keydown', e => {
      const list = enabled();
      if (!list.length) return;
      const i = Math.max(0, list.findIndex(li => li.classList.contains('job-dd-option-active')));
      switch (e.key) {
        case 'ArrowDown': e.preventDefault(); setActive(list[Math.min(list.length - 1, i + 1)]); break;
        case 'ArrowUp': e.preventDefault(); setActive(list[Math.max(0, i - 1)]); break;
        case 'Home': e.preventDefault(); setActive(list[0]); break;
        case 'End': e.preventDefault(); setActive(list[list.length - 1]); break;
        case 'Enter': case ' ': e.preventDefault(); pick(list[i]); break;
        case 'Escape': e.preventDefault(); close(); trigger.focus(); break;
        case 'Tab': close(); break;
      }
    });
    document.addEventListener('click', e => { if (!wrap.contains(e.target)) close(); });
    document.addEventListener('keydown', e => { if (e.key === 'Escape' && !menu.classList.contains('hidden')) { close(); trigger.focus(); } });

    // Runtime-injected <option>s (endpoint list, target-URL list, dependency
    // parents) + programmatic disabled toggles → rebuild trigger + menu.
    new MutationObserver(() => rebuild()).observe(sel, { childList: true, subtree: true, attributes: true, attributeFilter: ['disabled'] });
    // ponytail: a bare `sel.value = x` with no dispatched 'change' leaves the
    // trigger label stale until the next open(); the value stays correct and
    // open() re-reads it, so not worth patching the value setter.
    sel.addEventListener('change', () => rebuild());

    rebuild();
  }

  _enhanceAllSelects() {
    // #jobSelect keeps its bespoke controller (gear / Default badge / popover).
    document.querySelectorAll('select:not(#jobSelect)').forEach(sel => this._enhanceSelect(sel));
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

    const stored = getDefaultJob(this._activeEndpoint);
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
    // Endpoint list not loaded yet (first paint race) — leave the guard
    // un-armed so a later load() retries once _activeEndpoint is known.
    if (!this._activeEndpoint) return;
    this._defaultJobRestored = true;

    const stored = getDefaultJob(this._activeEndpoint);
    if (!stored) return;

    // This endpoint's saved default names a job it doesn't currently expose —
    // stay on "All Jobs"; the saved default is kept for when it reappears.
    const exists = Array.from(jobSelect.options).some(o => o.value === stored);
    if (!exists) return;

    if (jobSelect.value !== stored) {
      jobSelect.value = stored;
      jobSelect.dispatchEvent(new Event('change')); // also triggers _updateJobDefaultUI via the change listener
    } else {
      this._updateJobDefaultUI();
    }
  }

  // Endpoint switch: the previous endpoint's job list and any active job
  // filter mean nothing against a different Prometheus (filtering on a job
  // the new endpoint has never scraped yields an empty grid that looks
  // stuck). Drop to "All Jobs", prune the stale <option>s / <li>s, and
  // re-arm the restore so the next load() applies the NEW endpoint's own
  // Default Job (getDefaultJob(this._activeEndpoint)) if it has one.
  _resetJobFilter() {
    this.selectedJob = 'all';
    this._defaultJobRestored = false;
    this._lastDataSignature = null;

    const jobSelect = document.getElementById('jobSelect');
    if (jobSelect) {
      Array.from(jobSelect.options).forEach(o => { if (o.value !== 'all') o.remove(); });
      jobSelect.value = 'all';
    }
    const els = this._jobFilterEls;
    if (els && els.ddMenu) {
      Array.from(els.ddMenu.children).forEach(li => { if (li.dataset.value !== 'all') li.remove(); });
    }
    this._updateJobDefaultUI();
  }

  _checkStateTransitions(newTargets) {
    const now = Date.now();
    newTargets.forEach(t => {
      const prev = this.previousStates[t.instance];
      const curr = t.health || 'unknown';
      // Only a real 'down' ages a down-counter. 'unknown' = no Prometheus
      // sample — stamping now here is what produced the phantom "Down 57s"
      // ticker on every startup.
      if (curr === 'down') {
        if (t.downSince && t.downSince > 0) {
          this.downStartTimes[t.instance] = t.downSince * 1000;
        } else if (!this.downStartTimes[t.instance]) {
          this.downStartTimes[t.instance] = now;
        }
      } else {
        delete this.downStartTimes[t.instance];
      }

      if (prev && prev !== curr) {
        const label = curr === 'up' ? 'back online' : (curr === 'down' ? 'went offline' : 'reporting no data');
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
    if (this.errorMsg) this.errorMsg.textContent = msg || 'Prometheus engine is unreachable';
    // Preserve existing table layout and host cards so previously loaded data remains visible!
    if (this.table && (!this.data || this.data.length === 0)) {
      this._downCardElements = [];
      this._maintCardElements = [];
      this.table.innerHTML = `
        <div class="empty-state">
          <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
          <span>Waiting for Prometheus data — ${this._esc(msg)}</span>
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
    const down = this.data.filter(t => t.health === 'down').length;
    const slow = this.data.filter(t => t.health === 'up' && t.responseTimeMs > slowThresholdMs(t)).length;

    // Backend authoritative state (Phase 2 canonical monitoring model)
    const serverSummary = this.serverPayload ? this.serverPayload.summary : null;
    const serverStatus = this.serverPayload ? this.serverPayload.system_status : null;

    // Alarm-eligible downs — respect backend's authoritative is_alarmable flag if available,
    // otherwise fallback to non-maintenance, non-suppressed down targets.
    const alarmableDown = this.data.filter(t => t.is_alarmable !== undefined ? (t.is_alarmable && t.health !== 'up') : (t.health !== 'up' && !t.maintenance && !t.suppressedBy));
    const hasAlarm = serverSummary ? serverSummary.has_alarm : (alarmableDown.length > 0 || this.data.some(t => t.is_alarmable));

    // Live snapshot summary cards: Total Hosts, Online, Warning, Offline
    const onlineHealthy = Math.max(0, up - slow);
    if (this.statTotal) this.statTotal.textContent = total;
    if (this.statUp) this.statUp.textContent = onlineHealthy;
    const statSlow = document.getElementById('instSlow');
    if (statSlow) statSlow.textContent = slow;
    if (this.statDown) this.statDown.textContent = down;

    // Only realtime mode owns Card 5 here — historical mode leaves it to
    // loadAvailability()'s /api/availability response. Previously this also
    // fired on a bare '—' placeholder (first paint / preset switch), which
    // raced that response and made the card flicker between the live
    // snapshot % and the historical %.
    if (this.isRealtime) {
      const pct = total > 0 ? (up / total) * 100 : 0;
      this.statUptime.textContent = `${pct.toFixed(2)}%`;
      // Realtime is a live snapshot, never a windowed aggregate — drop any
      // "limited data" marker left over from a historical range.
      this.statUptime.classList.remove('is-limited-data');
      this.statUptime.removeAttribute('title');
    }
    // Keep an open drawer's uptime figure live too.
    if (this.selectedTarget) {
      const fresh = this.data.find(t => t.instance === this.selectedTarget.instance) || this.selectedTarget;
      this._updateDrawerUptime(fresh);
    }

    // Last probe time
    if (this.lastProbe) {
      this.lastProbe.textContent = `Updated ${new Date().toLocaleTimeString()}`;
    }

    // Acknowledge Button & Global health badge (Server-Side Global Incident State)
    const ackBtn = document.getElementById('ackAlarmBtn');
    const ackLabel = document.getElementById('ackBtnLabel');

    // Global Server-Side Acknowledgment check
    const isGloballyAcked = serverSummary ? !!serverSummary.is_acknowledged : (alarmableDown.length > 0 && alarmableDown.every(t => t.acknowledged));
    const wasAcknowledged = this.isAcknowledged;
    this.isAcknowledged = isGloballyAcked;
    // A fresh, still-unacknowledged outage arrived while a prior outage was
    // acked (ack -> unacked transition without ever hitting all-clear) — let
    // the alarm sound again for it instead of staying muted by the old guard.
    if (wasAcknowledged && !isGloballyAcked && this.monitor && typeof this.monitor.resetOutageAlarm === 'function') {
      this.monitor.resetOutageAlarm();
    }

    // Track acknowledged down instances from server payload
    const currentDownList = alarmableDown.map(t => t.instance);
    this.acknowledgedDownInstances = new Set(this.data.filter(t => t.acknowledged).map(t => t.instance));

    // Wallboard "critical spotlight" (Phase 14) — jump to a newly-down host
    // once, the moment it appears, instead of re-jumping every poll tick.
    if (this.monitor && typeof this.monitor.spotlightHost === 'function') {
      const freshlyDown = currentDownList.filter(inst => !this._spotlightedInstances.has(inst));
      currentDownList.forEach(inst => this._spotlightedInstances.add(inst));
      if (freshlyDown.length > 0) this.monitor.spotlightHost(freshlyDown[0]);
    }
    if (currentDownList.length === 0) this._spotlightedInstances.clear();

    if (hasAlarm || alarmableDown.length > 0 || slow > 0) {
      if (ackBtn) {
        ackBtn.classList.remove('hidden');
        if (this.isAcknowledged) {
          ackBtn.className = 'ack-alarm-btn ack-done is-acked';
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

    // Authoritative Global health badge & Alarm Trigger
    const isCritical = serverStatus === 'CRITICAL' || alarmableDown.length > 0;
    const isDegraded = serverStatus === 'WARNING' || slow > 0;

    if (isCritical) {
      if (this.healthBadge) {
        this.healthBadge.className = 'status-pill pill-critical';
        this.healthBadge.innerHTML = '<span class="pill-dot"></span><span class="pill-label">Critical</span>';
      }
      if (!this.isAcknowledged && this.monitor && typeof this.monitor.playAlarm === 'function') {
        this.monitor.playAlarm();
      } else if (this.isAcknowledged && this.monitor && typeof this.monitor.stopAlarm === 'function') {
        this.monitor.stopAlarm();
      }
    } else {
      if (this.healthBadge) {
        if (isDegraded) {
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
    if (liveDot) liveDot.style.background = isCritical ? 'var(--critical)' : (isDegraded ? 'var(--warning, #f59e0b)' : 'var(--success)');

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
      rows = rows.filter(t => t.health === 'up' && !(t.responseTimeMs > slowThresholdMs(t)));
    } else if (this.activeStatus === 'down') {
      rows = rows.filter(t => t.health !== 'up');
    } else if (this.activeStatus === 'slow') {
      rows = rows.filter(t => t.health === 'up' && t.responseTimeMs > slowThresholdMs(t));
    }

    // Search filter
    if (this.searchQ) {
      rows = rows.filter(t => t.instance.toLowerCase().includes(this.searchQ));
    }

    if (this.countBadge) this.countBadge.textContent = rows.length;
    if (!this.table) return;

    if (rows.length === 0) {
      this._downCardElements = [];
      this._maintCardElements = [];
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
        const ao = a.health !== 'up' ? 2 : (a.responseTimeMs > slowThresholdMs(a) ? 1 : 0);
        const bo = b.health !== 'up' ? 2 : (b.responseTimeMs > slowThresholdMs(b) ? 1 : 0);
        if (ao !== bo) return bo - ao;
        return a.instance.localeCompare(b.instance, undefined, { numeric: true, sensitivity: 'base' });
      }
    });

    this._sortedRows = rows;

    // Paginate — pageSize is adaptive (see _calculateGridCapacity), not a
    // fixed count. Clamp instead of resetting to page 1 so polling/live
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
        const isDown = t.health === 'down';
        const isNoData = !isUp && !isDown;   // 'unknown' — Prometheus has no probe sample; not an outage
        const isSlow = isUp && t.responseTimeMs > slowThresholdMs(t);

        const stateClass = t.maintenance ? 'hc-maintenance' : (isDown ? 'hc-down' : (isNoData ? 'hc-nodata' : (isSlow ? 'hc-slow' : 'hc-up')));
        const isAcked = isDown && !t.maintenance && this.acknowledgedDownInstances.has(t.instance);
        const fullClass = `host-card ${stateClass}${t.suppressedBy ? ' hc-suppressed' : ''}${isAcked ? ' hc-acked' : ''}`;
        if (card.className !== fullClass) {
          card.className = fullClass;
        }

        let latencyText = '';
        if (t.maintenance) {
          const remainMs = t.maintenanceUntil ? Math.max(0, t.maintenanceUntil * 1000 - now) : 0;
          latencyText = `Maint ${this._fmtDownAging(remainMs)} left`;
        } else if (isNoData) {
          latencyText = 'No data';
        } else if (isDown) {
          let downMs = 0;
          if (t.downSince && t.downSince > 0) {
            downMs = Math.max(0, now - (t.downSince * 1000));
          } else if (this.downStartTimes[t.instance]) {
            downMs = Math.max(0, now - this.downStartTimes[t.instance]);
          }
          latencyText = this._downLabel(t, this._fmtDownAging(downMs));
        } else {
          latencyText = (t.responseTimeMs != null) ? `${t.responseTimeMs} ms` : '—';
        }

        const ipEl = card.querySelector('.hc-ip') || card.children[0];
        if (ipEl && ipEl.textContent !== t.instance) ipEl.textContent = t.instance;

        const latEl = card.querySelector('.hc-latency') || card.children[1];
        if (latEl && latEl.textContent !== latencyText) latEl.textContent = latencyText;
      });
    } else {
      if (rows.length === 0) {
        let msg = 'No monitored hosts found.';
        if (this.searchQ) {
          msg = `No hosts matching "${this._esc(this.searchQ)}".`;
        } else if (this.activeStatus && this.activeStatus !== 'all') {
          msg = `No hosts currently with status "${this._esc(this.activeStatus)}".`;
        }
        this.table.innerHTML = `<div class="empty-state" style="grid-column: 1 / -1; padding: 40px 20px; text-align: center; color: var(--text-secondary);">
          <div style="font-size: 14px; font-weight: 500;">${msg}</div>
        </div>`;
        this._downCardElements = [];
        this._maintCardElements = [];
        return;
      }

      const focusedCard = document.activeElement ? document.activeElement.closest('.host-card') : null;
      const focusedInst = focusedCard ? focusedCard.dataset.instance : null;

      // Re-render only when structure/filter changes
      this.table.innerHTML = rows.map((t, i) => {
        const isUp = t.health === 'up';
        const isDown = t.health === 'down';
        const isNoData = !isUp && !isDown;   // 'unknown' — Prometheus has no probe sample; not an outage
        const isSlow = isUp && t.responseTimeMs > slowThresholdMs(t);

        const stateClass = t.maintenance ? 'hc-maintenance' : (isDown ? 'hc-down' : (isNoData ? 'hc-nodata' : (isSlow ? 'hc-slow' : 'hc-up')));
        const isAcked = isDown && !t.maintenance && this.acknowledgedDownInstances.has(t.instance);
        const fullClass = `host-card ${stateClass}${t.suppressedBy ? ' hc-suppressed' : ''}${isAcked ? ' hc-acked' : ''}`;
        let latencyText = '';
        if (t.maintenance) {
          const remainMs = t.maintenanceUntil ? Math.max(0, t.maintenanceUntil * 1000 - now) : 0;
          latencyText = `Maint ${this._fmtDownAging(remainMs)} left`;
        } else if (isNoData) {
          latencyText = 'No data';
        } else if (isDown) {
          let downMs = 0;
          if (t.downSince && t.downSince > 0) {
            downMs = Math.max(0, now - (t.downSince * 1000));
          } else if (this.downStartTimes[t.instance]) {
            downMs = Math.max(0, now - this.downStartTimes[t.instance]);
          }
          latencyText = this._downLabel(t, this._fmtDownAging(downMs));
        } else {
          latencyText = (t.responseTimeMs != null) ? `${t.responseTimeMs} ms` : '—';
        }

        return `<div class="${fullClass}"
                     data-instance="${this._esc(t.instance)}"
                     role="listitem"
                     tabindex="0"
                     aria-label="${this._esc(t.instance)} — ${t.maintenance ? 'Under maintenance' : (isDown ? (t.suppressedBy ? `Offline, correlated with ${this._esc(t.suppressedBy)}` : 'Offline') : (isSlow ? 'Slow' : 'Online'))}"
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

    // Cache down and maintenance card DOM references to avoid periodic querySelectorAll in _tickDownCounters
    this._downCardElements = Array.from(this.table.querySelectorAll('.host-card.hc-down')).map(card => ({
      inst: card.dataset.instance,
      latEl: card.querySelector('.hc-latency')
    }));
    this._maintCardElements = Array.from(this.table.querySelectorAll('.host-card.hc-maintenance')).map(card => ({
      inst: card.dataset.instance,
      latEl: card.querySelector('.hc-latency')
    }));
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

      barsHtml += `<div title="${timeStr} • ${statusText}" style="flex:1; height:${barHeight}; background:${barColor}; border-radius:2px; transition:all 0.2s ease;"></div>`;
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
      leftLabels.push(`<span style="position:absolute; right:0; top:${topPct}%; transform:translateY(-50%); font-size:10.5px; font-family:var(--font-mono); color:var(--text-secondary); opacity:0.85; white-space:nowrap;">${formattedVal}</span>`);
      rightLabels.push(`<span style="position:absolute; left:0; top:${topPct}%; transform:translateY(-50%); font-size:10.5px; font-family:var(--font-mono); color:var(--text-secondary); opacity:0.85; white-space:nowrap;">${formattedVal}</span>`);
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
      const timeLabel = dObj.toLocaleTimeString(DATE_LOCALE, { hour: '2-digit', minute: '2-digit', second: '2-digit' });
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
          <div style="display:flex; justify-content:space-between; align-items:center; padding:4px 8px; background:rgba(15,23,42,0.6); border:1px solid var(--border); border-radius:4px; font-size:11px; font-family:var(--font-mono);">
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
      leftLabels.push(`<span style="position:absolute; right:0; top:${topPct}%; transform:translateY(-50%); font-size:10.5px; font-family:var(--font-mono); color:var(--text-secondary); opacity:0.85; white-space:nowrap;">${formattedVal}</span>`);
      rightLabels.push(`<span style="position:absolute; left:0; top:${topPct}%; transform:translateY(-50%); font-size:10.5px; font-family:var(--font-mono); color:var(--text-secondary); opacity:0.85; white-space:nowrap;">${formattedVal}</span>`);
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
      const timeLabel = dObj.toLocaleTimeString(DATE_LOCALE, { hour: '2-digit', minute: '2-digit', second: '2-digit' });
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
        const ongoingBadge = ev.ongoing ? '<span style="font-size:10px; background:rgba(56,189,248,0.15); color:#38BDF8; padding:1px 5px; border-radius:3px; margin-left:6px; font-weight:500;">Ongoing</span>' : '';

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
              <div style="font-size:10px; color:var(--text-muted);">Status Duration</div>
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
      if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
      return `${Math.floor(diff / 86400)}d ago`;
    } catch { return '—'; }
  }

  _esc(s) { return escapeHtml(s); }
}



/* ════════════════════════════════════════════════════════════════════════════
   MAIN MONITOR (Dashboard + coordination)
   ════════════════════════════════════════════════════════════════════════════ */
class ServerMonitor {
  constructor() {
    this.isMuted = false;
    this.isInitialized = false;

    // ── DOM refs ──────────────────────────────────
    this.alarmAudio = document.getElementById('alarmAudio');

    // ── Sub-pages ─────────────────────────────────
    this.instancesPage = new InstancesPage(this);
    this.logsPage = new LogsPage(this);
    this.historyPage = new HistoryPage(this);

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

    // Poll logs continuously (not just while the modal is open) so the
    // "+N new" nav badge can fire even when the operator is elsewhere —
    // slower cadence in the background, tightened to 5s once the modal opens.
    this.logsPage._startPolling(30000);
    this.logsPage.load();
    this._checkSelfHealth();
    this._selfHealthInterval = setInterval(() => this._checkSelfHealth(), 20000);

    // Kiosk / TV Standby lifecycle management: fully pause every recurring
    // timer while the tab/display is hidden (a backgrounded wallboard was
    // still hammering /instances every 5s, /api/availability every 15s,
    // /health every 20s and ticking 1/s), then resume with one immediate
    // clean sync on wake — no timer accumulation.
    document.addEventListener('visibilitychange', () => {
      const ip = this.instancesPage;
      if (document.visibilityState === 'visible') {
        ip.startPolling(ip.currentInterval);
        ip.startAvailabilityPolling();
        ip.startDownCounterTicker();
        if (ip.autoRotate) ip._startAutoRotate();
        if (!this._selfHealthInterval) {
          this._selfHealthInterval = setInterval(() => this._checkSelfHealth(), 20000);
        }
        ip.load();
        ip.loadAvailability();
        this._checkSelfHealth();
      } else {
        ip.stopPolling();
        ip.stopAvailabilityPolling();
        ip.stopDownCounterTicker();
        ip._stopAutoRotate();
        if (this._selfHealthInterval) {
          clearInterval(this._selfHealthInterval);
          this._selfHealthInterval = null;
        }
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
      if (tab !== 'history') this.historyPage.onDeactivate();
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
      this.historyPage.onDeactivate();
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

        // Keep InstancesPage's notion of the active endpoint current — it
        // keys the per-endpoint Default Job (restore + save + badge).
        const active = data.endpoints.find(ep => ep.active);
        if (this.instancesPage) this.instancesPage._activeEndpoint = active ? active.url : null;

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
          if (data.endpoints.length === 0) {
            listContainer.innerHTML = '<div style="padding:10px; font-size:12px; color:var(--text-secondary);">No Prometheus endpoint configured. Add one above — until then, no metric data is fetched.</div>';
          }
          data.endpoints.forEach(ep => {
            const row = document.createElement('div');
            row.style.cssText = 'display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:8px; padding:8px 10px; background:var(--surface); border:1px solid var(--border); border-radius:var(--r-sm); font-size:12px; margin-bottom:6px;';
            const statusDot = ep.online ? '<span style="color:#22C55E; margin-right:6px;">● Online</span>' : '<span style="color:#EF4444; margin-right:6px;">● Offline</span>';
            const activeBadge = ep.active ? '<span style="background:var(--accent-bg); color:var(--accent); padding:2px 6px; border-radius:4px; font-size:10px; font-weight:600; margin-left:6px;">ACTIVE</span>' : '';
            const safeUrl = escapeHtml(ep.url);

            row.innerHTML = `
              <div style="display:flex; align-items:center; overflow:hidden; flex:1; min-width:0;">
                ${statusDot}
                <span style="font-family:var(--font-mono); font-weight:500; text-overflow:ellipsis; overflow:hidden; white-space:nowrap; color:var(--text-primary); min-width:0; flex:1;">${safeUrl}</span>
                ${activeBadge}
              </div>
              <div style="display:flex; gap:6px; flex-shrink:0; margin-left:10px; flex-wrap:wrap; justify-content:flex-end;">
                ${!ep.active ? `<button class="btn btn-secondary btn-sm select-ep-btn" data-url="${safeUrl}" style="padding:2px 8px; font-size:11px;">Select</button>` : ''}
                <button class="btn btn-danger btn-sm del-ep-btn" data-url="${safeUrl}" style="padding:2px 8px; font-size:11px; background:rgba(239,68,68,0.15); color:#EF4444; border:1px solid rgba(239,68,68,0.3);">Delete</button>
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
                title: 'Delete Prometheus Endpoint',
                message: `Are you sure you want to delete endpoint "${targetUrl}"?`,
                confirmText: 'Delete Endpoint',
                cancelText: 'Cancel',
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
    // Reachable from InstancesPage (this.monitor._syncEndpointsUI) so a poll
    // that detects another client repointed the server endpoint can re-sync
    // the topbar picker + _activeEndpoint.
    this._syncEndpointsUI = fetchEndpoints;

    const selectEndpoint = async (url) => {
      try {
        const res = await apiFetch('/api/endpoints/select', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ url })
        });
        const data = await res.json();
        if (data.ok) {
          await fetchEndpoints();
          this.instancesPage._resetJobFilter();
          this.instancesPage.load();
          this.instancesPage.loadAvailability();
        }
      } catch (e) { }
    };

    const deleteEndpoint = async (url) => {
      try {
        const res = await apiFetch('/api/endpoints', {
          method: 'DELETE',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ url })
        });
        const data = await res.json();
        if (data.ok) {
          await fetchEndpoints();
          this.instancesPage.load();
        } else if (errorEl) {
          errorEl.textContent = data.error || 'Failed to delete endpoint';
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
          const res = await apiFetch('/api/endpoints', {
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
            errorEl.textContent = data.error || 'Failed to add endpoint';
            errorEl.classList.remove('hidden');
          }
        } catch (e) {
          if (errorEl) {
            errorEl.textContent = 'Failed to connect to server';
            errorEl.classList.remove('hidden');
          }
        }
      });
    }

    // Initial load — then one more load() so this endpoint's Default Job is
    // restored on first paint (the load() from onActivate races ahead of
    // _activeEndpoint being known).
    fetchEndpoints().then(() => { if (this.instancesPage) this.instancesPage.load(); });
  }


  /* ── onActivate (dashboard page) ───────────────── */
  onActivate() { /* already polling */ }

  /* ── Sound control ─────────────────────────────── */
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

  // The configured MP3 on <audio id="alarmAudio"> is the ONLY sound this app
  // ever plays — no synth/oscillator/TTS fallback. If it can't play (blocked
  // autoplay, decode/network failure, or a synchronous throw from an older
  // engine), nothing else plays: the operator is told via toast + console,
  // full stop. `token` guards three overlapping async races on this one
  // shared <audio> element: an ack (stopAlarm) or mute (toggleSound) firing
  // while play() is still pending, and a stale attempt's promise settling
  // after a newer attempt has already superseded it.
  playAlarm() {
    if (this.isMuted) return;
    if (this.isPlayingAlarm || this.hasPlayedForCurrentOutage) return;

    this.isPlayingAlarm = true;
    this.hasPlayedForCurrentOutage = true;
    this._alarmPlayToken = (this._alarmPlayToken || 0) + 1;
    const token = this._alarmPlayToken;

    if (!this.alarmAudio) {
      this.isPlayingAlarm = false;
      this._reportAlarmAudioFailure('No audio element available');
    } else {
      this.alarmAudio.loop = true;
      this.alarmAudio.muted = false;
      this.alarmAudio.currentTime = 0;
      try {
        const p = this.alarmAudio.play();
        if (p !== undefined) {
          p.then(() => {
            if (token !== this._alarmPlayToken || !this.isPlayingAlarm || this.isMuted) return;
            console.log('[InfraWatch] Single MP3 alarm playing cleanly');
          }).catch((e) => {
            if (token !== this._alarmPlayToken || !this.isPlayingAlarm || this.isMuted) return;
            this.isPlayingAlarm = false;
            this._reportAlarmAudioFailure(e);
          });
        }
      } catch (e) {
        this.isPlayingAlarm = false;
        this._reportAlarmAudioFailure(e);
      }
    }

    // Automatically stop sound after 1 minute (60,000 ms)
    if (this.alarmTimeout) clearTimeout(this.alarmTimeout);
    this.alarmTimeout = setTimeout(() => {
      console.log('[InfraWatch] 1 minute alarm limit reached. Stopping audio.');
      this.stopAlarmAudioOnly();
    }, 60000);
  }

  _reportAlarmAudioFailure(err) {
    console.error('[InfraWatch] Alarm audio failed — no sound will play:', err);
    this.instancesPage?._triggerEventToast?.('⚠ Alarm sound failed to play — no sound (check browser autoplay/volume)');
  }

  stopAlarmAudioOnly() {
    this.isPlayingAlarm = false;
    if (this.alarmAudio) {
      try {
        this.alarmAudio.pause();
        this.alarmAudio.currentTime = 0;
      } catch (e) { }
    }
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

  /* ── Escape HTML ───────────────────────────────── */
  _esc(str) { return escapeHtml(str); }

  /* ── Cleanup ───────────────────────────────────── */
  destroy() {
    this.instancesPage.onDeactivate();
  }
}



function _initAuthHandlers() {
  // apiFetch (net.js) dispatches this on a 401 instead of reaching in here.
  window.addEventListener('iw:unauthorized', () => showLoginModal());

  // First-run Admin Setup Form
  const setupForm = document.getElementById('setupForm');
  if (setupForm) {
    setupForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const username = document.getElementById('setupUsernameInput')?.value.trim();
      const password = document.getElementById('setupPasswordInput')?.value;
      const confirm = document.getElementById('setupConfirmPasswordInput')?.value;
      const displayName = document.getElementById('setupDisplayNameInput')?.value.trim();
      const errorEl = document.getElementById('setupError');

      if (errorEl) errorEl.classList.add('hidden');

      try {
        const res = await fetch('/api/auth/setup', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
          credentials: 'same-origin',
          body: JSON.stringify({
            username, password, confirm_password: confirm, display_name: displayName
          })
        });
        const data = await res.json();
        if (res.ok && data.ok) {
          closeSetupModal();
          window.isSystemInitialized = true;
          window.currentUser = data.user;
          updateUserUI(data.user);
          if (window.monitor && window.monitor.instancesPage) {
            window.monitor.instancesPage._triggerEventToast(`System initialized. Logged in as ${data.user.username}`);
          }
        } else {
          if (errorEl) {
            errorEl.textContent = data.error || 'Failed to initialize administrator';
            errorEl.classList.remove('hidden');
          }
        }
      } catch (err) {
        if (errorEl) {
          errorEl.textContent = err.message || 'Network error';
          errorEl.classList.remove('hidden');
        }
      }
    });
  }

  // Operator Login Form
  const loginForm = document.getElementById('loginForm');
  if (loginForm) {
    loginForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const username = document.getElementById('loginUsernameInput')?.value.trim();
      const password = document.getElementById('loginPasswordInput')?.value;
      const errorEl = document.getElementById('loginError');

      if (errorEl) errorEl.classList.add('hidden');

      try {
        const res = await fetch('/api/auth/login', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' },
          credentials: 'same-origin',
          body: JSON.stringify({ username, password })
        });
        const data = await res.json();
        if (res.ok && data.ok) {
          closeLoginModal();
          window.currentUser = data.user;
          updateUserUI(data.user);
          if (window.monitor && window.monitor.instancesPage) {
            window.monitor.instancesPage._triggerEventToast(`Logged in as ${data.user.username}`);
            window.monitor.instancesPage.load();
          }
        } else {
          if (errorEl) {
            errorEl.textContent = data.error || 'Invalid username or password';
            errorEl.classList.remove('hidden');
          }
        }
      } catch (err) {
        if (errorEl) {
          errorEl.textContent = err.message || 'Network error';
          errorEl.classList.remove('hidden');
        }
      }
    });
  }

  // User Menu dropdown toggle & action buttons
  const userMenuBtn = document.getElementById('userMenuBtn');
  const userDropdown = document.getElementById('userDropdown');
  if (userMenuBtn && userDropdown) {
    userMenuBtn.addEventListener('click', (e) => {
      e.preventDefault();
      e.stopPropagation();
      const isHidden = userDropdown.classList.contains('hidden');
      if (isHidden) {
        userDropdown.classList.remove('hidden');
        userMenuBtn.setAttribute('aria-expanded', 'true');
      } else {
        userDropdown.classList.add('hidden');
        userMenuBtn.setAttribute('aria-expanded', 'false');
      }
    });
    document.addEventListener('click', (e) => {
      if (!userDropdown.classList.contains('hidden') && !e.target.closest('#userAuthWrap')) {
        userDropdown.classList.add('hidden');
        userMenuBtn.setAttribute('aria-expanded', 'false');
      }
    });
  }

  document.getElementById('headerLoginBtn')?.addEventListener('click', () => {
    userDropdown?.classList.add('hidden');
    showLoginModal();
  });

  document.getElementById('closeLoginModalBtn')?.addEventListener('click', closeLoginModal);
  document.getElementById('cancelLoginBtn')?.addEventListener('click', closeLoginModal);

  document.getElementById('headerLogoutBtn')?.addEventListener('click', async () => {
    userDropdown?.classList.add('hidden');
    try {
      await fetch('/api/auth/logout', {
        method: 'POST',
        headers: { 'X-Requested-With': 'XMLHttpRequest' },
        credentials: 'same-origin'
      });
    } catch (e) {}
    window.currentUser = null;
    updateUserUI(null);
    if (window.monitor && window.monitor.instancesPage) {
      window.monitor.instancesPage._triggerEventToast('Logged out');
      window.monitor.instancesPage.load();
    }
  });

  document.getElementById('headerManageUsersBtn')?.addEventListener('click', () => {
    userDropdown?.classList.add('hidden');
    showUsersModal();
  });

  document.getElementById('closeUsersModalBtn')?.addEventListener('click', closeUsersModal);
  document.getElementById('cancelUsersModalBtn')?.addEventListener('click', closeUsersModal);
  initUsersListActions();

  const createUserForm = document.getElementById('createUserForm');
  if (createUserForm) {
    createUserForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const username = document.getElementById('newUsernameInput')?.value.trim();
      const displayName = document.getElementById('newDisplayNameInput')?.value.trim();
      const password = document.getElementById('newPasswordInput')?.value;
      const role = document.getElementById('newRoleSelect')?.value || 'viewer';
      const errorEl = document.getElementById('createUserError');
      const successEl = document.getElementById('createUserSuccess');

      if (errorEl) errorEl.classList.add('hidden');
      if (successEl) successEl.classList.add('hidden');

      try {
        const res = await apiFetch('/api/auth/users', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            username,
            display_name: displayName,
            password,
            role
          })
        });
        const data = await res.json();
        if (res.ok && data.ok) {
          if (successEl) {
            successEl.textContent = `User "${username}" (${role}) created successfully!`;
            successEl.classList.remove('hidden');
          }
          document.getElementById('newUsernameInput').value = '';
          document.getElementById('newDisplayNameInput').value = '';
          document.getElementById('newPasswordInput').value = '';
          fetchUsersList();
          if (window.monitor && window.monitor.instancesPage) {
            window.monitor.instancesPage._triggerEventToast(`Created user "${username}"`);
          }
        } else {
          if (errorEl) {
            errorEl.textContent = data.error || 'Failed to create user';
            errorEl.classList.remove('hidden');
          }
        }
      } catch (err) {
        if (errorEl) {
          errorEl.textContent = err.message || 'Network error';
          errorEl.classList.remove('hidden');
        }
      }
    });
  }

  // Check auth status on startup
  checkAuthStatus();
}

// ── Bootstrap ────────────────────────────────────────
const _boot = () => {
  window.monitor = new ServerMonitor();
  _initAuthHandlers();
};

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
