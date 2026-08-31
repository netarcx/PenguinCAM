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
  var current = null;

  function value(id) { return Number(document.getElementById(id).value); }
  function requestBody() {
    return {
      machine_id: (document.getElementById('level-machine') || {}).value || cfg.machineId,
      width: value('level-width'),
      height: value('level-height'),
      tool_diameter: value('level-tool'),
      stepover_percent: value('level-stepover'),
      depth: value('level-depth'),
      safe_z: value('level-safe-z'),
      feed_rate: value('level-feed'),
      plunge_rate: value('level-plunge'),
      spindle_speed: value('level-rpm')
    };
  }

  function css(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
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
    var height = result.area.height;
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
    error.textContent = '';
    download.disabled = true;
    try {
      var response = await fetch('/api/bed-leveling', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(requestBody())
      });
      var result = await response.json();
      if (!response.ok || !result.success) throw new Error(result.error || 'Could not generate the program.');
      current = result;
      gcode.value = result.gcode;
      stats.textContent = result.stats.rows + ' rows · ' + result.stats.cutting_distance +
        ' in · about ' + result.stats.estimated_minutes + ' min';
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
    URL.revokeObjectURL(url);
  });

  var machine = document.getElementById('level-machine');
  if (machine) machine.addEventListener('change', async function () {
    var selected = cfg.machines[machine.value];
    if (!selected) return;
    var width = document.getElementById('level-width');
    var height = document.getElementById('level-height');
    width.value = selected.x_max;
    width.max = selected.x_max;
    height.value = selected.y_max;
    height.max = selected.y_max;
    document.getElementById('level-safe-z').max = selected.z_max;
    var defaults = selected.bed_leveling || {};
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
    try {
      var response = await fetch('/set-machine', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ machine_id: machine.value })
      });
      if (!response.ok) throw new Error('The machine could not be changed.');
      cfg.machineId = machine.value;
      generate();
    } catch (ignored) {
      error.textContent = 'The machine could not be changed.';
    }
  });

  form.addEventListener('submit', generate);
  window.addEventListener('resize', function () { if (current) draw(current); });
  generate();
}());
