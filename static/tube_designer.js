/* PenguinCAM custom tube designer.
 *
 * Owns the "Custom" tube pattern: the palette, the feature list, the properties strip
 * and the design document itself. wizard.js keeps owning the Layout canvas and calls in
 * here at five points - drawing the panel (`render`), placing (`placeAt`), picking
 * (`hitTest`/`select`), moving and deleting (`nudge`/`removeSelected`), and asking the
 * server what the design comes to (`refresh`).
 *
 * ONE RULE ABOVE ALL: this file never decides whether a feature is legal, how big a
 * named size is, or how many holes a run comes to. It sends the design to
 * /api/tube-pattern and draws the answer. The wizard duplicated the pattern generator's
 * constants exactly once, to work a count out for itself, and the copy went stale and
 * disagreed with the program it was describing. The size menu, the grid, the diameters
 * and every refusal below come from the server.
 */
(function () {
  'use strict';

  var api = {};
  var ctx = null;            // {state, cfg, onGeometry, onChange, parseLength}
  var geom = null;           // last resolved geometry from the server
  var selected = -1;         // index into state.tubeDesign.features, or -1
  var token = 0;             // guards against a stale response landing late
  var timer = null;

  function $(sel, root) { return (root || document).querySelector(sel); }
  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      if (k === 'class') node.className = attrs[k];
      else if (k === 'text') node.textContent = attrs[k];
      else if (k.slice(0, 2) === 'on') node.addEventListener(k.slice(2), attrs[k]);
      else if (attrs[k] !== null && attrs[k] !== undefined && attrs[k] !== false) node.setAttribute(k, attrs[k]);
    });
    (children || []).forEach(function (c) { if (c) node.appendChild(c); });
    return node;
  }
  function num(v) { var n = parseFloat(v); return isFinite(n) ? n : null; }

  /* The palette. Runs and arrays are conveniences that the SERVER expands into plain
     holes; nothing in the program knows they existed. */
  var KINDS = [
    { id: 'hole', label: 'Hole', sized: true },
    { id: 'hole-run', label: 'Hole run (a line of them)', sized: true },
    { id: 'hole-array', label: 'Hole array (rows x columns)', sized: true },
    { id: 'bearing', label: 'Bearing bore', sized: false },
    { id: 'pocket', label: 'Pocket (rounded rectangle)', sized: false },
  ];

  var CUSTOM_SIZE = '__custom__';

  function cfg() { return (ctx && ctx.cfg) || {}; }
  function grid() { return cfg().grid || 0.125; }
  function sizeOptions() {
    return (cfg().sizes || []).filter(function (s) { return s.kind === 'clearance'; });
  }

  function design() {
    var state = ctx.state;
    if (!state.tubeDesign || !state.tubeDesign.features) {
      state.tubeDesign = { version: 1, features: [] };
    }
    return state.tubeDesign;
  }
  function features() { return design().features; }

  function enabled() {
    return ctx && ctx.state.mode === 'tubing' && ctx.state.tubePattern === 'custom';
  }

  /* --------------------------------------------------------------- the document */

  function snap(v) {
    var g = grid();
    return Math.round(v / g) * g;
  }

  function currentKind() {
    var sel = $('#td-kind');
    return (sel && sel.value) || 'hole';
  }
  function currentSize() {
    var sel = $('#td-size');
    return (sel && sel.value) || (sizeOptions()[0] || {}).id || '10-32';
  }
  function currentDiameter() {
    var input = $('#td-diameter');
    return num(input && input.value);
  }

  /* Give a new feature its size the way the palette says: a named size, or the custom
     diameter box. Named is sent by NAME - the server resolves it - so this cannot ship a
     wrong number even if this file's idea of 10-32 ever drifted. */
  function applySize(feature) {
    if (currentSize() === CUSTOM_SIZE) {
      var d = currentDiameter();
      if (d) feature.diameter = d;
      else feature.size = (sizeOptions()[0] || {}).id;
    } else {
      feature.size = currentSize();
    }
    return feature;
  }

  function defaultFeature(kind, x, y) {
    var tool = (ctx.state.tool_diameter || 0.157);
    if (kind === 'bearing') return { type: 'bearing', x: x, y: y };
    if (kind === 'pocket') {
      return { type: 'pocket', x: x, y: y, w: 1.0, h: 2.0,
               corner_radius: Math.max(0.25, tool / 2) };
    }
    if (kind === 'hole-run') {
      return applySize({ type: 'hole-run', x: x, y: y, pitch: 0.5, count: 4, axis: 'y' });
    }
    if (kind === 'hole-array') {
      return applySize({ type: 'hole-array', x: x, y: y, pitch_x: 0.5, pitch_y: 0.5,
                         cols: 2, rows: 4 });
    }
    return applySize({ type: 'hole', x: x, y: y });
  }

  api.placeAt = function (x, y) {
    if (!enabled()) return;
    features().push(defaultFeature(currentKind(), snap(x), snap(y)));
    selected = features().length - 1;
    changed();
  };

  api.selectedIndex = function () { return selected; };

  /* Where the selected feature is authored to be, so a drag can keep the grab offset
     instead of snapping the feature's centre onto the cursor. */
  api.selectedPosition = function () {
    var f = features()[selected];
    return f ? { x: f.x, y: f.y } : null;
  };

  api.select = function (index) {
    selected = (index >= 0 && index < features().length) ? index : -1;
    render();
    if (ctx.onChange) ctx.onChange();
  };

  api.removeSelected = function () {
    if (selected < 0 || selected >= features().length) return false;
    features().splice(selected, 1);
    selected = -1;
    changed();
    return true;
  };

  /* Move by a world-space delta. Snapped to the grid ABSOLUTELY, not by adding a snapped
     delta: dragging with a fractional offset would otherwise walk a feature off the grid
     a thousandth at a time. */
  api.moveSelected = function (x, y) {
    var f = features()[selected];
    if (!f) return;
    f.x = snap(x);
    f.y = snap(y);
    changed();
  };

  api.nudge = function (dx, dy) {
    var f = features()[selected];
    if (!f) return false;
    f.x = snap(f.x + dx * grid());
    f.y = snap(f.y + dy * grid());
    changed();
    return true;
  };

  /* Which feature is under this point. The server's resolved geometry is the truth about
     where a feature actually IS - a run of eight holes is not a point at its origin - so
     the hit test walks that, falling back to the origin for a feature the server refused
     and therefore returned no geometry for. */
  api.hitTest = function (x, y) {
    var fs = (geom && geom.features) || [];
    var best = -1, bestD = Infinity;
    fs.forEach(function (f) {
      var d = featureDistance(f, x, y);
      if (d !== null && d < bestD) { bestD = d; best = f.index; }
    });
    if (best >= 0 && bestD <= grid()) return best;
    // Nothing under the cursor: fall back to the authored positions, so a refused
    // feature (no geometry) can still be picked up and moved somewhere legal.
    var fallback = -1, fbD = Infinity;
    features().forEach(function (f, i) {
      var d = Math.hypot((f.x || 0) - x, (f.y || 0) - y);
      if (d < fbD) { fbD = d; fallback = i; }
    });
    // -1, not `best`: `best` is the NEAREST feature at any distance, and returning it
    // for a click on empty tube meant the second feature could never be placed - every
    // click after the first was read as picking up the first one.
    return fbD <= grid() * 2 ? fallback : -1;
  };

  function featureDistance(f, x, y) {
    var best = null;
    (f.holes || []).forEach(function (h) {
      var d = Math.hypot(h.x - x, h.y - y) - h.d / 2;
      if (best === null || d < best) best = d;
    });
    (f.pockets || []).forEach(function (ring) {
      var minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
      ring.forEach(function (p) {
        minX = Math.min(minX, p[0]); maxX = Math.max(maxX, p[0]);
        minY = Math.min(minY, p[1]); maxY = Math.max(maxY, p[1]);
      });
      var dx = Math.max(minX - x, 0, x - maxX);
      var dy = Math.max(minY - y, 0, y - maxY);
      var d = Math.hypot(dx, dy);
      if (best === null || d < best) best = d;
    });
    return best;
  }

  /* ------------------------------------------------------------------ the server */

  api.geometry = function () { return geom; };

  /* Debounced, because this fires on every drag frame and every keystroke in the
     properties strip. The token guards ordering: a slow answer for an old design must
     not overwrite a fast answer for the current one. */
  api.refresh = function () {
    if (!enabled()) { geom = null; if (ctx.onGeometry) ctx.onGeometry(null); return; }
    if (timer) clearTimeout(timer);
    timer = setTimeout(send, 300);
  };

  function send() {
    var state = ctx.state;
    var mine = ++token;
    var fd = new FormData();
    fd.append('size', state.tubeSize);
    fd.append('length', state.tubePatternLength || 0);
    fd.append('tool', state.tool_diameter);
    if (state.machine_id) fd.append('machine_id', state.machine_id);
    fd.append('design', JSON.stringify(design()));
    fetch('/api/tube-pattern', { method: 'POST', body: fd, credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (mine !== token) return;
        geom = (j && !j.error) ? j : null;
        render();
        if (ctx.onGeometry) ctx.onGeometry(geom);
      })
      .catch(function () {
        if (mine !== token) return;
        geom = null;
        render();
        if (ctx.onGeometry) ctx.onGeometry(null);
      });
  }

  function changed() {
    render();
    api.refresh();
    if (ctx.onChange) ctx.onChange();
  }

  /* ---------------------------------------------------------------------- the panel */

  function labelFor(f, index) {
    var resolved = geom && geom.features && geom.features[index];
    var n = resolved ? (resolved.holes || []).length : 0;
    var what = f.type;
    if (f.type === 'hole') what = 'Hole';
    else if (f.type === 'bearing') what = 'Bearing bore';
    else if (f.type === 'pocket') what = 'Pocket ' + fmt(f.w) + '" x ' + fmt(f.h) + '"';
    else if (f.type === 'hole-run') what = 'Run of ' + (n || f.count);
    else if (f.type === 'hole-array') what = 'Array ' + f.cols + ' x ' + f.rows
      + (n ? ' (' + n + ')' : '');
    var size = f.size ? ' ' + f.size : (f.diameter ? ' ' + fmt(f.diameter) + '"' : '');
    return what + size + ' at ' + fmt(f.x) + ', ' + fmt(f.y);
  }

  /* What the list DOM depends on: the features themselves, how many holes each resolved
     to, and whether the server refused it. Not the selection - that is a class change. */
  var listSig = null;
  var propsSig = null;

  function listSignature() {
    var counts = ((geom && geom.features) || []).map(function (f) {
      return (f.holes || []).length + (f.ok ? '' : '!');
    });
    return JSON.stringify([features(), counts]);
  }

  function fmt(v) {
    var n = num(v);
    return n === null ? '?' : (Math.round(n * 1000) / 1000).toString();
  }

  /* Every properties control keeps a `sync` that re-reads its value from the feature -
     but NEVER while it has focus. Editing a field re-resolves the design, which used to
     rebuild this whole strip: the input the user was typing into was destroyed after the
     first character. Now the strip is rebuilt only when a DIFFERENT feature is selected,
     and otherwise just re-syncs the fields nobody is typing in. */
  var propsFields = [];

  function numberField(label, get, step, onCommit) {
    var input = el('input', {
      type: 'number', step: step, value: get(), autocomplete: 'off',
      class: 'td-num',
      oninput: function () { var v = num(this.value); if (v !== null) onCommit(v); },
    });
    propsFields.push(function () {
      if (document.activeElement !== input) input.value = get();
    });
    return el('label', { class: 'td-prop' }, [el('span', { text: label }), input]);
  }

  function selectField(label, options, get, onCommit) {
    var sel = el('select', { autocomplete: 'off',
      onchange: function () { onCommit(this.value); } });
    options.forEach(function (o) {
      sel.appendChild(el('option', { value: o.id, text: o.label }));
    });
    sel.value = get();
    propsFields.push(function () {
      if (document.activeElement !== sel) sel.value = get();
    });
    return el('label', { class: 'td-prop' }, [el('span', { text: label }), sel]);
  }

  function propsFor(f) {
    var rows = [];
    rows.push(numberField('X', function () { return f.x; }, grid(),
                          function (v) { f.x = v; changed(); }));
    rows.push(numberField('Y', function () { return f.y; }, grid(),
                          function (v) { f.y = v; changed(); }));
    if (f.type === 'hole' || f.type === 'hole-run' || f.type === 'hole-array') {
      var opts = sizeOptions().map(function (s) {
        return { id: s.id, label: s.label + ' (' + s.diameter + '")' };
      });
      opts.push({ id: CUSTOM_SIZE, label: 'Custom diameter' });
      rows.push(selectField('Size', opts, function () { return f.size || CUSTOM_SIZE; },
        function (v) {
          if (v === CUSTOM_SIZE) { f.diameter = f.diameter || 0.25; delete f.size; }
          else { f.size = v; delete f.diameter; }
          changed();
        }));
      if (!f.size) {
        rows.push(numberField('Diameter', function () { return f.diameter; }, 0.001,
          function (v) { f.diameter = v; changed(); }));
      }
    }
    if (f.type === 'hole-run') {
      rows.push(numberField('Pitch', function () { return f.pitch; }, grid(),
                            function (v) { f.pitch = v; changed(); }));
      rows.push(numberField('Count', function () { return f.count; }, 1,
                            function (v) { f.count = Math.round(v); changed(); }));
      rows.push(selectField('Along', [{ id: 'y', label: 'the tube (Y)' },
                                      { id: 'x', label: 'the face (X)' }],
                            function () { return f.axis || 'y'; },
                            function (v) { f.axis = v; changed(); }));
    }
    if (f.type === 'hole-array') {
      rows.push(numberField('Pitch X', function () { return f.pitch_x; }, grid(),
                            function (v) { f.pitch_x = v; changed(); }));
      rows.push(numberField('Pitch Y', function () { return f.pitch_y; }, grid(),
                            function (v) { f.pitch_y = v; changed(); }));
      rows.push(numberField('Columns', function () { return f.cols; }, 1,
                            function (v) { f.cols = Math.round(v); changed(); }));
      rows.push(numberField('Rows', function () { return f.rows; }, 1,
                            function (v) { f.rows = Math.round(v); changed(); }));
    }
    if (f.type === 'pocket') {
      rows.push(numberField('Width', function () { return f.w; }, grid(),
                            function (v) { f.w = v; changed(); }));
      rows.push(numberField('Height', function () { return f.h; }, grid(),
                            function (v) { f.h = v; changed(); }));
      rows.push(numberField('Corner radius', function () { return f.corner_radius; }, 0.0625,
                            function (v) { f.corner_radius = v; changed(); }));
    }
    return rows;
  }

  function render() {
    if (!ctx) return;
    var panel = $('#tube-designer');
    if (!panel) return;
    panel.hidden = !enabled();
    if (!enabled()) return;

    var sizeSel = $('#td-size');
    if (sizeSel && !sizeSel.options.length) {
      sizeOptions().forEach(function (s) {
        sizeSel.appendChild(el('option', { value: s.id,
                                           text: s.label + ' (' + s.diameter + '")' }));
      });
      sizeSel.appendChild(el('option', { value: CUSTOM_SIZE, text: 'Custom diameter' }));
      if (sizeSel.querySelector('option[value="10-32"]')) sizeSel.value = '10-32';
    }
    var kindSel = $('#td-kind');
    if (kindSel && !kindSel.options.length) {
      KINDS.forEach(function (k) {
        kindSel.appendChild(el('option', { value: k.id, text: k.label }));
      });
    }
    var sized = KINDS.filter(function (k) { return k.id === currentKind(); })[0];
    var sizeField = $('#td-size-field');
    if (sizeField) sizeField.hidden = !(sized && sized.sized);
    var diaField = $('#td-diameter-field');
    if (diaField) diaField.hidden = !(sized && sized.sized && currentSize() === CUSTOM_SIZE);

    // The feature list. Rebuilt only when its CONTENT changed - otherwise just
    // re-labelled and re-classed. A rebuild replaces the row under the pointer, and a
    // mousedown followed by a mouseup on a DIFFERENT node produces no click at all: with
    // focus still in the tube-length field, the field's blur handler redrew this panel
    // mid-press and the row you clicked was never selected.
    var list = $('#td-list');
    if (list) {
      var sig = listSignature();
      if (sig !== listSig) {
        listSig = sig;
        list.innerHTML = '';
        features().forEach(function (f, i) {
          var item = el('li', { class: 'td-item',
            onclick: function () { api.select(i); },
          }, [el('span', { class: 'td-item-label', text: labelFor(f, i) }),
              el('button', { type: 'button', class: 'btn small td-remove',
                             title: 'Remove this feature',
                             onclick: function (e) {
                               e.stopPropagation();
                               selected = i; api.removeSelected();
                             }, text: 'x' })]);
          list.appendChild(item);
        });
        if (!features().length) {
          list.appendChild(el('li', { class: 'td-empty',
            text: 'No features yet - click the tube in the Layout panel to place one.' }));
        }
      }
      Array.prototype.forEach.call(list.querySelectorAll('.td-item'), function (row, i) {
        var resolved = geom && geom.features && geom.features[i];
        row.className = 'td-item' + (i === selected ? ' selected' : '')
                        + (resolved && !resolved.ok ? ' bad' : '');
      });
    }

    // The properties strip for whatever is selected. Rebuilt only when a different
    // feature (or a differently-shaped one) is selected; otherwise the existing fields
    // re-read their values, skipping whichever one is being typed into.
    var props = $('#td-props');
    if (props) {
      var f = features()[selected];
      var psig = f ? (selected + ':' + f.type + ':' + (f.size ? 'named' : 'custom')) : '';
      props.hidden = !f;
      if (psig !== propsSig) {
        propsSig = psig;
        propsFields = [];
        props.innerHTML = '';
        if (f) propsFor(f).forEach(function (row) { props.appendChild(row); });
      } else {
        propsFields.forEach(function (sync) { sync(); });
      }
    }

    // Errors and warnings, straight from the server. No client-side validity model.
    var errors = $('#td-errors');
    if (errors) {
      var msgs = (geom && geom.errors) || [];
      var warn = (geom && geom.warnings) || [];
      errors.textContent = msgs.concat(features().length ? warn : []).join('\n');
    }
    var note = $('#td-note');
    if (note) {
      note.textContent = geom
        ? ('This design cuts ' + geom.summary + ' per face, milled with your '
           + 'end mill, and is mirrored onto the opposite wall.')
        : 'Enter the tube length to see what this design cuts.';
    }
  }

  api.render = render;

  /* Firefox restores selects across a soft reload, so the palette can come back showing
     something other than what this module thinks is selected. Nothing here depends on an
     event having fired: the panel is rebuilt from the controls' ACTUAL values. */
  api.adopt = function () { render(); };

  api.init = function (options) {
    ctx = options;
    design();                       // make sure the document exists before anything reads it
    var kindSel = $('#td-kind');
    if (kindSel) kindSel.addEventListener('change', function () { render(); });
    var sizeSel = $('#td-size');
    if (sizeSel) sizeSel.addEventListener('change', function () { render(); });
    render();
  };

  api.enabled = enabled;

  window.PCTubeDesigner = api;
})();
