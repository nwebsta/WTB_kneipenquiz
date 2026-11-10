// Prüf-Warteschlange nur neu laden, wenn niemand gerade etwas eintippt oder anhakt.
function canRefreshQueue() {
  const q = document.getElementById('queue');
  if (!q) return false;
  if (q.contains(document.activeElement) && document.activeElement !== document.body) return false;
  for (const el of q.querySelectorAll('input[name=reason]')) if (el.value.trim()) return false;
  for (const el of q.querySelectorAll('input[type=checkbox]')) if (el.checked) return false;
  return true;
}

// Tab-Titel zeigt offene Prüfungen.
document.addEventListener('htmx:afterSwap', function () {
  const c = document.getElementById('pending-count');
  if (c) document.title = (c.textContent !== '0' ? '(' + c.textContent + ') ' : '') + 'Prüfen · Admin';
});
