(function () {
  'use strict';

  var cfg = window.BedLeveling || { machines: {} };
  var form = document.getElementById('level-form');
  var canvas = document.getElementById('level-canvas');
  var error = document.getElementById('level-error');
  var gcode = document.getElementById('level-gcode');
  var download = document.getElementById('level-download');
  var stats = document.getElementById('level-stats');
  var empty = document.getElementById('level-empty');
  var generateButton = form.querySelector('.level-generate');
  var direction = document.getElementById('level-direction');
  var directionHelp = document.getElementById('level-direction-help');
  var feedNote = document.getElementById('level-feed-note');
  var current = null;

  function value(id) { return Number(document.getElementById(id).value); }
  function requestBody() {
    return {
      machine_id: (document.getElementById('level-machine') || {}).value || cfg.machineId,
      width: value('level-width'),
      length: value('level-length'),
      tool_diameter: value('level-tool'),
      material: document.getElementById('level-material').value,
      flutes: value('level-flutes'),
      stepover_percent: value('level-stepover'),
      depth: value('level-depth'),
      safe_z: value('level-safe-z'),
      feed_rate: value('level-feed'),
      plunge_rate: value('level-plunge'),
      spindle_speed: value('level-rpm'),
      raster_direction: document.getElementById('level-direction').value
    };
  }

  function css(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function updateDirectionHelp() {
    var width = value('level-width');
    var length = value('level-length');
    var longAxis = width >= length ? 'X (width)' : 'Y (length)';
    var shortAxis = width >= length ? 'Y (length)' : 'X (width)';
    var axis = direction.value === 'long' ? longAxis : shortAxis;
    directionHelp.textContent = 'Cutting passes will run along ' + axis + '.';
  }

  function draw(result) {
    var box = canvas.getBoundingClientRect();
    if (!box.width || !box.height) return;
    var dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(box.width * dpr);
    canvas.height = Math.round(box.height * dpr);
    var ctx = canvas.getContext('2d');
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, box.width, box.height);

    var pad = 28;
    var width = result.area.width;
    var height = result.area.length;
    var scale = Math.min((box.width - pad * 2) / width, (box.height - pad * 2) / height);
    var ox = (box.width - width * scale) / 2;
    var oy = (box.height + height * scale) / 2;
    function point(p) { return [ox + p[0] * scale, oy - p[1] * scale]; }

    ctx.fillStyle = css('--well');
    ctx.strokeStyle = css('--line-3');
    ctx.lineWidth = 1;
    ctx.fillRect(ox, oy - height * scale, width * scale, height * scale);
    ctx.strokeRect(ox, oy - height * scale, width * scale, height * scale);

    ctx.beginPath();
    result.path.forEach(function (p, index) {
      var q = point(p);
      if (index === 0) ctx.moveTo(q[0], q[1]); else ctx.lineTo(q[0], q[1]);
    });
    ctx.strokeStyle = css('--accent-dim');
    ctx.lineCap = 'butt';
    ctx.lineJoin = 'miter';
    ctx.lineWidth = Math.max(1, result.tool_diameter * scale);
    ctx.stroke();

    ctx.beginPath();
    result.path.forEach(function (p, index) {
      var q = point(p);
      if (index === 0) ctx.moveTo(q[0], q[1]); else ctx.lineTo(q[0], q[1]);
    });
    ctx.strokeStyle = css('--accent');
    ctx.lineWidth = 1.5;
    ctx.stroke();

    ctx.fillStyle = css('--accent');
    ctx.beginPath();
    ctx.arc(ox, oy, 4, 0, Math.PI * 2);
    ctx.fill();
  }

  async function generate(event) {
    if (event) event.preventDefault();
    if (!form.reportValidity()) return;
    error.textContent = '';
    download.disabled = true;
    generateButton.disabled = true;
    generateButton.textContent = 'Building program…';
    try {
      var response = await fetch('/api/bed-leveling', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(requestBody())
      });
      var result;
      try {
        result = await response.json();
      } catch (parseError) {
        throw new Error('The server returned an unreadable response. Please try again.');
      }
      if (!response.ok || !result.success) throw new Error(result.error || 'Could not generate the program.');
      current = result;
      document.getElementById('level-feed').value = result.feeds.feed_rate;
      document.getElementById('level-plunge').value = result.feeds.plunge_rate;
      document.getElementById('level-rpm').value = result.feeds.spindle_speed;
      feedNote.textContent = 'Calculated by the ' + result.feeds.source + ' for a ' +
        result.feeds.flutes + '-flute cutter.';
      feedNote.classList.toggle('has-warning', result.feeds.warnings.length > 0);
      if (result.feeds.warnings.length) {
        feedNote.textContent += ' ' + result.feeds.warnings.join(' ');
      }
      gcode.value = result.gcode;
      stats.textContent = result.stats.passes + ' passes · along ' +
        result.stats.pass_axis + ' · ' + result.stats.actual_stepover +
        ' in · about ' + result.stats.estimated_minutes + ' min';
      stats.title = result.stats.cutting_distance + ' inches of cutting motion';
      empty.hidden = true;
      download.disabled = false;
      draw(result);
    } catch (err) {
      current = null;
      error.textContent = err.message;
      stats.textContent = '';
      gcode.value = '';
      empty.hidden = false;
      var context = canvas.getContext('2d');
      context.clearRect(0, 0, canvas.width, canvas.height);
    } finally {
      generateButton.disabled = false;
      generateButton.textContent = 'Update preview & program';
    }
  }

  download.addEventListener('click', function () {
    if (!current) return;
    var url = URL.createObjectURL(new Blob([current.gcode], { type: 'text/plain' }));
    var link = document.createElement('a');
    link.href = url;
    link.download = current.filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    window.setTimeout(function () { URL.revokeObjectURL(url); }, 0);
  });

  var machine = document.getElementById('level-machine');
  if (machine) machine.addEventListener('change', async function () {
    var selected = cfg.machines[machine.value];
    if (!selected) return;
    var requestedMachine = machine.value;
    machine.disabled = true;
    try {
      var response = await fetch('/set-machine', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ machine_id: requestedMachine })
      });
      if (!response.ok) throw new Error('The machine could not be changed.');
      cfg.machineId = requestedMachine;
      var width = document.getElementById('level-width');
      var length = document.getElementById('level-length');
      width.value = selected.x_max;
      width.max = selected.x_max;
      length.value = selected.y_max;
      length.max = selected.y_max;
      document.getElementById('level-safe-z').max = selected.z_max;
      var defaults = selected.bed_leveling || {};
      var materialSelect = document.getElementById('level-material');
      materialSelect.textContent = '';
      (selected.materials || []).forEach(function (material) {
        var option = document.createElement('option');
        option.value = material.id;
        option.textContent = material.name;
        option.selected = material.id === selected.default_material;
        materialSelect.appendChild(option);
      });
      [
        ['level-tool', 'tool_diameter'],
        ['level-stepover', 'stepover_percent'],
        ['level-depth', 'depth'],
        ['level-safe-z', 'safe_z'],
        ['level-feed', 'feed_rate'],
        ['level-plunge', 'plunge_rate'],
        ['level-rpm', 'spindle_speed']
      ].forEach(function (fields) {
        if (defaults[fields[1]] !== undefined && defaults[fields[1]] !== null) {
          document.getElementById(fields[0]).value = defaults[fields[1]];
        }
      });
      if (defaults.flutes !== undefined && defaults.flutes !== null) {
        document.getElementById('level-flutes').value = defaults.flutes;
      }
      updateDirectionHelp();
      generate();
    } catch (ignored) {
      machine.value = cfg.machineId;
      error.textContent = 'The machine could not be changed.';
    } finally {
      machine.disabled = false;
    }
  });

  ['level-width', 'level-length', 'level-direction'].forEach(function (id) {
    document.getElementById(id).addEventListener('input', updateDirectionHelp);
  });
  form.addEventListener('submit', generate);
  window.addEventListener('resize', function () { if (current) draw(current); });
  updateDirectionHelp();
  generate();
}());
