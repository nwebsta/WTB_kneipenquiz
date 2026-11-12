// Karten-Editor: Kneipen, Straßen, Bahnhöfe und Reserve-Bars bearbeiten.
// Das Modell ist die Spieldaten-JSON. Gespeichert wird alles auf einmal, der Server prüft und nummeriert neu.
(function () {
  'use strict';
  const S = { data: null, struktur: true, phase: '', quelle: '', dirty: false, mode: null, modeGroup: null, selected: null };
  const map = L.map('ed-map').setView([51.4345, 7.0065], 15);
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19, attribution: '&copy; OpenStreetMap-Mitwirkende'
  }).addTo(map);
  const layer = L.layerGroup().addTo(map);
  let fitted = false;
  const $ = id => document.getElementById(id);

  function esc(s) { return String(s ?? '').replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c])); }
  function slug(t) {
    return (String(t).toLowerCase().replace(/ä/g, 'ae').replace(/ö/g, 'oe').replace(/ü/g, 'ue').replace(/ß/g, 'ss')
      .replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '')) || 'feld';
  }
  function uniqueId(base, list) {
    const used = new Set(list.map(x => x.id));
    let id = base, n = 2;
    while (used.has(id)) id = base + '-' + (n++);
    return id;
  }
  function allSpots() { return [...S.data.fields, ...S.data.stations, ...S.data.reserve]; }
  function group(id) { return S.data.groups.find(g => g.id === id); }
  function numbers() {
    const no = {}; let n = 1;
    for (const g of S.data.groups) for (const f of S.data.fields) if (f.group === g.id) no[f.id] = n++;
    return no;
  }
  function stationLetter(s) { return (s.name || '?').charAt(0).toUpperCase(); }
  function setDirty(v = true) {
    S.dirty = v;
    $('ed-save').disabled = !v;
    $('ed-status').textContent = v ? 'Ungespeicherte Änderungen' : ('Gespeichert · ' + S.quelle);
  }
  function message(text, ok, notes) {
    $('ed-msg').innerHTML = text ? `<div class="flash ${ok ? 'ok' : 'err'}">${esc(text)}${(notes || []).map(n => `<div class="small">⚠️ ${esc(n)}</div>`).join('')}</div>` : '';
  }
  function listOf(kind) { return kind === 'field' ? S.data.fields : kind === 'station' ? S.data.stations : S.data.reserve; }
  function find(kind, id) { return listOf(kind).find(x => x.id === id); }

  // ------------------------------------------------------------------ Karte
  function renderMap() {
    layer.clearLayers();
    const no = numbers();
    const bounds = [];
    for (const g of S.data.groups) {
      const pts = S.data.fields.filter(f => f.group === g.id).map(f => [f.lat, f.lon]);
      if (pts.length > 1) L.polyline(pts, {color: g.color, weight: 5, opacity: .8, interactive: false}).addTo(layer);
    }
    const add = (kind, item, html, size) => {
      bounds.push([item.lat, item.lon]);
      const sel = S.selected && S.selected.kind === kind && S.selected.id === item.id;
      const icon = L.divIcon({className: 'ed-icon' + (sel ? ' sel' : ''), html, iconSize: [size, size], iconAnchor: [size / 2, size / 2]});
      const m = L.marker([item.lat, item.lon], {icon, draggable: true, title: item.name, zIndexOffset: sel ? 1000 : 0}).addTo(layer);
      m.on('dragend', e => {
        const p = e.target.getLatLng();
        item.lat = +p.lat.toFixed(6); item.lon = +p.lng.toFixed(6);
        setDirty(); renderPanel(); renderMap();
      });
      m.on('click', () => select(kind, item.id, true));
    };
    for (const f of S.data.fields) {
      const g = group(f.group) || {color: '#888'};
      const big = f.type === 'kneipe';
      add('field', f, `<div class="ed-dot${big ? ' big' : ''}" style="background:${esc(g.color)}">${no[f.id] || '?'}</div>`, big ? 34 : 28);
    }
    for (const s of S.data.stations) add('station', s, `<div class="ed-sq">${esc(stationLetter(s))}</div>`, 30);
    for (const r of S.data.reserve) add('reserve', r, `<div class="ed-res">R</div>`, 22);
    if (!fitted && bounds.length) { map.fitBounds(bounds, {padding: [30, 30]}); fitted = true; }
  }

  map.on('click', e => {
    if (!S.mode) return;
    const lat = +e.latlng.lat.toFixed(6), lon = +e.latlng.lng.toFixed(6);
    let item;
    if (S.mode === 'field') {
      item = {id: uniqueId(slug('neue-kneipe'), allSpots()), name: 'Neue Kneipe', type: 'bar', group: S.modeGroup, address: '', lat, lon};
      S.data.fields.push(item);
    } else if (S.mode === 'station') {
      item = {id: uniqueId(slug('neuer-bahnhof'), allSpots()), name: 'Neuer Bahnhof', venue: '', verified_open: true, lat, lon};
      S.data.stations.push(item);
    } else {
      item = {id: uniqueId(slug('neue-reserve'), allSpots()), name: 'Neue Reserve-Bar', address: '', lat, lon};
      S.data.reserve.push(item);
    }
    const kind = S.mode;
    setMode(null);
    setDirty(); renderAll(); select(kind, item.id, false, true);
  });

  function setMode(mode, groupId) {
    S.mode = mode; S.modeGroup = groupId || null;
    const box = $('ed-mode');
    map.getContainer().style.cursor = mode ? 'crosshair' : '';
    if (!mode) { box.hidden = true; return; }
    const what = mode === 'field' ? `eine Kneipe in „${esc(group(groupId).name)}“` : mode === 'station' ? 'einen Bahnhof' : 'eine Reserve-Bar';
    box.innerHTML = `📍 Klicke auf die Karte, um ${what} zu setzen. <button class="btn small" data-action="cancel-mode">Abbrechen</button>`;
    box.hidden = false;
  }

  function select(kind, id, scroll, focusName) {
    S.selected = {kind, id};
    renderMap();
    document.querySelectorAll('.ed-row.sel').forEach(r => r.classList.remove('sel'));
    const row = document.querySelector(`.ed-row[data-kind="${kind}"][data-id="${CSS.escape(id)}"]`);
    if (row) {
      row.classList.add('sel');
      if (scroll || focusName) row.scrollIntoView({block: 'center', behavior: 'smooth'});
      if (focusName) { const inp = row.querySelector('input[data-prop="name"]'); if (inp) { inp.focus(); inp.select(); } }
    }
  }

  // ------------------------------------------------------------------ Bearbeitungsleiste
  function inp(kind, id, prop, value, attrs = '') {
    return `<input data-kind="${kind}" data-id="${esc(id)}" data-prop="${prop}" value="${esc(value)}" ${attrs}>`;
  }
  function moveBtns(kind, id) {
    if (!S.struktur) return '';
    return `<button class="btn small" data-action="up" data-kind="${kind}" data-id="${esc(id)}" title="nach oben">▲</button>` +
           `<button class="btn small" data-action="down" data-kind="${kind}" data-id="${esc(id)}" title="nach unten">▼</button>`;
  }
  function delBtn(kind, id, allowed = true) {
    if (!S.struktur) return '';
    return `<button class="btn small bad" data-action="delete" data-kind="${kind}" data-id="${esc(id)}" ${allowed ? '' : 'disabled title="Erst alle Kneipen der Straße verschieben oder löschen"'}>✕</button>`;
  }
  function coords(kind, item) {
    return `<button class="btn small" data-action="locate" data-kind="${kind}" data-id="${esc(item.id)}" title="auf der Karte zeigen">📍</button>` +
           `<span class="small muted">${item.lat.toFixed(5)}, ${item.lon.toFixed(5)}</span>`;
  }

  function renderPanel() {
    const d = S.data, no = numbers();
    let h = '';
    if (!S.struktur) {
      h += `<div class="flash err small">Das Spiel läuft. Namen, Adressen, Typ, Farben und Positionen (Punkte ziehen) lassen sich ändern.
            Hinzufügen, Löschen und Straßen neu zuordnen geht nur in der Vorbereitung.</div>`;
    }
    h += `<section class="ed-sec"><h3>Allgemein</h3>
      <label>Titel ${inp('meta', '', 'game', d.game || '')}</label>
      <label>Stadt ${inp('meta', '', 'city', d.city || '')}</label></section>`;

    // Straßen
    h += `<section class="ed-sec"><h3>Straßen <span class="muted small">(${d.groups.length})</span></h3>`;
    for (const g of d.groups) {
      const count = d.fields.filter(f => f.group === g.id).length;
      h += `<div class="ed-row" data-kind="group" data-id="${esc(g.id)}">
        <input type="color" data-kind="group" data-id="${esc(g.id)}" data-prop="color" value="${esc(g.color)}">
        ${inp('group', g.id, 'name', g.name, 'class="grow"')}
        <span class="small muted">${count} Kn.</span>${moveBtns('group', g.id)}${delBtn('group', g.id, count === 0)}</div>`;
    }
    if (S.struktur) h += `<button class="btn small" data-action="add-group">+ Straße</button>`;
    h += `</section>`;

    // Kneipen nach Straßen
    h += `<section class="ed-sec"><h3>Kneipen <span class="muted small">(${d.fields.length})</span></h3>`;
    for (const g of d.groups) {
      h += `<div class="ed-ghead" style="--c:${esc(g.color)}">${esc(g.name)}
              ${S.struktur ? `<button class="btn small" data-action="add-field" data-group="${esc(g.id)}">+ Kneipe</button>` : ''}</div>`;
      for (const f of d.fields.filter(x => x.group === g.id)) {
        h += `<div class="ed-row" data-kind="field" data-id="${esc(f.id)}">
          <div class="row nowrap"><b class="ed-no">${no[f.id]}</b>${inp('field', f.id, 'name', f.name, 'class="grow" placeholder="Name"')}
            <select data-kind="field" data-id="${esc(f.id)}" data-prop="type">
              <option value="kneipe" ${f.type === 'kneipe' ? 'selected' : ''}>Kneipe</option>
              <option value="bar" ${f.type !== 'kneipe' ? 'selected' : ''}>Bar</option></select></div>
          <div class="row nowrap">${inp('field', f.id, 'address', f.address || '', 'class="grow" placeholder="Adresse"')}
            <select data-kind="field" data-id="${esc(f.id)}" data-prop="group" ${S.struktur ? '' : 'disabled'}>
              ${d.groups.map(o => `<option value="${esc(o.id)}" ${o.id === f.group ? 'selected' : ''}>${esc(o.name)}</option>`).join('')}</select></div>
          <div class="row nowrap">${coords('field', f)}<span class="grow"></span>${moveBtns('field', f.id)}${delBtn('field', f.id)}</div>
        </div>`;
      }
    }
    h += `</section>`;

    // Bahnhöfe
    h += `<section class="ed-sec"><h3>Bahnhöfe <span class="muted small">(${d.stations.length})</span></h3>`;
    for (const s of d.stations) {
      h += `<div class="ed-row" data-kind="station" data-id="${esc(s.id)}">
        <div class="row nowrap"><b class="ed-no ed-sqno">${esc(stationLetter(s))}</b>${inp('station', s.id, 'name', s.name, 'class="grow" placeholder="Name"')}</div>
        <div class="row nowrap">${inp('station', s.id, 'venue', s.venue || '', 'class="grow" placeholder="Kiosk / Adresse"')}
          <label class="check small"><input type="checkbox" data-kind="station" data-id="${esc(s.id)}" data-prop="verified_open" ${s.verified_open ? 'checked' : ''}> geöffnet bestätigt</label></div>
        <div class="row nowrap">${coords('station', s)}<span class="grow"></span>${delBtn('station', s.id)}</div></div>`;
    }
    if (S.struktur) h += `<button class="btn small" data-action="add-station">+ Bahnhof</button>`;
    h += `<p class="small muted">Der Buchstabe (N, O, S, W …) ist der erste Buchstabe des Namens.</p></section>`;

    // Reserve
    h += `<section class="ed-sec"><h3>Reserve-Bars <span class="muted small">(${d.reserve.length})</span></h3>
      <p class="small muted">Erscheinen erst auf der Karte, wenn sie unter Verwaltung eine Bar ersetzen.</p>`;
    for (const r of d.reserve) {
      h += `<div class="ed-row" data-kind="reserve" data-id="${esc(r.id)}">
        <div class="row nowrap">${inp('reserve', r.id, 'name', r.name, 'class="grow" placeholder="Name"')}</div>
        <div class="row nowrap">${inp('reserve', r.id, 'address', r.address || '', 'class="grow" placeholder="Adresse"')}</div>
        <div class="row nowrap">${coords('reserve', r)}<span class="grow"></span>${delBtn('reserve', r.id)}</div></div>`;
    }
    if (S.struktur) h += `<button class="btn small" data-action="add-reserve">+ Reserve-Bar</button>`;
    h += `</section>`;
    $('ed-body').innerHTML = h;
    if (S.selected) {
      const row = document.querySelector(`.ed-row[data-kind="${S.selected.kind}"][data-id="${CSS.escape(S.selected.id)}"]`);
      if (row) row.classList.add('sel');
    }
  }

  function renderAll() { renderPanel(); renderMap(); }

  // ------------------------------------------------------------------ Eingaben
  function applyInput(el) {
    const {kind, id, prop} = el.dataset;
    if (!kind || !prop) return false;
    let value = el.type === 'checkbox' ? el.checked : el.value;
    if (kind === 'meta') { S.data[prop] = value; return false; }
    const item = kind === 'group' ? group(id) : find(kind, id);
    if (!item) return false;
    item[prop] = value;
    if (kind === 'field' && prop === 'group') {     // in neuer Straße hinten anstellen
      S.data.fields = S.data.fields.filter(x => x !== item);
      S.data.fields.push(item);
    }
    return prop === 'group' || prop === 'type' || prop === 'color' || (kind === 'station' && prop === 'name');
  }
  document.addEventListener('input', e => {
    if (!e.target.closest('#ed-body')) return;
    applyInput(e.target); setDirty();
  });
  document.addEventListener('change', e => {
    if (!e.target.closest('#ed-body')) return;
    const structural = applyInput(e.target); setDirty();
    if (structural) renderAll(); else renderMap();
  });

  function move(kind, id, dir) {
    const list = kind === 'group' ? S.data.groups : listOf(kind);
    const i = list.findIndex(x => x.id === id);
    let j = i + dir;
    if (kind === 'field') {      // nur innerhalb derselben Straße tauschen
      while (j >= 0 && j < list.length && list[j].group !== list[i].group) j += dir;
    }
    if (j < 0 || j >= list.length) return;
    [list[i], list[j]] = [list[j], list[i]];
    setDirty(); renderAll();
  }

  document.addEventListener('click', e => {
    const b = e.target.closest('[data-action]');
    if (!b) return;
    const {action, kind, id} = b.dataset;
    if (action === 'cancel-mode') return setMode(null);
    if (action === 'add-field') return setMode('field', b.dataset.group);
    if (action === 'add-station') return setMode('station');
    if (action === 'add-reserve') return setMode('reserve');
    if (action === 'add-group') {
      const name = prompt('Name der neuen Straße (Farbe):', 'Neue Straße');
      if (!name) return;
      S.data.groups.push({id: uniqueId(slug(name), S.data.groups), name, color: '#7a5cff', field_ids: []});
      setDirty(); renderAll(); return;
    }
    if (action === 'up' || action === 'down') return move(kind, id, action === 'up' ? -1 : 1);
    if (action === 'locate') {
      const item = find(kind, id);
      if (item) { map.setView([item.lat, item.lon], Math.max(map.getZoom(), 17)); select(kind, id, false); }
      return;
    }
    if (action === 'delete') {
      const item = kind === 'group' ? group(id) : find(kind, id);
      if (!item || !confirm(`„${item.name}“ wirklich löschen?`)) return;
      if (kind === 'group') S.data.groups = S.data.groups.filter(g => g.id !== id);
      else if (kind === 'field') S.data.fields = S.data.fields.filter(x => x.id !== id);
      else if (kind === 'station') S.data.stations = S.data.stations.filter(x => x.id !== id);
      else S.data.reserve = S.data.reserve.filter(x => x.id !== id);
      if (S.selected && S.selected.id === id) S.selected = null;
      setDirty(); renderAll();
    }
  });

  // ------------------------------------------------------------------ Laden / Speichern
  async function load() {
    const r = await fetch('/editor/api/daten', {credentials: 'same-origin'});
    if (r.status === 401 || r.redirected) { location.href = '/login'; return; }
    const j = await r.json();
    S.data = j.data; S.struktur = j.struktur; S.phase = j.phase; S.quelle = j.quelle; S.selected = null;
    setMode(null); setDirty(false); renderAll();
  }
  $('ed-save').addEventListener('click', async () => {
    $('ed-save').disabled = true;
    $('ed-status').textContent = 'speichert …';
    try {
      const r = await fetch('/editor/api/speichern', {method: 'POST', credentials: 'same-origin',
        headers: {'Content-Type': 'application/json'}, body: JSON.stringify(S.data)});
      const j = await r.json();
      if (j.ok) {
        S.data = j.data; S.quelle = j.quelle || S.quelle;
        setDirty(false); renderAll();
      } else {
        setDirty(true);
      }
      message(j.message, j.ok, j.notes);
    } catch (err) {
      setDirty(true); message('Speichern fehlgeschlagen: ' + err, false);
    }
  });
  $('ed-reset').addEventListener('click', () => {
    if (S.dirty && !confirm('Alle ungespeicherten Änderungen verwerfen?')) return;
    message(''); load();
  });
  window.addEventListener('beforeunload', e => { if (S.dirty) { e.preventDefault(); e.returnValue = ''; } });
  load();
})();
