/* PenguinCAM multi-tool operations editor.
 *
 * Owns the wizard's "Tools & Operations" step: the table of tools loaded in the job, and
 * per part an ordered list of operations saying which tool cuts what. wizard.js keeps
 * owning everything else and calls in here at five points - whether the step exists
 * (`enabled`), drawing it (`render`), leaving it (`validate`), submitting
 * (`buildFormData`), and reading back results.
 *
 * The operation order is the user's and is never rearranged: it encodes intent that no
 * heuristic can recover (rough before finish, profile before chamfer). The server groups
 * the work by tool across parts without disturbing any part's own order, so writing the
 * operations in the order you would run them is always right.
 */
(function () {
  'use strict';

  var api = {};
  var ctx = null;              // {state, parseLength, onChange, cfg}
  var featureRequest = 0;      // guards against a stale survey response landing late

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

  /* The bits on the shelf. The host passes a getter so that saving a bit here refreshes
     the Setup panel's picker too, without either side keeping its own copy. */
  function library() {
    var lib = ctx.cfg && ctx.cfg.toolLibrary;
    if (typeof lib === 'function') lib = lib();
    return lib || {};
  }
  function bitId(name) {
    if (ctx.bitId) return ctx.bitId(name);
    return String(name || '').trim().toLowerCase()
      .replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '') || 'bit';
  }
  function isSaved(tool) {
    var entry = library()[bitId(tool.name)];
    return !!(entry && entry.source === 'team');
  }

  /* Save this row to the team config, or take it off the shelf again. The whole point is
     that a cutter you own gets written down once: the server keys it by name, so saving
     a bit whose name is already there corrects that entry rather than adding a twin. */
  function toggleSaved(tool) {
    var errors = $('#mt-errors');
    var saved = isSaved(tool);
    var body = saved ? { id: bitId(tool.name) }
                     : { tool: { name: tool.name, diameter_text: tool.diameter_text || String(tool.diameter),
                                 flutes: tool.flutes, type: tool.type,
                                 included_angle: tool.type === 'vbit' ? tool.included_angle : null } };
    fetch(saved ? '/tools/delete' : '/tools/save', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    }).then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) {
        // Only ever WRITE an error here: this element is the operation plan's own
        // validation output, and clearing it on a successful save would wipe a
        // "this part has no operations" warning the operator still needs.
        if (!res.ok && errors) errors.textContent = res.j.error || 'Could not save that bit.';
        if (res.j && res.j.library && ctx.setToolLibrary) ctx.setToolLibrary(res.j.library);
        else api.render();
      })
      .catch(function (e) {
        if (errors) errors.textContent = 'Could not reach the server to save that bit: ' + e;
      });
  }

  var OP_TYPES = [
    { id: 'holes', label: 'Holes' },
    { id: 'pockets', label: 'Pockets' },
    { id: 'interior', label: 'Holes + pockets' },
    { id: 'perimeter', label: 'Perimeter (profile)' },
    { id: 'chamfer', label: 'Chamfer (V-tool)' },
  ];

  var DRILL_PURPOSES = [
    { id: 'clearance', label: 'Clearance hole' },
    { id: 'tap', label: 'Tap drill (undersize)' },
    { id: 'spot', label: 'Centre/spot only' },
  ];

  var TOOL_TYPES = [
    { id: 'endmill', label: 'End mill' },
    { id: 'vbit', label: 'V-tool' },
    { id: 'drill', label: 'Drill' },
  ];

  /* ------------------------------------------------------------------ state */

  function defaultTools() {
    var lib = library();
    // Seed with the team's configured default cutter, so a job that turns multi-tool on
    // but only ever needs one tool works without opening the tool table at all.
    var dia = parseFloat(ctx.cfg && ctx.cfg.defaultTool) || 0.157;
    var match = Object.keys(lib).filter(function (k) {
      return Math.abs((lib[k].diameter || 0) - dia) < 0.0005
             && (lib[k].type || 'endmill') === 'endmill';
    })[0];
    // Show the team's own text ("4mm"), not String(0.15748031496062992). CFG keeps
    // defaultToolText for exactly this and it was going unused.
    var text = (ctx.cfg && ctx.cfg.defaultToolText) || (Math.round(dia * 10000) / 10000) + '"';
    return [{ slot: 1, name: match ? lib[match].name : 'End mill',
              diameter: dia, diameter_text: text,
              flutes: match ? (lib[match].flutes || 1) : 1,
              type: 'endmill', included_angle: 90 }];
  }

  function tools() {
    if (!ctx.state.tools || !ctx.state.tools.length) ctx.state.tools = defaultTools();
    return ctx.state.tools;
  }

  function nextSlot() {
    return tools().reduce(function (m, t) { return Math.max(m, t.slot); }, 0) + 1;
  }

  function toolBySlot(slot) {
    return tools().filter(function (t) { return t.slot === slot; })[0] || null;
  }

  /** Operations live on the part, so adding or removing a part carries its plan with it. */
  function opsFor(part) {
    if (!part.ops) part.ops = [];
    return part.ops;
  }

  /** Identity of the answer a survey would give: the inputs it actually depends on. */
  function surveyKey(part, payloadTools) {
    return [part.name, Math.round(part.rotation) % 360, !!part.flipped,
            ctx.state.material, ctx.state.thickness, payloadTools].join('|');
  }

  function newOp(type) {
    var op = { op_type: type || 'perimeter', tool_slot: tools()[0].slot, name: '',
               depth: null, scope: {} };
    if (op.op_type === 'chamfer') op.scope = { targets: ['perimeter'], width: 0.02 };
    return op;
  }

  api.enabled = function () {
    // 2.5D drives depth from the CAD layers and tubing runs its own fixed program, so
    // the operations editor only applies to flat 2D work for now.
    return !!ctx && !!ctx.state.multitool && ctx.state.mode === '2d';
  };

  /* -------------------------------------------------------------- surveying */

  /** Ask the server what each part actually contains, so scopes can be offered against
   *  real hole sizes instead of numbers typed from memory. */
  api.refreshFeatures = function (only) {
    if (!api.enabled()) return Promise.resolve();
    var token = ++featureRequest;
    var payloadTools = JSON.stringify(api.toolsPayload());
    // Only survey what we have no current answer for. This route is capped at 30/min,
    // and re-surveying every part on every entry to the step - and again on every
    // Suggest click - burned that budget in a handful of clicks, after which each part
    // showed a "could not reach the server" that was really a 429.
    var targets = only || ctx.state.parts.filter(function (p) {
      return p.surveyKey !== surveyKey(p, payloadTools);
    });
    if (!targets.length) { api.render(); return Promise.resolve(); }
    var pending = targets.map(function (part) {
      var fd = new FormData();
      fd.append('file', part.file, part.name + '.dxf');
      fd.append('thickness', ctx.state.thickness);
      fd.append('material', ctx.state.material);
      if (ctx.state.machine_id) fd.append('machine_id', ctx.state.machine_id);
      fd.append('rotation', Math.round(part.rotation) % 360);
      fd.append('mirror', part.flipped ? '1' : '0');
      fd.append('name', part.name);
      fd.append('tools', payloadTools);
      fd.append('mill_diameter', millDiameter());
      return fetch('/part-features', { method: 'POST', body: fd })
        .then(function (r) {
          if (r.status === 429) throw new Error('rate-limited');
          return r.json();
        })
        .then(function (j) {
          if (token !== featureRequest) return;      // a newer survey superseded this one
          part.features = j && j.success ? j.features : null;
          part.featureError = j && !j.success ? (j.error || 'Could not read this part') : null;
          part.surveyKey = j && j.success ? surveyKey(part, payloadTools) : null;
          part.drills = j && j.success ? j.drill_suggestions : null;
          part.plan = j && j.success ? j.suggested_plan : null;
          // Deliberately NOT auto-filling the operation list here. It used to be seeded
          // from a second, tool-constrained suggester, so a part surveyed with only a
          // default end mill silently got a plan that milled every hole - and the better
          // answer sat behind a button nobody had a reason to press. One suggestion,
          // one visible action.
        })
        .catch(function (err) {
          if (token !== featureRequest) return;
          part.surveyKey = null;
          // A 429 is the rate limiter, not a dead server. Saying "could not reach the
          // server" sent people restarting things that were working perfectly.
          part.featureError = (err && err.message === 'rate-limited')
            ? 'Too many part reads in a minute - wait a moment, then reopen this step.'
            : 'Could not read this part from the server.';
        });
    });
    return Promise.all(pending).then(function () {
      if (token === featureRequest) api.render();
    });
  };

  /* ----------------------------------------------------------------- render */

  /* One button, two states: this cutter is written down in the team config, or it is
     not. Disabled where the config cannot be written (the hosted app reads it from
     Onshape), with the reason in the tooltip rather than a silent no-op. */
  function saveBitButton(tool) {
    var writable = !(ctx.cfg && ctx.cfg.toolsWritable === false);
    var saved = isSaved(tool);
    return el('button', {
      type: 'button',
      class: 'mt-save-bit' + (saved ? ' saved' : ''),
      'aria-pressed': saved ? 'true' : 'false',
      text: saved ? '\u2605' : '\u2606',
      disabled: !writable || !String(tool.name || '').trim(),
      title: !writable
        ? 'Saved bits live in the team config file, which this copy cannot write'
        : (saved ? 'Remove "' + tool.name + '" from the team\u2019s saved bits'
                 : 'Save "' + tool.name + '" to the team\u2019s saved bits'),
      'aria-label': (saved ? 'Remove ' : 'Save ') + (tool.name || 'this bit'),
      onclick: function () { toggleSaved(tool); },
    });
  }

  function toolRow(tool, index) {
    var isV = tool.type === 'vbit';
    function change(field, cast) {
      return function (e) {
        tool[field] = cast ? cast(e.target.value) : e.target.value;
        if (field === 'type') api.render();
        touch();
      };
    }
    return el('tr', {}, [
      el('td', { class: 'mt-slot', text: 'T' + tool.slot }),
      el('td', {}, [el('input', { type: 'text', value: tool.name, 'aria-label': 'Tool name',
                                  oninput: change('name') })]),
      el('td', {}, [el('input', {
        type: 'text', class: 'mt-narrow', value: tool.diameter_text || tool.diameter,
        'aria-label': 'Diameter', placeholder: 'e.g. 1/4" or 6mm',
        oninput: function (e) {
          tool.diameter_text = e.target.value;
          var parsed = ctx.parseLength(e.target.value);
          // Do not retain the previous valid diameter behind visibly invalid text. That
          // let validation pass and submitted a size different from the one on screen.
          tool.diameter = parsed;
          e.target.classList.toggle('invalid', !(parsed > 0));
          touch();
        }
      })]),
      el('td', {}, [el('input', { type: 'number', class: 'mt-narrow', min: '1', max: '6',
                                  value: tool.flutes, 'aria-label': 'Flutes',
                                  oninput: change('flutes', function (v) { return parseInt(v, 10) || 1; }) })]),
      el('td', {}, [select(TOOL_TYPES, tool.type, change('type'), 'Tool type')]),
      el('td', {}, [isV ? el('input', {
        type: 'number', class: 'mt-narrow', min: '10', max: '179', step: '1',
        value: tool.included_angle, 'aria-label': 'Included angle',
        oninput: change('included_angle', function (v) { return parseFloat(v) || 90; })
      }) : el('span', { class: 'mt-dim', text: '—' })]),
      el('td', { class: 'mt-row-actions' }, [
        saveBitButton(tool),
        el('button', {
          type: 'button', class: 'mt-remove', title: 'Remove this tool',
          'aria-label': 'Remove tool T' + tool.slot, text: '×',
          onclick: function () { removeTool(index); }
        })]),
    ]);
  }

  function select(options, value, onchange, label) {
    var sel = el('select', { 'aria-label': label || '', onchange: onchange });
    options.forEach(function (o) {
      var opt = el('option', { value: o.id, text: o.label });
      if (String(o.id) === String(value)) opt.selected = true;
      sel.appendChild(opt);
    });
    return sel;
  }

  /** The end mill a suggested plan should be built around: the largest end mill already
   *  in the table, else the team's configured default. */
  function millDiameter() {
    var mills = tools().filter(function (t) { return t.type === 'endmill'; });
    if (mills.length) {
      return mills.reduce(function (m, t) { return Math.max(m, t.diameter); }, 0);
    }
    return parseFloat(ctx.cfg && ctx.cfg.defaultTool) || 0.157;
  }

  /** Replace this part's plan with the server's suggestion, adding any tools it needs.
   *  This is the one action that takes a part from "just uploaded" to "ready to cut". */
  function applyPlan(part) {
    var plan = part.plan;
    if (!plan) return;
    // No slot remapping. The survey posts our own tool table as `available`, so the
    // server reuses those tools by their REAL slot numbers and assigns new ones that do
    // not collide - every slot in plan.operations is already correct in our numbering.
    //
    // Remapping was not just redundant, it was wrong: once the server started reusing
    // tools, plan.tools held only the NEW ones, so an operation on a reused tool found
    // no entry in the map and silently fell back to whichever tool happened to be first.
    plan.tools.forEach(function (t) {
      if (toolBySlot(t.slot)) return;      // nothing should claim an occupied slot
      tools().push({
        slot: t.slot, name: t.name, diameter: t.diameter,
        diameter_text: t.diameter.toFixed(4), flutes: t.flutes,
        type: t.type, included_angle: t.included_angle || 90,
      });
    });
    part.ops = plan.operations.map(function (o) {
      return { op_type: o.op_type, tool_slot: o.tool_slot,
               name: o.name, depth: o.depth, scope: o.scope || {} };
    });
    pruneUnusedTools();
  }

  /** Drop tools no operation references, so the table stays a description of the job
   *  rather than an accumulation of everything ever tried. Never touches the last tool. */
  function pruneUnusedTools() {
    var used = {};
    ctx.state.parts.forEach(function (p) {
      opsFor(p).forEach(function (o) { used[o.tool_slot] = true; });
    });
    var keep = tools().filter(function (t) { return used[t.slot]; });
    if (keep.length) ctx.state.tools = keep;
  }

  function haveTool(candidate) {
    return tools().some(function (t) {
      return Math.abs(t.diameter - candidate.diameter) < 5e-4
             && (t.type || 'endmill') === (candidate.type || 'endmill');
    });
  }

  /** Add a suggested twist drill to the tool table, already typed as a drill so it takes
   *  the drilling toolpath rather than an end mill's helical entry. */
  function addDrill(match) {
    if (haveTool({ diameter: match.drill.diameter, type: 'drill' })) return;
    tools().push({
      slot: nextSlot(),
      name: match.drill.label + ' drill',
      diameter: match.drill.diameter,
      diameter_text: match.drill.diameter.toFixed(4),
      flutes: 2, type: 'drill', included_angle: 118,
    });
  }

  function removeTool(index) {
    var tool = tools()[index];
    var inUse = ctx.state.parts.some(function (p) {
      return opsFor(p).some(function (o) { return o.tool_slot === tool.slot; });
    });
    if (inUse && !confirm('T' + tool.slot + ' is used by an operation. Remove it anyway? '
                          + 'Those operations will move to the first remaining tool.')) return;
    tools().splice(index, 1);
    if (!tools().length) ctx.state.tools = defaultTools();
    var fallback = tools()[0].slot;
    ctx.state.parts.forEach(function (p) {
      opsFor(p).forEach(function (o) { if (!toolBySlot(o.tool_slot)) o.tool_slot = fallback; });
    });
    api.render();
    touch();
  }

  function addToolFromPreset(key) {
    var preset = library()[key] || {};
    tools().push({
      slot: nextSlot(),
      name: preset.name || 'End mill',
      diameter: preset.diameter || 0.25,
      // The team's own text ("6mm", '1/4"'), not a re-rendered decimal.
      diameter_text: preset.diameter_text || (preset.diameter ? String(preset.diameter) : '0.25'),
      flutes: preset.flutes || 1,
      type: preset.type || 'endmill',
      included_angle: preset.included_angle || 90,
    });
    api.render();
    touch();
  }

  function holeSummary(features) {
    if (!features) return '';
    var counts = {};
    (features.holes || []).forEach(function (h) { counts[h.diameter] = (counts[h.diameter] || 0) + 1; });
    var sizes = Object.keys(counts).sort(function (a, b) { return a - b; })
      .map(function (d) { return counts[d] + ' x ' + parseFloat(d).toFixed(3) + '"'; });
    var bits = [];
    if (sizes.length) bits.push('Holes: ' + sizes.join(', '));
    if ((features.pockets || []).length) bits.push((features.pockets || []).length + ' pocket(s)');
    if (features.has_perimeter) bits.push('perimeter');
    return bits.join(' · ') || 'No machinable features found';
  }

  /** A length field that parses units ("1/64", "0.4mm") and reverts on nonsense. */
  function lenInput(placeholder, get, set, label) {
    return el('input', {
      type: 'text', class: 'mt-narrow', placeholder: placeholder, 'aria-label': label,
      value: get() === null || get() === undefined ? '' : get(),
      oninput: function (e) {
        var raw = e.target.value.trim();
        if (!raw) { set(null); e.target.classList.remove('invalid'); touch(); return; }
        var parsed = ctx.parseLength(raw);
        e.target.classList.toggle('invalid', !parsed);
        if (parsed) { set(parsed); touch(); }
      }
    });
  }

  function scopeControls(op, part) {
    var wrap = el('div', { class: 'mt-scope' });

    var tool = toolBySlot(op.tool_slot);
    if (tool && tool.type === 'drill' && op.op_type !== 'chamfer') {
      // What the hole is FOR decides which drill suits it, and the three answers differ
      // by more than any tolerance - see docs/MULTI_TOOL_GUIDE.md.
      wrap.appendChild(select(DRILL_PURPOSES, op.scope.purpose || 'clearance',
        function (e) {
          op.scope.purpose = e.target.value;
          api.render();
          touch();
        }, 'What the hole is for'));
      if (op.scope.purpose === 'spot') {
        wrap.appendChild(el('span', { class: 'mt-dim', text: 'depth' }));
        wrap.appendChild(lenInput('auto', function () { return op.scope.spot_depth; },
                                  function (v) { op.scope.spot_depth = v; }, 'Spot depth'));
      } else {
        wrap.appendChild(el('span', { class: 'mt-dim', text: 'size tol' }));
        wrap.appendChild(lenInput('0.010',
          function () { return op.scope.size_tolerance; },
          function (v) { op.scope.size_tolerance = v; },
          'How far a hole may sit from the drill size'));
      }
    }

    if (op.op_type === 'holes' || op.op_type === 'interior') {
      wrap.appendChild(el('span', { class: 'mt-dim', text: 'hole dia' }));
      wrap.appendChild(lenInput('min', function () { return op.scope.min_diameter; },
                                function (v) { op.scope.min_diameter = v; }, 'Minimum hole diameter'));
      wrap.appendChild(el('span', { class: 'mt-dim', text: 'to' }));
      wrap.appendChild(lenInput('max', function () { return op.scope.max_diameter; },
                                function (v) { op.scope.max_diameter = v; }, 'Maximum hole diameter'));
    } else if (op.op_type === 'chamfer') {
      ['perimeter', 'holes', 'pockets'].forEach(function (target) {
        var targets = op.scope.targets || (op.scope.targets = ['perimeter']);
        var box = el('input', { type: 'checkbox', onchange: function (e) {
          var i = targets.indexOf(target);
          if (e.target.checked && i < 0) targets.push(target);
          if (!e.target.checked && i >= 0) targets.splice(i, 1);
          touch();
        } });
        box.checked = targets.indexOf(target) >= 0;
        wrap.appendChild(el('label', { class: 'mt-check' }, [box, el('span', { text: target })]));
      });
      wrap.appendChild(el('span', { class: 'mt-dim', text: 'width' }));
      wrap.appendChild(lenInput('0.02', function () { return op.scope.width; },
                                function (v) { op.scope.width = v; }, 'Chamfer width'));
    } else if (op.op_type === 'pockets') {
      wrap.appendChild(el('span', { class: 'mt-dim', text: 'all pockets' }));
    } else {
      wrap.appendChild(el('span', { class: 'mt-dim', text: 'whole outline' }));
    }
    return wrap;
  }

  function opRow(part, op, index) {
    var ops = opsFor(part);
    var toolOptions = tools().map(function (t) {
      return { id: t.slot, label: 'T' + t.slot + ' – ' + t.name };
    });

    function move(delta) {
      var to = index + delta;
      if (to < 0 || to >= ops.length) return;
      ops.splice(to, 0, ops.splice(index, 1)[0]);
      api.render();
      touch();
    }

    var depthInput = el('input', {
      type: 'text', class: 'mt-narrow', placeholder: 'through',
      'aria-label': 'Depth of cut, blank for through',
      value: op.depth === null || op.depth === undefined ? '' : op.depth,
      oninput: function (e) {
        var raw = e.target.value.trim();
        if (!raw) { op.depth = null; e.target.classList.remove('invalid'); touch(); return; }
        var parsed = ctx.parseLength(raw);
        e.target.classList.toggle('invalid', !parsed);
        if (parsed) { op.depth = parsed; touch(); }
      }
    });
    if (op.op_type === 'chamfer' || op.op_type === 'perimeter') {
      // A chamfer's depth follows from its width and the V-tool angle, and a profile that
      // doesn't go through the stock wouldn't free the part.
      depthInput.disabled = true;
      depthInput.value = '';
      depthInput.placeholder = op.op_type === 'chamfer' ? 'from width' : 'through';
    }

    return el('li', { class: 'mt-op' }, [
      el('span', { class: 'mt-step', text: String(index + 1) }),
      select(OP_TYPES, op.op_type, function (e) {
        op.op_type = e.target.value;
        op.scope = op.op_type === 'chamfer' ? { targets: ['perimeter'], width: 0.02 } : {};
        if (op.op_type === 'chamfer') {
          var v = tools().filter(function (t) { return t.type === 'vbit'; })[0];
          if (v) op.tool_slot = v.slot;
        }
        api.render();
        touch();
      }, 'Operation type'),
      select(toolOptions, op.tool_slot, function (e) {
        op.tool_slot = parseInt(e.target.value, 10);
        touch();
      }, 'Tool'),
      scopeControls(op, part),
      depthInput,
      el('span', { class: 'mt-effect', text: opEffect(part, op),
                   title: 'What this operation will cut' }),
      el('span', { class: 'mt-move' }, [
        el('button', { type: 'button', class: 'btn small', text: '↑', title: 'Move earlier',
                       'aria-label': 'Move operation earlier', onclick: function () { move(-1); } }),
        el('button', { type: 'button', class: 'btn small', text: '↓', title: 'Move later',
                       'aria-label': 'Move operation later', onclick: function () { move(1); } }),
        el('button', { type: 'button', class: 'mt-remove', text: '×', title: 'Remove operation',
                       'aria-label': 'Remove operation', onclick: function () {
                         ops.splice(index, 1); api.render(); touch();
                       } }),
      ]),
    ]);
  }

  /** What an operation will actually cut, in the operator's terms. Shown on the row so
   *  a plan can be checked at a glance rather than by generating it and reading G-code. */
  function opEffect(part, op) {
    var f = part.features;
    if (!f) return '';
    if (op.op_type === 'perimeter') return f.has_perimeter ? 'the outline' : 'no outline!';
    if (op.op_type === 'chamfer') {
      var targets = (op.scope.targets || []).join(' + ') || 'nothing selected';
      return targets + ' @ ' + (num(op.scope.width) || 0.02).toFixed(3) + '"';
    }
    var bits = [];
    if (op.op_type === 'holes' || op.op_type === 'interior') {
      var holes = (f.holes || []).filter(function (h) { return holeInScope(h, op); });
      var sizes = {};
      holes.forEach(function (h) { sizes[h.diameter] = (sizes[h.diameter] || 0) + 1; });
      var names = Object.keys(sizes).sort(function (a, b) { return a - b; })
        .map(function (d) { return sizes[d] + ' x ' + parseFloat(d).toFixed(4) + '"'; });
      bits.push(names.length ? names.join(', ') : 'no holes match');
    }
    if (op.op_type === 'pockets' || op.op_type === 'interior') {
      var pockets = (f.pockets || []).filter(function (pk) { return pocketInScope(pk, op); });
      bits.push(pockets.length
        ? pockets.length + ' pocket' + (pockets.length === 1 ? '' : 's')
        : 'no pockets match');
    }
    return bits.join(', ');
  }

  /** The order the program will actually run in, and what it costs in tool changes.
   *  Mirrors tooling.order_operations: group by tool without reordering any part. */
  function runOrder() {
    var queues = ctx.state.parts.map(function (p) { return opsFor(p).slice(); });
    var sequence = [];
    var guard = 0;
    while (queues.some(function (q) { return q.length; }) && guard++ < 500) {
      var current = null;
      for (var i = 0; i < queues.length; i++) {
        if (queues[i].length) { current = queues[i][0].tool_slot; break; }
      }
      if (current === null) break;
      queues.forEach(function (q, qi) {
        while (q.length && q[0].tool_slot === current) {
          sequence.push({ part: ctx.state.parts[qi], op: q.shift() });
        }
      });
    }
    var changes = 0;
    sequence.forEach(function (item, i) {
      if (i && item.op.tool_slot !== sequence[i - 1].op.tool_slot) changes++;
    });
    return { sequence: sequence, changes: changes };
  }

  /** A compact strip showing the emitted order, so the cost of a plan is visible before
   *  it is generated rather than discovered in the header afterwards. */
  function runOrderPanel() {
    var order = runOrder();
    if (!order.sequence.length) return null;
    var box = el('div', { class: 'mt-runorder' }, [
      el('span', { class: 'mt-dim', text: 'Run order:' }),
    ]);
    var lastSlot = null;
    order.sequence.forEach(function (item, i) {
      if (i) box.appendChild(el('span', { class: 'mt-arrow', text: '▸' }));
      if (item.op.tool_slot !== lastSlot) {
        var tool = toolBySlot(item.op.tool_slot);
        box.appendChild(el('strong', {
          class: 'mt-chip',
          text: 'T' + item.op.tool_slot,
          title: tool ? tool.name : 'this tool is no longer in the table',
        }));
        lastSlot = item.op.tool_slot;
      }
      box.appendChild(el('span', {
        class: 'mt-step-name',
        text: item.op.name || item.op.op_type,
        title: item.part.name,
      }));
    });
    box.appendChild(el('span', {
      class: 'mt-dim mt-changes',
      text: order.changes + ' tool change' + (order.changes === 1 ? '' : 's'),
    }));
    return box;
  }

  function partBlock(part) {
    var ops = opsFor(part);
    var body = el('div', { class: 'mt-part' });

    var head = el('div', { class: 'mt-part-head' }, [el('strong', { text: part.name })]);
    if (part.featureError) {
      head.appendChild(el('span', { class: 'mt-dim mt-warn', text: part.featureError }));
    } else if (part.features) {
      head.appendChild(el('span', { class: 'mt-dim', text: holeSummary(part.features) }));
    } else {
      head.appendChild(el('span', { class: 'mt-dim', text: 'reading part...' }));
    }
    body.appendChild(head);

    // What this part needs, and one button that supplies it. Deliberately ABOVE the
    // operation list: you cannot sensibly plan a part with tools you have not chosen
    // yet, and this is where you find out which ones those are.
    if (part.plan && part.plan.tools.length) {
      var wanted = part.plan.tools;
      var missing = wanted.filter(function (t) { return !haveTool(t); });
      var line = el('div', { class: 'mt-drills' }, [
        el('span', { class: 'mt-dim', text: 'This part needs:' }),
      ]);
      wanted.forEach(function (t, i) {
        if (i) line.appendChild(el('span', { class: 'mt-dim', text: ',' }));
        line.appendChild(el('span', {
          class: haveTool(t) ? 'mt-have' : 'mt-need',
          text: t.name,
          title: haveTool(t) ? 'already in your tool table' : 'not loaded yet',
        }));
      });
      line.appendChild(el('button', {
        type: 'button', class: 'btn small primary',
        text: ops.length ? 'Re-plan this part' : 'Set up this part',
        title: 'Load the tools this part needs and build the operation list',
        onclick: function () { applyPlan(part); api.render(); touch(); },
      }));
      if (missing.length && ops.length) {
        line.appendChild(el('button', {
          type: 'button', class: 'btn small',
          text: '+ ' + missing.length + ' tool' + (missing.length === 1 ? '' : 's'),
          title: 'Add only the missing tools, keeping the operations you have',
          onclick: function () {
            missing.forEach(function (t) {
              tools().push({ slot: nextSlot(), name: t.name, diameter: t.diameter,
                             diameter_text: t.diameter.toFixed(4), flutes: t.flutes,
                             type: t.type, included_angle: t.included_angle || 90 });
            });
            api.render();
            touch();
          },
        }));
      }
      body.appendChild(line);
    }
    ((part.plan && part.plan.notes) || []).forEach(function (note) {
      body.appendChild(el('p', { class: 'hint', text: note }));
    });

    if (ops.length) {
      var list = el('ol', { class: 'mt-ops' });
      ops.forEach(function (op, i) { list.appendChild(opRow(part, op, i)); });
      body.appendChild(list);
    } else if (part.features) {
      body.appendChild(el('p', { class: 'hint', text:
        'No operations yet. "Set up this part" builds the whole plan from its geometry.' }));
    }

    body.appendChild(el('div', { class: 'mt-part-actions' }, [
      el('button', { type: 'button', class: 'btn small', text: '+ Operation',
                     onclick: function () { ops.push(newOp('holes')); api.render(); touch(); } }),
    ]));

    var changes = 0;
    ops.forEach(function (op, i) { if (i && op.tool_slot !== ops[i - 1].tool_slot) changes++; });
    if (changes > 1) {
      body.appendChild(el('p', { class: 'hint', text:
        'This order needs ' + changes + ' tool changes on this part. Grouping operations '
        + 'that share a tool cuts that down, as long as the order still makes sense.' }));
    }
    return body;
  }

  /* Focus survival across a re-render.
   *
   * api.render rebuilds #mt-tools-body, #mt-runorder and #mt-parts from scratch with
   * innerHTML = '', which destroys whatever node the caret was in. Any keystroke that
   * calls touch() re-renders, so without this a user typing a tool diameter loses focus
   * (and their caret position) after every single character.
   *
   * The generated controls carry no ids of their own, so the key is structural: the
   * index path from the rebuilt container down to the focused node. Same data renders
   * the same tree, so the path finds the same control again. If the tree HAS changed
   * shape - a row added or removed - the path simply misses and focus falls back to the
   * document, which is the honest outcome rather than focusing some unrelated field.
   */
  var FOCUS_ROOTS = ['mt-tools-body', 'mt-runorder', 'mt-parts'];

  function focusKey(node) {
    if (!node || node === document.body) return null;
    var path = [];
    var cur = node;
    while (cur && cur.parentNode) {
      if (cur.id && FOCUS_ROOTS.indexOf(cur.id) >= 0) {
        var key = { root: cur.id, path: path };
        // Caret position matters as much as focus itself: restoring focus to the start
        // of a partly-typed number would have the next keystroke land in the wrong place.
        if (typeof node.selectionStart === 'number') {
          try {
            key.start = node.selectionStart;
            key.end = node.selectionEnd;
          } catch (e) { /* selection unsupported on this input type - focus alone is fine */ }
        }
        return key;
      }
      path.unshift(Array.prototype.indexOf.call(cur.parentNode.childNodes, cur));
      cur = cur.parentNode;
    }
    return null;      // focus was outside the editor; nothing for us to restore
  }

  function restoreFocus(key) {
    if (!key) return;
    var node = document.getElementById(key.root);
    for (var i = 0; node && i < key.path.length; i++) {
      node = node.childNodes[key.path[i]];
    }
    if (!node || typeof node.focus !== 'function') return;
    node.focus();
    if (typeof key.start === 'number' && typeof node.setSelectionRange === 'function') {
      try { node.setSelectionRange(key.start, key.end); } catch (e) { /* not a text input */ }
    }
  }

  api.render = function () {
    var host = $('#mt-tools-body');
    if (!host) return;
    var focused = focusKey(document.activeElement);
    var scroller = $('#mt-parts');
    var scrollTop = scroller ? scroller.scrollTop : 0;
    host.innerHTML = '';
    tools().forEach(function (t, i) { host.appendChild(toolRow(t, i)); });

    var preset = $('#mt-tool-preset');
    if (preset) {
      // Rebuilt every render, not just when empty: saving a bit has to show up here
      // immediately, or the list is out of date the moment it becomes useful.
      var lib = library(), chosen = preset.value;
      preset.innerHTML = '';
      var groups = [['team', 'Saved by your team'], ['builtin', 'Built in']];
      groups.forEach(function (pair) {
        var keys = Object.keys(lib).filter(function (k) {
          return (lib[k].source === 'team') === (pair[0] === 'team');
        });
        if (!keys.length) return;
        var group = el('optgroup', { label: pair[1] });
        if (ctx.cfg && ctx.cfg.sortBits) keys = ctx.cfg.sortBits(lib, keys);
        keys.forEach(function (key) {
          var size = lib[key].diameter_text
                     || (Math.round((lib[key].diameter || 0) * 10000) / 10000) + '"';
          group.appendChild(el('option', { value: key,
                                           text: lib[key].name + '  \u00b7  ' + size }));
        });
        preset.appendChild(group);
      });
      if (chosen && lib[chosen]) preset.value = chosen;
    }

    var order = $('#mt-runorder');
    if (order) {
      order.innerHTML = '';
      var panel = runOrderPanel();
      if (panel) order.appendChild(panel);
    }

    var parts = $('#mt-parts');
    if (!parts) return;
    parts.innerHTML = '';
    if (!ctx.state.parts.length) {
      parts.appendChild(el('p', { class: 'hint', text: 'Add a part on the Parts step first.' }));
    } else {
      ctx.state.parts.forEach(function (p) { parts.appendChild(partBlock(p)); });
    }

    renderMessages();
    if (scroller) scroller.scrollTop = scrollTop;
    restoreFocus(focused);
  };

  /** Errors block the step; notes are advice and do not. */
  function renderMessages() {
    var box = $('#mt-errors');
    if (!box) return;
    var lines = api.validate().map(function (m) { return '• ' + m; })
      .concat(api.notes().map(function (m) { return '⚠ ' + m; }));
    box.textContent = lines.join('\n');
  }

  function touch() {
    if (ctx.onChange) ctx.onChange();
    renderMessages();
  }

  /* --------------------------------------------------------------- validate */

  /** Advisory notes: shown, but never a reason to block the step. Mirrors the split
   *  the server makes between errors and warnings. */
  api.notes = function () { return api.enabled() ? collect().notes : []; };

  api.validate = function () { return api.enabled() ? collect().msgs : []; };

  function collect() {
    var msgs = [];
    var notes = [];
    var slots = {};
    tools().forEach(function (t) {
      if (!(t.diameter > 0)) msgs.push('T' + t.slot + ' needs a positive diameter.');
      if (slots[t.slot]) msgs.push('Two tools are both called T' + t.slot + '.');
      slots[t.slot] = true;
    });

    ctx.state.parts.forEach(function (part) {
      var ops = opsFor(part);
      if (!ops.length) {
        msgs.push(part.name + ': no operations - it would not be cut at all.');
        return;
      }
      ops.forEach(function (op) {
        if (!toolBySlot(op.tool_slot)) {
          msgs.push(part.name + ': an operation points at a tool that is no longer in the list.');
        }
        if (op.op_type === 'chamfer') {
          var tool = toolBySlot(op.tool_slot);
          var width = num(op.scope.width);
          if (!(op.scope.targets || []).length) {
            msgs.push(part.name + ': the chamfer has no edges selected.');
          }
          if (!(width > 0)) {
            msgs.push(part.name + ': the chamfer needs a width.');
          } else if (tool && width > tool.diameter / 2) {
            msgs.push(part.name + ': a ' + width.toFixed(3) + '" chamfer is wider than T'
                      + tool.slot + ' can cut in one pass (max '
                      + (tool.diameter / 2).toFixed(3) + '").');
          }
          if (tool && tool.type !== 'vbit') {
            msgs.push(part.name + ': the chamfer is assigned to T' + tool.slot
                      + ', which is not a V-tool.');
          }
        }
        // Depth past the stock is a WARNING on the server ("cutting through instead"),
        // so blocking it here left anyone who deliberately typed a through-depth stuck on
        // this step with no way forward.
        if (op.depth !== null && op.depth !== undefined && op.depth >= ctx.state.thickness) {
          notes.push(part.name + ': a depth is past the stock thickness - it will cut '
                     + 'through instead.');
        }
      });

      // Every feature must be cut exactly once - the same rule the server enforces.
      // Counting only "is it covered at least once" missed the double-claim case, so
      // adding a second unscoped Holes operation sailed through here and failed at
      // Preview with "would be cut twice".
      if (part.features) {
        checkCoverage(part, 'hole', part.features.holes || [],
                      ['holes', 'interior'], holeInScope, msgs);
        checkCoverage(part, 'pocket', part.features.pockets || [],
                      ['pockets', 'interior'], pocketInScope, msgs);
        // ...and the outline. Coverage checked holes and pockets only, so a plan whose
        // ONLY operation was a chamfer passed every gate - a program that breaks an
        // edge and never cuts the part free, with Download enabled. A chamfer runs
        // along an edge that some other operation has to make first.
        var cuts = ops.filter(function (o) { return o.op_type !== 'chamfer'; });
        if (part.features.has_perimeter && !cuts.some(function (o) {
              return o.op_type === 'perimeter';
            })) {
          msgs.push(part.name + ': nothing cuts the outline - the part would stay '
                    + 'attached to the sheet. Add a Perimeter operation.');
        }
        if (!cuts.length) {
          msgs.push(part.name + ': a chamfer is the only operation - there would be '
                    + 'nothing for it to break the edge of.');
        }
      }
    });
    return { msgs: msgs, notes: notes };
  }

  function holeInScope(feature, op) {
    if (op.scope.indices) return op.scope.indices.indexOf(feature.index) >= 0;
    var lo = num(op.scope.min_diameter), hi = num(op.scope.max_diameter);
    return (lo === null || feature.diameter >= lo - 1e-4)
        && (hi === null || feature.diameter <= hi + 1e-4);
  }

  function pocketInScope(feature, op) {
    if (op.scope.indices) return op.scope.indices.indexOf(feature.index) >= 0;
    var lo = num(op.scope.min_area), hi = num(op.scope.max_area);
    return (lo === null || feature.area >= lo - 1e-4)
        && (hi === null || feature.area <= hi + 1e-4);
  }

  function checkCoverage(part, kind, features, opTypes, inScope, msgs) {
    if (!features.length) return;
    var relevant = opsFor(part).filter(function (o) { return opTypes.indexOf(o.op_type) >= 0; });
    var uncovered = 0, doubled = 0;
    features.forEach(function (f) {
      var hits = relevant.filter(function (op) { return inScope(f, op); }).length;
      if (hits === 0) uncovered++;
      if (hits > 1) doubled++;
    });
    if (uncovered) {
      msgs.push(part.name + ': ' + uncovered + ' of ' + features.length + ' ' + kind
                + 's are not cut by any operation.');
    }
    if (doubled) {
      msgs.push(part.name + ': ' + doubled + ' ' + kind
                + 's are claimed by two operations and would be cut twice.');
    }
  }

  /* ----------------------------------------------------------------- submit */

  /** Tools an operation actually references, matching the server's `used_tools`. */
  function usedTools() {
    var slots = {};
    ctx.state.parts.forEach(function (p) {
      opsFor(p).forEach(function (o) { slots[o.tool_slot] = true; });
    });
    return tools().filter(function (t) { return slots[t.slot]; });
  }

  api.usedToolCount = function () {
    return api.enabled() ? usedTools().length : 0;
  };

  /** Widest tool the job will cut with - the clearance the server enforces between
   *  parts, since a wide V-tool reaches further into a neighbour than the profile
   *  cutter does. Returns 0 when there is nothing to report. */
  api.widestUsedDiameter = function () {
    if (!api.enabled()) return 0;
    return usedTools().reduce(function (m, t) { return Math.max(m, t.diameter || 0); }, 0);
  };

  api.toolsPayload = function () {
    return tools().map(function (t) {
      return { slot: t.slot, name: t.name, diameter: t.diameter, flutes: t.flutes,
               type: t.type, included_angle: t.included_angle };
    });
  };

  /** Build the multipart body for /process-multitool. `placements` comes from wizard.js,
   *  which owns the layout canvas and therefore where each part sits on the stock. */
  /** The Setup step's deburr checkbox, realized as multi-tool operations: make sure a
   *  matching V-bit is in the tool table and every part's plan ends with a chamfer op.
   *  Idempotent - parts that already have a chamfer op (auto or hand-made) are left
   *  alone, so re-running after adding a part only fills the gap. */
  /* Mirror the Setup panel's deburr settings into the operation plan.
   *
   * Re-entrant on purpose: this runs again every time one of those settings changes, so
   * it UPDATES what it added last time rather than bailing out because a chamfer op
   * already exists. Bailing meant the V-bit and width in the plan were whatever the box
   * was first ticked with - Setup could read 1/4" 60 deg 0.05" while the program was cut
   * with 1/2" 90 deg 0.02", and generating pushed a third, unreferenced V-bit into the
   * tool table because the diameter no longer matched.
   *
   * Anything the user authored by hand (no _deburr flag) is left alone. */
  api.applyDeburr = function (chamfer) {
    if (!api.enabled()) return;
    var vbit = tools().filter(function (t) { return t._deburr; })[0]
      || tools().filter(function (t) {
           return t.type === 'vbit' && Math.abs((t.diameter || 0) - chamfer.bit) < 5e-4
                  && Math.abs((t.included_angle || 90) - chamfer.angle) < 0.5;
         })[0];
    if (!vbit) {
      vbit = { slot: nextSlot(), _deburr: true };
      tools().push(vbit);
    }
    if (vbit._deburr) {   // ours to keep in step; a hand-added V-bit is not
      vbit.name = chamfer.angle + ' deg V-bit';
      vbit.diameter = chamfer.bit;
      vbit.diameter_text = chamfer.bit_text || String(chamfer.bit);
      vbit.flutes = vbit.flutes || 2;
      vbit.type = 'vbit';
      vbit.included_angle = chamfer.angle;
    }
    var targets = [];
    if (chamfer.perimeter) targets.push('perimeter');
    if (chamfer.holes) targets.push('holes');
    if (chamfer.pockets) targets.push('pockets');
    ctx.state.parts.forEach(function (part) {
      var ops = opsFor(part);
      var mine = ops.filter(function (o) { return o.op_type === 'chamfer' && o._deburr; })[0];
      if (mine) {
        mine.tool_slot = vbit.slot;
        mine.scope = { targets: targets.slice(), width: chamfer.width };
        return;
      }
      if (ops.some(function (o) { return o.op_type === 'chamfer'; })) return;  // hand-authored
      ops.push({ op_type: 'chamfer', tool_slot: vbit.slot, name: 'Edge break',
                 depth: null, scope: { targets: targets.slice(), width: chamfer.width },
                 _deburr: true });
    });
    api.render();
    touch();
  };

  /** Undo of applyDeburr: removes only what it added (flagged _deburr), so chamfer
   *  operations and V-bits the user authored by hand survive unticking the box. */
  api.clearDeburr = function () {
    if (!ctx) return;
    ctx.state.parts.forEach(function (part) {
      part.ops = opsFor(part).filter(function (o) { return !o._deburr; });
    });
    var stillUsed = ctx.state.parts.some(function (p) {
      return opsFor(p).some(function (o) {
        var t = toolBySlot(o.tool_slot);
        return t && t._deburr;
      });
    });
    if (!stillUsed) ctx.state.tools = tools().filter(function (t) { return !t._deburr; });
    if (!tools().length) ctx.state.tools = defaultTools();
    api.render();
    touch();
  };

  api.buildFormData = function (placements, jobName, timestamp, stock) {
    var fd = new FormData();
    var job = {
      material: ctx.state.material,
      thickness: ctx.state.thickness,
      machine_id: ctx.state.machine_id,
      tab_spacing: ctx.state.tab_spacing,
      name: jobName,
      tools: api.toolsPayload(),
      parts: [],
    };
    if (ctx.state.max_pass_depth) job.max_pass_depth = ctx.state.max_pass_depth;
    // Job-wide, not per operation: every tool change re-zeros Z to the same surface.
    job.z_datum = ctx.state.zDatum || 'board';
    if (ctx.state.dryRun) job.dry_run_lift = 2.0;
    // Job-wide like the Z datum. Without this the summary said "names engraved" and
    // the multi-tool program carried no engraving at all.
    if (ctx.state.engrave) job.engrave = true;
    // The sheet the placements are absolute on, so the server checks the parts against
    // the real stock rather than against their own bounding box.
    if (stock) job.stock = stock;
    ctx.state.parts.forEach(function (part, i) {
      var place = placements[i] || { x: 0, y: 0 };
      job.parts.push({
        file_index: i,
        name: part.name,
        place_x: place.x,
        place_y: place.y,
        rotation: part.rotation,
        mirror: !!part.flipped,
        operations: opsFor(part).map(function (op) {
          return { op_type: op.op_type, tool_slot: op.tool_slot, name: op.name || '',
                   depth: op.depth, scope: op.scope || {} };
        }),
      });
      fd.append('file_' + i, part.file, part.name + '.dxf');
    });
    fd.append('job', JSON.stringify(job));
    fd.append('timestamp', timestamp);
    return fd;
  };

  /* ------------------------------------------------------------------- init */

  api.init = function (options) {
    ctx = options;
    if (!ctx.state.tools || !ctx.state.tools.length) ctx.state.tools = defaultTools();

    var addBtn = $('#mt-add-tool');
    if (addBtn) {
      addBtn.addEventListener('click', function () {
        var sel = $('#mt-tool-preset');
        addToolFromPreset(sel ? sel.value : '250_1f');
      });
    }
    api.render();
  };

  window.PCMultiTool = api;
}());
