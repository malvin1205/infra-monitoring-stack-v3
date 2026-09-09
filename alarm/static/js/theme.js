/* Theme + splash-screen init.
 *
 * Loaded render-blocking in <head> (no defer) so the anti-FOUC data-theme
 * set below runs before the stylesheet is applied. Anything that touches
 * the DOM waits for DOMContentLoaded, since <head> scripts run before the
 * body is parsed.
 *
 * Theme resolution: explicit choice in localStorage ('iw-theme') wins,
 * otherwise follow the OS preference, otherwise dark. The <html> tag ships
 * data-theme="dark" as the SSR default.
 */
(function () {
  function resolveTheme() {
    var t = null;
    try { t = localStorage.getItem('iw-theme'); } catch (e) { /* private mode / storage blocked */ }
    if (t !== 'light' && t !== 'dark') {
      t = (window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches) ? 'light' : 'dark';
    }
    return t;
  }

  function applyTheme(t) {
    document.documentElement.setAttribute('data-theme', t);
    var darkIcon = document.getElementById('themeIconDark');
    var lightIcon = document.getElementById('themeIconLight');
    if (darkIcon) darkIcon.style.display = (t === 'light') ? 'none' : '';
    if (lightIcon) lightIcon.style.display = (t === 'light') ? '' : 'none';
  }

  // 1. Anti-FOUC — runs immediately, before the stylesheet applies. The icon
  //    elements don't exist yet; applyTheme just sets data-theme here.
  applyTheme(resolveTheme());

  // 2. DOM wiring — once the body exists.
  document.addEventListener('DOMContentLoaded', function () {
    applyTheme(resolveTheme()); // re-run now that the theme icons are in the DOM

    var themeBtn = document.getElementById('themeToggleBtn');
    if (themeBtn) {
      themeBtn.addEventListener('click', function () {
        var next = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
        try { localStorage.setItem('iw-theme', next); } catch (e) { /* private mode */ }
        applyTheme(next);
      });
    }

    var enterBtn = document.getElementById('enterDashboardBtn');
    var splashOverlay = document.getElementById('splashOverlay');
    if (splashOverlay) {
      var dismissed = false;
      try {
        dismissed = localStorage.getItem('iw-splash-dismissed') === 'true'
                 || localStorage.getItem('iw-audio-unlocked') === 'true';
      } catch (e) { /* private mode */ }
      if (dismissed) splashOverlay.classList.add('splash-hidden');
    }
    if (enterBtn && splashOverlay) {
      enterBtn.addEventListener('click', function () {
        if (window.monitor && typeof window.monitor.unlockAudio === 'function') {
          window.monitor.unlockAudio();
        }
        try {
          localStorage.setItem('iw-splash-dismissed', 'true');
          localStorage.setItem('iw-audio-unlocked', 'true');
        } catch (e) {}
        splashOverlay.classList.add('splash-hidden');
      });
    }
  });
})();
