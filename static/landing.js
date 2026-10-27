// Signed-in visitors get "Open app" instead of "Sign in".
fetch("/api/me").then((r) => {
  if (r.ok) document.querySelectorAll('a[href="/app"]').forEach((a) => { a.textContent = "Open app"; });
}).catch(() => {});
