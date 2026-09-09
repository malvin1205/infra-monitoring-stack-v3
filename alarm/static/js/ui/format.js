/* Small pure formatting / escaping helpers shared across the app. */

/**
 * HTML-escape for innerHTML string building. The one implementation; each
 * page class re-exposes it as this._esc() for call-site brevity.
 * null / undefined collapse to "".
 */
export function escapeHtml(str) {
  return String(str ?? '').replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}

/**
 * Effective "slow" latency threshold (ms) for a target. The backend sends a
 * per-instance slowThresholdMs on every /instances row (honouring
 * SlowThresholdRepository overrides); fall back to 500 for an older payload.
 * `!= null` not `||` — an override of 0 ("always slow") is valid.
 */
export function slowThresholdMs(t) {
  return (t && t.slowThresholdMs != null) ? t.slowThresholdMs : 500;
}
