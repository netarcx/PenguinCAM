/* UV-CAM multi-part wizard.
 * One codebase for the standalone (/app, source=upload) and embedded
 * (/onshape-panel, source=onshape) contexts; the part source is the only
 * difference and is selected via window.PenguinCAM.source.
 */
(function () {
  'use strict';

  var CFG = window.PenguinCAM || { source: 'upload', bed: { width: 24, height: 24 },
                                   defaultTool: 0.25, defaultToolText: '1/4"',
                                   defaultMaterial: 'aluminum', machines: {} };
  var DEBUG = /(?:^|[?&])debug=1(?:&|$)/.test(location.search);

  var ALL_STEPS = ['setup', 'parts', 'tools', 'layout', 'preview'];
  // Every mode uses the Layout step. In 2D it nests parts on a sheet; in tubing it's
  // used only to orient the face(s) to the tube-jig axis (see the tube handling in the
  // rotate + validate paths below). 2.5D is a single part positioned at the origin.
  // "tools" is the multi-tool operations editor (static/multitool.js) and is only in the
  // flow when the job actually uses several tools, so the ordinary single-tool flow is
  // the same four steps it has always been.
  function steps() {
    if (multiToolOn()) return ALL_STEPS;
    return ALL_STEPS.filter(function (s) { return s !== 'tools'; });
  }

  // The editor lives in its own file and is optional; treat a missing script as "off"
  // rather than letting the whole wizard fail to start.
  function multiToolOn() {
    return !!(window.PCMultiTool && window.PCMultiTool.enabled());
  }

  function drilledTubePatternOn() {
    return state.mode === 'tubing' && state.tubePattern === 'holes';
  }

  // A drilled pattern substitutes a fixed 0.201" twist drill. That tool may only peck
  // the holes; the generator deliberately refuses to push it sideways to face an end.
  function tubeEndMillingAvailable() {
    return state.mode === 'tubing' && !drilledTubePatternOn();
  }

  function engraveOn() {
    return state.mode === '2d' && state.engrave;
  }

  function effectiveToolText() {
    return drilledTubePatternOn() ? '0.201" twist drill' : state.tool_diameter_text;
  }

  function effectiveToolDiameter() {
    return drilledTubePatternOn() ? 0.201 : state.tool_diameter;
  }

  var state = {
    source: CFG.source,
    step: 'setup',
    mode: '2d',
    machine_id: null,
    material: CFG.defaultMaterial || 'aluminum',
    tool_diameter: parseFloat(CFG.defaultTool) || 0.25,
    tool_diameter_text: CFG.defaultToolText || '1/4"',  // user's raw input, shown verbatim (e.g. "4mm")
    tool_flutes: 1,
    thickness: 0.25,
    thickness_text: '0.25"',
    tab_spacing: 6.0,
    // Optional deburr / chamfer pass (2D standard mode): a V-bit edge break run after
    // the profile, behind a manual tool change. Widths/diameters are inches; the _text
    // fields keep the user's raw input (e.g. '1/2"' or "6mm") for display.
    chamfer: { on: false, bit: 0.5, bit_text: '1/2"', angle: 90,
               width: 0.02, width_text: '0.02"',
               perimeter: true, holes: true, pockets: false },
    // Which surface the operator zeros Z on: 'board' (the sacrifice board, the
    // default this app has always used) or 'stock_top'. It changes only the numbers in
    // the program, never the motion - but zeroing on the wrong one puts every cut a
    // material thickness out, so it is stated on the setup panel and in the summary.
    zDatum: 'board',
    // Prove the setup before committing to a cut: the same program raised clear of the
    // work with the spindle off. Never sticky across a generate - see bindDryRun.
    dryRun: false,
    // The sheet being cut from, chosen out of the team's stock list. null means the
    // long-standing behaviour: the stock IS the parts' combined bounding box, and the
    // G54 origin is its lower-left corner. With a sheet chosen, the sheet is the stock
    // and the origin is the SHEET's corner, so a part keeps its place on the material
    // between jobs.
    stock: null,
    // Optional repeatable table fixture. Three removable dowels locate two stock edges;
    // external bolts drive low-profile clamps rather than drilling through good stock.
    // Coordinates are machine-bed coordinates, while every part remains stock-relative.
    fixture: { on: false,
               x: 0.375, x_text: '0.375"', y: 0.375, y_text: '0.375"',
               pin: 0.25, pin_text: '0.25"', bolt: 0.25, bolt_text: '0.25"' },
    //: A one-off message from Auto-arrange / Fill sheet, shown above the layout errors
    //: and cleared as soon as the layout changes under it - a notice about a nest that
    //: no longer exists is worse than no notice.
    layoutNotice: '',
    // Cut each part's name into its own face. Off by default: it costs cycle time and
    // needs a fine cutter, so it should be a decision rather than a surprise.
    engrave: false,
    // Optional ceiling on the depth of one contour pass (inches; null = automatic).
    // More, shallower passes to baby fragile or multi-flute cutters - clamp-only.
    max_pass_depth: null,
    max_pass_depth_text: '',
    // Tubing-only settings.
    tubeHeight: 1.0,
    tubeHeight_text: '1"',
    squareEnd: false,
    cutToLength: false,
    thicknessTouched: false,
    thicknessBeforeTube: null,
    tubeSize: '2x1-flat',
    tubePattern: 'none',          // 'none' = pattern comes from the user's DXF
    tubePatternLength: 0,
    tubePatternLength_text: '',
    // The custom design, when tubePattern === 'custom'. Owned by static/tube_designer.js;
    // sent verbatim to /process, which resolves and validates it server-side.
    tubeDesign: { version: 1, features: [] },
    // The machine envelope is a read-only constraint; the parts' combined bounding box
    // is the stock (G54 origin = its lower-left).
    machine: { width: CFG.bed.width || 24, height: CFG.bed.height || 24, name: CFG.machineName || 'Machine' },
    multitool: false,     // when true, the Tools & Ops step plans several tools per part
    tools: null,          // [{slot,name,diameter,flutes,type,included_angle}] - see multitool.js
    parts: [],            // {id,name,width,height,outline,holes,inner,file,cx,cy,rotation,flipped,ops}
    selectedIds: [],
    zoom: 1,
    saveAction: 'download',   // 'download' | 'drive' (final-step action)
    lastResponse: null,
  };
  // Canvas text uses the same two faces as the chrome around it (the canvas has
  // no stylesheet of its own, so the stacks are repeated here).
  var CANVAS_FONT = "'Barlow', system-ui, -apple-system, sans-serif";
  var CANVAS_MONO = "ui-monospace, 'Roboto Mono', Menlo, Consolas, monospace";
  // The bits on the shelf: the team's saved cutters merged over the built-ins, as the
  // server assembled them. Kept on CFG so the Tools panel and the Setup picker are
  // always looking at the same list - saving a bit in one refreshes the other.
  function toolLibrary() { return CFG.toolLibrary || {}; }
  function setToolLibrary(library) {
    CFG.toolLibrary = library || {};
    renderBitPicker();
    if (window.PCMultiTool && window.PCMultiTool.render) window.PCMultiTool.render();
  }
  /* Bits in the order a person looks for one: smallest first, ties by name. The
     library arrives as a JSON object, and JSON objects come back key-sorted, which put
     the 3mm cutter after the 3/8 in one. */
  function sortedBitIds(lib, ids) {
    return ids.slice().sort(function (a, b) {
      var da = lib[a].diameter || 0, db = lib[b].diameter || 0;
      if (Math.abs(da - db) > 1e-6) return da - db;
      return String(lib[a].name).localeCompare(String(lib[b].name));
    });
  }

  // Must agree with slugify_tool_id in team_config.py: the id is how the browser asks
  // "is this row already saved?" without carrying the whole list around.
  function bitId(name) {
    return String(name || '').trim().toLowerCase()
      .replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '') || 'bit';
  }

  var partSeq = 0;
  var debugEvents = [];
  var viewer = null;

  /* ----------------------------------------------------------------- utils */
  function $(sel, root) { return (root || document).querySelector(sel); }
  function $all(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }
  function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }

  // Parse an Onshape-style length: a number with an optional unit; no unit means inches
  // (this app is inch-native). Returns the value in inches, or null if unparseable.
  // Inspired by frcdesign/FRCDesignApp's input-parser (github.com/frcdesign/FRCDesignApp).
  var LENGTH_TO_INCH = {
    '': 1, 'in': 1, 'inch': 1, 'inches': 1, '"': 1,
    'mm': 1 / 25.4, 'millimeter': 1 / 25.4, 'millimeters': 1 / 25.4,
    'cm': 1 / 2.54, 'centimeter': 1 / 2.54, 'centimeters': 1 / 2.54,
    'm': 1 / 0.0254, 'meter': 1 / 0.0254, 'meters': 1 / 0.0254,
    'ft': 12, 'foot': 12, 'feet': 12, "'": 12,
    'yd': 36, 'yard': 36, 'yards': 36,
  };
  function parseLength(text) {
    if (text == null) return null;
    var s = String(text).trim().toLowerCase();
    var value, rest;
    var frac = s.match(/^([+-]?\d+)\s*\/\s*(\d+)\s*(.*)$/);   // fractions (e.g. 1/8", common for SAE tools)
    if (frac) {
      var den = parseFloat(frac[2]);
      if (!den) return null;
      value = parseFloat(frac[1]) / den; rest = frac[3];
    } else {
      var dec = s.match(/^([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*(.*)$/);
      if (!dec) return null;
      value = parseFloat(dec[1]); rest = dec[2];
    }
    var factor = LENGTH_TO_INCH[rest.trim()];
    if (!isFinite(value) || factor == null) return null;
    var inches = value * factor;
    return inches > 0 ? inches : null;
  }

  // Wire a text input as an Onshape-style length field. Parses units live; on commit a
  // bare number/fraction gets the implied inch mark (never converts between unit systems,
  // so "4mm" stays "4mm"); invalid input is flagged and reverted on blur. onValid(inches,
  // text) is called with each accepted value; getText() returns the last valid text (for
  // revert). Shared by the tool-diameter and thickness fields.
  function bindLengthField(input, getText, onValid) {
    if (!input) return;
    function commit(raw) {
      raw = raw.trim();
      if (/^[+-]?[0-9.\/\s]+$/.test(raw)) raw += '"';   // bare number/fraction -> implied inch mark
      onValid(parseLength(raw), raw);
      input.value = raw;
    }
    if (parseLength(input.value)) commit(input.value);   // adopt the rendered default/config value
    input.addEventListener('input', function () {
      var inches = parseLength(this.value);
      if (inches) { onValid(inches, this.value.trim()); this.classList.remove('invalid'); }
      else { this.classList.add('invalid'); }            // keep last valid value; flag bad text
    });
    input.addEventListener('change', function () {
      this.classList.remove('invalid');
      if (parseLength(this.value)) commit(this.value);
      else { this.value = getText(); }                   // revert unparseable input on blur
    });
  }

  // Apply light/dark theme (tied to Onshape's theme). The server sets it on <html> at
  // render; this handles live changes if Onshape posts a theme update while open.
  function applyTheme(theme) {
    if (theme !== 'light' && theme !== 'dark') return;
    document.documentElement.setAttribute('data-theme', theme);
    if (viewer && viewer.setTheme) viewer.setTheme(theme);
    if (state.step === 'layout') drawLayout();
    dbg('theme', theme);
  }

  function dbg(label, data) {
    debugEvents.unshift({ t: new Date().toISOString().slice(11, 19), label: label, data: data });
    debugEvents = debugEvents.slice(0, 12);
    renderDebug();
  }

  function renderDebug() {
    if (!DEBUG) return;
    var el = $('#debug-content');
    if (!el) return;
    var snapshot = {
      source: state.source, step: state.step, mode: state.mode,
      tool: state.tool_diameter, machine: state.machine,
      parts: state.parts.map(function (p) {
        return { name: p.name, w: p.width, h: p.height, cx: p.cx, cy: p.cy, rot: p.rotation };
      }),
    };
    el.textContent = JSON.stringify(snapshot, null, 1) + '\n--- events ---\n' +
      debugEvents.map(function (e) { return e.t + ' ' + e.label + ' ' + (e.data != null ? JSON.stringify(e.data) : ''); }).join('\n');
  }

  function timestamp() {
    var d = new Date();
    function p(n) { return String(n).padStart(2, '0'); }
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' +
      p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
  }

  /* -------------------------------------------------------------- geometry */
  function rotatePoint(x, y, deg) {
    // Match the server: transform_coordinates rotates clockwise (angle_rad = -radians).
    var a = -deg * Math.PI / 180;
    return [x * Math.cos(a) - y * Math.sin(a), x * Math.sin(a) + y * Math.cos(a)];
  }

  // Returns the part's placed footprint: outline points normalized so the rotated
  // bounding-box minimum is (0,0), plus that footprint's width/height. Mirrors the
  // server pinning the rotated bbox-min to placement_offset.
  function placedShape(part) {
    var fx = part.flipped ? -1 : 1;   // horizontal flip (mirror across X) before rotating
    var pts = part.outline.map(function (pt) { return rotatePoint(fx * pt[0], pt[1], part.rotation); });
    var minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    pts.forEach(function (pt) {
      if (pt[0] < minX) minX = pt[0]; if (pt[0] > maxX) maxX = pt[0];
      if (pt[1] < minY) minY = pt[1]; if (pt[1] > maxY) maxY = pt[1];
    });
    var norm = pts.map(function (pt) { return [pt[0] - minX, pt[1] - minY]; });
    var holes = (part.holes || []).map(function (h) {
      var c = rotatePoint(fx * h.cx, h.cy, part.rotation);
      return { cx: c[0] - minX, cy: c[1] - minY, r: h.r };
    });
    // Internal feature rings (2.5D pockets/steps) ride the same flip+rotate+normalize.
    var inner = (part.inner || []).map(function (ring) {
      return ring.map(function (pt) {
        var c = rotatePoint(fx * pt[0], pt[1], part.rotation);
        return [c[0] - minX, c[1] - minY];
      });
    });
    return { pts: norm, holes: holes, inner: inner, w: maxX - minX, h: maxY - minY,
             minX: minX, minY: minY, flipX: fx };
  }

  // Parts are stored by their center (cx, cy) so rotation happens in place. The
  // placement is the derived axis-aligned footprint whose bbox-min the server pins to
  // place_x/place_y.
  function placement(part) {
    var s = placedShape(part);
    return { x: part.cx - s.w / 2, y: part.cy - s.h / 2, w: s.w, h: s.h, shape: s };
  }

  function footprint(part) {
    var p = placement(part);
    return { minX: p.x, minY: p.y, maxX: p.x + p.w, maxY: p.y + p.h, shape: p.shape };
  }

  // The placed perimeter polygon in sheet coordinates (mirror of placed_polygon()).
  function placedPolygon(part) {
    var p = placement(part);
    return p.shape.pts.map(function (pt) { return [p.x + pt[0], p.y + pt[1]]; });
  }

  function partLabelText(part) {
    var number = String(part.number || '').trim();
    return String(part.name || 'part').trim() + (number ? ' #' + number : '');
  }

  function placedLabelAnchor(part) {
    var pl = placement(part), s = pl.shape;
    var localX = part.label_x == null ? part.width / 2 : part.label_x;
    var localY = part.label_y == null ? part.height / 2 : part.label_y;
    var transformed = rotatePoint(s.flipX * localX, localY, part.rotation);
    return { x: pl.x + transformed[0] - s.minX,
             y: pl.y + transformed[1] - s.minY };
  }

  function setLabelAnchorFromWorld(part, wx, wy) {
    var pl = placement(part), s = pl.shape;
    if (!pointInPoly([wx, wy], placedPolygon(part))) return false;
    var transformed = [wx - pl.x + s.minX, wy - pl.y + s.minY];
    var local = rotatePoint(transformed[0], transformed[1], -part.rotation);
    part.label_x = s.flipX * local[0];
    part.label_y = local[1];
    invalidatePreview();
    return true;
  }

  function segPointDist(px, py, ax, ay, bx, by) {
    var dx = bx - ax, dy = by - ay, len2 = dx * dx + dy * dy;
    if (len2 === 0) return Math.hypot(px - ax, py - ay);
    var t = ((px - ax) * dx + (py - ay) * dy) / len2;
    t = Math.max(0, Math.min(1, t));
    return Math.hypot(px - (ax + t * dx), py - (ay + t * dy));
  }
  function segsIntersect(a, b, c, d) {
    function ccw(p, q, r) { return (r[1] - p[1]) * (q[0] - p[0]) > (q[1] - p[1]) * (r[0] - p[0]); }
    return ccw(a, c, d) !== ccw(b, c, d) && ccw(a, b, c) !== ccw(a, b, d);
  }
  function pointInPoly(pt, poly) {
    var x = pt[0], y = pt[1], inside = false;
    for (var i = 0, j = poly.length - 1; i < poly.length; j = i++) {
      var xi = poly[i][0], yi = poly[i][1], xj = poly[j][0], yj = poly[j][1];
      if (((yi > y) !== (yj > y)) && (x < (xj - xi) * (y - yi) / (yj - yi) + xi)) inside = !inside;
    }
    return inside;
  }
  // Minimum distance between two polygon outlines (0 if they intersect or one
  // contains the other). Used for the kerf-gap proximity test.
  function polyMinDist(A, B) {
    if (pointInPoly(A[0], B) || pointInPoly(B[0], A)) return 0;
    var min = Infinity;
    for (var i = 0; i < A.length; i++) {
      var a1 = A[i], a2 = A[(i + 1) % A.length];
      for (var j = 0; j < B.length; j++) {
        var b1 = B[j], b2 = B[(j + 1) % B.length];
        if (segsIntersect(a1, a2, b1, b2)) return 0;
        var d = Math.min(
          segPointDist(a1[0], a1[1], b1[0], b1[1], b2[0], b2[1]),
          segPointDist(a2[0], a2[1], b1[0], b1[1], b2[0], b2[1]),
          segPointDist(b1[0], b1[1], a1[0], a1[1], a2[0], a2[1]),
          segPointDist(b2[0], b2[1], a1[0], a1[1], a2[0], a2[1])
        );
        if (d < min) min = d;
      }
    }
    return min;
  }

  // Combined footprint of a set of parts (all parts by default). This is the stock;
  // its lower-left is the G54 origin. Returns null when empty.
  function combinedBBox(parts) {
    parts = parts || state.parts;
    if (!parts.length) return null;
    var minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    parts.forEach(function (p) {
      var f = footprint(p);
      if (f.minX < minX) minX = f.minX; if (f.minY < minY) minY = f.minY;
      if (f.maxX > maxX) maxX = f.maxX; if (f.maxY > maxY) maxY = f.maxY;
    });
    return { minX: minX, minY: minY, maxX: maxX, maxY: maxY, w: maxX - minX, h: maxY - minY };
  }

  function isSelected(id) { return state.selectedIds.indexOf(id) >= 0; }
  function selectedParts() { return state.parts.filter(function (p) { return isSelected(p.id); }); }

  // Validate the layout: the combined bounding box (the stock) must fit the machine,
  // and no two parts may overlap or sit closer than one kerf. Real-geometry overlap so
  // a part nesting into another's concave region isn't a false positive.
  // Kerf for the layout clearance check: the widest tool the job will actually use.
  // Must match frc_cam_gui_app's `kerf = max(t.diameter for t in job.used_tools)`, or the
  // Layout step passes a nest that Preview then refuses.
  function jobKerf() {
    if (multiToolOn() && window.PCMultiTool) {
      var widest = window.PCMultiTool.widestUsedDiameter();
      if (widest) return widest;
    }
    return state.tool_diameter;
  }

  function fixtureGeometry() {
    if (!state.fixture.on) return { pins: [], bolts: [], errors: [] };
    var f = state.fixture, s = state.stock, errors = [];
    if (!s) return { pins: [], bolts: [], errors: ['Choose a stock size before enabling the locator fixture.'] };
    var right = state.machine.width - f.x - s.width;
    var top = state.machine.height - f.y - s.height;
    if (right < -1e-6 || top < -1e-6) {
      errors.push('The stock at X ' + fmtSize(f.x) + ', Y ' + fmtSize(f.y)
                  + ' does not fit the configured machine bed.');
    }
    // The pin is tangent to the stock; its far edge is one diameter outside the stock
    // corner. Keep another 0.05" of spoilboard between that edge and the bed boundary.
    var pinChannel = f.pin + 0.05;
    if (f.x < pinChannel) errors.push('Stock corner X needs at least ' + fmtSize(pinChannel)
                                      + ' clearance for the left locator pin.');
    if (f.y < pinChannel) errors.push('Stock corner Y needs at least ' + fmtSize(pinChannel)
                                      + ' clearance for the lower locator pins.');
    var pins = [
      // Tangent to the stock edge: these are locators, not merely nearby holes.
      { label: 'P1 lower', x: f.x + s.width * 0.25, y: f.y - f.pin / 2 },
      { label: 'P2 lower', x: f.x + s.width * 0.75, y: f.y - f.pin / 2 },
      { label: 'P3 left', x: f.x - f.pin / 2, y: f.y + s.height * 0.5 },
    ];
    if (s.width * 0.5 < f.pin + 0.05) {
      errors.push('The stock is too narrow to separate the two lower dowel holes.');
    }
    var boltChannel = f.bolt + 0.10, bolts = [];
    // Prefer the far edges so pins establish the datum and the clamps push toward it.
    if (top >= boltChannel) {
      bolts = [{ label: 'B1 upper', x: f.x + s.width * 0.25, y: f.y + s.height + top / 2 },
               { label: 'B2 upper', x: f.x + s.width * 0.75, y: f.y + s.height + top / 2 }];
      if (s.width * 0.5 < f.bolt + 0.05) {
        errors.push('The stock is too narrow to separate the two upper clamp-bolt holes.');
      }
    } else if (right >= boltChannel) {
      bolts = [{ label: 'B1 right', x: f.x + s.width + right / 2, y: f.y + s.height * 0.25 },
               { label: 'B2 right', x: f.x + s.width + right / 2, y: f.y + s.height * 0.75 }];
      if (s.height * 0.5 < f.bolt + 0.05) {
        errors.push('The stock is too short to separate the two right clamp-bolt holes.');
      }
    } else {
      errors.push('Leave at least ' + fmtSize(boltChannel)
                  + ' above or to the right of the stock for external clamp bolts.');
    }
    return { pins: pins, bolts: bolts, errors: errors, right: right, top: top };
  }

  function validateLayout() {
    // Tubing isn't nested on a sheet: the two faces are opposite walls of one tube, so
    // the machine-bounds and part-overlap checks don't apply. The Layout step exists
    // only to orient the tube to the jig axis.
    if (state.mode === 'tubing') return { bad: {}, msgs: [], tooBig: false, bbox: combinedBBox() };
    var msgs = [];
    var bad = {};
    var gap = jobKerf();
    var bbox = combinedBBox();
    var tooBig = false;
    if (bbox && (bbox.w > state.machine.width + 1e-6 || bbox.h > state.machine.height + 1e-6)) {
      tooBig = true;
      // toFixed: the machine size comes from a mm config, so the one message the user is
      // meant to act on read "31.496062992125985" x 19.68503937007874".
      msgs.push('Parts (' + bbox.w.toFixed(2) + '" x ' + bbox.h.toFixed(2) + '") exceed the machine (' +
                state.machine.width.toFixed(2) + '" x ' + state.machine.height.toFixed(2) + '").');
    }
    var items = state.parts.map(function (p) { return { id: p.id, name: p.name, box: footprint(p), poly: placedPolygon(p) }; });
    // A part hanging off the sheet is the mistake a stock list exists to prevent: the
    // machine check above passes happily, and the cut runs off the material.
    if (state.stock) {
      var sheetW = state.stock.width, sheetH = state.stock.height;
      if (sheetW > state.machine.width + 1e-6 || sheetH > state.machine.height + 1e-6) {
        tooBig = true;
        msgs.push('"' + state.stock.name + '" (' + sheetW.toFixed(2) + '" x '
                  + sheetH.toFixed(2) + '") does not fit the machine.');
      }
      // Half a kerf, because the profile pass rides OUTSIDE the outline: a part whose
      // outline is flush with the sheet edge still cuts past it, into the spoilboard or
      // straight through X0 into a soft limit.
      var pad = gap / 2;
      items.forEach(function (item) {
        var b = item.box;
        if (b.minX - pad < -1e-6 || b.minY - pad < -1e-6
            || b.maxX + pad > sheetW + 1e-6 || b.maxY + pad > sheetH + 1e-6) {
          bad[item.id] = true;
          msgs.push(item.name + ' and its cut path hang off "' + state.stock.name + '".');
        }
      });
    }
    if (state.fixture.on) {
      fixtureGeometry().errors.forEach(function (message) { msgs.push('Fixture: ' + message); });
    }
    for (var i = 0; i < items.length; i++) {
      for (var j = i + 1; j < items.length; j++) {
        var a = items[i].box, c = items[j].box;
        var clearX = (a.maxX + gap <= c.minX + 1e-6) || (c.maxX + gap <= a.minX + 1e-6);
        var clearY = (a.maxY + gap <= c.minY + 1e-6) || (c.maxY + gap <= a.minY + 1e-6);
        if (clearX || clearY) continue;
        if (polyMinDist(items[i].poly, items[j].poly) < gap - 1e-6) {
          bad[items[i].id] = true; bad[items[j].id] = true;
          msgs.push(items[i].name + ' and ' + items[j].name + ' overlap or are too close.');
        }
      }
    }
    return { bad: bad, msgs: msgs, tooBig: tooBig, bbox: bbox };
  }

  // Running summary of decisions, revealed progressively: material/thickness/tool from
  // Setup (shown on Parts+), part count (Layout+), stock bbox size (Preview). Lets the
  // user confirm at the end what they chose without navigating back.
  function updateSummary() {
    var box = $('#wiz-summary');
    if (!box) return;
    var list = steps();
    var idx = list.indexOf(state.step);
    if (idx < list.indexOf('parts')) { box.hidden = true; return; }
    box.hidden = false;

    var chips = [];
    if (state.mode === 'tubing') {
      chips.push('Aluminum Tube');
      chips.push(state.thickness_text + ' wall');
      chips.push('tube ' + state.tubeHeight_text + ' tall');
      if (tubePatternOn()) {
        chips.push(state.tubePattern === 'holes' ? 'drilled pattern'
          : state.tubePattern === 'custom'
            ? ('custom design: ' + (tubePatternGeom ? tubePatternGeom.summary
                                                    : 'nothing yet'))
            : 'truss lightening');
      }
    } else {
      var msel = $('#f-material');
      chips.push(msel && msel.options[msel.selectedIndex] ? msel.options[msel.selectedIndex].text : state.material);
      chips.push(state.mode === '2.5d' ? '2.5D · thickness from CAD' : (state.thickness_text + ' thick'));
    }
    if (multiToolOn()) {
      // Tools actually used by an operation - a listed-but-unassigned tool inflates the
      // count and disagrees with the header the operator reads.
      var n = window.PCMultiTool ? window.PCMultiTool.usedToolCount()
                                 : (state.tools || []).length;
      chips.push(n + ' tool' + (n === 1 ? '' : 's'));
    } else {
      // A drilled pattern runs a 0.201" twist drill whatever the tool field says - the
      // server substitutes it. The chip is what an operator reads before loading a tool,
      // so it has to name the real one.
      chips.push(drilledTubePatternOn()
        ? '⌀ 0.201" twist drill'
        : '⌀ ' + state.tool_diameter_text + ' / ' + state.tool_flutes + '-flute tool');
    }
    if (state.chamfer.on && state.mode === '2d') {
      chips.push(state.chamfer.width_text + ' chamfer · '
                 + state.chamfer.angle + '° V-bit');
    }
    if (state.max_pass_depth && state.mode !== 'tubing') {
      chips.push('≤ ' + state.max_pass_depth_text + ' per pass');
    }
    // Always shown, both ways round: this is the number the operator has to match on
    // the machine, so it should never be something you have to remember choosing.
    if (state.dryRun) chips.push('DRY RUN - cuts air');
    if (state.engrave && state.mode === '2d') chips.push('names engraved');
    chips.push(state.mode === 'tubing' ? 'Z0 = tube origin'
               : (state.zDatum === 'stock_top' ? 'Z0 = stock top' : 'Z0 = spoilboard'));
    if (state.mode === 'tubing') {
      if (tubeEndMillingAvailable() && state.squareEnd) chips.push('square near end');
      if (tubeEndMillingAvailable() && state.cutToLength) chips.push('cut far end');
      if (!tubePatternOn()) chips.push(state.parts.length + ' face' + (state.parts.length === 1 ? '' : 's'));
    } else {
      if (idx >= list.indexOf('layout')) {
        chips.push(state.parts.length + ' part' + (state.parts.length === 1 ? '' : 's'));
      }
      if (idx >= list.indexOf('preview')) {
        var st = state.lastResponse && state.lastResponse.stock;
        var w = st ? st.width : null, h = st ? st.height : null;
        if (w == null) { var bb = combinedBBox(); if (bb) { w = bb.w; h = bb.h; } }
        if (w != null) chips.push('stock ' + (+w).toFixed(2) + ' x ' + (+h).toFixed(2) + '"');
      }
    }

    box.innerHTML = '';
    chips.forEach(function (c) {
      var s = document.createElement('span');
      s.className = 'chip';
      s.textContent = c;
      box.appendChild(s);
    });
  }

  /* ------------------------------------------------------------ step nav */
  // Number and show only the steps in the current (mode-dependent) sequence. Numbers
  // reflect sequence position, so they don't renumber as you advance or complete steps.
  function renderStepbar() {
    var list = steps();
    $all('#stepbar li').forEach(function (li) {
      var i = list.indexOf(li.getAttribute('data-step'));
      if (i < 0) { li.hidden = true; }
      else { li.hidden = false; li.setAttribute('data-num', i + 1); }
    });
  }

  function gotoStep(name) {
    state.step = name;
    renderStepbar();
    // Full-page grid mode shows all four steps at once (2x2) and just HIGHLIGHTS
    // the current one; the narrow single-step mode (Onshape panel) hides the rest.
    // Either way the per-step side effects below still fire on transition, so a
    // quadrant stays "incomplete until you get there" (e.g. Preview generates only
    // when you actually reach it).
    var gridMode = $('#wizard').classList.contains('grid');
    var inFlow = steps();
    $all('.step').forEach(function (s) {
      var stepName = s.getAttribute('data-step');
      var isCurrent = stepName === name;
      // Grid mode shows every step at once - but only the ones actually in the flow, so
      // the operations editor stays out of sight while the job uses a single tool.
      if (gridMode) { s.hidden = inFlow.indexOf(stepName) < 0; s.classList.toggle('current', isCurrent); }
      else { s.hidden = !isCurrent; s.classList.remove('current'); }
    });
    var order = steps();
    $all('#stepbar li').forEach(function (li) {
      var s = li.getAttribute('data-step');
      li.classList.toggle('active', s === name);
      li.classList.toggle('done', order.indexOf(s) >= 0 && order.indexOf(s) < order.indexOf(name));
    });
    var idx = order.indexOf(name);
    scrollStepIntoView(name);
    $('#btn-back').disabled = idx === 0;
    var nextBtn = $('#btn-next');
    var isPreview = name === 'preview';
    nextBtn.hidden = isPreview;
    $('#final-action').hidden = !isPreview;
    if (name === 'tools' && window.PCMultiTool) {
      window.PCMultiTool.render();
      // Re-survey on entry: the parts, their rotation, the material and the tool list can
      // all have changed since the last visit, and every one of them moves the answer.
      window.PCMultiTool.refreshFeatures();
    }
    if (name === 'layout') {
      updateLayoutInfo();
      // A generated pattern has nothing cached until the geometry has been asked for -
      // and Layout is reached before anything is generated, which is why this step came
      // up blank.
      if (tubePatternOn() && !tubePatternGeom) refreshTubePatternGeometry();
      // Tubing shares one orientation across both faces; select them together so the
      // rotation handle drives the whole tube at once, and keep them packed tidily.
      if (state.mode === 'tubing') {
        restackTubeParts();
        state.selectedIds = state.parts.map(function (p) { return p.id; });
      }
      updateLayoutHint();
      resetHandleDir();
      refitView();
      drawLayout();
    }
    if (isPreview && multiToolOn()) {
      // The Tools gate only fires when you pass THROUGH that step. In full-page grid
      // mode the dropzone stays live, so a part added afterwards reached Preview with an
      // empty operation list. Re-check on the way in, whatever route got us here.
      var planProblems = window.PCMultiTool.validate();
      if (planProblems.length) {
        $('#gen-status').textContent = '';
        $('#preview-errors').textContent =
          'Finish the operation plan first:\n'
          + planProblems.map(function (m) { return '• ' + m; }).join('\n');
        $('#btn-do').disabled = true;
        $('#final-action').hidden = false;
        updateSummary();
        dbg('step', name);
        return;
      }
    }
    if (isPreview) {
      state.saveAction = preferredAction();
      $('#btn-do').disabled = true;   // enabled once generation finishes
      setupFinalAction();
      resetPreview();
      generate();                     // auto-generate on entry
    }
    // Keep Onshape in continuous face-selection mode only while on the Parts step.
    if (state.source === 'onshape' && window.PenguinCAM.startFaceSelection) {
      if (name === 'parts') {
        var el = $('#select-status'); if (el) el.textContent = 'Select a face in Onshape…';
        window.PenguinCAM.startFaceSelection();
      } else {
        window.PenguinCAM.stopFaceSelection();
      }
    }
    updateSummary();
    dbg('step', name);
  }

  function canLeave(name) {
    if (name === 'parts' && state.parts.length === 0 && !tubePatternOn()) {
      alert('Add at least one part before continuing.');
      return false;
    }
    if (name === 'parts' && partsOverCap()) {
      var cap = partCapForMode();
      alert(state.mode === '2.5d'
        ? '2.5D machines one part per job, and ' + state.parts.length + ' are loaded. '
          + 'Remove ' + partsOverCap() + ' before continuing, or switch to 2D.'
        : 'Tubing machines at most two faces, and ' + state.parts.length + ' are loaded. '
          + 'Remove ' + partsOverCap() + ' before continuing.');
      return false;
    }
    if (name === 'tools' && multiToolOn()) {
      var problems = window.PCMultiTool.validate();
      if (problems.length) {
        alert('Fix the operation plan first:\n' + problems.map(function (m) {
          return '• ' + m;
        }).join('\n'));
        return false;
      }
    }
    if (name === 'layout') {
      var v = validateLayout();
      if (v.msgs.length) {
        alert('Fix the layout first:\n' + v.msgs.join('\n'));
        return false;
      }
    }
    return true;
  }

  /* Bring the step you just moved to into view. Only matters where #wiz-body scrolls
     (the stacked layouts); in the side-by-side grid every panel is already on screen, so
     this is a no-op there. Without it, pressing Next below 900px changed the pills and
     the footer button and left you looking at the panel you started on. */
  function scrollStepIntoView(name) {
    var panel = $('.step[data-step="' + name + '"]');
    if (!panel || panel.hidden) return;
    var body = $('#wiz-body');
    if (!body || body.scrollHeight <= body.clientHeight + 2) return;
    try {
      panel.scrollIntoView({ block: 'start', behavior: 'smooth' });
    } catch (e) {
      panel.scrollIntoView();   // older engines take no options object
    }
  }

  // Jump to a step via the stepbar. Backward is always allowed; forward must clear the
  // same gates as pressing Next through each intervening step.
  function navigateTo(name) {
    var order = steps();
    var target = order.indexOf(name), cur = order.indexOf(state.step);
    if (target < 0) return;
    // Re-clicking the step you are on is normally a no-op - except Preview, where
    // re-entering IS the regenerate action. Changing any setting while standing there
    // retires the program and told you to "press Next", a button that is hidden on
    // that very step; the only way back was Back and then forward again.
    if (target === cur && name !== 'preview') return;
    if (target > cur) {
      for (var i = cur; i < target; i++) { if (!canLeave(order[i])) return; }
    }
    gotoStep(name);
  }

  /* --------------------------------------------------------------- setup */
  var thicknessBound = false;

  function bindSetup() {
    var machineSel = $('#f-machine');
    if (machineSel) {
      state.machine_id = machineSel.value;
      machineSel.addEventListener('change', function () { selectMachine(this.value); });
    }

    $all('input[name="mode"]').forEach(function (r) {
      r.addEventListener('change', function () {
        state.mode = this.value;
        applyModeUI();
        dbg('mode', state.mode);
      });
    });

    var bitSel = $('#f-tool-bit');
    if (bitSel) {
      bitSel.addEventListener('change', function () {
        if (this.value) applyBit(this.value);
        this.value = '';           // a picker, not a setting: the field is the truth
      });
    }

    var engraveBox = $('#f-engrave');
    if (engraveBox) {
      engraveBox.addEventListener('change', function () {
        state.engrave = this.checked;
        updateConditionalSettings();
        updateSummary();
        invalidatePreview();
      });
    }

    var dryBox = $('#f-dry-run');
    if (dryBox) {
      dryBox.addEventListener('change', function () {
        state.dryRun = this.checked;
        updateSummary();
        invalidatePreview();
        if (state.step === 'preview') gotoStep('preview');   // regenerate immediately
      });
    }

    $all('input[name="z_datum"]').forEach(function (r) {
      r.addEventListener('change', function () {
        state.zDatum = this.value;
        updateZDatumUI();
        invalidatePreview();
        dbg('z_datum', state.zDatum);
      });
    });

    bindLengthField($('#f-tool'),
      function () { return state.tool_diameter_text; },
      function (inches, text) {
        state.tool_diameter = inches; state.tool_diameter_text = text;
        refreshLayoutFromTool();   // the kerf readout and the spacing check both move
        invalidatePreview();
      });
    var fluteInput = $('#f-tool-flutes');
    if (fluteInput) {
      var commitFlutes = function () {
        var n = Number(fluteInput.value);
        var ok = Number.isInteger(n) && n >= 1 && n <= 12;
        fluteInput.classList.toggle('invalid', !ok);
        if (ok) {
          state.tool_flutes = n;
          invalidatePreview(); updateSummary();
        }
        return ok;
      };
      fluteInput.addEventListener('input', commitFlutes);
      fluteInput.addEventListener('change', function () {
        if (!commitFlutes()) {
          fluteInput.value = state.tool_flutes;
          fluteInput.classList.remove('invalid');
        }
      });
    }
    bindLengthField($('#f-thickness'),
      function () { return state.thickness_text; },
      function (inches, text) {
        state.thickness = inches; state.thickness_text = text;
        // Once it is theirs, no mode switch may overwrite it in either direction.
        if (thicknessBound) state.thicknessTouched = true;
        updateZDatumUI();   // the stock-top hint quotes the depth it will cut to
        invalidatePreview();
      });
    thicknessBound = true;   // bindLengthField commits the rendered default first
    $('#f-material').addEventListener('change', function () {
      if (state.mode !== 'tubing') state.material = this.value;
      invalidatePreview(); updateSummary();
    });
    bindLengthField($('#f-tube-height'),
      function () { return state.tubeHeight_text; },
      function (inches, text) {
        state.tubeHeight = inches; state.tubeHeight_text = text;
        invalidatePreview(); updateSummary();
      });
    var sizeSel = $('#f-tube-size');
    if (sizeSel) {
      state.tubeSize = sizeSel.value;
      sizeSel.addEventListener('change', function () {
        state.tubeSize = this.value; syncTubeHeightToSize();
        applyTubePatternUI(); invalidatePreview(); updateSummary();
        refreshTubePatternGeometry();
      });
    }
    var patSel = $('#f-tube-pattern');
    if (patSel) {
      patSel.addEventListener('change', function () {
        state.tubePattern = this.value; applyTubePatternUI(); updatePartsModeNote();
        invalidatePreview(); refreshTubePatternGeometry(); updateSummary();
      });
    }
    bindLengthField($('#f-tube-pattern-length'),
      function () { return state.tubePatternLength_text; },
      function (inches, text) {
        state.tubePatternLength = inches; state.tubePatternLength_text = text;
        applyTubePatternUI(); invalidatePreview(); refreshTubePatternGeometry();
      });
    $('#f-square-end').addEventListener('change', function () {
      state.squareEnd = this.checked;
      invalidatePreview(); updateSummary();
    });
    $('#f-cut-to-length').addEventListener('change', function () {
      state.cutToLength = this.checked;
      invalidatePreview(); updateSummary();
    });
    var maxPass = $('#f-max-pass');
    if (maxPass) {
      // Optional field: blank is a valid state (automatic), so bindLengthField's
      // revert-on-blur behavior doesn't fit here.
      var commitMaxPass = function (el) {
        var raw = el.value.trim();
        if (!raw) {
          state.max_pass_depth = null; state.max_pass_depth_text = '';
          el.classList.remove('invalid'); invalidatePreview(); return true;
        }
        var inches = parseLength(/^[+-]?[0-9.\/\s]+$/.test(raw) ? raw + '"' : raw);
        el.classList.toggle('invalid', !inches);
        if (inches) {
          state.max_pass_depth = inches; state.max_pass_depth_text = raw;
          invalidatePreview();
        }
        return !!inches;
      };
      maxPass.addEventListener('input', function () { commitMaxPass(this); });
      maxPass.addEventListener('change', function () {
        if (!commitMaxPass(this)) { this.value = state.max_pass_depth_text; this.classList.remove('invalid'); }
      });
    }
    var chamferBox = $('#f-chamfer');
    if (chamferBox) {
      chamferBox.addEventListener('change', function () {
        state.chamfer.on = this.checked;
        $('#chamfer-options').hidden = !this.checked;
        // In multi-tool mode the checkbox drives the operations editor: it adds a
        // V-bit and a chamfer operation per part (or removes the ones it added).
        if (multiToolOn() && window.PCMultiTool) {
          if (this.checked) window.PCMultiTool.applyDeburr(state.chamfer);
          else window.PCMultiTool.clearDeburr();
        }
        invalidatePreview(); updateSummary();
      });
      // Every deburr setting has to reach the operations editor, not just the
      // checkbox that turns it on. Without this the plan kept whatever the V-bit and
      // width were when the box was first ticked, so Setup could read 1/4" 60 deg while
      // the program was cut with 1/2" 90 deg.
      function syncDeburrToPlan() {
        if (state.chamfer.on && multiToolOn() && window.PCMultiTool) {
          window.PCMultiTool.applyDeburr(state.chamfer);
        }
      }
      bindLengthField($('#f-chamfer-bit'),
        function () { return state.chamfer.bit_text; },
        function (inches, text) {
          state.chamfer.bit = inches; state.chamfer.bit_text = text;
          syncDeburrToPlan();
          invalidatePreview();
        });
      bindLengthField($('#f-chamfer-width'),
        function () { return state.chamfer.width_text; },
        function (inches, text) {
          state.chamfer.width = inches; state.chamfer.width_text = text;
          syncDeburrToPlan();
          invalidatePreview(); updateSummary();
        });
      $('#f-chamfer-angle').addEventListener('change', function () {
        var v = parseFloat(this.value);
        if (isFinite(v) && v > 0 && v < 180) state.chamfer.angle = v;
        else this.value = state.chamfer.angle;   // reject rather than send a bad angle
        syncDeburrToPlan();
        invalidatePreview();
      });
      ['perimeter', 'holes', 'pockets'].forEach(function (t) {
        $('#f-chamfer-' + t).addEventListener('change', function () {
          state.chamfer[t] = this.checked;
          syncDeburrToPlan();
          invalidatePreview();
        });
      });
    }
    var mt = $('#f-multitool');
    if (mt && !window.PCMultiTool) {
      // The editor lives in its own file. If it failed to load, a checkbox that silently
      // does nothing is worse than no checkbox: say so and disable it.
      mt.disabled = true;
      mt.checked = false;
      var wrap = $('#multitool-toggle');
      if (wrap) {
        wrap.title = 'The multi-tool editor failed to load. Reload the page to retry.';
        wrap.style.opacity = '0.5';
      }
    } else if (mt) {
      mt.addEventListener('change', function () {
        state.multitool = this.checked;
        applyMultiToolUI();
        dbg('multitool', state.multitool);
      });
    }
    applyModeUI();
  }

  // Switch the active machine: pull its bed size, name, tool default, and material list
  // from the per-machine data the server embedded (CFG.machines) so the Layout bounds,
  // tool default, and Material dropdown reflect the SELECTED machine, not the default.
  // Also persists the choice to the session so a reload keeps it.
  function selectMachine(mid) {
    var oldInfo = (CFG.machines || {})[state.machine_id];
    state.machine_id = mid;
    var info = (CFG.machines || {})[mid];
    if (info) {
      state.machine = { width: info.x_max || state.machine.width, height: info.y_max || state.machine.height, name: info.name || mid };
      rebuildMaterialOptions(info.materials);
      // Follow the new machine's default tool ONLY if the field still holds the previous
      // machine's default (i.e. the user hasn't typed a custom value) — never clobber input.
      var toolInput = $('#f-tool');
      if (toolInput && info.tool_text && oldInfo && state.tool_diameter_text === oldInfo.tool_text) {
        toolInput.value = info.tool_text;
        state.tool_diameter_text = info.tool_text;
        state.tool_diameter = info.tool || parseLength(info.tool_text) || state.tool_diameter;
      }
      if (state.step === 'layout') { updateLayoutInfo(); refitView(); drawLayout(); }
      updateFixtureUI();
      updateSummary();
    }
    // Persist to the session (fire-and-forget) so a page/iframe reload keeps this machine.
    fetch('/set-machine', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ machine_id: mid }),
    }).catch(function () {});
    dbg('machine', mid);
  }

  // Rebuild the Material <select> for the selected machine, keeping the current material
  // if it still exists (else preferring plywood, then the first available).
  function rebuildMaterialOptions(materials) {
    var sel = $('#f-material');
    if (!sel || !materials || !materials.length) return;
    var prev = state.material;
    sel.innerHTML = '';
    var ids = [];
    materials.forEach(function (m) {
      var opt = document.createElement('option');
      opt.value = m.id; opt.textContent = m.name;
      sel.appendChild(opt);
      ids.push(m.id);
    });
    var fallback = CFG.defaultMaterial || 'aluminum';
    var pick = ids.indexOf(prev) >= 0 ? prev
               : (ids.indexOf(fallback) >= 0 ? fallback : ids[0]);
    sel.value = pick;
    if (state.mode !== 'tubing') state.material = pick;
  }

  // Reshape the Setup form for the selected mode. Tubing forces the aluminum-tube
  // material (hiding the selector), relabels thickness as wall thickness, and reveals
  // the tube-only fields; 2.5D hides thickness (derived from CAD).
  /* True when the tube pattern is generated rather than drawn, which is the one flow in
     the wizard that needs no part file at all. */
  function tubePatternOn() {
    return state.mode === 'tubing' && state.tubePattern !== 'none';
  }

  /* True for the one generated pattern the USER authors: the custom designer. It is a
     tube pattern in every other respect - no DXF, a stated length, the same preview -
     so it goes through tubePatternOn() everywhere except where the editor is involved. */
  function tubeDesignOn() {
    return state.mode === 'tubing' && state.tubePattern === 'custom';
  }

  /* Mirror the pattern controls, and say up front how many holes the tube will get -
     the count follows from the length, and an operator who typed the wrong length would
     otherwise not find out until they read the program. */
  /* A generated program belongs to the settings that produced it. In full-page grid
     mode the Setup panel stays live while Preview is on screen, so ANY input that feeds
     the generator can be changed while a finished program sits there with Download still
     enabled - the panel reading Aluminium / 0.5" / 1/4" beside a button handing over the
     plywood 4mm program. Anything that changes what would be generated must retire what
     already was. Wired to every such input, not just the tube ones: material, thickness,
     tool, tab spacing, the mode-specific tube fields, and adding or removing a part. */
  function invalidatePreview() {
    if (state.step !== 'preview' && $('#preview-result').hidden) return;
    resetPreview();
    $('#btn-do').disabled = true;
    $('#gen-status').textContent = '';
    $('#preview-errors').textContent =
      'Settings changed. Click the Preview step above to generate the program again.';
  }

  /* The pattern the server would generate, fetched so Layout can draw the tube before
     anything has been made and so the count comes from the generator instead of a copy
     of its constants living over here. */
  var tubePatternGeom = null;
  var tubePatternToken = 0;

  /* One place that takes a resolved pattern - generated or custom - and makes the whole
     UI agree with it. Shared so the custom path cannot drift into updating one panel and
     not another. */
  function adoptTubeGeometry(g) {
    tubePatternGeom = g;
    applyTubePatternUI();
    updateLayoutInfo();
    refitView();
    drawLayout();
  }

  function refreshTubePatternGeometry() {
    // A custom design is resolved by POSTing the design itself; the editor owns that
    // request (and its debounce) and hands the answer back through adoptTubeGeometry.
    if (tubeDesignOn()) {
      if (window.PCTubeDesigner) window.PCTubeDesigner.refresh();
      else adoptTubeGeometry(null);
      return;
    }
    if (!tubePatternOn() || !(state.tubePatternLength > 0)) {
      tubePatternGeom = null;
      drawLayout();
      return;
    }
    var token = ++tubePatternToken;
    var q = '?size=' + encodeURIComponent(state.tubeSize)
          + '&length=' + state.tubePatternLength
          + '&mode=' + encodeURIComponent(state.tubePattern)
          + '&tool=' + state.tool_diameter;
    fetch('/api/tube-pattern' + q, { credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        if (token !== tubePatternToken) return;   // a later edit already superseded this
        adoptTubeGeometry(j && !j.error ? j : null);
      })
      .catch(function () { /* the note falls back to its own arithmetic */ });
  }

  /* One visibility table for controls whose values only apply in certain workflows.
     Keeping these rules together prevents a hidden setting from continuing to affect
     output, and prevents separate event handlers from disagreeing about what is shown. */
  function updateConditionalSettings() {
    var is2d = state.mode === '2d';
    var is25 = state.mode === '2.5d';
    var isTube = state.mode === 'tubing';
    var drilled = drilledTubePatternOn();
    var on = multiToolOn();

    var guidance = $('#mode-guidance');
    if (guidance) {
      guidance.textContent = isTube
        ? 'Tube workflow: choose the real tube orientation and wall thickness. Sheet nesting, fixtures, Z datum, engraving and multi-tool operations do not apply.'
        : is25
          ? '2.5D workflow: depth comes from the CAD layers. The part is placed at the program origin; sheet nesting, fixtures, engraving and multi-tool operations do not apply.'
          : '2D workflow: choose the cutter, material and thickness, then add and nest as many flat parts as fit on the stock.';
    }

    var el = $('#thickness-field'); if (el) el.hidden = is25;
    el = $('#thickness-derived'); if (el) el.hidden = !is25;
    el = $('#material-field'); if (el) el.hidden = isTube;
    el = $('#tube-fields'); if (el) el.hidden = !isTube;
    el = $('#chamfer-fields'); if (el) el.hidden = !is2d;
    el = $('#max-pass-field'); if (el) el.hidden = isTube;
    el = $('#max-pass-hint'); if (el) el.hidden = isTube;

    // A sheet and its locator fixture feed only /process-job, the flat 2D path. The
    // 2.5D single-part route normalizes its own origin and tube jobs use their jig.
    el = $('#stock-row'); if (el) el.hidden = !is2d;
    el = $('#fixture-panel'); if (el) el.hidden = !is2d;
    el = $('#btn-arrange'); if (el) el.hidden = !is2d;
    el = $('#btn-fill'); if (el) el.hidden = !is2d;

    el = $('#engrave-toggle'); if (el) el.hidden = !is2d;
    el = $('#engrave-note'); if (el) el.hidden = !engraveOn();
    el = $('#multitool-toggle'); if (el) el.hidden = !is2d;
    el = $('#multitool-note'); if (el) el.hidden = !on;

    // The generated drilled pattern owns its tool. Do not show editable end-mill fields
    // whose values will be ignored, or end-facing choices the drill cannot perform.
    var hideSingleTool = on || drilled;
    ['#tool-field', '#tool-flutes-field', '#tool-bit-field'].forEach(function (id) {
      var field = $(id);
      if (field) field.hidden = hideSingleTool
        || (id === '#tool-bit-field' && !Object.keys(toolLibrary()).length);
    });
    el = $('#tube-tool-notice');
    if (el) {
      el.hidden = !drilled;
      el.textContent = drilled
        ? 'Tool is fixed for this pattern: load a 0.201" #10-clearance twist drill. Cutter diameter, flute count and saved end mills are not used.'
        : '';
    }
    el = $('#tube-end-fields'); if (el) el.hidden = drilled;
    el = $('#tube-end-note'); if (el) el.hidden = !drilled;
  }

  function applyTubePatternUI() {
    var box = $('#tube-pattern-fields');
    if (box) box.hidden = !tubePatternOn();
    if (window.PCTubeDesigner) window.PCTubeDesigner.render();
    updateConditionalSettings();
    updateLayoutInfo();
    var note = $('#tube-pattern-note');
    if (!note) return;
    if (!tubePatternOn()) { note.textContent = ''; return; }
    var len = state.tubePatternLength;
    if (!(len > 0)) { note.textContent = 'Enter the tube length to see what will be cut.'; return; }
    if (state.tubePattern === 'custom') {
      // The count comes from the server or not at all: this panel has no idea what a
      // named size is, and inventing an answer here is exactly how the fixed-pattern
      // note drifted from the program it was describing.
      note.textContent = tubePatternGeom
        ? 'Custom design: ' + tubePatternGeom.summary + ' per face. Place them on the '
          + 'tube in the Layout panel.'
        : 'Place features on the tube in the Layout panel.';
      return;
    }
    if (tubePatternGeom && tubePatternGeom.length === state.tubePatternLength) {
      // Counts straight from the generator, so the note cannot drift from the program.
      var gh = tubePatternGeom.holes.length, gp = tubePatternGeom.pockets.length;
      if (!gh && !gp) {
        note.textContent = (tubePatternGeom.warnings || []).join(' ')
          || 'This tube pattern would cut nothing.';
        return;
      }
      note.textContent = gh
        ? gh + ' holes per face, drilled with a 0.201" twist drill - load the drill, '
              + 'not an end mill.'
        : gp + ' truss pockets per face, milled with your end mill. No holes.';
      return;
    }
    var SPACING = 0.5, END_MARGIN = 0.375, CELL = 2.0;
    var wide = state.tubeSize === '2x1-flat';
    if (state.tubePattern === 'holes') {
      var usable = len - 2 * END_MARGIN;
      if (usable < 0) {
        note.textContent = 'Too short to hole at least ' + END_MARGIN + '" from both ends.';
        return;
      }
      var cols = Math.floor(usable / SPACING) + 1;
      var perCol = wide ? 3 : 1;
      note.textContent = cols + ' columns of ' + perCol + ', ' + (cols * perCol)
        + ' holes per face. Drilled with a 0.201" twist drill - load the drill, not an end mill.';
    } else {
      var run = len - 2 * END_MARGIN;
      var cells = Math.floor(run / CELL);
      if (cells < 1) { note.textContent = 'Too short for a lightening triangle.'; return; }
      note.textContent = cells + ' triangles per face, '
        + (wide ? '1.5' : '0.5') + '" x 1.875" each. Milled with your end mill. No holes.';
    }
  }

  //: The wall thickness a tube job starts from. Real 1x1/2x1 is 1/16"-1/8".
  var TUBE_WALL_DEFAULT_TEXT = '0.0625"';

  //: How tall each tube size is. The server derives the height from the SIZE - a 2x1 on
  //: its 1" face is a 2" TALL tube - so leaving this field showing 1.0" had the panel and
  //: the program describing different parts, and the height sets the safe-Z retract.
  var TUBE_HEIGHTS = { '1x1': 1.0, '2x1-flat': 1.0, '2x1-standing': 2.0 };

  function syncTubeHeightToSize() {
    var h = TUBE_HEIGHTS[state.tubeSize];
    if (!h) return;
    state.tubeHeight = h;
    state.tubeHeight_text = h + '"';
    var field = $('#f-tube-height');
    if (field) field.value = state.tubeHeight_text;
  }

  function setThicknessField(inches, text) {
    state.thickness = inches;
    state.thickness_text = text;
    var field = $('#f-thickness');
    if (field) field.value = text;
  }

  function applyModeUI() {
    var is25 = state.mode === '2.5d';
    var isTube = state.mode === 'tubing';
    updateZDatumUI();   // tubing has a jig zero of its own; the choice is hidden there
    var tl = $('#thickness-label'); if (tl) tl.textContent = isTube ? 'Tube wall thickness' : 'Material thickness';
    if (isTube) {
      state.material = 'aluminum_tube';
    } else {
      var msel = $('#f-material'); if (msel) state.material = msel.value;
    }
    // A tube is held in a jig, not nested on a sheet; 2.5D is normalized to its program
    // origin and likewise does not consume sheet placement. Leaving these active let a
    // sheet be chosen for a tube - and picking one rewrote the "thickness" field, which
    // in tubing is the WALL thickness, from 0.0625" to the sheet's 0.25". Four times
    // the wall, feeding depth per pass and pecking, plus a plywood material that
    // survived the trip back to 2D.
    if ((isTube || is25) && state.stock) {
      // Chosen in another mode and still selected: drop it rather than carry a sheet
      // into a program that has no sheet or placement fields.
      state.stock = null;
      var ss = $('#f-stock'); if (ss) ss.value = '';
      applyStockUI();
    }
    if (isTube) syncTubeHeightToSize();
    // The thickness field carries the sheet default (1/4") into tubing, where it means
    // WALL thickness. Real 1x1 and 2x1 is 1/16"-1/8", so every tube job quoted a cycle
    // time three to four times too long and pecked four times as often as it needed to.
    // Only the untouched default is replaced - a value the user typed is theirs.
    // Swapped BOTH ways, and only while the user has not typed their own value. Applying
    // the tube default without ever undoing it left a 1/4" plate set to 1/16" after a
    // trip through Tubing and back - the program then cut 1/16" deep and never freed the
    // part, with nothing on screen to say so.
    if (isTube && !state.thicknessTouched && state.thickness_text !== TUBE_WALL_DEFAULT_TEXT) {
      state.thicknessBeforeTube = state.thickness_text;
      setThicknessField(0.0625, TUBE_WALL_DEFAULT_TEXT);
    } else if (!isTube && !state.thicknessTouched && state.thicknessBeforeTube) {
      var back = state.thicknessBeforeTube;
      state.thicknessBeforeTube = null;
      setThicknessField(parseLength(back) || 0.25, back);
    }
    applyTubePatternUI();
    applyMultiToolUI();
    updatePartsModeNote();
    // In full-page grid mode the Layout canvas and the operations editor are on screen
    // whatever step is current, and gotoStep refreshes only the step you are standing
    // on. Switching mode from Setup therefore left the previous mode's drawing on the
    // canvas (the tube, after a trip through Tubing) and an operation list missing any
    // part added while the editor was mode-disabled - which made its "no operations"
    // block at Preview look wrong instead of actionable. gotoStep already refreshed
    // whichever of these IS the current step, so only the other ones need doing here.
    if (state.step !== 'layout') { refitView(); drawLayout(); }
    if (multiToolOn() && state.step !== 'tools') {
      window.PCMultiTool.render();
      window.PCMultiTool.refreshFeatures();
    }
  }

  // Reflect the multi-tool toggle everywhere it shows: the single-tool field it replaces,
  // the extra grid column, the step bar, and the explanatory note.
  function applyMultiToolUI() {
    var on = multiToolOn();
    updateConditionalSettings();
    // The deburr checkbox stays in both flavors of 2D; entering multi-tool mode with it
    // already checked materializes the V-bit + chamfer ops in the editor.
    if (on && state.chamfer.on && window.PCMultiTool) window.PCMultiTool.applyDeburr(state.chamfer);
    $('#wizard').classList.toggle('has-tools', on);
    // The toggle changes which steps exist, and gotoStep is the ONLY thing that decides
    // which sections are visible. Without re-running it, ticking the box applied the
    // 5-quadrant grid while the new step's panel stayed hidden - the widest column on
    // screen just went blank - and unticking left an orphaned panel with no grid
    // placement, collapsing the four real quadrants into strips.
    //
    // In full-page grid mode every step is on screen at once, so the toggle can also be
    // switched off while standing ON the step it removes; land somewhere real then.
    gotoStep(steps().indexOf(state.step) < 0 ? 'parts' : state.step);
    // gotoStep only renders the operations editor when you NAVIGATE to it, and in grid
    // mode the panel is on screen the moment the box is ticked. Without this it sat
    // there reading "Add a part on the Parts step first" beside a Parts panel listing
    // the part, with nothing to say it was simply out of date.
    if (on && window.PCMultiTool) {
      window.PCMultiTool.render();
      window.PCMultiTool.refreshFeatures();
    }
  }

  function updatePartsModeNote() {
    var note = $('#parts-mode-note');
    if (!note) return;
    if (state.mode === '2.5d') {
      note.textContent = '2.5D mode: one part per job (thickness comes from the CAD layers).';
    } else if (tubeDesignOn()) {
      note.textContent = 'Place features on the tube in the Layout panel: choose what to '
        + 'place, then click the tube. Both walls get the design (face 2 is mirrored).';
    } else if (tubePatternOn()) {
      // Silence here meant a student uploaded a DXF, saw it accepted and listed, and got
      // a program generated from the tube length that ignored it completely.
      note.textContent = 'Nothing to add: the pattern is generated from the tube length. '
        + 'Any DXF dropped here is ignored - go straight to Layout.';
    } else if (state.mode === 'tubing') {
      note.textContent = 'Tubing: add 1 face (mirrored onto the opposite side) or 2 faces (a distinct pattern per side).';
    } else {
      note.textContent = 'Add as many parts as fit on the sheet.';
    }
    // A custom design has nowhere to put a DXF: the geometry is the design. Leaving the
    // dropzone live invited a student to add one and watch it be ignored.
    if (state.source === 'upload') {
      var dz = $('#upload-source');
      if (dz) dz.hidden = tubeDesignOn();
    }
    if (partsOverCap()) {
      note.textContent += '  ** ' + state.parts.length + ' loaded but only '
        + partCapForMode() + ' will be machined - remove ' + partsOverCap() + '. **';
    }
  }

  /* The saved-bits picker beside the tool fields. Choosing a bit fills both diameter
     and flute count; losing the latter made a physical 4-flute aluminum cutter run a
     program calculated as if it had one flute. Team bits are listed first. */
  function renderBitPicker() {
    var sel = $('#f-tool-bit'), field = $('#tool-bit-field');
    if (!sel || !field) return;
    var lib = toolLibrary();
    var ids = Object.keys(lib);
    // Empty libraries, multi-tool plans and fixed-drill patterns each own their own tool
    // choice, so the saved single-tool picker has nothing truthful to show there.
    field.hidden = !ids.length || multiToolOn() || drilledTubePatternOn();
    if (!ids.length) return;
    var team = ids.filter(function (id) { return lib[id].source === 'team'; });
    var builtin = ids.filter(function (id) { return lib[id].source !== 'team'; });
    sel.innerHTML = '';
    sel.appendChild(new Option('Pick a bit\u2026', ''));
    [[team, 'Saved by your team'], [builtin, 'Built in']].forEach(function (pair) {
      if (!pair[0].length) return;
      var group = document.createElement('optgroup');
      group.label = pair[1];
      sortedBitIds(lib, pair[0]).forEach(function (id) {
        var bit = lib[id];
        var size = bit.diameter_text || (Math.round(bit.diameter * 10000) / 10000) + '"';
        group.appendChild(new Option(bit.name + '  \u00b7  ' + size, id));
      });
      sel.appendChild(group);
    });
    sel.value = '';
  }

  function applyBit(id) {
    var bit = toolLibrary()[id];
    var field = $('#f-tool');
    if (!bit || !field) return;
    field.value = bit.diameter_text || String(bit.diameter);
    // Commit through the field's own handler so the value is parsed, validated and
    // shown exactly as a typed one would be.
    field.dispatchEvent(new Event('change', { bubbles: true }));
    var flutes = $('#f-tool-flutes');
    if (flutes && bit.flutes) {
      flutes.value = bit.flutes;
      flutes.dispatchEvent(new Event('change', { bubbles: true }));
    }
  }

  /* Says what the operator will have to do at the machine, in the order they do it.
     Tubing has a jig zero of its own, so the choice does not apply there and the panel
     says so rather than showing a control that would be ignored. */
  /* Anything that changes the kerf has to reach the Layout panel, which in grid mode
     is on screen the whole time. It used to keep showing the old tool until you left
     the step and came back - the validation was right, the readout was not. */
  function refreshLayoutFromTool() {
    updateLayoutInfo();
    drawLayout();
  }

  function updateZDatumUI() {
    var field = $('#zzero-field'), hint = $('#zzero-hint');
    if (!field || !hint) return;
    var isTube = state.mode === 'tubing';
    var top = state.zDatum === 'stock_top';
    field.hidden = isTube;
    hint.textContent = isTube
      ? 'Tube jobs are zeroed to the tube in its jig, so this does not apply.'
      : (top
         ? 'Touch off the top face of the material. Cutting Z is negative throughout, '
           + 'down to about Z-' + (state.thickness + 0.02).toFixed(3) + '" to cut through.'
         : 'Touch off the spoilboard, through the stock. The same zero works whatever '
           + 'the material measures.');
    // The 3D preview names the origin too, and it is the same origin.
    var originNote = $('#viewer-origin-note');
    if (originNote) {
      originNote.textContent = (isTube
        ? 'Origin (0,0,0) is the tube origin in the jig, at the bottom-left of the face'
        : 'Origin (0,0,0) is the lower-left of the stock, '
          + (top ? 'at its top face' : 'on top of the sacrifice board'))
        + '. Drag to orbit, scroll to zoom.';
    }
  }

  /* ------------------------------------------------------- stock and nesting */

  /* The sheets the shop has, as the server assembled them. Kept on CFG like the bit
     library so one save refreshes every list that shows them. */
  function stockList() { return CFG.savedStock || []; }
  function setStockList(list) {
    CFG.savedStock = list || [];
    var chosen = state.stock && state.stock.id;
    renderStockPicker();
    if (chosen && !stockList().some(function (s) { return s.id === chosen; })) {
      state.stock = null;          // the sheet we were using was deleted
      applyStockUI();
    }
  }

  function renderStockPicker() {
    var sel = $('#f-stock');
    if (!sel) return;
    var sheets = stockList();
    sel.innerHTML = '';
    sel.appendChild(new Option('Just the parts (no defined sheet)', ''));
    [[false, 'Sheets'], [true, 'Offcuts']].forEach(function (pair) {
      var group = sheets.filter(function (s) { return !!s.remnant === pair[0]; });
      if (!group.length) return;
      var og = document.createElement('optgroup');
      og.label = pair[1];
      group.forEach(function (sheet) {
        og.appendChild(new Option(
          sheet.name + '  ·  ' + fmtSize(sheet.width) + ' x ' + fmtSize(sheet.height),
          sheet.id));
      });
      sel.appendChild(og);
    });
    sel.value = (state.stock && state.stock.id) || '';
  }

  /* Save a sheet size to the team config, or save what is left of the current one as
     an offcut. The second is the one that pays: an offcut nobody wrote down is an
     offcut nobody uses, and it ends up in the bin while someone opens a fresh sheet. */
  function postStock(entry, done) {
    fetch('/stock/save', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ stock: entry }),
    }).then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) {
        if (res.j && res.j.stock) setStockList(res.j.stock);
        if (!res.ok) alert(res.j.error || 'Could not save that stock.');
        else if (done) done(res.j);
      })
      .catch(function (e) { alert('Could not reach the server to save that stock: ' + e); });
  }

  function promptStockLength(label, suggested) {
    var value = prompt(label + ' (for example 48", 1200mm, or 4ft):', suggested || '');
    if (value === null) return null;
    value = value.trim();
    if (!parseLength(value)) {
      alert(label + ' is not a valid positive size. Try 48", 1200mm, or 4ft.');
      return false;
    }
    return value;
  }

  function saveCurrentSheet() {
    var bb = combinedBBox();
    // Offer useful defaults, but ask for the actual material dimensions. Deriving the
    // stock from the parts forced a user with a 48 x 96 sheet to edit YAML just to tell
    // the layout what was on the table. The nest remains a fallback suggestion when one
    // exists; with no parts yet, the machine envelope is a better starting point.
    var pad = state.stock ? 0 : jobKerf();
    var w = state.stock ? state.stock.width : (bb ? bb.w + 2 * pad : state.machine.width);
    var h = state.stock ? state.stock.height : (bb ? bb.h + 2 * pad : state.machine.height);
    var name = prompt('Name this stock (it goes in the team config):',
                      state.stock ? state.stock.name
                                  : fmtSize(w) + ' x ' + fmtSize(h) + ' ' + state.material);
    if (!name) return;
    var widthText = promptStockLength('Stock width', fmtSize(w));
    if (widthText === null || widthText === false) return;
    var heightText = promptStockLength('Stock length', fmtSize(h));
    if (heightText === null || heightText === false) return;
    postStock({ name: name, width: widthText, height: heightText,
                thickness: state.thickness_text, material: state.material }, function (result) {
      var saved = stockList().filter(function (sheet) { return sheet.id === result.saved_id; })[0];
      if (!saved) return;
      state.stock = saved;
      var picker = $('#f-stock'); if (picker) picker.value = saved.id;
      applyStockUI();
      invalidatePreview();
    });
  }

  /* The unused part of the sheet, as the largest rectangle left over to the right of
     or above everything placed. Approximate on purpose: an offcut is a thing you put
     back in the rack, and a number you can trust to be NO BIGGER than what is really
     there is worth more than an exact irregular polygon. */
  function saveRemnant() {
    if (!state.stock) { alert('Pick the sheet you are cutting from first.'); return; }
    var used = combinedBBox();
    if (!used) { alert('Nothing has been cut from this sheet yet.'); return; }
    if (validateLayout().msgs.length) {
      alert('Fix the layout first - what is left of a sheet cannot be measured from a '
            + 'nest that does not fit on it.');
      return;
    }
    var gap = jobKerf();
    // Clamped to the sheet. A part dragged off the left edge sends maxX negative, and
    // an unclamped subtraction then offered an offcut WIDER than the whole sheet - the
    // exact fiction the offcut list exists to keep out of the rack.
    var usedX = Math.min(Math.max(used.maxX, 0), state.stock.width);
    var usedY = Math.min(Math.max(used.maxY, 0), state.stock.height);
    var right = { w: state.stock.width - usedX - gap, h: state.stock.height };
    var above = { w: state.stock.width, h: state.stock.height - usedY - gap };
    var pick = (right.w * right.h >= above.w * above.h) ? right : above;
    if (!(pick.w > 0.5 && pick.h > 0.5)) {
      alert('What is left of this sheet is too small to be worth racking.');
      return;
    }
    var name = prompt('Name this offcut:',
                      fmtSize(pick.w) + ' x ' + fmtSize(pick.h) + ' ' + state.material + ' offcut');
    if (!name) return;
    postStock({ name: name, width: pick.w, height: pick.h, remnant: true,
                thickness: state.thickness_text, material: state.material });
  }

  function fmtSize(inches) {
    return (Math.round(inches * 100) / 100) + '"';
  }

  // A repeatable physical placement that needs only a tape measure. This is guidance,
  // not a coordinate transform: the program remains stock-relative and the operator
  // sets G54 at the sheet corner after placing it. Centering leaves useful clamp space
  // on both sides and makes the suggestion work for any stock that fits the bed.
  function centeredStockPlacement() {
    if (!state.stock) return null;
    var x = (state.machine.width - state.stock.width) / 2;
    var y = (state.machine.height - state.stock.height) / 2;
    if (x < -1e-6 || y < -1e-6) return null;
    return { x: Math.max(0, x), y: Math.max(0, y) };
  }

  function fixturePointText(points) {
    return points.map(function (p) {
      return p.label + ' X ' + fmtFixtureSize(p.x) + ', Y ' + fmtFixtureSize(p.y);
    }).join('; ');
  }

  function fmtFixtureSize(inches) { return (+inches).toFixed(3) + '"'; }

  function updateFixtureUI() {
    var toggle = $('#f-fixture'), options = $('#fixture-options'), note = $('#fixture-note');
    if (!toggle || !options) return;
    toggle.disabled = !state.stock;
    if (!state.stock && state.fixture.on) state.fixture.on = false;
    toggle.checked = state.fixture.on;
    options.hidden = !state.fixture.on;
    if (!state.fixture.on || !note) { if (note) note.textContent = ''; return; }
    var geometry = fixtureGeometry();
    var bits = [];
    if (geometry.pins.length) bits.push('Dowel holes: ' + fixturePointText(geometry.pins) + '.');
    if (geometry.bolts.length) bits.push('Clamp-bolt holes: ' + fixturePointText(geometry.bolts) + '.');
    if (geometry.errors.length) bits.push('Fix before cutting: ' + geometry.errors.join(' '));
    else bits.push('Coordinates are measured from the machine-bed lower-left. Remove the dowels after clamping. Clamp bodies are not modeled; keep them outside every toolpath.');
    note.textContent = bits.join(' ');
  }

  /* What the sheet choice changes: the material and thickness follow it when the sheet
     records them (a 1/4" plywood offcut is 1/4" plywood), the canvas reframes, and the
     usage readout appears. */
  function applyStockUI() {
    var note = $('#stock-note'), usage = $('#dro-usage');
    var sheet = state.stock;
    if (usage) usage.hidden = !sheet;
    var remnant = $('#btn-save-remnant'); if (remnant) remnant.hidden = !sheet;
    if (note) {
      var placement = centeredStockPlacement();
      note.textContent = sheet
        ? (state.fixture.on
           ? ('Cutting from "' + sheet.name + '" in the locator fixture. Stock lower-left: X '
              + fmtFixtureSize(state.fixture.x) + ', Y ' + fmtFixtureSize(state.fixture.y)
              + ' from the machine-bed lower-left. Set G54 X/Y at the stock corner.')
           : placement
           ? ('Cutting from "' + sheet.name + '". To center it on the ' + state.machine.name
              + ' bed, place its lower-left corner ' + fmtSize(placement.x)
              + ' from the bed’s left edge and ' + fmtSize(placement.y)
              + ' from its lower edge. Set G54 X/Y at that sheet corner.')
           : ('"' + sheet.name + '" is larger than the configured ' + state.machine.name
              + ' bed and cannot be positioned on it.'))
        : '';
    }
    if (sheet) {
      if (sheet.thickness && !state.thicknessTouched) {
        state.thickness = sheet.thickness;
        state.thickness_text = sheet.thickness_text || (sheet.thickness + '"');
        var tf = $('#f-thickness'); if (tf) tf.value = state.thickness_text;
      }
      if (sheet.material) {
        var msel = $('#f-material');
        if (msel && Array.prototype.some.call(msel.options,
              function (o) { return o.value === sheet.material; })) {
          msel.value = sheet.material;
          if (state.mode !== 'tubing') state.material = sheet.material;
        }
      }
    }
    updateFixtureUI();
    updateUsage();
    updateSummary();
    refitView();
    drawLayout();
  }

  /* How much of the sheet the parts occupy, measured as the sum of their footprints -
     the area each part denies to its neighbours, which is the number that decides
     whether another one will fit. It is not the parts' true area: a part nested into
     another's concave corner is counted twice over that overlap. */
  function updateUsage() {
    var el = $('#info-usage');
    if (!el || !state.stock) return;
    var sheetArea = state.stock.width * state.stock.height;
    var used = 0;
    state.parts.forEach(function (p) {
      var s = placedShape(p);
      used += s.w * s.h;      // footprint: what the part denies to its neighbours
    });
    var pct = sheetArea > 0 ? (used / sheetArea) * 100 : 0;
    // Over 100% is not a rounding artefact, it is parts that are not on the sheet, so
    // it is reported rather than clamped to a meaningless ceiling.
    el.textContent = (pct > 999 ? '>999' : pct.toFixed(0)) + '% of '
                     + fmtSize(state.stock.width) + ' x ' + fmtSize(state.stock.height);
  }

  /* Shelf-pack the parts onto the sheet, tallest first.
   *
   * Deliberately the simple algorithm: rows of parts, each row as tall as its tallest
   * member, new row when the current one runs out of width. It is not optimal, but it
   * is predictable, it never overlaps, and a person can see why it did what it did -
   * which matters more than the last few percent when someone is standing at a machine
   * deciding whether to trust it. Everything it produces is still draggable afterwards.
   *
   * Returns the number of parts it could not place. */
  function autoArrange() {
    var sheet = state.stock;
    var gap = jobKerf();                      // one kerf between neighbours, plus a hair
    var margin = gap;
    var sheetW = sheet ? sheet.width : state.machine.width;
    var sheetH = sheet ? sheet.height : state.machine.height;

    var items = state.parts.map(function (p) {
      var s = placedShape(p);
      return { part: p, w: s.w, h: s.h };
    }).sort(function (a, b) { return b.h - a.h || b.w - a.w; });

    var x = margin, y = margin, rowHeight = 0, unplaced = 0;
    items.forEach(function (item) {
      // Too wide for ANY shelf. Tested before the wrap, because wrapping first sent a
      // part that can never fit to a fresh shelf and then placed it there regardless -
      // hanging off the right edge and counted as placed, so "fill sheet" answered
      // "how many fit?" with parts that do not.
      if (item.w > sheetW - 2 * margin + 1e-6) {
        unplaced++;
        return;
      }
      if (x + item.w > sheetW - margin + 1e-6) {   // next shelf
        x = margin;
        y += rowHeight + gap;
        rowHeight = 0;
      }
      if (y + item.h > sheetH - margin + 1e-6) {
        unplaced++;                                // no room left on this sheet
        return;
      }
      // placedShape gives the footprint; place() works from the part's centre.
      item.part.cx = x + item.w / 2;
      item.part.cy = y + item.h / 2;
      x += item.w + gap;
      rowHeight = Math.max(rowHeight, item.h);
    });
    return unplaced;
  }

  /* Add copies of the selected part (or the only part) until the sheet is full. The
     question a shop actually asks is "how many of these fit on this?", and the honest
     way to answer it is to place them. */
  function fillSheet() {
    var sheet = state.stock;
    if (!sheet) return { added: 0, reason: 'Choose a sheet first - "fill" needs a size to fill.' };
    var seed = state.parts.filter(function (p) { return isSelected(p.id); })[0]
               || state.parts[0];
    if (!seed) return { added: 0, reason: 'Add a part first.' };

    // Stop where the SERVER stops. Filling past its limit would hand back a job the
    // very next step refuses, which is a worse answer than "that is as many as I can
    // cut in one go".
    var cap = parseInt(CFG.maxParts, 10) || 60;
    var added = 0, hitCap = false;
    while (true) {
      if (state.parts.length >= cap) { hitCap = true; break; }
      var copy = duplicatePart(seed.id, { silent: true });
      if (!copy) break;
      if (autoArrange() > 0) {    // it did not fit: take it back off the sheet
        state.parts = state.parts.filter(function (p) { return p.id !== copy.id; });
        autoArrange();
        break;
      }
      added++;
    }
    return { added: added, hitCap: hitCap, reason: '' };
  }

  /* --------------------------------------------------------------- parts */
  function thumbnailSVG(part) {
    var W = 50, H = 50, pad = 5;
    var scale = Math.min((W - 2 * pad) / (part.width || 1), (H - 2 * pad) / (part.height || 1));
    function map(x, y) { return [pad + x * scale, H - pad - y * scale]; }
    function ringPath(ring) { return ring.map(function (pt, i) { var m = map(pt[0], pt[1]); return (i ? 'L' : 'M') + m[0].toFixed(1) + ' ' + m[1].toFixed(1); }).join(' ') + ' Z'; }
    var d = ringPath(part.outline);
    var holes = (part.holes || []).map(function (h) { var m = map(h.cx, h.cy); return '<circle cx="' + m[0].toFixed(1) + '" cy="' + m[1].toFixed(1) + '" r="' + Math.max(1, h.r * scale).toFixed(1) + '" fill="none" stroke="' + cssVar('--muted') + '"/>'; }).join('');
    var inner = (part.inner || []).map(function (ring) { return '<path d="' + ringPath(ring) + '" fill="none" stroke="' + cssVar('--muted') + '" stroke-width="1"/>'; }).join('');
    return '<svg width="' + W + '" height="' + H + '" viewBox="0 0 ' + W + ' ' + H + '"><path d="' + d + '" fill="none" stroke="' + cssVar('--ink-2') + '" stroke-width="1.5"/>' + inner + holes + '</svg>';
  }

  function renderParts() {
    var ul = $('#parts-list');
    ul.innerHTML = '';
    state.parts.forEach(function (p) {
      var li = document.createElement('li');
      li.className = 'part-item';
      li.innerHTML = thumbnailSVG(p) +
        '<div class="meta"><div class="part-identity">' +
        '<label>Name <input class="part-name" type="text" maxlength="80"></label>' +
        '<label>Number <input class="part-number" type="text" maxlength="20"></label>' +
        '</div><div class="dims">' + p.width.toFixed(2) + '" x ' + p.height.toFixed(2) +
        '" &middot; drag the purple label in Layout</div></div>' +
        '<button class="duplicate" title="Duplicate" aria-label="Duplicate">&#10064;</button>' +
        '<button class="remove" title="Remove" aria-label="Remove">&times;</button>';
      var nameInput = li.querySelector('.part-name');
      var numberInput = li.querySelector('.part-number');
      nameInput.value = p.name;
      numberInput.value = p.number || '';
      nameInput.addEventListener('input', function () {
        var clean = this.value.replace(/[\x00-\x1f\x7f()]/g, '').trimStart();
        if (this.value !== clean) this.value = clean;
        p.name = clean;
        drawLayout(); invalidatePreview();
      });
      nameInput.addEventListener('change', function () {
        p.name = this.value.trim() || 'part';
        this.value = p.name;
        partListChanged();
      });
      numberInput.addEventListener('input', function () {
        var clean = this.value.replace(/[\x00-\x1f\x7f()]/g, '').trimStart();
        if (this.value !== clean) this.value = clean;
        p.number = clean;
        drawLayout(); invalidatePreview();
      });
      numberInput.addEventListener('change', function () {
        p.number = this.value.trim();
        this.value = p.number;
        partListChanged();
      });
      li.querySelector('.duplicate').addEventListener('click', function () { duplicatePart(p.id); });
      li.querySelector('.remove').addEventListener('click', function () { removePart(p.id); });
      ul.appendChild(li);
    });
    renderDebug();
  }

  /* How many parts the CURRENT mode will actually machine. 2.5D generates from one part
     and tubing posts at most two faces, and both limits were enforced only when adding -
     so loading three parts in 2D and then switching mode left three on screen, three in
     the summary, and one or two in the program. Silently machining a subset is the worst
     of the options; the extras are kept and the step is blocked instead, so nothing the
     user loaded is thrown away behind their back. */
  function partCapForMode() {
    if (state.mode === '2.5d') return 1;
    if (state.mode === 'tubing') return tubePatternOn() ? Infinity : 2;
    return Infinity;
  }

  function partsOverCap() {
    var cap = partCapForMode();
    return cap === Infinity ? 0 : Math.max(0, state.parts.length - cap);
  }

  function addPartFromOutline(data, file) {
    if (state.mode === '2.5d' && state.parts.length >= 1) {
      alert('2.5D mode allows only one part. Remove the current part first, or switch to 2D mode.');
      return;
    }
    if (state.mode === 'tubing' && state.parts.length >= 2) {
      alert('Tubing allows at most two faces (one per side). Remove a face first.');
      return;
    }
    // Adopt the CAD-discovered stock thickness when the server found one.
    //  - 2.5D: the field is hidden and thickness is authoritative (G-code re-derives it
    //    from the DXF regardless); always adopt.
    //  - 2D: seed the still-editable field from the first part's designed height so the
    //    user starts from the real value and can change it. Only the first part seeds it,
    //    so re-importing later parts won't clobber a value the user may have adjusted.
    if (data.thickness && (state.mode === '2.5d' || state.parts.length === 0)) {
      state.thickness = data.thickness;
      state.thickness_text = (Math.round(data.thickness * 10000) / 10000) + '"';   // shown in inches
      var tin = $('#f-thickness'); if (tin) tin.value = state.thickness_text;       // reflect in the editable 2D field
    }
    var p = {
      id: ++partSeq,
      name: data.name || ('part ' + (partSeq)),
      number: String(partSeq), label_x: data.width / 2, label_y: data.height / 2,
      width: data.width, height: data.height,
      outline: data.outline, holes: data.holes || [], inner: data.inner || [],
      file: file,
      cx: 0, cy: 0, rotation: 0, flipped: false,
    };
    // Initial placement: bottom edge on Y=0, stacked to the right of existing parts.
    var s = placedShape(p);
    var startX = 0;
    state.parts.forEach(function (q) { startX = Math.max(startX, footprint(q).maxX + state.tool_diameter); });
    p.cx = startX + s.w / 2;
    p.cy = s.h / 2;
    state.parts.push(p);
    renderParts();
    partListChanged();
    dbg('part-added', { name: p.name, w: p.width, h: p.height });
    return p;      // so a caller restoring a saved job can put it back where it was
  }

  /* The part list feeds the operation plan and the generated program, and in grid mode
     both can already be on screen when it changes. Without this, a part dropped while
     standing on Preview was invisible to the multi-tool editor - no row, no "this part
     has no operations" error - and Download still handed over the program from before it
     existed. */
  function partListChanged() {
    state.layoutNotice = '';      // it described the nest as it was a moment ago
    if (multiToolOn() && window.PCMultiTool) {
      window.PCMultiTool.render();
      window.PCMultiTool.refreshFeatures();
    }
    updateSummary();
    updatePartsModeNote();   // the over-cap warning depends on the count
    invalidatePreview();
    // Unconditionally: in grid mode the Layout canvas is on screen from every step, so a
    // part added or removed while standing elsewhere must show up on it immediately, not
    // on the next visit to Layout. Harmless in narrow mode - the canvas is just hidden.
    refitView();
    drawLayout();
  }

  /* Clone a loaded part: same geometry and file (never mutated - flip and rotation are
     flags applied at draw/submit time, so sharing the arrays is safe), its own id and a
     name of its own, placed clear of everything like a fresh import. The operation plan
     is copied too - a duplicate almost always wants cutting the same way - but the
     survey results are not: the name is part of the survey key, so the editor re-reads
     the copy on its own. The name gets no parentheses on purpose - part names end up in
     G-code comments, where parens nest and break controllers. */
  function duplicatePart(id, opts) {
    opts = opts || {};
    var src = state.parts.filter(function (p) { return p.id === id; })[0];
    if (!src) return null;
    if (state.mode === '2.5d') {
      if (!opts.silent) alert('2.5D machines one part per job, so a duplicate cannot be added. Switch to 2D mode to cut several.');
      return null;
    }
    if (state.mode === 'tubing' && state.parts.length >= 2) {
      if (!opts.silent) alert('Tubing allows at most two faces (one per side). Remove a face first.');
      return null;
    }
    var names = state.parts.map(function (p) { return p.name; });
    var base = src.name.replace(/ copy( \d+)?$/, '');
    var name = base + ' copy', n = 2;
    while (names.indexOf(name) >= 0) name = base + ' copy ' + (n++);
    var p = {
      id: ++partSeq,
      name: name,
      number: String(partSeq), label_x: src.label_x, label_y: src.label_y,
      width: src.width, height: src.height,
      outline: src.outline, holes: src.holes, inner: src.inner,
      file: src.file,
      cx: 0, cy: 0, rotation: src.rotation, flipped: src.flipped,
    };
    if (src.ops) p.ops = JSON.parse(JSON.stringify(src.ops));
    // Same initial placement rule as addPartFromOutline: bottom edge on Y=0, stacked to
    // the right of every existing part with a kerf between.
    var s = placedShape(p);
    var startX = 0;
    state.parts.forEach(function (q) { startX = Math.max(startX, footprint(q).maxX + state.tool_diameter); });
    p.cx = startX + s.w / 2;
    p.cy = s.h / 2;
    state.parts.push(p);
    if (!opts.silent) {
      renderParts();
      partListChanged();
      dbg('part-duplicated', { from: src.name, name: name });
    }
    return p;
  }

  function removePart(id) {
    state.parts = state.parts.filter(function (p) { return p.id !== id; });
    state.selectedIds = state.selectedIds.filter(function (sid) { return sid !== id; });
    renderParts();
    partListChanged();
  }

  function uploadDxf(file, onAdded) {
    if (!file || !/\.(dxf|step|stp)$/i.test(file.name)) {
      alert('Please choose a .dxf, .step, or .stp file.');
      return;
    }
    var isStep = /\.(step|stp)$/i.test(file.name);
    if (isStep && state.parts.length) {
      alert('2.5D STEP mode allows one part. Remove the current part before importing this STEP file.');
      return;
    }
    if (!isStep && state.mode === '2.5d') {
      alert('2.5D upload needs a STEP file with depth information. Switch to 2D for a flat DXF.');
      return;
    }
    var fd = new FormData();
    fd.append('file', file);
    dbg('part-outline:req', file.name);
    fetch('/part-outline', { method: 'POST', body: fd })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) {
        if (!res.ok || !res.j.success) {
          dbg('part-outline:err', res.j.error);
          alert('Could not read part: ' + (res.j.error || 'unknown error'));
          if (onAdded) onAdded(null);
          return;
        }
        var machiningFile = file;
        if (res.j.multilayer && res.j.dxf) {
          // STEP is a transport format, while /process deliberately has one geometry
          // contract: depth-layered DXF. Keep the converted bytes in the browser just
          // like an Onshape import, so generation stays stateless/serverless-safe.
          var bin = atob(res.j.dxf);
          var arr = new Uint8Array(bin.length);
          for (var i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
          machiningFile = new File([arr], (res.j.name || 'part') + '.dxf',
                                   { type: 'application/dxf' });
          state.mode = '2.5d';
          var mode25 = $('input[name="mode"][value="2.5d"]');
          if (mode25) { mode25.disabled = false; mode25.checked = true; }
          applyModeUI();
        }
        dbg('part-outline:ok', { name: res.j.name, w: res.j.width, h: res.j.height });
        var added = addPartFromOutline(res.j, machiningFile);
        if (onAdded) onAdded(added);
      })
      .catch(function (e) { dbg('part-outline:fail', String(e)); alert('Upload failed: ' + e); });
  }

  function bindParts() {
    if (state.source === 'onshape') {
      $('#upload-source').hidden = true;
      $('#onshape-source').hidden = false;
      // Face selection is continuous while on the Parts step (armed by gotoStep); the
      // #select-status label just reflects state — no button.
      function setSel(msg) { var el = $('#select-status'); if (el) el.textContent = msg; }
      window.PenguinCAM.onPart = function (data, fileOrBlob) {
        var file = fileOrBlob instanceof File ? fileOrBlob : new File([fileOrBlob], (data.name || 'part') + '.dxf');
        addPartFromOutline(data, file);
      };
      window.PenguinCAM.onSelectionBusy = function (busy) {
        setSel(busy ? 'Importing face, please wait…' : 'Select a face in Onshape…');
      };
      window.PenguinCAM.onSelectionError = function (msg) {
        dbg('onshape:error', msg);
        // Strip any trailing sentence punctuation the server message already carries
        // so we don't render a double period before the appended instruction.
        var clean = String(msg == null ? '' : msg).replace(/[.!?\s]+$/, '');
        setSel('Import failed: ' + clean + '. Select a face to try again.');
      };
    } else {
      var dz = $('#dropzone'), input = $('#f-dxf');
      dz.addEventListener('click', function () { input.click(); });
      dz.addEventListener('keydown', function (e) { if (e.key === 'Enter' || e.key === ' ') input.click(); });
      input.addEventListener('change', function () { if (this.files[0]) uploadDxf(this.files[0]); this.value = ''; });
      ['dragover', 'dragenter'].forEach(function (ev) { dz.addEventListener(ev, function (e) { e.preventDefault(); dz.classList.add('drag'); }); });
      ['dragleave', 'drop'].forEach(function (ev) { dz.addEventListener(ev, function (e) { e.preventDefault(); dz.classList.remove('drag'); }); });
      dz.addEventListener('drop', function (e) { if (e.dataTransfer.files[0]) uploadDxf(e.dataTransfer.files[0]); });
    }
    updatePartsModeNote();
  }

  /* -------------------------------------------------------------- layout */
  var canvasState = { scale: 1, wcx: 0, wcy: 0, ccx: 0, ccy: 0, action: null, handleDir: [0, 1] };

  // Tube faces aren't positioned by the user (the backend ignores placement for tubes),
  // so keep them tidy: left-to-right, bottom-aligned at Y=0, one kerf apart, using each
  // face's current (rotated) footprint. Called after a tube rotation so the faces don't
  // drift apart when they change orientation. Same stacking as addPartFromOutline.
  function restackTubeParts() {
    var x = 0;
    state.parts.forEach(function (p) {
      var s = placedShape(p);
      p.cx = x + s.w / 2;
      p.cy = s.h / 2;
      x += s.w + state.tool_diameter;
    });
  }

  // Give the canvas the backing store its box actually has. It used to be a fixed
  // 600x400 bitmap that CSS stretched, so the drawing was soft and locked to 3:2
  // however much room the panel had. Everything downstream works in canvas.width/
  // height units already (worldToCanvas, the hit tests, the drag maths).
  function sizeLayoutCanvas() {
    var canvas = $('#layout-canvas');
    if (!canvas) return false;
    var rect = canvas.getBoundingClientRect();
    // A hidden panel measures 0x0; keep the last good size rather than
    // collapsing the drawing to nothing.
    if (rect.width < 2 || rect.height < 2) return false;
    var w = Math.round(rect.width), h = Math.round(rect.height);
    if (canvas.width === w && canvas.height === h) return false;
    canvas.width = w;
    canvas.height = h;
    return true;
  }

  // Fit the parts' combined bounding box to ~80% of the canvas (times zoom), centered.
  // Called only on explicit events (entering Layout, zoom) — NOT every frame, so the
  // view stays put while dragging/rotating and part motion is actually visible.
  function refitView() {
    var canvas = $('#layout-canvas');
    if (!canvas) return;
    sizeLayoutCanvas();
    var bb = combinedBBox();
    if (state.stock && state.mode !== 'tubing') {
      bb = { minX: 0, minY: 0, maxX: state.stock.width, maxY: state.stock.height,
             w: state.stock.width, h: state.stock.height };
    } else if (tubePatternOn() && tubePatternGeom) {
      // No parts to bound, so fit the tube instead - otherwise the view falls back to a
      // fixed 10" square and the tube is drawn off the edge of it.
      bb = { minX: 0, minY: 0, maxX: tubePatternGeom.face_width,
             maxY: tubePatternGeom.length,
             w: tubePatternGeom.face_width, h: tubePatternGeom.length };
    }
    var w = bb ? Math.max(bb.w, 0.001) : 10;
    var h = bb ? Math.max(bb.h, 0.001) : 10;
    canvasState.wcx = bb ? (bb.minX + bb.maxX) / 2 : 0;
    canvasState.wcy = bb ? (bb.minY + bb.maxY) / 2 : 0;
    canvasState.ccx = canvas.width / 2;
    canvasState.ccy = canvas.height / 2;
    canvasState.scale = Math.min(canvas.width / w, canvas.height / h) * 0.8 * state.zoom;
  }
  function worldToCanvas(x, y) {
    return [canvasState.ccx + (x - canvasState.wcx) * canvasState.scale,
            canvasState.ccy - (y - canvasState.wcy) * canvasState.scale];
  }
  function canvasToWorld(cx, cy) {
    return [canvasState.wcx + (cx - canvasState.ccx) / canvasState.scale,
            canvasState.wcy - (cy - canvasState.ccy) / canvasState.scale];
  }

  // The rotation handle orbits the selection center along canvasState.handleDir (a
  // world-space unit vector that follows the pointer while rotating and persists after).
  // The bounding box stays axis-aligned; only the handle moves around it.
  function selectionHandle(selBox) {
    var cxw = (selBox.minX + selBox.maxX) / 2, cyw = (selBox.minY + selBox.maxY) / 2;
    var ctr = worldToCanvas(cxw, cyw);
    var dir = canvasState.handleDir || [0, 1];
    var dp = worldToCanvas(cxw + dir[0], cyw + dir[1]);
    var ux = dp[0] - ctr[0], uy = dp[1] - ctr[1], ul = Math.hypot(ux, uy) || 1; ux /= ul; uy /= ul;
    var a = worldToCanvas(selBox.minX, selBox.minY), b = worldToCanvas(selBox.maxX, selBox.maxY);
    var hw = Math.abs(b[0] - a[0]) / 2, hh = Math.abs(b[1] - a[1]) / 2;
    // Distance from center to the box edge along the handle direction (ray-rectangle
    // hit), so the stem meets the box exactly instead of a circumscribed circle.
    var tEdge = Math.min(
      Math.abs(ux) > 1e-6 ? hw / Math.abs(ux) : Infinity,
      Math.abs(uy) > 1e-6 ? hh / Math.abs(uy) : Infinity
    );
    if (!isFinite(tEdge)) tEdge = 0;
    return { ex: ctr[0] + ux * tEdge, ey: ctr[1] + uy * tEdge, hx: ctr[0] + ux * (tEdge + 26), hy: ctr[1] + uy * (tEdge + 26) };
  }

  // Point the handle sensibly when the selection changes: along a single part's "up",
  // or north for a group.
  function resetHandleDir() {
    var sel = selectedParts();
    canvasState.handleDir = (sel.length === 1) ? rotatePoint(0, 1, sel[0].rotation) : [0, 1];
  }

  /* The tube, face-on, as it sits on the jig: X across the face, Y along the tube away
     from the spindle. Same frame the pattern is authored in, so nothing has to be
     transformed to draw it. */
  function drawTubePattern(ctx, col, geom) {
    var W = geom.face_width, L = geom.length;
    var a = worldToCanvas(0, 0), b = worldToCanvas(W, L);
    ctx.save();
    ctx.fillStyle = 'rgba(150,165,190,0.10)';
    ctx.strokeStyle = col.ink;
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.rect(a[0], a[1], b[0] - a[0], b[1] - a[1]);
    ctx.fill();
    ctx.stroke();

    ctx.fillStyle = 'rgba(20,22,28,0.85)';
    ctx.strokeStyle = col.accent;
    ctx.lineWidth = 1;
    (geom.pockets || []).forEach(function (ring) {
      ctx.beginPath();
      ring.forEach(function (pt, i) {
        var p = worldToCanvas(pt[0], pt[1]);
        if (i === 0) ctx.moveTo(p[0], p[1]); else ctx.lineTo(p[0], p[1]);
      });
      ctx.closePath(); ctx.fill(); ctx.stroke();
    });
    (geom.holes || []).forEach(function (h) {
      var c = worldToCanvas(h.x, h.y);
      var r = Math.max(1, (h.d / 2) * canvasState.scale);
      ctx.beginPath(); ctx.arc(c[0], c[1], r, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
    });

    // A custom design gets two more layers on top: whatever the server REFUSED, in the
    // danger colour (its geometry is not in the lists above, so this is the only way it
    // is visible at all), and the selected feature outlined. The browser decides neither
    // - `ok` and the message both come from the server.
    if (tubeDesignOn() && geom.features) {
      var sel = window.PCTubeDesigner ? window.PCTubeDesigner.selectedIndex() : -1;
      geom.features.forEach(function (f) {
        var bad = !f.ok, chosen = f.index === sel;
        if (!bad && !chosen) return;
        ctx.save();
        ctx.strokeStyle = bad ? col.danger : col.ok;
        ctx.lineWidth = 2;
        if (bad) ctx.setLineDash([4, 3]);
        (f.pockets || []).forEach(function (ring) {
          ctx.beginPath();
          ring.forEach(function (pt, i) {
            var p = worldToCanvas(pt[0], pt[1]);
            if (i === 0) ctx.moveTo(p[0], p[1]); else ctx.lineTo(p[0], p[1]);
          });
          ctx.closePath(); ctx.stroke();
        });
        (f.holes || []).forEach(function (h) {
          var c = worldToCanvas(h.x, h.y);
          var r = Math.max(2, (h.d / 2) * canvasState.scale);
          ctx.beginPath(); ctx.arc(c[0], c[1], r, 0, Math.PI * 2); ctx.stroke();
        });
        ctx.restore();
      });
    }

    // The jig origin, which is what the operator actually sets.
    var o = worldToCanvas(0, 0);
    ctx.fillStyle = col.ok;
    ctx.beginPath(); ctx.arc(o[0], o[1], 4, 0, Math.PI * 2); ctx.fill();
    ctx.fillStyle = col.muted;
    ctx.font = '11px ' + CANVAS_FONT;
    ctx.fillText('G54 origin', o[0] + 7, o[1] - 6);
    ctx.restore();
  }

  /* Set while rendering the nest for the setup sheet: ink on white, whatever theme
     the screen is in. */
  var printPalette = null;

  function drawFixture(ctx, col) {
    if (!state.fixture.on || !state.stock) return;
    var geometry = fixtureGeometry(), f = state.fixture;
    ctx.save();
    ctx.font = '10px ' + CANVAS_MONO;
    ctx.textBaseline = 'middle';
    geometry.pins.forEach(function (pin) {
      // fixtureGeometry is in machine coordinates; the layout canvas is stock-relative.
      var p = worldToCanvas(pin.x - f.x, pin.y - f.y);
      var r = Math.max(3, f.pin * canvasState.scale / 2);
      ctx.fillStyle = col.accent; ctx.strokeStyle = col.ink; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.arc(p[0], p[1], r, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
      ctx.fillStyle = col.ink; ctx.fillText(pin.label.split(' ')[0], p[0] + r + 3, p[1]);
    });
    geometry.bolts.forEach(function (bolt) {
      var p = worldToCanvas(bolt.x - f.x, bolt.y - f.y);
      var r = Math.max(3, f.bolt * canvasState.scale / 2);
      ctx.strokeStyle = col.accent; ctx.lineWidth = 2;
      ctx.strokeRect(p[0] - r, p[1] - r, 2 * r, 2 * r);
      ctx.beginPath(); ctx.moveTo(p[0] - r, p[1] - r); ctx.lineTo(p[0] + r, p[1] + r);
      ctx.moveTo(p[0] + r, p[1] - r); ctx.lineTo(p[0] - r, p[1] + r); ctx.stroke();
      ctx.fillStyle = col.ink; ctx.fillText(bolt.label.split(' ')[0], p[0] + r + 3, p[1]);
    });
    ctx.restore();
  }

  function drawLayout() {
    var canvas = $('#layout-canvas');
    if (!canvas) return;
    var ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, canvas.width, canvas.height);

    var v = validateLayout();
    // One line per part is unreadable at 60 parts - it pushes the canvas to a sliver,
    // and the sixtieth copy of the same sentence tells nobody anything the first did
    // not. Cap it and say how many were folded away.
    var MAX_MSGS = 6;
    var shown = v.msgs.slice(0, MAX_MSGS);
    if (v.msgs.length > MAX_MSGS) {
      shown.push('...and ' + (v.msgs.length - MAX_MSGS) + ' more like these.');
    }
    // The notice from Auto-arrange / Fill sheet is redrawn with the errors rather than
    // written over them afterwards: this function runs again on the very next frame
    // (the ResizeObserver refit), so anything merely appended to the element vanished
    // about 50 ms after it appeared.
    if (state.layoutNotice) shown.unshift(state.layoutNotice);
    $('#layout-errors').textContent = shown.join('\n');
    // Flip is hidden in 2.5D (a mirror isn't recoverable when features live at specific
    // depths on one face) and in tubing (the opposite wall is handled server-side by
    // mirroring the pattern, not by a user flip).
    var flipBtn = $('#btn-flip');
    if (flipBtn) { flipBtn.hidden = (state.mode === '2.5d' || state.mode === 'tubing'); flipBtn.disabled = state.selectedIds.length === 0; }

    // Theme-aware colors (read the CSS variables so the canvas matches light/dark),
    // unless we are drawing for paper.
    var col = printPalette || {
      ink: cssVar('--ink') || '#e9e7e2',
      muted: cssVar('--muted') || '#8d8e8a',
      danger: cssVar('--danger') || '#e4564a',
      accent: cssVar('--accent') || '#a970ff',
      ok: cssVar('--ok') || '#4caf6d',
    };

    // A generated tube pattern has no parts to nest, so this canvas had nothing to draw
    // and the Layout step came up blank. Draw the tube itself: the outline the operator
    // clamps in the jig, and the pattern that will be cut into it.
    if (tubePatternOn() && tubePatternGeom) {
      drawTubePattern(ctx, col, tubePatternGeom);
      return;
    }

    // The chosen sheet, drawn as the material itself: a solid edge with the unused
    // area left plain, so "will this fit" and "how much is left" are one glance.
    if (state.stock && state.mode !== 'tubing') {
      var s0 = worldToCanvas(0, 0);
      var s1 = worldToCanvas(state.stock.width, state.stock.height);
      ctx.save();
      ctx.fillStyle = col.muted;
      ctx.globalAlpha = 0.07;
      ctx.fillRect(Math.min(s0[0], s1[0]), Math.min(s0[1], s1[1]),
                   Math.abs(s1[0] - s0[0]), Math.abs(s1[1] - s0[1]));
      ctx.globalAlpha = 1;
      ctx.strokeStyle = col.muted;
      ctx.lineWidth = 1.5;
      ctx.strokeRect(Math.min(s0[0], s1[0]), Math.min(s0[1], s1[1]),
                     Math.abs(s1[0] - s0[0]), Math.abs(s1[1] - s0[1]));
      ctx.restore();
      drawFixture(ctx, col);
    }

    // Stock = combined bounding box (dotted). Red if it exceeds the machine. The G54
    // origin marker sits at its lower-left.
    var bb = v.bbox;
    if (bb) {
      var a = worldToCanvas(bb.minX, bb.minY), c = worldToCanvas(bb.maxX, bb.maxY);
      ctx.save();
      ctx.setLineDash([5, 4]);
      ctx.strokeStyle = v.tooBig ? col.danger : col.muted; ctx.lineWidth = 1;
      ctx.strokeRect(Math.min(a[0], c[0]), Math.min(a[1], c[1]), Math.abs(c[0] - a[0]), Math.abs(c[1] - a[1]));
      ctx.setLineDash([]);
      ctx.restore();
    }

    // Parts.
    state.parts.forEach(function (p) {
      var pl = placement(p), s = pl.shape;
      var invalid = !!v.bad[p.id];
      var selected = isSelected(p.id);
      ctx.beginPath();
      s.pts.forEach(function (pt, i) {
        var pc = worldToCanvas(pl.x + pt[0], pl.y + pt[1]);
        if (i) ctx.lineTo(pc[0], pc[1]); else ctx.moveTo(pc[0], pc[1]);
      });
      ctx.closePath();
      ctx.fillStyle = invalid ? col.danger : (selected ? col.accent : col.muted);
      ctx.globalAlpha = invalid ? 0.18 : (selected ? 0.2 : 0.12);
      ctx.fill();
      ctx.globalAlpha = 1;
      ctx.strokeStyle = invalid ? col.danger : (selected ? col.accent : col.muted);
      ctx.lineWidth = selected ? 2 : 1;
      ctx.stroke();
      s.holes.forEach(function (h) {
        var hc = worldToCanvas(pl.x + h.cx, pl.y + h.cy);
        ctx.beginPath(); ctx.arc(hc[0], hc[1], Math.max(1, h.r * canvasState.scale), 0, 7); ctx.strokeStyle = col.muted; ctx.stroke();
      });
      (s.inner || []).forEach(function (ring) {
        ctx.beginPath();
        ring.forEach(function (pt, i) {
          var rc = worldToCanvas(pl.x + pt[0], pl.y + pt[1]);
          if (i) ctx.lineTo(rc[0], rc[1]); else ctx.moveTo(rc[0], rc[1]);
        });
        ctx.closePath(); ctx.strokeStyle = col.muted; ctx.lineWidth = 1; ctx.stroke();
      });
      if (state.engrave && state.mode === '2d') {
        var label = placedLabelAnchor(p);
        var labelCanvas = worldToCanvas(label.x, label.y);
        ctx.save();
        ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
        ctx.font = '600 ' + Math.max(11, Math.min(20, 0.18 * canvasState.scale)) + 'px ' + CANVAS_FONT;
        ctx.fillStyle = col.accent;
        ctx.fillText(partLabelText(p), labelCanvas[0], labelCanvas[1]);
        ctx.beginPath(); ctx.arc(labelCanvas[0], labelCanvas[1], 4, 0, Math.PI * 2);
        ctx.strokeStyle = col.accent; ctx.lineWidth = 1; ctx.stroke();
        ctx.restore();
      }
      var lc = worldToCanvas(pl.x, pl.y + pl.h);
      ctx.fillStyle = col.ink; ctx.font = '11px ' + CANVAS_FONT;
      ctx.fillText(p.name + (p.flipped ? ' (flipped)' : ''), lc[0] + 3, lc[1] + 12);
    });

    // Selection box + rotation handle.
    var selBox = combinedBBox(selectedParts());
    if (selBox) {
      var a2 = worldToCanvas(selBox.minX, selBox.minY), b2 = worldToCanvas(selBox.maxX, selBox.maxY);
      ctx.save();
      ctx.strokeStyle = col.accent; ctx.lineWidth = 1; ctx.setLineDash([4, 3]);
      ctx.strokeRect(Math.min(a2[0], b2[0]), Math.min(a2[1], b2[1]), Math.abs(b2[0] - a2[0]), Math.abs(b2[1] - a2[1]));
      ctx.setLineDash([]);
      var hg = selectionHandle(selBox);
      ctx.beginPath(); ctx.moveTo(hg.ex, hg.ey); ctx.lineTo(hg.hx, hg.hy);
      ctx.strokeStyle = col.accent; ctx.lineWidth = 1.5; ctx.stroke();
      ctx.beginPath(); ctx.arc(hg.hx, hg.hy, 6, 0, 7); ctx.fillStyle = col.accent; ctx.fill();
      ctx.restore();
    }

    // Origin marker + labeled axes, drawn last so they sit on top of the parts. The
    // origin (green dot) is the G54 lower-left; X (red) points +X to the right, Y (green)
    // points +Y up. Colors match the 3D preview so both views read the same.
    // On a sheet the G54 origin is the SHEET's corner, which is what the note above the
    // canvas, the setup sheet and the generated program all say. Drawing it on the
    // parts' bounding box put the marker in the middle of the sheet, contradicting both.
    var originX = state.stock ? 0 : (bb && bb.minX);
    var originY = state.stock ? 0 : (bb && bb.minY);
    if (bb || state.stock) {
      var o = worldToCanvas(originX, originY);
      ctx.save();
      ctx.fillStyle = col.ok;
      ctx.beginPath(); ctx.arc(o[0], o[1], 4, 0, 7); ctx.fill();
      var L = 42, head = 6;
      [['#ff0000', 1, 0, 'X'], ['#2ea043', 0, -1, 'Y']].forEach(function (ax) {
        var color = ax[0], dx = ax[1], dy = ax[2], label = ax[3];
        var ex = o[0] + dx * L, ey = o[1] + dy * L;
        ctx.strokeStyle = color; ctx.fillStyle = color; ctx.lineWidth = 2;
        ctx.beginPath(); ctx.moveTo(o[0], o[1]); ctx.lineTo(ex, ey); ctx.stroke();
        var ang = Math.atan2(ey - o[1], ex - o[0]);
        ctx.beginPath(); ctx.moveTo(ex, ey);
        ctx.lineTo(ex - head * Math.cos(ang - 0.4), ey - head * Math.sin(ang - 0.4));
        ctx.lineTo(ex - head * Math.cos(ang + 0.4), ey - head * Math.sin(ang + 0.4));
        ctx.closePath(); ctx.fill();
        ctx.font = 'bold 12px ' + CANVAS_FONT; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
        ctx.fillText(label, o[0] + dx * (L + 11), o[1] + dy * (L + 11));
      });
      ctx.restore();
    }

    // Combined size readout, upper-right - or, with nothing to nest yet, what to
    // do about it, centred on the sheet it would be nested on.
    ctx.save();
    if (bb) {
      ctx.textAlign = 'right'; ctx.font = '12px ' + CANVAS_MONO;
      ctx.fillStyle = v.tooBig ? col.danger : col.muted;
      // The stock is what gets clamped to the table, so that is the size to show; the
      // nest's own extent is the second line, for deciding whether another part fits.
      ctx.fillText(state.stock
        ? (fmtSize(state.stock.width) + ' x ' + fmtSize(state.stock.height) + ' sheet')
        : (bb.w.toFixed(2) + '" x ' + bb.h.toFixed(2) + '"'), canvas.width - 8, 18);
      if (state.stock) {
        ctx.fillStyle = col.muted;
        ctx.fillText('nest ' + bb.w.toFixed(2) + '" x ' + bb.h.toFixed(2) + '"',
                     canvas.width - 8, 34);
      }
    } else {
      ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
      ctx.font = '13px ' + CANVAS_FONT;
      ctx.fillStyle = col.muted;
      ctx.fillText('Add a part in the Parts panel and it appears here.',
                   canvas.width / 2, canvas.height / 2);
    }
    ctx.restore();
  }

  function hitTest(wx, wy) {
    for (var i = state.parts.length - 1; i >= 0; i--) {
      var b = footprint(state.parts[i]);
      if (wx >= b.minX && wx <= b.maxX && wy >= b.minY && wy <= b.maxY) return state.parts[i];
    }
    return null;
  }

  function hitLabel(cx, cy) {
    if (!state.engrave || state.mode !== '2d') return null;
    for (var i = state.parts.length - 1; i >= 0; i--) {
      var label = placedLabelAnchor(state.parts[i]);
      var c = worldToCanvas(label.x, label.y);
      if (Math.hypot(cx - c[0], cy - c[1]) <= 14) return state.parts[i];
    }
    return null;
  }

  function bindLayout() {
    var canvas = $('#layout-canvas');
    function evtCanvas(e) {
      var rect = canvas.getBoundingClientRect();
      var t = e.touches ? e.touches[0] : e;
      return [(t.clientX - rect.left) * (canvas.width / rect.width),
              (t.clientY - rect.top) * (canvas.height / rect.height)];
    }
    /* The custom designer takes the canvas over: there are no parts to nest, so a click
       either picks up a feature or places a new one. */
    function designDown(w, e) {
      var TD = window.PCTubeDesigner;
      var hit = TD.hitTest(w[0], w[1]);
      if (hit >= 0) {
        TD.select(hit);
        var at = TD.selectedPosition();
        canvasState.action = { type: 'design-drag',
                               grab: at ? [at.x - w[0], at.y - w[1]] : [0, 0] };
      } else {
        TD.placeAt(w[0], w[1]);
      }
      drawLayout();
      e.preventDefault();
    }

    function down(e) {
      var c = evtCanvas(e), w = canvasToWorld(c[0], c[1]);
      if (tubeDesignOn() && window.PCTubeDesigner) { designDown(w, e); return; }
      var labelHit = hitLabel(c[0], c[1]);
      if (labelHit) {
        state.selectedIds = [labelHit.id];
        canvasState.action = { type: 'label-drag', part: labelHit };
        drawLayout(); e.preventDefault(); return;
      }
      var shift = e.shiftKey;
      var selBox = combinedBBox(selectedParts());
      // Rotation handle rotates the whole selection about its center.
      if (selBox) {
        var hg = selectionHandle(selBox);
        if (Math.hypot(c[0] - hg.hx, c[1] - hg.hy) <= 12) {
          var pivot = [(selBox.minX + selBox.maxX) / 2, (selBox.minY + selBox.maxY) / 2];
          // Tubing rotates all faces together (they share one orientation on the jig),
          // so snapshot every part, not just the selection.
          var rotParts = state.mode === 'tubing' ? state.parts : selectedParts();
          canvasState.action = {
            type: 'rotate', pivot: pivot,
            refAngle: Math.atan2(w[1] - pivot[1], w[0] - pivot[0]),
            snap: rotParts.map(function (p) { return { p: p, cx: p.cx, cy: p.cy, rot: p.rotation }; })
          };
          e.preventDefault();
          return;
        }
      }
      var hit = hitTest(w[0], w[1]);
      if (hit) {
        if (shift) {
          if (isSelected(hit.id)) state.selectedIds = state.selectedIds.filter(function (id) { return id !== hit.id; });
          else state.selectedIds.push(hit.id);
          resetHandleDir();
        } else {
          if (!isSelected(hit.id)) { state.selectedIds = [hit.id]; resetHandleDir(); }
          // /process places a 2.5D part at the program origin and accepts rotation but
          // no X/Y placement. Let the part be selected for its rotation handle without
          // offering a drag gesture whose result would be ignored at generation time.
          if (state.mode !== '2.5d') {
            canvasState.action = {
              type: 'drag', startWorld: w,
              snap: selectedParts().map(function (p) { return { p: p, cx: p.cx, cy: p.cy }; })
            };
          }
        }
        drawLayout();
        e.preventDefault();
      } else if (!shift) {
        state.selectedIds = [];  // click empty space to deselect
        drawLayout();
      }
    }
    function move(e) {
      var act = canvasState.action;
      if (!act) return;
      var c = evtCanvas(e), w = canvasToWorld(c[0], c[1]);
      if (act.type === 'design-drag') {
        if (window.PCTubeDesigner) {
          window.PCTubeDesigner.moveSelected(w[0] + act.grab[0], w[1] + act.grab[1]);
        }
        drawLayout();
        e.preventDefault();
        return;
      }
      if (act.type === 'label-drag') {
        setLabelAnchorFromWorld(act.part, w[0], w[1]);
        drawLayout(); e.preventDefault(); return;
      }
      if (act.type === 'drag') {
        var dx = w[0] - act.startWorld[0], dy = w[1] - act.startWorld[1];
        act.snap.forEach(function (s) { s.p.cx = s.cx + dx; s.p.cy = s.cy + dy; });
      } else if (act.type === 'rotate') {
        var cur = Math.atan2(w[1] - act.pivot[1], w[0] - act.pivot[0]);
        // Handle follows the pointer around the box (and persists after release).
        var hl = Math.hypot(w[0] - act.pivot[0], w[1] - act.pivot[1]) || 1;
        canvasState.handleDir = [(w[0] - act.pivot[0]) / hl, (w[1] - act.pivot[1]) / hl];
        var cwDeg = -(cur - act.refAngle) * 180 / Math.PI;  // clockwise-positive delta
        if (state.mode === 'tubing') {
          // A tube must stay square to its jig: hard-snap to 90 deg (no free angle) and
          // rotate every face in place about its own center so both walls keep the same
          // orientation. Position is irrelevant for a tube, so there's no orbit.
          cwDeg = Math.round(cwDeg / 90) * 90;
          act.snap.forEach(function (s) {
            s.p.rotation = (((s.rot + cwDeg) % 360) + 360) % 360;
          });
        } else {
          var snapped = Math.round(cwDeg / 45) * 45;
          if (Math.abs(snapped - cwDeg) <= 5) cwDeg = snapped;
          act.snap.forEach(function (s) {
            var vv = rotatePoint(s.cx - act.pivot[0], s.cy - act.pivot[1], cwDeg);
            s.p.cx = act.pivot[0] + vv[0];
            s.p.cy = act.pivot[1] + vv[1];
            s.p.rotation = (((s.rot + cwDeg) % 360) + 360) % 360;
          });
        }
      }
      drawLayout();
      e.preventDefault();
    }
    function up() {
      // After rotating a tube, re-pack the faces adjacently so a horizontal→vertical
      // flip doesn't leave them spread apart, then refit the view to the tidy bbox.
      if (canvasState.action && canvasState.action.type === 'rotate' && state.mode === 'tubing') {
        restackTubeParts();
        refitView();
        drawLayout();
      }
      canvasState.action = null;
    }
    canvas.addEventListener('mousedown', down);
    window.addEventListener('mousemove', move);
    window.addEventListener('mouseup', up);
    canvas.addEventListener('touchstart', down, { passive: false });
    canvas.addEventListener('touchmove', move, { passive: false });
    canvas.addEventListener('touchend', up);

    /* Keyboard editing for the designer: nudge by one snap, and delete. Ignored while a
       form control has focus, so typing 0.5 into a properties box does not also move the
       feature it belongs to. */
    window.addEventListener('keydown', function (e) {
      if (!tubeDesignOn() || !window.PCTubeDesigner) return;
      var t = e.target, tag = t && t.tagName;
      if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA'
          || tag === 'BUTTON' || tag === 'A'
          || (e.target && e.target.getAttribute
              && e.target.getAttribute('role') === 'button')) return;
      var TD = window.PCTubeDesigner, handled = false;
      if (e.key === 'Delete' || e.key === 'Backspace') handled = TD.removeSelected();
      else if (e.key === 'ArrowLeft') handled = TD.nudge(-1, 0);
      else if (e.key === 'ArrowRight') handled = TD.nudge(1, 0);
      else if (e.key === 'ArrowUp') handled = TD.nudge(0, 1);
      else if (e.key === 'ArrowDown') handled = TD.nudge(0, -1);
      if (handled) { e.preventDefault(); drawLayout(); }
    });

    $('#btn-flip').addEventListener('click', function () {
      if (state.mode === '2.5d' || state.mode === 'tubing') return;  // flip not allowed in 2.5D/tubing
      selectedParts().forEach(function (p) { p.flipped = !p.flipped; });
      drawLayout();
    });
    var stockSel = $('#f-stock');
    if (stockSel) {
      stockSel.addEventListener('change', function () {
        var sheet = stockList().filter(function (x) { return x.id === this.value; }, this)[0];
        state.stock = sheet || null;
        applyStockUI();
        invalidatePreview();
        dbg('stock', state.stock && state.stock.name);
      });
    }
    var fixtureToggle = $('#f-fixture');
    function fixtureChanged() {
      updateFixtureUI();
      invalidatePreview();
      drawLayout();
    }
    if (fixtureToggle) {
      fixtureToggle.addEventListener('change', function () {
        state.fixture.on = this.checked && !!state.stock;
        fixtureChanged();
      });
    }
    [['#f-fixture-x', 'x'], ['#f-fixture-y', 'y'],
     ['#f-fixture-pin', 'pin'], ['#f-fixture-bolt', 'bolt']].forEach(function (spec) {
      bindLengthField($(spec[0]),
        function () { return state.fixture[spec[1] + '_text']; },
        function (inches, text) {
          state.fixture[spec[1]] = inches;
          state.fixture[spec[1] + '_text'] = text;
          fixtureChanged();
        });
    });

    var saveJobBtn = $('#btn-save-job');
    if (saveJobBtn) saveJobBtn.addEventListener('click', saveCurrentJob);
    var openJobBtn = $('#btn-open-job');
    if (openJobBtn) {
      openJobBtn.addEventListener('click', function () {
        var sel = $('#f-job');
        if (sel && sel.value) openSavedJob(sel.value);
      });
    }
    var deleteJobBtn = $('#btn-delete-job');
    if (deleteJobBtn) {
      deleteJobBtn.addEventListener('click', function () {
        var sel = $('#f-job');
        if (!sel || !sel.value) return;
        var label = sel.options[sel.selectedIndex].textContent;
        if (!confirm('Delete the saved job "' + label + '"? The DXFs saved with it go too.')) return;
        fetch('/jobs/delete', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ id: sel.value }),
        }).then(function (r) { return r.json(); })
          .then(function (j) { if (j.jobs) setSavedJobs(j.jobs); })
          .catch(function (e) { alert('Could not delete that job: ' + e); });
      });
    }

    var sheetBtn = $('#btn-setup-sheet');
    if (sheetBtn) sheetBtn.addEventListener('click', openSetupSheet);

    var saveStockBtn = $('#btn-save-stock');
    if (saveStockBtn) saveStockBtn.addEventListener('click', saveCurrentSheet);
    var saveRemnantBtn = $('#btn-save-remnant');
    if (saveRemnantBtn) saveRemnantBtn.addEventListener('click', saveRemnant);

    var arrangeBtn = $('#btn-arrange');
    if (arrangeBtn) {
      arrangeBtn.addEventListener('click', function () {
        if (!state.parts.length) { alert('Add a part first.'); return; }
        var unplaced = autoArrange();
        state.layoutNotice = unplaced
          ? (unplaced + ' part' + (unplaced === 1 ? '' : 's') + ' would not fit'
             + (state.stock ? ' on "' + state.stock.name + '"' : ' on the machine')
             + ' and stayed where they were.')
          : '';
        resetHandleDir(); refitView(); drawLayout(); updateUsage(); invalidatePreview();
      });
    }

    var fillBtn = $('#btn-fill');
    if (fillBtn) {
      fillBtn.addEventListener('click', function () {
        var res = fillSheet();
        if (res.reason) { alert(res.reason); return; }
        renderParts(); partListChanged();
        resetHandleDir(); refitView(); drawLayout(); updateUsage();
        state.layoutNotice = res.hitCap
          ? ('Stopped at ' + state.parts.length + ' parts, the most one job can hold. '
             + 'Cut this sheet, then fill another.')
          : (res.added ? ('Added ' + res.added + '.') : 'No more copies fit on "'
             + state.stock.name + '".');
        drawLayout();
      });
    }

    $('#btn-zoom-in').addEventListener('click', function () { state.zoom = Math.min(5, state.zoom * 1.25); refitView(); drawLayout(); });
    $('#btn-zoom-out').addEventListener('click', function () { state.zoom = Math.max(0.2, state.zoom / 1.25); refitView(); drawLayout(); });

    // Re-fit whenever the panel changes size - the window resizing, the Tools
    // column appearing, or the first layout pass after load, which is what
    // sizes the canvas in the first place (the observer fires on observe()).
    // Deferred to the next frame: resizing the canvas inside the observer's own
    // delivery cycle makes the browser report "ResizeObserver loop completed with
    // undelivered notifications" to window.onerror. It settles either way, but an
    // error handler should not have to know that.
    var refitQueued = false;
    function refit() {
      if (refitQueued) return;
      refitQueued = true;
      window.requestAnimationFrame(function () {
        refitQueued = false;
        if (sizeLayoutCanvas()) { refitView(); drawLayout(); }
      });
    }
    if (window.ResizeObserver) new ResizeObserver(refit).observe(canvas);
    else window.addEventListener('resize', refit);
  }

  function updateLayoutInfo() {
    var el = $('#info-machine-name'); if (el) el.textContent = state.machine.name;
    el = $('#info-machine-size');
    // toFixed: the config is in mm, so the converted inches arrived as
    // 31.496062992125985 and were printed verbatim.
    if (el) el.textContent = state.machine.width.toFixed(2) + '" x ' + state.machine.height.toFixed(2) + '"';
    el = $('#info-tool');
    if (el) {
      // Show the kerf actually being enforced, not the (hidden) single-tool field.
      el.textContent = multiToolOn()
        ? jobKerf().toFixed(4) + '" widest'
        : effectiveToolText();
    }
  }

  // The Layout hint reads differently for tubing: there's no sheet to nest on — the
  // step exists only to square the face(s) to the tube-jig axis (the machine's Y axis).
  function updateLayoutHint() {
    var el = $('#layout-hint');
    if (!el) return;
    if (tubeDesignOn()) {
      el.textContent = 'Click the tube to place the feature chosen in the Parts panel; '
        + 'click one to select it, drag to move, arrow keys nudge by '
        + ((CFG.tubeDesigner && CFG.tubeDesigner.grid) || 0.125) + '", Delete removes. '
        + 'The design is mirrored onto the opposite wall.';
    } else if (state.mode === 'tubing' && tubePatternOn()) {
      // A generated pattern has no parts, so there is no selection box and no rotation
      // handle to drag - the hint used to promise one that is never drawn.
      el.textContent = 'The pattern is generated from the tube length, so there is '
        + 'nothing to place here - this is a preview of what will be cut.';
    } else if (state.mode === 'tubing') {
      el.textContent = 'Drag the round handle to rotate the tube in 90 deg steps. ' +
        'Orient each face so the tube runs vertically (the Y axis) — that is the axis of the ' +
        'tube jig on the machine. Both faces rotate together.';
    } else if (state.mode === '2.5d') {
      el.textContent = 'Use the round handle to rotate the part. Its lower-left is placed '
        + 'at the G54 origin automatically; sheet position and dragging do not apply to '
        + 'this single-part workflow.';
    } else {
      el.textContent = 'Click to select (Shift-click for multiple), ' +
        'drag to move, drag the round handle to rotate (snaps to 45°). The dotted box is the stock; ' +
        'its lower-left is the G54 origin.';
    }
  }

  /* -------------------------------------------------------------- saved jobs */

  /* "Make six more of last week's gearbox plates" was a from-scratch rebuild every
     time: re-upload every DXF, re-enter the material and thickness, re-nest the sheet -
     and the nest was never quite the same twice. A saved job brings all of it back. */
  function savedJobs() { return CFG.savedJobs || []; }

  function renderJobPicker() {
    var wrap = $('#saved-jobs'), sel = $('#f-job');
    if (!wrap || !sel) return;
    var jobs = savedJobs();
    wrap.hidden = !jobs.length;
    sel.innerHTML = '';
    jobs.forEach(function (job) {
      var bits = [];
      if (job.part_count) bits.push(job.part_count + ' part' + (job.part_count === 1 ? '' : 's'));
      if (job.material) bits.push(job.material);
      if (job.thickness_text) bits.push(job.thickness_text);
      sel.appendChild(new Option(job.name + (bits.length ? '  ·  ' + bits.join(', ') : ''),
                                 job.id));
    });
  }

  function setSavedJobs(jobs) {
    CFG.savedJobs = jobs || [];
    renderJobPicker();
  }

  /* Everything needed to cut this again. The DXFs travel with it - a job that depended
     on files still being in someone's Downloads folder would not be saved at all. */
  function saveCurrentJob() {
    if (!state.parts.length) { alert('Add a part before saving a job.'); return; }
    var name = prompt('Name this job (you will pick it from a list next time):',
                      jobFilename().replace(/_/g, ' '));
    if (!name) return;

    var pending = state.parts.length;
    var parts = new Array(state.parts.length);
    state.parts.forEach(function (p, i) {
      var reader = new FileReader();
      reader.onload = function () {
        var bytes = new Uint8Array(reader.result), binary = '';
        for (var b = 0; b < bytes.length; b++) binary += String.fromCharCode(bytes[b]);
        var pl = placement(p);
        parts[i] = {
          name: p.name, number: p.number || '', dxf_base64: btoa(binary),
          // The corner for anything that reads a job file the way the wire format
          // means "place", and the centre because that is what a part actually holds.
          place_x: pl.x, place_y: pl.y,
          center_x: p.cx, center_y: p.cy,
          label_x: p.label_x, label_y: p.label_y,
          rotation: p.rotation, mirror: !!p.flipped,
          ops: p.ops || null,
        };
        if (--pending === 0) postJob(name, parts);
      };
      reader.onerror = function () {
        pending = -1;
        alert('Could not read ' + p.name + '’s DXF, so the job was not saved.');
      };
      reader.readAsArrayBuffer(p.file);
    });
  }

  function currentJobSetup() {
    return {
      material: state.material,
      thickness: state.thickness, thickness_text: state.thickness_text,
      tool_diameter: state.tool_diameter, tool_diameter_text: state.tool_diameter_text,
      tool_flutes: state.tool_flutes,
      mode: state.mode, z_datum: state.zDatum,
      tab_spacing: state.tab_spacing,
      max_pass_depth: state.max_pass_depth,
      engrave: engraveOn(),
      chamfer: state.chamfer.on ? state.chamfer : null,
      multitool: multiToolOn(),
      tools: multiToolOn() ? (state.tools || null) : null,
      stock: state.stock ? { id: state.stock.id, name: state.stock.name,
                             width: state.stock.width, height: state.stock.height } : null,
      fixture: state.fixture.on ? Object.assign({}, state.fixture) : null,
    };
  }

  function postJob(name, parts) {
    fetch('/jobs/save', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: name, setup: currentJobSetup(), parts: parts }),
    }).then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) {
        if (res.j && res.j.jobs) setSavedJobs(res.j.jobs);
        alert(res.ok ? (res.j.message || 'Saved.')
                     : (res.j.error || 'Could not save that job.'));
      })
      .catch(function (e) { alert('Could not reach the server to save the job: ' + e); });
  }

  /* Rebuild the wizard from a saved job. The DXFs come back as base64 and are turned
     into real File objects, so every part goes through exactly the same path as a fresh
     upload - one code path for "loaded" and "just dropped in" means a saved job cannot
     drift into behaving differently from the job it was saved from. */
  function openSavedJob(jobId) {
    fetch('/jobs/open', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: jobId }),
    }).then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) {
        if (!res.ok) { alert(res.j.error || 'Could not open that job.'); return; }
        applySavedJob(res.j.job);
      })
      .catch(function (e) { alert('Could not reach the server to open the job: ' + e); });
  }

  function applySavedJob(job) {
    var setup = job || {};
    var missingStock = '';
    // Setup first, so every part is measured against the right material and tool.
    if (setup.mode) {
      var modeRadio = document.querySelector('input[name="mode"][value="' + setup.mode + '"]');
      if (modeRadio && !modeRadio.disabled) { modeRadio.checked = true; state.mode = setup.mode; }
    }
    if (setup.material) {
      var msel = $('#f-material');
      if (msel) { msel.value = setup.material; state.material = setup.material; }
    }
    if (setup.thickness) {
      state.thickness = setup.thickness;
      state.thickness_text = setup.thickness_text || (setup.thickness + '"');
      var tf = $('#f-thickness'); if (tf) tf.value = state.thickness_text;
      state.thicknessTouched = true;
    }
    if (setup.tool_diameter) {
      state.tool_diameter = setup.tool_diameter;
      state.tool_diameter_text = setup.tool_diameter_text || (setup.tool_diameter + '"');
      var tl = $('#f-tool'); if (tl) tl.value = state.tool_diameter_text;
    }
    if (setup.tool_flutes) {
      state.tool_flutes = setup.tool_flutes;
      var tf = $('#f-tool-flutes'); if (tf) tf.value = state.tool_flutes;
    }
    if (setup.z_datum) {
      state.zDatum = setup.z_datum;
      var zr = document.querySelector('input[name="z_datum"][value="' + setup.z_datum + '"]');
      if (zr) zr.checked = true;
    }
    state.engrave = !!setup.engrave;
    var eb = $('#f-engrave'); if (eb) eb.checked = state.engrave;
    if (setup.max_pass_depth) state.max_pass_depth = setup.max_pass_depth;
    if (setup.tab_spacing) state.tab_spacing = setup.tab_spacing;

    // Multi-tool, its tool table and the chamfer, restored BEFORE the parts load: they
    // decide which steps exist and which cutter each operation means. Restoring the
    // per-part ops without them reopened a multi-tool job as a single-tool one that cut
    // the whole part with whatever bit happened to be in the tool box - twice the saved
    // diameter, and no warning.
    state.chamfer = setup.chamfer
      ? { on: true, width: setup.chamfer.width, bit: setup.chamfer.bit,
          angle: setup.chamfer.angle, perimeter: !!setup.chamfer.perimeter,
          holes: !!setup.chamfer.holes, pockets: !!setup.chamfer.pockets }
      : { on: false, width: state.chamfer.width, bit: state.chamfer.bit,
          angle: state.chamfer.angle, perimeter: true, holes: false, pockets: false };
    var wantMulti = !!setup.multitool && window.PCMultiTool;
    state.multitool = wantMulti;
    state.tools = wantMulti && setup.tools && setup.tools.length ? setup.tools : null;
    var mtBox = $('#f-multitool');
    if (mtBox && !mtBox.disabled) mtBox.checked = wantMulti;
    state.stock = null;
    if (setup.stock && setup.stock.id) {
      var known = stockList().filter(function (x) { return x.id === setup.stock.id; })[0];
      if (known) {
        state.stock = known;
        var ssel = $('#f-stock'); if (ssel) ssel.value = known.id || '';
      } else {
        // The sheet has been deleted from the team config since the job was saved.
        // Falling back to the saved snapshot left the picker reading "Just the parts"
        // while the validator, the note and the setup sheet all enforced a sheet that
        // no longer exists - and re-picking the already-shown option fires no change
        // event, so the only way out was a reload.
        missingStock = setup.stock.name || 'the saved sheet';
        var s0 = $('#f-stock'); if (s0) s0.value = '';
      }
    }
    if (setup.fixture && state.stock) {
      ['x', 'y', 'pin', 'bolt'].forEach(function (key) {
        var value = parseFloat(setup.fixture[key]);
        if (isFinite(value) && value > 0) {
          state.fixture[key] = value;
          state.fixture[key + '_text'] = setup.fixture[key + '_text'] || (value + '"');
          var field = $('#f-fixture-' + (key === 'pin' ? 'pin' : key === 'bolt' ? 'bolt' : key));
          if (field) field.value = state.fixture[key + '_text'];
        }
      });
      state.fixture.on = true;
    } else {
      state.fixture.on = false;
    }

    // Then the parts, through the ordinary upload path.
    state.parts = [];
    state.selectedIds = [];
    var queue = (job.parts || []).slice();
    var loaded = 0, failed = [];
    // Every part settles the count, including the ones that do not load. Returning
    // early on a bad DXF meant `loaded` could never reach the queue length, so the
    // wizard sat on the Parts step forever with no error and no way to tell why.
    function settle(name, ok) {
      if (!ok) failed.push(name);
      if (++loaded < queue.length) return;
      renderParts();
      partListChanged();
      applyModeUI();
      applyStockUI();
      // After the parts exist, so the operations editor can bind each part's restored
      // ops to the restored tool table.
      if (state.multitool && window.PCMultiTool) window.PCMultiTool.render();
      updateSummary();
      refitView();
      drawLayout();
      gotoStep('layout');
      var trouble = [];
      if (failed.length) {
        trouble.push('Opened without ' + failed.join(', ')
                     + (failed.length === 1 ? ': its' : ': their')
                     + ' drawing could not be read.');
      }
      if (missingStock) {
        trouble.push('"' + missingStock + '" is no longer in the stock list, so this '
                     + 'opened with no sheet. Pick one before cutting.');
      }
      if (trouble.length) alert(trouble.join('\n\n') + '\n\nCheck the nest before cutting.');
    }
    queue.forEach(function (saved) {
      var file = dxfFileFromBase64(saved.dxf_base64, saved.name + '.dxf');
      if (!file) { settle(saved.name || 'a part', false); return; }
      uploadDxf(file, function (part) {
        if (part) {
          part.name = saved.name || part.name;
          part.number = saved.number != null ? String(saved.number) : (part.number || '');
          part.rotation = saved.rotation || 0;
          part.flipped = !!saved.mirror;
          // Rotation and mirror have to be set BEFORE the footprint is measured, or a
          // job saved before format 2 comes back placed as though it were never turned.
          if (typeof saved.center_x === 'number' && typeof saved.center_y === 'number') {
            part.cx = saved.center_x; part.cy = saved.center_y;
          } else {
            var s0 = placedShape(part);          // format 1 stored only the corner
            part.cx = (saved.place_x || 0) + s0.w / 2;
            part.cy = (saved.place_y || 0) + s0.h / 2;
          }
          if (saved.ops) part.ops = saved.ops;
          if (typeof saved.label_x === 'number') part.label_x = saved.label_x;
          if (typeof saved.label_y === 'number') part.label_y = saved.label_y;
        }
        settle(saved.name || 'a part', !!part);
      });
    });
  }

  function dxfFileFromBase64(b64, filename) {
    try {
      var binary = atob(b64 || ''), bytes = new Uint8Array(binary.length);
      for (var i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
      return new File([bytes], filename, { type: 'application/dxf' });
    } catch (e) {
      return null;
    }
  }

  /* ------------------------------------------------------------- setup sheet */

  /* A page to take to the machine.
   *
   * Everything on it already exists somewhere in the app - the nest is the Layout
   * canvas, the tools are the tool table, the zero surface is the Z datum - but it is
   * spread across four panels on a laptop that is not next to the machine. An operator
   * standing at the controller has a phone or a piece of paper, and the one question
   * they need answered is "what do I load, where do I zero, and what should I see".
   *
   * Opened in its own window and printed from there, so nothing about the wizard's
   * layout has to survive a print stylesheet. */
  function openSetupSheet() {
    var win = window.open('', 'penguincam-setup-sheet');
    if (!win) {
      alert('Your browser blocked the setup sheet window. Allow pop-ups for this page.');
      return;
    }
    var doc = win.document;
    doc.open();
    doc.write(setupSheetHTML());
    doc.close();
    win.focus();
  }

  function setupSheetRows() {
    var resp = state.lastResponse || {};
    var rows = [];
    var bb = combinedBBox();
    rows.push(['Machine', state.machine.name]);
    rows.push(['Material', materialLabel() + ', ' + state.thickness_text + ' thick']);
    if (state.stock) {
      rows.push(['Stock', state.stock.name + '  (' + fmtSize(state.stock.width) + ' x '
                 + fmtSize(state.stock.height) + ')']);
      var placement = state.fixture.on ? null : centeredStockPlacement();
      if (state.fixture.on) {
        var fixture = fixtureGeometry();
        rows.push(['Fixture stock corner', 'X ' + fmtFixtureSize(state.fixture.x) + ', Y '
                   + fmtFixtureSize(state.fixture.y) + ' from machine-bed lower-left']);
        if (fixture.pins.length) rows.push(['Locator dowels', fixturePointText(fixture.pins)]);
        if (fixture.bolts.length) rows.push(['Clamp bolts', fixturePointText(fixture.bolts)]);
        rows.push(['Fixture sequence', 'Seat stock against all 3 dowels, tighten external '
                   + 'clamps, remove dowels, then set G54 at the stock lower-left']);
        rows.push(['Clamp clearance', 'Bolt holes are shown, but clamp bodies vary. Confirm every clamp stays outside the toolpath.']);
      } else if (placement) {
        rows.push(['Place stock', 'Center on bed: sheet lower-left at X '
                   + fmtSize(placement.x) + ', Y ' + fmtSize(placement.y)
                   + ' measured from the bed lower-left']);
      }
    } else if (bb) {
      rows.push(['Stock', fmtSize(bb.w) + ' x ' + fmtSize(bb.h) + ' (the parts)']);
    }
    // The single most important line on the page.
    rows.push(['ZERO Z ON', state.mode === 'tubing' ? 'The tube in its jig'
               : (state.zDatum === 'stock_top' ? 'The TOP FACE of the stock'
                                               : 'The SACRIFICE BOARD, through the stock')]);
    rows.push(['Zero X and Y on', 'The lower-left corner of the '
               + (state.stock ? 'sheet' : 'stock')]);
    if (resp.cycle_time) rows.push(['Estimated cycle', resp.cycle_time]);
    if (resp.tool_changes) {
      rows.push(['Tool changes', resp.tool_changes + ' - the program pauses at each one']);
    }
    if (state.dryRun) rows.push(['DRY RUN', 'This program cuts AIR. It proves the setup only.']);
    return rows;
  }

  /* What this job IS, in the words someone would use out loud: "4 x bracket", or
     "3 parts" once there are several different ones. The generated filename goes
     underneath, because that is what they will look for on the machine. */
  function setupSheetTitle(counts) {
    var names = Object.keys(counts);
    if (!names.length) return 'Setup sheet';
    if (names.length === 1) {
      return (counts[names[0]] > 1 ? counts[names[0]] + ' x ' : '') + names[0];
    }
    var total = names.reduce(function (sum, n) { return sum + counts[n]; }, 0);
    return total + ' parts, ' + names.length + ' different';
  }

  function materialLabel() {
    var sel = $('#f-material');
    var opt = sel && sel.options[sel.selectedIndex];
    return opt ? opt.text : state.material;
  }

  function setupSheetTools() {
    var resp = state.lastResponse || {};
    if (resp.tools && resp.tools.length) return resp.tools.slice();
    if (multiToolOn() && window.PCMultiTool) {
      return (state.tools || []).map(function (t) {
        return 'T' + t.slot + '  ' + t.name + '  ' + (t.diameter_text || t.diameter);
      });
    }
    return [state.tool_diameter_text + ' ' + state.tool_flutes + '-flute end mill'];
  }

  function setupSheetHTML() {
    var canvas = $('#layout-canvas');
    var nest = '';
    try {
      if (canvas) {
        printPalette = { ink: '#111111', muted: '#666666', danger: '#b00020',
                         accent: '#6d28d9', ok: '#1a7f37' };
        var ctx = canvas.getContext('2d');
        drawLayout();
        // Paint white UNDER what was drawn, so the PNG is ink on paper rather than
        // ink on nothing (a transparent PNG prints as whatever the browser assumes).
        ctx.save();
        ctx.globalCompositeOperation = 'destination-over';
        ctx.fillStyle = '#ffffff';
        ctx.fillRect(0, 0, canvas.width, canvas.height);
        ctx.restore();
        nest = canvas.toDataURL('image/png');
      }
    } catch (e) {
      nest = '';        // tainted canvas: the sheet is still worth printing without it
    } finally {
      printPalette = null;
      drawLayout();     // put the screen back the way it was
    }
    var partCounts = {};
    state.parts.forEach(function (p) {
      var base = partLabelText(p);
      partCounts[base] = (partCounts[base] || 0) + 1;
    });

    function esc(text) {
      return String(text).replace(/&/g, '&amp;').replace(/</g, '&lt;')
                         .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }
    var rows = setupSheetRows().map(function (r) {
      var key = (r[0] === 'ZERO Z ON' || r[0] === 'DRY RUN') ? ' class="key"' : '';
      return '<tr' + key + '><th>' + esc(r[0]) + '</th><td>' + esc(r[1]) + '</td></tr>';
    }).join('');
    var tools = setupSheetTools().map(function (t) {
      return '<li>' + esc(t) + '</li>';
    }).join('');
    var parts = Object.keys(partCounts).map(function (name) {
      return '<li>' + esc(name) + (partCounts[name] > 1
             ? ' <b>x' + partCounts[name] + '</b>' : '') + '</li>';
    }).join('');
    var filename = (state.lastResponse && state.lastResponse.filename_display)
                   || jobFilename();

    return '<!DOCTYPE html><html><head><meta charset="utf-8">'
      + '<title>Setup sheet - ' + esc(filename) + '</title><style>'
      + 'body{font:13px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;'
      + 'color:#111;margin:24px;max-width:760px}'
      // A CAD part name can be long and unbroken; without this it runs off the right
      // edge of the printed page, taking the file name with it.
      + 'h1,.sub,td{overflow-wrap:anywhere}'
      + 'h1{font-size:19px;margin:0 0 2px}'
      + '.sub{color:#555;margin:0 0 16px;font-size:12px}'
      + 'table{border-collapse:collapse;width:100%;margin-bottom:16px}'
      + 'th,td{text-align:left;padding:5px 8px;border-bottom:1px solid #ddd;vertical-align:top}'
      + 'th{width:150px;color:#444;font-weight:600}'
      + 'tr.key th,tr.key td{background:#f3e8ff;font-weight:700}'
      + 'h2{font-size:13px;margin:16px 0 6px;text-transform:uppercase;letter-spacing:.06em;color:#444}'
      + 'ul{margin:0;padding-left:20px}'
      + 'img{max-width:100%;border:1px solid #ccc;margin-top:6px}'
      + '.check{margin-top:18px;border-top:2px solid #111;padding-top:10px}'
      + '.check li{margin-bottom:4px}'
      + '@media print{body{margin:0}.noprint{display:none}}'
      + '</style></head><body>'
      + '<h1>' + esc(setupSheetTitle(partCounts)) + '</h1>'
      + '<p class="sub">' + esc(filename) + '.nc &middot; UV-CAM setup sheet &middot; '
      + esc(new Date().toLocaleString()) + '</p>'
      + '<table>' + rows + '</table>'
      + '<h2>Tools, in the order the program asks for them</h2><ul>' + tools + '</ul>'
      + '<h2>Parts on this sheet</h2><ul>' + parts + '</ul>'
      + (nest ? '<h2>Nest</h2><img src="' + nest + '" alt="Part layout">' : '')
      + '<div class="check"><h2>Before you press cycle start</h2><ul>'
      + '<li>Stock clamped, and no clamp in the toolpath</li>'
      + (state.fixture.on ? '<li>Locator dowels removed after the stock was clamped</li>' : '')
      + '<li>The right tool is in the spindle</li>'
      + '<li>Z zeroed on the surface named above &mdash; not the other one</li>'
      + '<li>X and Y zeroed at the lower-left corner</li>'
      + '<li>Dust shoe and eye protection</li>'
      + '</ul></div>'
      + '<p class="noprint"><button onclick="window.print()">Print this</button></p>'
      + '</body></html>';
  }

  /* ------------------------------------------------------------- preview */
  function resetPreview() {
    $('#preview-result').hidden = true;
    $('#preview-errors').textContent = '';
    $('#gen-status').textContent = '';
    showResumePrograms([]);
  }

  function generate() {
    $('#preview-errors').textContent = '';
    $('#gen-status').textContent = 'Generating…';

    if (state.mode === 'tubing') { generateTube(); }
    else if (state.mode === '2.5d') { generateSingle(); }
    else if (multiToolOn()) { generateMultiTool(); }
    else { generateJob(); }
  }

  // Shared POST /process handler for the single-file paths (2.5D and tubing).
  function submitToProcess(fd, label) {
    dbg(label + ':req');
    return fetch('/process', { method: 'POST', body: fd })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) {
        if (!res.ok || res.j.error) { $('#preview-errors').textContent = res.j.details || res.j.error || 'Generation failed'; $('#gen-status').textContent = ''; return; }
        dbg(label + ':ok', { time: res.j.cycle_time });
        state.lastResponse = res.j;
        showResult(res.j);
      })
      .catch(function (e) { $('#preview-errors').textContent = 'Request failed: ' + e; $('#gen-status').textContent = ''; });
  }

  function generateTube() {
    var generated = tubePatternOn();
    var p = state.parts[0];
    if (!p && !generated) {
      $('#preview-errors').textContent = 'Add a tube face first.';
      $('#gen-status').textContent = '';
      return Promise.resolve();
    }
    if (generated && !(state.tubePatternLength > 0)) {
      $('#preview-errors').textContent =
        'Enter the tube length - a generated pattern has no drawing to measure.';
      $('#gen-status').textContent = '';
      return Promise.resolve();
    }
    var fd = new FormData();
    // A generated pattern carries its own geometry, so no DXF is sent even if one was
    // added: the server would ignore it, and uploading it would only imply otherwise.
    if (p && !generated) fd.append('file', p.file, p.name + '.dxf');
    // A second face (optional) machines a distinct pattern on the opposite side; with
    // only one face, the server mirrors it onto the other side. A generated pattern is
    // symmetric about the face centreline, so it never needs one.
    if (state.parts[1] && !generated) fd.append('file_face2', state.parts[1].file, state.parts[1].name + '.dxf');
    fd.append('material', 'aluminum_tube');
    if (state.machine_id) fd.append('machine_id', state.machine_id);
    fd.append('tool_diameter', state.tool_diameter);
    fd.append('tool_flutes', state.tool_flutes);
    fd.append('thickness', state.thickness);       // tube wall thickness
    // Both faces share one orientation on the jig; the backend applies this single
    // rotation to every face. Tube rotation is hard-snapped to 90 deg in the Layout step.
    fd.append('rotation', p ? ((Math.round((p.rotation || 0) / 90) * 90) % 360 + 360) % 360 : 0);
    fd.append('tube_height', state.tubeHeight);
    fd.append('square_end', tubeEndMillingAvailable() && state.squareEnd ? '1' : '0');
    fd.append('cut_to_length', tubeEndMillingAvailable() && state.cutToLength ? '1' : '0');
    fd.append('tube_size', state.tubeSize);
    fd.append('tube_pattern', state.tubePattern);
    if (generated) fd.append('tube_pattern_length', state.tubePatternLength);
    // The design itself. The server resolves the named sizes and validates every
    // feature again - this is a request, not an instruction.
    if (tubeDesignOn()) fd.append('tube_design', JSON.stringify(state.tubeDesign));
    // A tube job can be proved in the air like any other; the checkbox said so long
    // before this line existed.
    if (state.dryRun) fd.append('dry_run', '1');
    fd.append('timestamp', timestamp());
    if (p) fd.append('suggested_filename', p.name);
    return submitToProcess(fd, 'tube');
  }

  // Filename base for a multi-part job: the distinct part names joined with "_", capped
  // in length so a big nest doesn't produce an absurd filename (extra names collapse to
  // "+N"). One part -> just its name; no parts -> "job". The timestamp is appended server
  // side, matching the single-part (2.5D/tube) paths that send suggested_filename=p.name.
  function jobFilename() {
    var names = [];
    state.parts.forEach(function (p) { if (names.indexOf(p.name) < 0) names.push(p.name); });
    if (!names.length) return 'job';
    var CAP = 64, kept = [], used = 0;
    for (var i = 0; i < names.length; i++) {
      var addLen = (kept.length ? 1 : 0) + names[i].length;
      if (kept.length && used + addLen > CAP) break;
      kept.push(names[i]); used += addLen;
    }
    var remaining = names.length - kept.length;
    return kept.join('_') + (remaining > 0 ? '+' + remaining : '');
  }

  function generateJob() {
    var fd = new FormData();
    // The parts' combined bounding box is the stock; its lower-left is the G54 origin,
    // so placements are normalized relative to it.
    var bb = combinedBBox() || { minX: 0, minY: 0, w: 0, h: 0 };
    var job = {
      material: state.material, tool_diameter: state.tool_diameter,
      tool_flutes: state.tool_flutes, machine_id: state.machine_id,
      thickness: state.thickness, tab_spacing: state.tab_spacing,
      stock: { width: bb.w, height: bb.h },
      name: jobFilename(), parts: [],
    };
    if (state.chamfer.on) {
      var chamferTargets = [];
      if (state.chamfer.perimeter) chamferTargets.push('perimeter');
      if (state.chamfer.holes) chamferTargets.push('holes');
      if (state.chamfer.pockets) chamferTargets.push('pockets');
      job.chamfer = {
        width: state.chamfer.width, bit_diameter: state.chamfer.bit,
        bit_angle: state.chamfer.angle, targets: chamferTargets,
      };
    }
    if (state.max_pass_depth) job.max_pass_depth = state.max_pass_depth;
    job.z_datum = state.zDatum;
    if (state.dryRun) job.dry_run = '1';
    if (engraveOn()) job.engrave = '1';
    var sheet = state.stock;
    if (sheet) {
      // The sheet is the stock, so placements are absolute on it and the origin is
      // its corner - not the bounding box of whatever happens to be placed today.
      job.stock = { width: sheet.width, height: sheet.height, from_library: true,
                    name: sheet.name };
    }
    state.parts.forEach(function (p, i) {
      var pl = placement(p);
      var label = placedLabelAnchor(p);
      job.parts.push({
        file_index: i, name: p.name,
        engrave_text: partLabelText(p),
        engrave_anchor_x: sheet ? label.x : label.x - bb.minX,
        engrave_anchor_y: sheet ? label.y : label.y - bb.minY,
        place_x: sheet ? pl.x : pl.x - bb.minX,
        place_y: sheet ? pl.y : pl.y - bb.minY,
        rotation: p.rotation, mirror: !!p.flipped,
      });
      fd.append('file_' + i, p.file, p.name + '.dxf');
    });
    fd.append('job', JSON.stringify(job));
    fd.append('timestamp', timestamp());
    dbg('process-job:req', { parts: job.parts.length });
    return fetch('/process-job', { method: 'POST', body: fd })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) {
        if (!res.ok || !res.j.success) { showGenErrors(res.j); return; }
        dbg('process-job:ok', { parts: (res.j.parts || []).length, time: res.j.cycle_time });
        state.lastResponse = res.j;
        showResult(res.j);
      })
      .catch(function (e) { dbg('process-job:fail', String(e)); $('#preview-errors').textContent = 'Request failed: ' + e; $('#gen-status').textContent = ''; });
  }

  // Multi-tool jobs post the same placements as generateJob, plus the tool table and each
  // part's operation list; the server orders the operations and inserts the tool changes.
  function generateMultiTool() {
    var bb = combinedBBox() || { minX: 0, minY: 0, w: 0, h: 0 };
    var sheet = state.stock;
    // Same rule as generateJob: on a sheet the placements are absolute and the origin
    // is the sheet's corner. This path used to always subtract the bounding box, so a
    // multi-tool nest was cut translated by however far the nest sat from the sheet's
    // corner - while the setup sheet told the operator to zero on that corner.
    var placements = state.parts.map(function (p) {
      var pl = placement(p);
      var label = placedLabelAnchor(p);
      return sheet
        ? { x: pl.x, y: pl.y, label_x: label.x, label_y: label.y }
        : { x: pl.x - bb.minX, y: pl.y - bb.minY,
            label_x: label.x - bb.minX, label_y: label.y - bb.minY };
    });
    // A part added after the deburr box was ticked has no chamfer op yet; the sync is
    // idempotent, so re-running it here catches up before the payload is built.
    if (state.chamfer.on) window.PCMultiTool.applyDeburr(state.chamfer);
    var fd = window.PCMultiTool.buildFormData(placements, jobFilename(), timestamp(),
      sheet ? { width: sheet.width, height: sheet.height, from_library: true,
                name: sheet.name } : null);
    dbg('process-multitool:req', { parts: state.parts.length });
    return fetch('/process-multitool', { method: 'POST', body: fd })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) {
        if (!res.ok || !res.j.success) { showGenErrors(res.j); return; }
        dbg('process-multitool:ok', { tools: res.j.tool_changes, time: res.j.cycle_time });
        state.lastResponse = res.j;
        showResult(res.j);
      })
      .catch(function (e) {
        dbg('process-multitool:fail', String(e));
        $('#preview-errors').textContent = 'Request failed: ' + e;
        $('#gen-status').textContent = '';
      });
  }

  function generateSingle() {
    var p = state.parts[0];
    if (!p) { $('#preview-errors').textContent = 'Add a part first.'; $('#gen-status').textContent = ''; return Promise.resolve(); }
    var fd = new FormData();
    fd.append('file', p.file, p.name + '.dxf');
    fd.append('material', state.material);
    if (state.machine_id) fd.append('machine_id', state.machine_id);
    fd.append('tool_diameter', state.tool_diameter);
    fd.append('tool_flutes', state.tool_flutes);
    fd.append('thickness', state.thickness);
    fd.append('origin_corner', 'bottom-left');
    fd.append('rotation', Math.round(p.rotation) % 360);
    fd.append('mirror', '0');  // 2.5D never mirrors (flip disabled in this mode)
    fd.append('tab_spacing', state.tab_spacing);
    if (state.max_pass_depth) fd.append('max_pass_depth', state.max_pass_depth);
    fd.append('z_datum', state.zDatum);
    if (state.dryRun) fd.append('dry_run', '1');
    if (engraveOn()) fd.append('engrave', '1');
    fd.append('timestamp', timestamp());
    fd.append('suggested_filename', p.name);
    return submitToProcess(fd, 'process');
  }

  function showGenErrors(j) {
    $('#gen-status').textContent = '';
    if (j && j.part_errors) {
      $('#preview-errors').textContent = j.part_errors.map(function (e) { return '• ' + e.error; }).join('\n');
    } else {
      $('#preview-errors').textContent = (j && (j.error || j.details)) || 'Generation failed';
    }
  }

  function showResult(resp) {
    $('#gen-status').textContent = '';
    $('#preview-result').hidden = false;
    var t = resp.cycle_time ? ('~' + resp.cycle_time + ' cycle') : '';
    var n;
    if (tubePatternOn()) {
      // Counting faces would report "0 faces" here: a generated pattern has no uploaded
      // face to count. Describe the tube that was machined instead.
      n = state.tubeSize + ' tube, ' + (state.tubePatternLength_text || state.tubePatternLength + '"');
      // Replace the PREDICTED count with the real one. The prediction is arithmetic on
      // copies of the generator's constants and knows nothing about the tool, so it went
      // on promising 11 triangles for a program that contained none (a cutter too fat to
      // enter them). A program that cuts nothing must not look like a success.
      var tp = resp.tube_preview, note = $('#tube-pattern-note');
      if (tp && note) {
        var holes = (tp.holes || []).length, pockets = (tp.pockets || []).length;
        if (!holes && !pockets) {
          note.textContent = 'This tube pattern cut nothing - see the warnings above.';
        } else {
          // What the PROGRAM contains, and which tool made it. Saying "drilled" for
          // every pattern with holes in it was wrong the moment a custom design could
          // mix a 1.125" bore with a clearance hole and mill both.
          var made = [];
          if (holes) made.push(holes + ' hole' + (holes === 1 ? '' : 's'));
          if (pockets) made.push(pockets + (tp.mode === 'lightening' ? ' triangle' : ' pocket')
                                 + (pockets === 1 ? '' : 's'));
          note.textContent = made.join(' and ') + ' per face, '
            + (tp.mode === 'holes' ? 'drilled.' : 'milled.');
        }
      }
    }
    else if (state.mode === 'tubing') { n = state.parts.length + ' face' + (state.parts.length === 1 ? '' : 's'); }
    else {
      var np = resp.parts ? resp.parts.length : 1;
      n = np + ' part' + (np === 1 ? '' : 's');
    }
    var bits = [n, t];
    if (resp.tools && resp.tools.length) {
      bits.splice(1, 0, resp.tools.length + ' tool' + (resp.tools.length === 1 ? '' : 's')
                        + ' · ' + (resp.tool_changes || 0) + ' change'
                        + (resp.tool_changes === 1 ? '' : 's'));
      // The M0 waits are operator time, so the estimate below them is cutting time only.
      if (resp.excludes_tool_change_time) bits.push('excludes tool-change time');
    }
    $('#preview-stats').textContent = bits.filter(Boolean).join(' · ');
    showResumePrograms(resp.restart_files || [], resp.restart_bundle || null);
    // Feeds warnings (a clamped feed, an odd flute count for the material) are advice,
    // not failures: show them without blocking the download.
    $('#preview-errors').textContent = (resp.warnings || []).map(function (w) {
      return '⚠ ' + w;
    }).join('\n');
    // ONE decision, made last. An unconditional `disabled = false` here silently undid
    // the checks above, so a tube pattern that cut nothing still offered a download.
    var tpv = resp.tube_preview;
    var cutsNothing = !!tpv && !(tpv.holes || []).length && !(tpv.pockets || []).length;
    $('#btn-do').disabled = cutsNothing;
    show3DPreview(resp);
    updateSummary();  // refresh the stock chip with the server-authoritative size
  }

  function showResumePrograms(files, bundle) {
    var box = $('#resume-programs');
    while (box.firstChild) box.removeChild(box.firstChild);
    box.hidden = !files.length;
    if (!files.length) return;

    var title = document.createElement('strong');
    title.textContent = 'Tool-change recovery files';
    box.appendChild(title);
    var note = document.createElement('p');
    note.textContent = 'If a run fails after a tool change, load the matching checkpoint file. '
      + 'Home or reference the router if position was lost, verify G54 X/Y, install the named '
      + 'tool, and re-zero G54 Z before pressing Cycle Start. Do not use a temporary G92 zero.';
    box.appendChild(note);
    var actions = document.createElement('div');
    actions.className = 'resume-actions';
    if (bundle) {
      var all = document.createElement('button');
      all.type = 'button';
      all.className = 'btn small primary';
      all.textContent = 'Download main + all recovery files';
      all.addEventListener('click', function () { doDownload(bundle.filename); });
      actions.appendChild(all);
    }
    files.forEach(function (file) {
      var button = document.createElement('button');
      button.type = 'button';
      button.className = 'btn small';
      button.textContent = 'Resume ' + file.checkpoint + ' · ' + file.description;
      button.addEventListener('click', function () { doDownload(file.filename); });
      actions.appendChild(button);
    });
    box.appendChild(actions);
  }

  function show3DPreview(resp) {
    if (typeof THREE === 'undefined' || typeof GcodeViewer === 'undefined') {
      $('#viewer-empty').textContent = '3D preview unavailable (three.js failed to load).';
      return;
    }
    if (!viewer) {
      viewer = new GcodeViewer({
        canvas: $('#gcode-canvas'), container: $('#viewer-container'),
        scrubber: $('#toolpath-scrubber'), scrubberContainer: $('#scrubber-container'),
        scrubberLabel: $('#scrubber-label'), scrubberOp: $('#scrubber-op'),
        playbackControls: $('#playback-controls'), playButton: $('#play-button'),
        restartButton: $('#restart-button'), speedSelect: $('#playback-speed'),
        resetButton: $('#reset-view'), emptyState: $('#viewer-empty'),
        showToolpath: $('#show-toolpath'),
      });
    }
    var W, D, stockH;
    if (state.mode === 'tubing') {
      // A tube face, not a sheet: use the first face's placed (rotated) extents so the
      // stock box matches the rotated G-code, plus the tube height for depth.
      var p0 = state.parts[0];
      var s0 = p0 ? placedShape(p0) : null;
      W = s0 ? s0.w : state.machine.width;
      D = s0 ? s0.h : state.machine.height;
      stockH = state.tubeHeight;
    } else {
      var bb = combinedBBox();
      W = (resp.stock && resp.stock.width) || (bb ? bb.w : state.machine.width);
      D = (resp.stock && resp.stock.height) || (bb ? bb.h : state.machine.height);
      stockH = state.thickness;
    }
    viewer.load(resp.gcode, {
      stockWidth: W, stockDepth: D,
      // Tube jobs keep their own jig frame whatever the sheet setting says.
      stockTopZ: (state.mode !== 'tubing' && state.zDatum === 'stock_top') ? 0 : stockH,
      stockHeight: stockH, toolDiameter: multiToolOn() ? jobKerf() : effectiveToolDiameter(),
      // Present only for a generated tube pattern, where the server knows the real
      // shape. The viewer then draws the tube itself with the pattern cut through it,
      // instead of a translucent box around the toolpath.
      tube: resp.tube_preview || null,
    });
    dbg('preview', { w: W, d: D });
  }

  /* ------------------------------------------------- final action (save) */
  var SAVE_PREF_KEY = 'penguincam_save_action';
  function readSavePref() { try { return localStorage.getItem(SAVE_PREF_KEY); } catch (e) { return null; } }
  function writeSavePref(a) { try { localStorage.setItem(SAVE_PREF_KEY, a); } catch (e) {} }
  function actionLabel(a) { return a === 'drive' ? 'Send to Google Drive' : 'Download Program'; }

  // Choose the default action: remembered preference, but only 'drive' if Drive is
  // configured now (falls back to download if a saved 'drive' pref is no longer valid).
  function preferredAction() {
    var pref = readSavePref();
    return (pref === 'drive' && CFG.driveEnabled) ? 'drive' : 'download';
  }

  // Configure the split button: caret + Drive option only when Drive is configured.
  function setupFinalAction() {
    var driveOk = !!CFG.driveEnabled;
    if (state.saveAction === 'drive' && !driveOk) state.saveAction = 'download';
    $('#btn-do-caret').hidden = !driveOk;
    $('#final-action').classList.toggle('has-caret', driveOk);
    var driveItem = document.querySelector('#do-menu li[data-action="drive"]');
    if (driveItem) driveItem.hidden = !driveOk;
    $('#btn-do').textContent = actionLabel(state.saveAction);
  }

  function chooseAction(a) {
    state.saveAction = a;
    writeSavePref(a);
    $('#btn-do').textContent = actionLabel(a);
    $('#do-menu').hidden = true;
    performAction(a);
  }

  function performAction(a) {
    var resp = state.lastResponse;
    if (!resp || !resp.filename) return;  // not generated yet
    if (a === 'drive') driveSave(resp.filename);
    else doDownload(resp.filename);
  }

  function doDownload(token) {
    // Open in a new top-level tab rather than an in-frame anchor: a sandboxed Onshape
    // iframe can block in-frame downloads, but the response forces attachment so the
    // new tab downloads and closes. (Relies on popups, which OAuth already uses.)
    window.open('/download/' + token, '_blank');
    $('#gen-status').textContent = 'Download started.';
  }

  function driveSave(token) {
    var status = $('#gen-status'), errs = $('#preview-errors');
    errs.textContent = '';
    status.textContent = 'Checking Google Drive…';
    fetch('/drive/status', { credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (st) {
        if (!st.enabled) { errs.textContent = 'Google Drive is not configured for your team.'; status.textContent = ''; return; }
        if (!st.authenticated) {
          status.textContent = 'Complete Google sign-in in the popup…';
          var popup = window.open('/auth/login', 'penguincam_gauth', 'width=600,height=700');
          if (!popup) { errs.textContent = 'Popup blocked — allow popups, then try again.'; status.textContent = ''; return; }
          var iv = setInterval(function () {
            if (popup.closed) { clearInterval(iv); driveUpload(token); }
          }, 500);
          setTimeout(function () { clearInterval(iv); }, 180000);
          return;
        }
        driveUpload(token);
      })
      .catch(function (e) { errs.textContent = 'Drive check failed: ' + e; status.textContent = ''; });
  }

  function driveUpload(token) {
    var status = $('#gen-status'), errs = $('#preview-errors'), btn = $('#btn-do');
    status.textContent = 'Uploading to Google Drive…';
    btn.disabled = true;
    // POST as JSON so require_auth returns 401 (not an HTML redirect) if still unauthed.
    fetch('/drive/upload/' + token, {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' }, body: '{}'
    })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) {
        btn.disabled = false;
        if (res.ok && res.j.success) { status.textContent = res.j.message || 'Saved to Google Drive.'; }
        else { errs.textContent = (res.j && res.j.message) || 'Drive upload failed.'; status.textContent = ''; }
      })
      .catch(function (e) { btn.disabled = false; errs.textContent = 'Drive upload failed: ' + e; status.textContent = ''; });
  }

  function bindFinalAction() {
    $('#btn-do').addEventListener('click', function () { if (!this.disabled) performAction(state.saveAction); });
    $('#btn-do-caret').addEventListener('click', function (e) {
      e.stopPropagation();
      var m = $('#do-menu'); m.hidden = !m.hidden;
    });
    $('#do-menu').addEventListener('click', function (e) {
      var li = e.target.closest('li[data-action]');
      if (li) chooseAction(li.getAttribute('data-action'));
    });
    document.addEventListener('click', function (e) {
      if (!e.target.closest('#final-action')) $('#do-menu').hidden = true;
    });
  }

  /* ----------------------------------------------------------------- init */
  function bindNav() {
    $('#btn-next').addEventListener('click', function () {
      var order = steps();
      var idx = order.indexOf(state.step);
      if (idx < order.length - 1 && canLeave(state.step)) {
        gotoStep(order[idx + 1]);
      }
    });
    $('#btn-back').addEventListener('click', function () {
      var order = steps();
      var idx = order.indexOf(state.step);
      if (idx > 0) gotoStep(order[idx - 1]);
    });

    // Clickable stepper pills (delegated), keyboard-activatable.
    var bar = $('#stepbar');
    function pillActivate(e) {
      var li = e.target.closest ? e.target.closest('li[data-step]') : null;
      if (!li) return;
      if (e.type === 'keydown' && e.key !== 'Enter' && e.key !== ' ') return;
      e.preventDefault();
      navigateTo(li.getAttribute('data-step'));
    }
    if (bar) { bar.addEventListener('click', pillActivate); bar.addEventListener('keydown', pillActivate); }
  }

  function bindConnect() {
    var btn = $('#btn-connect');
    if (!btn) return;
    var watching = false;
    function setStatus(msg) { $('#connect-status').textContent = msg; }

    // Confirm the iframe's own session is authenticated, then reload once to re-render
    // with the now-authenticated server context (config banner, material/tool options).
    function verifyAndEnter() {
      fetch('/onshape/authed', { credentials: 'same-origin' })
        .then(function (r) { return r.json(); })
        .then(function (j) {
          dbg('authed-check', j);
          if (j && j.authenticated) location.reload();
          else { watching = false; setStatus('Sign-in didn’t complete. Click Connect to try again.'); }
        })
        .catch(function (e) { watching = false; dbg('authed-check:err', String(e)); setStatus('Could not verify sign-in — try again.'); });
    }

    btn.addEventListener('click', function () {
      if (watching) return;
      var popup = window.open('/onshape/auth?popup=1', 'penguincam_oauth', 'width=520,height=720');
      if (!popup) { setStatus('Popup blocked — allow popups for this site, then click Connect.'); return; }
      watching = true;
      setStatus('Complete sign-in in the popup window…');
      // Watch the popup close locally (no server polling); cross-origin OAuth navigation
      // severs window.opener, so a postMessage from the popup isn't reliable. When it
      // closes, verify auth once. A safety cap stops the watcher if the popup lingers.
      var iv = setInterval(function () {
        if (popup.closed) { clearInterval(iv); setStatus('Finishing sign-in…'); verifyAndEnter(); }
      }, 500);
      setTimeout(function () { clearInterval(iv); }, 180000);
    });
  }

  /* Take whatever the controls ACTUALLY show and make that the state, before anything is
     drawn from it. Firefox restores radios, selects and checkboxes across a soft reload,
     so a page that came back with "Tubing" checked was read by the app as 2D: the radio
     said Tubing while the tube fields stayed hidden, which looks exactly like "it
     defaults to tubing and half the boxes are missing". autocomplete="off" in the markup
     stops the restore; this stops it MATTERING, for bfcache and session restore too.
     Text fields need no help - bindLengthField already commits whatever they render with. */
  function adoptControlsIntoState() {
    var modeRadio = document.querySelector('input[name="mode"]:checked');
    if (modeRadio && !modeRadio.disabled) state.mode = modeRadio.value;

    var sizeSel = $('#f-tube-size');
    if (sizeSel && sizeSel.value) state.tubeSize = sizeSel.value;
    var patSel = $('#f-tube-pattern');
    if (patSel && patSel.value) state.tubePattern = patSel.value;
    var matSel = $('#f-material');
    if (matSel && matSel.value && state.mode !== 'tubing') state.material = matSel.value;

    var zRadio = document.querySelector('input[name="z_datum"]:checked');
    if (zRadio && zRadio.value) state.zDatum = zRadio.value;

    var sq = $('#f-square-end'); if (sq) state.squareEnd = sq.checked;
    var ctl = $('#f-cut-to-length'); if (ctl) state.cutToLength = ctl.checked;

    // The designer's own controls, rebuilt from whatever they ACTUALLY show. Its palette
    // is a pair of selects, which Firefox restores across a soft reload just like the
    // ones above; the design document itself lives in state, not in the DOM.
    if (window.PCTubeDesigner) window.PCTubeDesigner.adopt();
  }

  /* One place that makes the DOM agree with `state`. Called at startup, and safe to call
     again: every function in it is idempotent and reads state rather than toggling it. */
  function syncUIFromState() {
    adoptControlsIntoState();   // the controls are the truth at startup, not the defaults
    applyModeUI();          // mode-dependent fields, labels, and the tube/multi-tool panels
    updateZDatumUI();
    renderBitPicker();
    renderStockPicker();
    renderJobPicker();
    applyStockUI();
    updatePartsModeNote();
    updateSummary();
    updateLayoutInfo();     // machine name, bed size and kerf in the Layout panel
    updateLayoutHint();
    refitView();
    drawLayout();
  }

  function init() {
    if (DEBUG) { $('#debug-overlay').hidden = false; }
    window.PenguinCAM.debug = dbg; // let the Onshape adapter log into the debug overlay
    // Expose the live mode so the Onshape panel adapter can request a 2.5D
    // (multi-layer) export instead of a flat one when the user picked 2.5D.
    window.PenguinCAM.getMode = function () { return state.mode; };
    // Started before bindSetup so the setup pass can already ask the editor whether
    // multi-tool is on (it answers "no" until it has been initialised).
    if (window.PCMultiTool) {
      window.PCMultiTool.init({
        state: state,
        parseLength: parseLength,
        cfg: { toolLibrary: toolLibrary, sortBits: sortedBitIds,
               defaultTool: CFG.defaultTool,
               defaultToolText: CFG.defaultToolText,
               toolsWritable: !!CFG.toolsWritable },
        setToolLibrary: setToolLibrary,
        bitId: bitId,
        onChange: updateSummary,
      });
    }
    // Started before bindSetup for the same reason the multi-tool editor is: the setup
    // pass calls applyTubePatternUI(), which asks the designer to draw its panel.
    if (window.PCTubeDesigner) {
      window.PCTubeDesigner.init({
        state: state,
        cfg: CFG.tubeDesigner || {},
        onGeometry: adoptTubeGeometry,
        onChange: function () { invalidatePreview(); updateSummary(); drawLayout(); },
      });
    }
    bindSetup();
    bindParts();
    bindLayout();
    bindNav();
    bindFinalAction();
    bindConnect();
    // Best-effort live theme sync: apply if Onshape posts a theme update while open.
    // (The load-time theme is already set server-side from the ?theme= URL param.)
    window.addEventListener('message', function (e) {
      var d = e.data;
      if (d && typeof d === 'object' && (d.theme === 'light' || d.theme === 'dark')) applyTheme(d.theme);
    });
    // Full-page (upload) mode shows all steps at once in a 2x2 grid; the narrow
    // Onshape panel iframe keeps the one-step-at-a-time wizard.
    if (state.source === 'upload') {
      $('#wizard').classList.add('grid');
      // STEP import produces the same depth-layered DXF contract as Onshape, so 2.5D
      // is available here too. A flat DXF is still refused while that mode is active.
    }
    // Draw the whole UI from state ONCE before the first paint. Everything below used
    // to run only as a side effect of a user event, so on first load the panels showed
    // whatever the static HTML happened to say. In full-page grid mode all four are on
    // screen at once, so the Layout panel sat there reading "Bed:   Tool: kerf" with the
    // values blank until you happened to visit that step - and switching mode and back
    // "fixed" it only because that re-enters gotoStep. First load must look exactly like
    // the state it is showing.
    syncUIFromState();
    gotoStep('setup');
    dbg('init', { source: state.source, authed: CFG.authenticated, theme: CFG.theme });
    if (state.source === 'onshape' && !CFG.authenticated) {
      $('#connect-overlay').hidden = false;
    }
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
