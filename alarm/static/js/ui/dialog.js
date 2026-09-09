/* Modal helpers shared across the app. Both are published on `window`
 * because callers reference them as window.trapModalFocus /
 * window.showConfirmDialog from several modules.
 */

/* ── Modal Focus Trap Helper (WCAG 2.1 SC 2.4.3) ──────── */
window.trapModalFocus = function (modalEl) {
  if (!modalEl) return () => {};
  const handler = function (e) {
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
window.showConfirmDialog = function ({ title, message, confirmText = 'Yes, Delete', cancelText = 'Cancel', isDanger = true }) {
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

    if (titleEl) titleEl.textContent = title || 'Confirm Action';
    if (msgEl) msgEl.textContent = message || 'Are you sure?';
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
