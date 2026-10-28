// Thin fetch wrapper: JSON in/out, errors carry the server's own message.
export async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    credentials: 'same-origin',
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data = null;
  try { data = await res.json(); } catch (_) { /* empty body */ }
  if (!res.ok) {
    const err = new Error((data && data.detail) || `Request failed (HTTP ${res.status})`);
    err.status = res.status;
    throw err;
  }
  return data;
}
