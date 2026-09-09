/* Thin fetch wrapper: sends the session cookie + the CSRF marker header,
 * and fires a global 'iw:unauthorized' event on a 401 so the auth layer
 * can show the login modal — this module stays dependency-free.
 */
export async function apiFetch(url, options = {}) {
  const { headers, ...rest } = options;
  const res = await fetch(url, {
    ...rest,
    headers: { 'X-Requested-With': 'XMLHttpRequest', ...(headers || {}) },
    credentials: 'same-origin'
  });
  if (res.status === 401 && !url.includes('/api/auth/')) {
    window.dispatchEvent(new Event('iw:unauthorized'));
  }
  return res;
}
