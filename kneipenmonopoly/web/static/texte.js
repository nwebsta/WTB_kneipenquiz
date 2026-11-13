// Reiter „Texte“: Bot-Texte bearbeiten, mit Live-Vorschau wie in Telegram.
(function () {
  'use strict';
  const info = JSON.parse(document.getElementById('tx-examples').textContent);
  const ALLOWED = 'b|i|u|s|code|pre';

  function escapeHtml(s) { return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }

  // Text so darstellen, wie Telegram (parse_mode=HTML) ihn zeigt. Platzhalter werden durch Beispielwerte ersetzt.
  function render(tpl) {
    const parts = [];   // Platzhalter-Werte merken, damit sie farbig markiert werden können
    let s = tpl.replace(/\{\{|\}\}|\{([A-Za-z0-9_]+)\}/g, (m, name) => {
      if (m === '{{') return '{';
      if (m === '}}') return '}';
      parts.push(name);
      const ex = info[name] ? info[name].beispiel : '{' + name + '}';
      return '\u0001' + (parts.length - 1) + '\u0002' + ex + '\u0003';
    });
    s = escapeHtml(s)
      .replace(new RegExp('&lt;(/?)(' + ALLOWED + ')&gt;', 'g'), '<$1$2>')
      .replace(/&amp;(amp|lt|gt|quot|#\d+);/g, '&$1;')
      .replace(/\u0001(\d+)\u0002/g, (m, i) => `<mark class="tx-val" title="{${parts[+i]}}">`)
      .replace(/\u0003/g, '</mark>')
      .replace(/\n/g, '<br>');
    return s;
  }

  // Was Telegram ablehnen würde (dann käme die Nachricht gar nicht an).
  function problems(tpl) {
    const p = [];
    const plain = tpl.replace(/\{\{|\}\}/g, '').replace(/\{[A-Za-z0-9_]+\}/g, '');
    if (/[{}]/.test(plain)) p.push('Geschweifte Klammer ohne Platzhalter. Als {{ bzw. }} schreiben.');
    const tagLess = plain.replace(new RegExp('</?(' + ALLOWED + ')>', 'g'), '');
    if (/</.test(tagLess) || />/.test(tagLess)) p.push('„<“ oder „>“ außerhalb von <b>, <i>, <code> … Telegram würde die Nachricht ablehnen. Als &lt; bzw. &gt; schreiben.');
    if (/&(?!(amp|lt|gt|quot|#\d+);)/.test(plain)) p.push('Ein „&“ muss als &amp;amp; geschrieben werden.');
    const stack = [];
    for (const m of plain.matchAll(new RegExp('<(/?)(' + ALLOWED + ')>', 'g'))) {
      if (!m[1]) stack.push(m[2]);
      else if (stack.pop() !== m[2]) { p.push(`Tag </${m[2]}> passt nicht zum geöffneten.`); break; }
    }
    if (stack.length) p.push(`<${stack[stack.length - 1]}> wird nicht geschlossen.`);
    return p;
  }

  function update(card) {
    const ta = card.querySelector('textarea');
    const bubble = card.querySelector('.tx-bubble');
    const warn = card.querySelector('.tx-warn');
    const value = ta.value;
    const plain = card.dataset.plain === '1';
    if (value.trim() === '') {
      bubble.innerHTML = '<i class="muted">(leer – wird nicht gesendet)</i>';
    } else if (plain) {         // Button/Pop-up: Telegram zeigt den Text ohne Formatierung
      bubble.innerHTML = escapeHtml(value.replace(/\{\{/g, '{').replace(/\}\}/g, '}')
        .replace(/\{([A-Za-z0-9_]+)\}/g, (m, n) => info[n] ? info[n].beispiel : m)).replace(/\n/g, '<br>');
    } else {
      bubble.innerHTML = render(value);
    }
    const p = plain ? [] : problems(value);
    warn.innerHTML = p.map(x => '⚠️ ' + escapeHtml(x)).join('<br>');
    const dirty = value !== card.dataset.saved;
    card.querySelector('.tx-dirty').hidden = !dirty;
    card.querySelector('.tx-save').disabled = !dirty || p.length > 0;
    ta.style.height = 'auto';
    ta.style.height = Math.max(70, ta.scrollHeight + 4) + 'px';
  }

  async function send(card, payload) {
    const status = card.querySelector('.tx-status');
    status.textContent = 'speichert …'; status.className = 'small tx-status';
    try {
      const r = await fetch('/texte/api/speichern', {method: 'POST', credentials: 'same-origin',
        headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
      if (r.status === 401) { location.href = '/login'; return; }
      const j = await r.json();
      status.textContent = j.message;
      status.className = 'small tx-status ' + (j.ok ? 'ok-text' : 'bad-text');
      if (j.ok && j.entry) {
        card.querySelector('textarea').value = j.entry.value;
        card.dataset.saved = j.entry.value;
        card.dataset.changed = j.entry.changed ? '1' : '0';
        card.querySelector('.tx-changed-tag').hidden = !j.entry.changed;
        card.querySelector('.tx-reset').hidden = !j.entry.changed;
        update(card);
      }
    } catch (e) {
      status.textContent = 'Speichern fehlgeschlagen: ' + e;
      status.className = 'small tx-status bad-text';
    }
  }

  document.querySelectorAll('.tx').forEach(card => {
    const ta = card.querySelector('textarea');
    card.dataset.saved = ta.value;
    update(card);
    ta.addEventListener('input', () => update(card));
    card.querySelector('.tx-save').addEventListener('click', () => send(card, {key: card.dataset.key, value: ta.value}));
    card.querySelector('.tx-reset').addEventListener('click', () => {
      if (confirm('Diesen Text auf den Standard zurücksetzen?')) send(card, {key: card.dataset.key, reset: true});
    });
    card.querySelectorAll('.tx-chip').forEach(chip => chip.addEventListener('click', () => {
      const ins = '{' + chip.dataset.ph + '}';
      const a = ta.selectionStart ?? ta.value.length, b = ta.selectionEnd ?? ta.value.length;
      ta.value = ta.value.slice(0, a) + ins + ta.value.slice(b);
      ta.focus(); ta.selectionStart = ta.selectionEnd = a + ins.length;
      update(card);
    }));
  });

  // Suche und Filter
  const search = document.getElementById('tx-search');
  const onlyChanged = document.getElementById('tx-changed');
  function filter() {
    const q = search.value.trim().toLowerCase();
    document.querySelectorAll('.tx').forEach(card => {
      const hay = (card.dataset.key + ' ' + card.querySelector('textarea').value).toLowerCase();
      const show = (!q || hay.includes(q)) && (!onlyChanged.checked || card.dataset.changed === '1');
      card.hidden = !show;
    });
    document.querySelectorAll('.tx-sec').forEach(h => {
      let el = h.nextElementSibling, any = false;
      while (el && !el.classList.contains('tx-sec')) { if (el.classList.contains('tx') && !el.hidden) any = true; el = el.nextElementSibling; }
      h.hidden = !any;
    });
  }
  search.addEventListener('input', filter);
  onlyChanged.addEventListener('change', filter);

  window.addEventListener('beforeunload', e => {
    if ([...document.querySelectorAll('.tx')].some(c => c.querySelector('textarea').value !== c.dataset.saved)) {
      e.preventDefault(); e.returnValue = '';
    }
  });
})();
