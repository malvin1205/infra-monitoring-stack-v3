/* Application shell: owns audio/alarm state, tab switching, endpoint
 * management, and constructs + coordinates the three page objects. */
import { apiFetch } from './net.js';
import { escapeHtml } from './ui/format.js';
import { InstancesPage } from './dashboard.js';
import { LogsPage } from './logs.js';
import { HistoryPage } from './history.js';

/* ════════════════════════════════════════════════════════════════════════════
   MAIN MONITOR (Dashboard + coordination)
   ════════════════════════════════════════════════════════════════════════════ */
export class ServerMonitor {
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
