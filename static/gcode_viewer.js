/* Self-contained 3D G-code toolpath viewer with playback.
 *
 * Arc tessellation, coordinate mapping gcode (x,y,z) -> THREE (x, z, -y) with
 * gcode-Z as the up axis, stock box, tool, green-completed / gold-upcoming
 * toolpath, scrubber + playback, packaged as a reusable module with no coupling
 * to a specific page. The wizard uses it.
 *
 * Usage:
 *   var v = new GcodeViewer({ canvas, container, scrubber, scrubberLabel,
 *                             scrubberOp, playButton, restartButton, speedSelect,
 *                             resetButton, emptyState });
 *   v.load(gcodeText, { stockWidth, stockHeight, stockDepth, toolDiameter });
 * Requires THREE and THREE.OrbitControls to be loaded first.
 */
(function () {
  'use strict';

  function GcodeViewer(els) {
    this.els = els;
    this.moves = [];
    this.completedLine = null;
    this.upcomingLine = null;
    this.toolMesh = null;
    this.stockHeight = 0;
    this.isPlaying = false;
    this.interval = null;
    this.speed = 40;
    this._initScene();
    this._bindControls();
  }

  GcodeViewer.prototype._initScene = function () {
    var canvas = this.els.canvas;
    var container = this.els.container || canvas.parentElement;
    this.container = container;

    var w = container.clientWidth || 600;
    var h = container.clientHeight || 400;

    this.scene = new THREE.Scene();
    this.scene.background = new THREE.Color(this._themeColors().bg);

    this.camera = new THREE.PerspectiveCamera(45, w / h, 0.1, 1000);
    this.camera.position.set(10, 10, 10);
    this.camera.lookAt(0, 0, 0);

    this.renderer = new THREE.WebGLRenderer({ canvas: canvas, antialias: true });
    this.renderer.setSize(w, h);
    this.renderer.setPixelRatio(window.devicePixelRatio);

    this.scene.add(new THREE.AmbientLight(0xffffff, 0.6));
    var dir = new THREE.DirectionalLight(0xffffff, 0.8);
    dir.position.set(5, 10, 7.5);
    this.scene.add(dir);

    this.controls = new THREE.OrbitControls(this.camera, this.renderer.domElement);
    this.controls.target.set(0, 0, 0);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.1;
    this.controls.screenSpacePanning = false;
    this.controls.minDistance = 1;
    this.controls.maxDistance = 500;
    this.controls.maxPolarAngle = Math.PI;
    this.controls.mouseButtons = { LEFT: THREE.MOUSE.ROTATE, MIDDLE: THREE.MOUSE.PAN, RIGHT: null };
    this.controls.update();

    this.optimalCam = { x: 10, y: 10, z: 10 };
    this.optimalLook = { x: 0, y: 0, z: 0 };

    var self = this;
    (function animate() {
      requestAnimationFrame(animate);
      if (self.controls) self.controls.update();
      self.renderer.render(self.scene, self.camera);
    })();

    window.addEventListener('resize', function () { self._resize(); });
  };

  GcodeViewer.prototype._resize = function () {
    var w = this.container.clientWidth || 600;
    var h = this.container.clientHeight || 400;
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
    this.renderer.setSize(w, h);
  };

  // Background/grid colors that follow the page theme (light/dark).
  GcodeViewer.prototype._themeColors = function () {
    var light = document.documentElement.getAttribute('data-theme') === 'light';
    return light ? { bg: 0xf2f4f7, grid1: 0xc7cfd8, grid2: 0xdde3ea }
                 : { bg: 0x0a0e14, grid1: 0x30363d, grid2: 0x1e2632 };
  };

  GcodeViewer.prototype.setTheme = function () {
    if (this.scene) this.scene.background = new THREE.Color(this._themeColors().bg);
  };

  GcodeViewer.prototype._bindControls = function () {
    var self = this, e = this.els;
    if (e.scrubber) e.scrubber.addEventListener('input', function (ev) { self._update(parseInt(ev.target.value, 10) || 0); });
    if (e.playButton) e.playButton.addEventListener('click', function () { self.isPlaying ? self._stop() : self._start(); });
    if (e.restartButton) e.restartButton.addEventListener('click', function () {
      if (e.scrubber) e.scrubber.value = 0;
      self._update(0);
      if (self.isPlaying) { self._stop(); setTimeout(function () { self._start(); }, 100); }
    });
    if (e.speedSelect) e.speedSelect.addEventListener('change', function (ev) {
      self.speed = parseInt(ev.target.value, 10) || 40;
      if (self.isPlaying) { self._stop(); self._start(); }
    });
    if (e.resetButton) e.resetButton.addEventListener('click', function () { self.resetView(); });
  };

  /* ----------------------------------------------------------- parsing */
  GcodeViewer.prototype._parse = function (gcode) {
    var lines = gcode.split('\n');
    var moves = [];
    var cx = 0, cy = 0, cz = 0;
    var b = { minX: Infinity, maxX: -Infinity, minY: Infinity, maxY: -Infinity, minZ: Infinity, maxZ: -Infinity };
    function track(x, y, z) {
      if (x < b.minX) b.minX = x; if (x > b.maxX) b.maxX = x;
      if (y < b.minY) b.minY = y; if (y > b.maxY) b.maxY = y;
      if (z < b.minZ) b.minZ = z; if (z > b.maxZ) b.maxZ = z;
    }
    for (var li = 0; li < lines.length; li++) {
      var t = lines[li].trim();
      if (!t || t.charAt(0) === '(' || t.charAt(0) === ';') continue;
      var gm = t.match(/^(G[0-3])\b/);
      if (!gm) continue;
      var type = gm[1];
      var xm = t.match(/X(-?[\d.]+)/), ym = t.match(/Y(-?[\d.]+)/), zm = t.match(/Z(-?[\d.]+)/);
      var nx = xm ? parseFloat(xm[1]) : cx, ny = ym ? parseFloat(ym[1]) : cy, nz = zm ? parseFloat(zm[1]) : cz;

      if (type === 'G2' || type === 'G3') {
        var im = t.match(/I(-?[\d.]+)/), jm = t.match(/J(-?[\d.]+)/);
        if (im && jm) {
          var ai = parseFloat(im[1]), aj = parseFloat(jm[1]);
          var ccx = cx + ai, ccy = cy + aj;
          var sa = Math.atan2(cy - ccy, cx - ccx), ea = Math.atan2(ny - ccy, nx - ccx);
          var r = Math.sqrt(ai * ai + aj * aj);
          var sweep = ea - sa, cw = type === 'G2';
          if (cw) { if (sweep > 0) sweep -= 2 * Math.PI; if (Math.abs(sweep) < 0.001) sweep = -2 * Math.PI; }
          else { if (sweep < 0) sweep += 2 * Math.PI; if (Math.abs(sweep) < 0.001) sweep = 2 * Math.PI; }
          if (isNaN(r) || r <= 0 || isNaN(sweep)) continue;
          var sx = cx, sy = cy, sz = cz;
          var segs = Math.max(8, Math.ceil(Math.abs(sweep) * r * 10));
          var zStep = (nz - sz) / segs;
          for (var s = 0; s < segs; s++) {
            var tt = (s + 1) / segs;
            var ang = sa + sweep * tt;
            var px = ccx + r * Math.cos(ang), py = ccy + r * Math.sin(ang), pz = sz + zStep * (s + 1);
            if (isNaN(px) || isNaN(py) || isNaN(pz)) continue;
            moves.push({ type: type, from: { x: cx, y: cy, z: cz }, to: { x: px, y: py, z: pz }, line: t });
            cx = px; cy = py; cz = pz; track(cx, cy, cz);
          }
          continue;
        }
      }
      if (nx !== cx || ny !== cy || nz !== cz) {
        moves.push({ type: type, from: { x: cx, y: cy, z: cz }, to: { x: nx, y: ny, z: nz }, line: t });
        cx = nx; cy = ny; cz = nz; track(cx, cy, cz);
      }
    }
    return { moves: moves, bounds: b };
  };

  /* -------------------------------------------------------------- load */
  /* ---------------------------------------------------------------- tube CAD ----
   * A solid model of the tube with the pattern actually cut into it, rather than the
   * translucent stock box the plate view uses.
   *
   * The machined wall is built as a THREE.Shape - the face rectangle - with every hole
   * and every pocket added as a Path hole, then extruded to the wall thickness. That is
   * a genuine cut-out, not a decal: you can see through the holes and read the truss.
   * The remaining three walls are plain boxes, which is enough to make it read as a tube
   * and costs nothing to build.
   *
   * Shape coordinates are the pattern's own frame (u = across the face, v = along the
   * tube). rotateX(-90 deg) maps local (u, v, w) to (u, w, -v), which is exactly the
   * gcode-to-scene mapping the rest of this viewer uses, so the model lands on the
   * toolpath without a second convention to keep straight.
   */
  GcodeViewer.prototype._buildTube = function (tube) {
    var W = tube.face_width, L = tube.length, H = tube.height;
    var wall = Math.min(tube.wall || 0.0625, H / 2);
    var group = new THREE.Group();

    var face = new THREE.Shape();
    face.moveTo(0, 0); face.lineTo(W, 0); face.lineTo(W, L); face.lineTo(0, L);
    face.lineTo(0, 0);

    (tube.holes || []).forEach(function (h) {
      var path = new THREE.Path();
      path.absarc(h.x, h.y, h.d / 2, 0, Math.PI * 2, true);
      face.holes.push(path);
    });
    (tube.pockets || []).forEach(function (ring) {
      if (!ring || ring.length < 3) return;
      var path = new THREE.Path();
      path.moveTo(ring[0][0], ring[0][1]);
      for (var i = 1; i < ring.length; i++) path.lineTo(ring[i][0], ring[i][1]);
      path.lineTo(ring[0][0], ring[0][1]);
      face.holes.push(path);
    });

    var metal = new THREE.MeshStandardMaterial({
      color: 0xb9c4d4, metalness: 0.65, roughness: 0.35, side: THREE.DoubleSide
    });

    var top = new THREE.Mesh(
      new THREE.ExtrudeGeometry(face, { depth: wall, bevelEnabled: false }), metal);
    top.rotation.x = -Math.PI / 2;
    top.position.y = H - wall;      // sit the machined wall at the top of the tube
    group.add(top);

    // The other three walls. Plain boxes: nothing is cut in them, and drawing them keeps
    // the section reading as a tube rather than a plate floating in space.
    function slab(w, h, d, x, y, z) {
      var m = new THREE.Mesh(new THREE.BoxGeometry(w, h, d), metal);
      m.position.set(x, y, z);
      return m;
    }
    group.add(slab(W, wall, L, W / 2, wall / 2, -L / 2));                       // bottom
    group.add(slab(wall, H - 2 * wall, L, wall / 2, H / 2, -L / 2));            // left
    group.add(slab(wall, H - 2 * wall, L, W - wall / 2, H / 2, -L / 2));        // right

    // A soft outline makes the edges legible against the toolpath colours.
    var edges = new THREE.LineSegments(
      new THREE.EdgesGeometry(new THREE.BoxGeometry(W, H, L)),
      new THREE.LineBasicMaterial({ color: 0x8fa3bf, transparent: true, opacity: 0.5 }));
    edges.position.set(W / 2, H / 2, -L / 2);
    group.add(edges);
    return group;
  };

  GcodeViewer.prototype.load = function (gcode, opts) {
    opts = opts || {};
    var parsed = this._parse(gcode);
    this.moves = parsed.moves;
    if (!this.moves.length) return;
    var b = parsed.bounds;
    var maxX = b.maxX, maxY = b.maxY, maxZ = b.maxZ, minX = b.minX, minY = b.minY;

    // Clear everything except lights.
    var keep = [];
    this.scene.children.forEach(function (c) { if (c instanceof THREE.Light) keep.push(c); });
    while (this.scene.children.length) this.scene.remove(this.scene.children[0]);
    // NOT forEach(this.scene.add, this.scene): Object3D.add is variadic, so forEach's
    // index and array arguments were passed in as extra children and three.js logged
    // "object not an instance of THREE.Object3D" on every single load. Harmless, but it
    // buried real errors in the console.
    keep.forEach(function (c) { this.scene.add(c); }, this);
    this.completedLine = this.upcomingLine = this.toolMesh = null;

    var tc = this._themeColors();
    this.scene.background = new THREE.Color(tc.bg);
    var maxDim = Math.max(maxX, maxY, maxZ);
    var gridSize = Math.max(maxX * 1.3, maxY * 1.3, 15);
    var grid = new THREE.GridHelper(gridSize, Math.ceil(gridSize), tc.grid1, tc.grid2);
    grid.position.set(gridSize / 3, 0, -gridSize / 3);
    if (opts.tube && opts.tube.length) {
      // Centre the grid under the tube; the default placement puts most of it off to one
      // side, which is what made the part look lost rather than sitting on a table.
      grid.position.set(opts.tube.face_width / 2, 0, -opts.tube.length / 2);
    }
    this.scene.add(grid);
    this._addAxes(Math.max(maxDim, 5) * 0.6);
    var ms = Math.max(0.15, maxDim * 0.02);
    this.scene.add(new THREE.Mesh(new THREE.SphereGeometry(ms, 16, 16), new THREE.MeshBasicMaterial({ color: 0xffffff })));

    // Stock box. Prefer explicit stock dims (multi-part sheet); fall back to toolpath extents.
    var stockW = opts.stockWidth || (maxX - minX);
    var stockD = opts.stockDepth || (maxY - minY);
    var stockH = opts.stockHeight || 0.25;
    this.stockHeight = stockH;
    if (opts.tube && opts.tube.face_width && opts.tube.length) {
      // A real model of the part, not a box standing in for it.
      this.stockHeight = opts.tube.height;
      this.scene.add(this._buildTube(opts.tube));
    } else {
      var stockGeo = new THREE.BoxGeometry(stockW, stockH, stockD);
      var stockMat = new THREE.MeshStandardMaterial({
        color: 0xe8f0ff, transparent: true, opacity: 0.15,
        metalness: 0.3, roughness: 0.7, side: THREE.DoubleSide, depthWrite: false
      });
      var stockMesh = new THREE.Mesh(stockGeo, stockMat);
      stockMesh.position.set(stockW / 2, stockH / 2, -stockD / 2);
      stockMesh.renderOrder = -1;
      this.scene.add(stockMesh);
    }

    var toolDia = opts.toolDiameter || 0.157;
    var toolLen = Math.max(maxZ * 1.5, 1.0);
    this.toolMesh = new THREE.Mesh(
      new THREE.CylinderGeometry(toolDia / 2, toolDia / 2, toolLen, 16),
      new THREE.MeshStandardMaterial({ color: 0xc0c0c0, metalness: 0.8, roughness: 0.2, emissive: 0x404040 })
    );
    this.toolMesh.userData.toolLength = toolLen;
    this.scene.add(this.toolMesh);

    // Scrubber + controls.
    if (this.els.scrubber) { this.els.scrubber.max = this.moves.length - 1; this.els.scrubber.value = 0; }
    if (this.els.scrubberContainer) this.els.scrubberContainer.style.display = 'block';
    if (this.els.playbackControls) this.els.playbackControls.style.display = 'flex';
    if (this.els.emptyState) this.els.emptyState.style.display = 'none';

    this._update(0);

    // Camera.
    if (opts.tube && opts.tube.face_width && opts.tube.length) {
      // A tube is long and thin, and the plate framing below - look at a third of the
      // extents from twice the longest dimension away - leaves it a sliver in the corner
      // of a huge grid. Frame it on its own bounding sphere instead, and look along the
      // length from above so the pattern on the machined face is what you actually see.
      var tw = opts.tube.face_width, tl = opts.tube.length, th = opts.tube.height;
      var cx = tw / 2, cy = th / 2, cz = -tl / 2;
      var radius = Math.sqrt(tw * tw + th * th + tl * tl) / 2;
      var fov = this.camera.fov * Math.PI / 180;
      var dist = (radius / Math.sin(fov / 2)) * 0.62;
      // Off the long axis and well above it: a tube viewed down its own length shows
      // almost nothing of the face that was machined.
      var dir = new THREE.Vector3(0.55, 0.62, -0.56).normalize();
      this.camera.position.set(cx + dir.x * dist, cy + dir.y * dist, cz + dir.z * dist);
      this.optimalCam = { x: this.camera.position.x, y: this.camera.position.y, z: this.camera.position.z };
      this.optimalLook = { x: cx, y: cy, z: cz };
    } else {
    var viewDist = Math.max(maxX, maxY, maxZ) * 2 || 10;
    this.camera.position.set(viewDist * 0.7, viewDist * 0.7, viewDist * 0.7);
    this.optimalCam = { x: this.camera.position.x, y: this.camera.position.y, z: this.camera.position.z };
    this.optimalLook = { x: maxX / 3, y: maxZ / 3, z: -maxY / 3 };
    }
    this.controls.target.set(this.optimalLook.x, this.optimalLook.y, this.optimalLook.z);
    this.controls.update();
    this._resize();
  };

  /* ---------------------------------------------------------- playback */
  GcodeViewer.prototype._update = function (idx) {
    if (!this.moves.length) return;
    var e = this.els;
    var move = this.moves[idx];
    if (e.scrubberLabel) e.scrubberLabel.textContent = 'Move ' + (idx + 1) + ' of ' + this.moves.length;
    if (e.scrubberOp) e.scrubberOp.textContent = (move.type === 'G0' ? 'Rapid' : 'Cut') + ': ' + move.line;

    if (this.toolMesh) {
      var x, y, z;
      if (idx === 0) {
        x = move.from.x; y = move.from.y;
        z = (!move.from.z || move.from.z === 0) ? this.stockHeight + 0.5 : move.from.z;
      } else {
        x = move.to.x; y = move.to.y; z = move.to.z;
      }
      var tl = this.toolMesh.userData.toolLength;
      this.toolMesh.position.set(x, z + tl / 2, -y);
    }

    if (this.completedLine) this.scene.remove(this.completedLine);
    if (this.upcomingLine) this.scene.remove(this.upcomingLine);

    if (idx < this.moves.length - 1) {
      var up = [];
      for (var i = idx; i < this.moves.length; i++) {
        var m = this.moves[i];
        if (i === idx) {
          var fz = (i === 0 && (!m.from.z || m.from.z === 0)) ? this.stockHeight + 0.5 : m.from.z;
          up.push(new THREE.Vector3(m.from.x, fz, -m.from.y));
        }
        up.push(new THREE.Vector3(m.to.x, m.to.z, -m.to.y));
      }
      this.upcomingLine = new THREE.Line(
        new THREE.BufferGeometry().setFromPoints(up),
        new THREE.LineBasicMaterial({ color: 0xfdb515, opacity: 0.8, transparent: true }));
      this.scene.add(this.upcomingLine);
    }

    if (idx > 0) {
      var done = [];
      for (var k = 0; k <= idx; k++) {
        var mm = this.moves[k];
        if (k === 0) {
          var fz2 = (!mm.from.z || mm.from.z === 0) ? this.stockHeight + 0.5 : mm.from.z;
          done.push(new THREE.Vector3(mm.from.x, fz2, -mm.from.y));
        }
        done.push(new THREE.Vector3(mm.to.x, mm.to.z, -mm.to.y));
      }
      this.completedLine = new THREE.Line(
        new THREE.BufferGeometry().setFromPoints(done),
        new THREE.LineBasicMaterial({ color: 0x2ea043 }));
      this.scene.add(this.completedLine);
    }
  };

  GcodeViewer.prototype._start = function () {
    // If playback is parked at the end (it auto-pauses there), pressing play should
    // replay from the beginning rather than sit stuck at the last move.
    var cur = this.els.scrubber ? parseInt(this.els.scrubber.value, 10) : 0;
    if (cur >= this.moves.length - 1) {
      if (this.els.scrubber) this.els.scrubber.value = 0;
      this._update(0);
    }
    this.isPlaying = true;
    if (this.els.playButton) this.els.playButton.classList.add('playing');
    var self = this;
    this.interval = setInterval(function () {
      var cur = self.els.scrubber ? parseInt(self.els.scrubber.value, 10) : 0;
      var max = self.moves.length - 1;
      if (cur >= max) { self._stop(); return; }
      if (self.els.scrubber) self.els.scrubber.value = cur + 1;
      self._update(cur + 1);
    }, 1000 / this.speed);
  };

  GcodeViewer.prototype._stop = function () {
    this.isPlaying = false;
    if (this.els.playButton) this.els.playButton.classList.remove('playing');
    if (this.interval) { clearInterval(this.interval); this.interval = null; }
  };

  GcodeViewer.prototype.resetView = function () {
    if (!this.controls) return;
    this.camera.position.set(this.optimalCam.x, this.optimalCam.y, this.optimalCam.z);
    this.controls.target.set(this.optimalLook.x, this.optimalLook.y, this.optimalLook.z);
    this.controls.update();
  };

  /* ------------------------------------------------------------- axes */
  // A billboard text label (canvas sprite) for an axis tip.
  GcodeViewer.prototype._axisSprite = function (text, hex) {
    var canvas = document.createElement('canvas');
    canvas.width = canvas.height = 64;
    var ctx = canvas.getContext('2d');
    ctx.fillStyle = hex;
    ctx.font = 'bold 48px sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(text, 32, 34);
    var tex = new THREE.CanvasTexture(canvas);
    tex.needsUpdate = true;
    return new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, transparent: true, depthTest: false, depthWrite: false }));
  };

  // Draw labeled machine axes at the origin. The gcode->THREE remap is (x, z, -y), so
  // machine +X -> THREE (1,0,0), +Y -> THREE (0,0,-1), +Z -> THREE (0,1,0). Colors match
  // the 2D layout view: X red, Y green, Z blue. Y therefore points toward the part (into
  // the scene), not toward the camera as the old generic AxesHelper's blue Z did.
  GcodeViewer.prototype._addAxes = function (len) {
    var self = this;
    var lblScale = Math.max(len * 0.18, 0.5);
    [
      { dir: [1, 0, 0], color: 0xff0000, hex: '#ff0000', label: 'X' },
      { dir: [0, 0, -1], color: 0x2ea043, hex: '#2ea043', label: 'Y' },
      { dir: [0, 1, 0], color: 0x2f81f7, hex: '#2f81f7', label: 'Z' },
    ].forEach(function (ax) {
      var end = new THREE.Vector3(ax.dir[0] * len, ax.dir[1] * len, ax.dir[2] * len);
      self.scene.add(new THREE.Line(
        new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(0, 0, 0), end]),
        new THREE.LineBasicMaterial({ color: ax.color })));
      var spr = self._axisSprite(ax.label, ax.hex);
      spr.scale.set(lblScale, lblScale, lblScale);
      spr.position.copy(end.clone().multiplyScalar(1.12));
      self.scene.add(spr);
    });
  };

  window.GcodeViewer = GcodeViewer;
})();
