"""Response-layer middleware for InfraWatch: cache-busting asset versioning,
gzip for text payloads, security headers, and the JSON error handlers.

Extracted verbatim from app.py — behaviour is unchanged. app.py wires it in
with `register_web_middleware(app)` right after the Flask app is configured.
"""
import gzip
import logging
import os

from flask import request, jsonify, render_template

logger = logging.getLogger("infrawatch")

GZIP_MIN_BYTES = 500
# Content types worth gzipping — text/JSON/JS/CSS/SVG. Binary assets (mp3,
# png) are already compressed; re-gzipping them wastes CPU for ~0 saving.
_GZIP_MIMETYPES = {
    'text/html', 'text/css', 'text/plain', 'text/xml',
    'application/javascript', 'text/javascript',
    'application/json', 'image/svg+xml',
}
# Same policy expressed as a header so it can't drift out of sync with CSP
# tightening: everything from same origin, plus the Google Fonts stylesheet
# (<link> in alarm.html) and the font files it pulls. 'unsafe-inline' is
# still required for the inline theme bootstrap and the ~100 inline style=
# attributes in the template — see frontend audit F30 for removing those.
_CSP = (
    "default-src 'self'; "
    "base-uri 'self'; "
    "object-src 'none'; "
    "frame-ancestors 'self'; "
    "form-action 'self'; "
    "img-src 'self' data:; "
    "font-src 'self' https://fonts.gstatic.com; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "script-src 'self' 'unsafe-inline'; "
    "connect-src 'self'"
)


def _maybe_gzip(response, is_static=False):
    accept_encoding = request.headers.get('Accept-Encoding', '')
    if 'gzip' not in accept_encoding or 'Content-Encoding' in response.headers:
        return
    if (response.mimetype or '').split(';')[0].strip() not in _GZIP_MIMETYPES:
        return
    # For a static FILE response (direct_passthrough + a file wrapper body),
    # only touch a plain 200 with no Range request — never a 206/304 or a
    # range fetch. Dynamic responses (JSON, rendered HTML) are always safe to
    # compress regardless of status, same as the previous behaviour.
    if is_static:
        if response.status_code != 200 or request.headers.get('Range'):
            return
        if response.direct_passthrough:
            response.direct_passthrough = False
    body = response.get_data()
    if len(body) < GZIP_MIN_BYTES:
        return
    compressed = gzip.compress(body, compresslevel=5)
    response.set_data(compressed)
    response.headers['Content-Encoding'] = 'gzip'
    response.headers['Content-Length'] = str(len(compressed))
    # Byte ranges no longer map to the (now gzipped) body — drop the offer so a
    # cache/client can't request a range against a mismatched length.
    response.headers.pop('Accept-Ranges', None)
    vary = response.headers.get('Vary')
    if not vary:
        response.headers['Vary'] = 'Accept-Encoding'
    elif 'accept-encoding' not in vary.lower():
        response.headers['Vary'] = f"{vary}, Accept-Encoding"


def register_web_middleware(app):
    """Attach the asset-version context processor, the after_request header/gzip
    pass, and the 404/405/500 JSON handlers to `app`."""

    @app.context_processor
    def inject_asset_version():
        # Appended as ?v=<mtime> on static asset URLs so a code change busts the
        # 5-minute browser cache immediately instead of serving stale JS/CSS.
        def asset_version(filename):
            path = os.path.join(app.static_folder, filename)
            try:
                return int(os.path.getmtime(path))
            except OSError:
                return 0
        return {"asset_version": asset_version}

    @app.after_request
    def add_header(response):
        is_static = request.path.startswith('/static/')

        # Security headers apply everywhere (static included — cheap, and a
        # stray HTML error page under /static/ still benefits).
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'SAMEORIGIN'
        response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
        response.headers['Content-Security-Policy'] = _CSP

        if is_static:
            if request.path.endswith(('.js', '.css')):
                # A module pulled in by another module's `import` is not
                # referenced in the template, so asset_version()'s ?v= bust
                # never reaches it — `immutable` would pin a stale copy for a
                # week after deploy. `no-cache` = keep the copy but revalidate
                # every load (cheap 304 on a LAN wallboard).
                response.headers['Cache-Control'] = 'no-cache'
            else:
                # ?v=<mtime> already busts this on every change, so cache hard.
                response.headers['Cache-Control'] = 'public, max-age=604800, immutable'
        else:
            response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
            response.headers['Pragma'] = 'no-cache'
            response.headers['Expires'] = '-1'

        # Compress text/JSON/JS/CSS/SVG for both dynamic responses and static
        # assets — gunicorn serves static itself here (no compressing proxy),
        # so without this the ~420 KB of JS+CSS ships uncompressed every load.
        _maybe_gzip(response, is_static=is_static)

        return response

    @app.errorhandler(404)
    def handle_404(e):
        if request.path.startswith('/api/') or request.path in ('/instances', '/status', '/logs', '/history', '/webhook', '/api/webhook', '/health', '/health/live', '/health/ready'):
            return jsonify({"ok": False, "error": "Resource not found"}), 404
        return render_template('alarm.html'), 404

    @app.errorhandler(405)
    def handle_405(e):
        return jsonify({"ok": False, "error": "Method not allowed"}), 405

    @app.errorhandler(500)
    def handle_500(e):
        logger.error(f"Internal server error: {e}", exc_info=True)
        return jsonify({"ok": False, "error": "Internal server error"}), 500

    return app
