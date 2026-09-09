/* Entry point. Loaded as <script type="module">. Wires the shell + auth
 * to the DOM and installs the app-wide key handlers. */
import './ui/dialog.js'; // window.trapModalFocus, window.showConfirmDialog
import { ServerMonitor } from './app-shell.js';
import { initAuth } from './auth.js';

// ── Bootstrap ────────────────────────────────────────
const _boot = () => {
  window.monitor = new ServerMonitor();
  initAuth();
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

