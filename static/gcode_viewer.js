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

  var MAX_GRID_DIVISIONS = 120;   // 1" squares, until the program is bigger than any bed

  // Every number that reaches the scene goes through this. A single malformed word in a
  // program - "X." parses to NaN - used to put NaN into a BufferGeometry, and three.js
  // then logged "Computed radius is NaN" on every frame and the camera fit came out
  // garbage. Bad input should cost you one move, not the whole preview.
  function num(v, dflt) {
    v = typeof v === 'number' ? v : parseFloat(v);
    return isFinite(v) ? v : dflt;
  }

  function GcodeViewer(els) {
    this.els = els;
    this.moves = [];
    this.index = 0;
    this.completedLine = null;
    this.upcomingLine = null;
    this.toolMesh = null;
    this.tubeGroup = null;
    this.grid = null;
    this.stockHeight = 0;
    this.isPlaying = false;
    this.raf = null;
    this.speed = 40;
    this.emptyText = (els.emptyState && els.emptyState.textContent) || '';
    this._initScene();
    this._bindControls();
    // A handle on the live viewer for the console and for browser tests. The wizard keeps
    // its instance in a closure, so without this there is no way to inspect the scene from
    // outside when something looks wrong.
    GcodeViewer.last = this;
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
    // The canvas is sized by CSS and only its backing store is set from JS (setSize's
    // third argument). Writing a pixel height onto the element instead would feed back
    // into a flex container that sizes itself from its content - the observer below would
    // then see the size it just caused, and the two could chase each other.
    canvas.style.width = '100%';
    canvas.style.height = '100%';
    this.renderer.setSize(w, h, false);
    // Uncapped, a 3x-DPR phone renders nine times the pixels of the CSS box for no visible
    // gain and a very visible frame rate.
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));

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

    // The canvas is width:100%/height:100% in CSS, so anything that changes the panel
    // width without changing the window - a scrollbar appearing, the preview panel being
    // revealed, a container that was display:none when the viewer was built - stretched
    // the WebGL image instead of re-rendering it. A window resize listener never sees any
    // of those.
    window.addEventListener('resize', function () { self._resize(); });
    if (window.ResizeObserver) {
      this.resizeObserver = new ResizeObserver(function () { self._resize(); });
      this.resizeObserver.observe(container);
    }
  };

  GcodeViewer.prototype._resize = function () {
    var w = this.container.clientWidth;
    var h = this.container.clientHeight;
    // A hidden container measures 0x0. Sizing the renderer to a fallback here would bake
    // in the wrong aspect and nothing would correct it once the panel appeared; the
    // ResizeObserver calls back the moment it has a real size.
    if (!w || !h) return;
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
    this.renderer.setSize(w, h, false);
  };

  /* ------------------------------------------------------------ disposal */
  // three.js frees GPU buffers only when you ask it to. Removing an object from the scene
  // detaches it and leaks its geometry, its material and any texture the material holds.
  // The old viewer rebuilt both toolpath polylines on every playback frame and dropped the
  // old ones on the floor: three seconds of playback leaked ~360 buffers, and a full pass
  // of a tube program leaked several thousand.
  GcodeViewer.prototype._dispose = function (obj) {
    if (!obj) return;
    obj.traverse(function (o) {
      if (o.geometry) o.geometry.dispose();
      var mats = o.material ? (Array.isArray(o.material) ? o.material : [o.material]) : [];
      mats.forEach(function (m) {
        if (!m) return;
        if (m.map) m.map.dispose();
        m.dispose();
      });
    });
  };

  GcodeViewer.prototype._remove = function (obj) {
    if (!obj) return;
    if (obj.parent) obj.parent.remove(obj);
    this._dispose(obj);
  };

  // Clear the scene back to its lights.
  GcodeViewer.prototype._clearScene = function () {
    var doomed = [];
    this.scene.children.forEach(function (c) { if (!(c instanceof THREE.Light)) doomed.push(c); });
    for (var i = 0; i < doomed.length; i++) this._remove(doomed[i]);
    this.completedLine = this.upcomingLine = this.toolMesh = null;
    this.tubeGroup = null;   // NOT just detached: a stale handle kept the whole tube's
    this.grid = null;        // geometry alive and let _update keep rotating a dead group.
  };

  // Background/grid colors that follow the page theme (light/dark).
  GcodeViewer.prototype._themeColors = function () {
    var light = document.documentElement.getAttribute('data-theme') === 'light';
    return light ? { bg: 0xf2f4f7, grid1: 0xc7cfd8, grid2: 0xdde3ea }
                 : { bg: 0x0a0e14, grid1: 0x30363d, grid2: 0x1e2632 };
  };

  // GridHelper bakes its two colours into the geometry's vertex colours, so there is no
  // material colour to repaint: the grid has to be rebuilt. Without this, switching to the
  // light theme left near-black gridlines on a near-white background.
  GcodeViewer.prototype._makeGrid = function () {
    if (!this.gridSize) return;
    this._remove(this.grid);
    var tc = this._themeColors();
    var divisions = Math.max(1, Math.min(Math.ceil(this.gridSize), MAX_GRID_DIVISIONS));
    this.grid = new THREE.GridHelper(this.gridSize, divisions, tc.grid1, tc.grid2);
    this.grid.position.copy(this.gridPos);
    this.scene.add(this.grid);
  };

  GcodeViewer.prototype.setTheme = function () {
    if (!this.scene) return;
    this.scene.background = new THREE.Color(this._themeColors().bg);
    this._makeGrid();
  };

  GcodeViewer.prototype._bindControls = function () {
    var self = this, e = this.els;
    if (e.showToolpath) {
      this.toolpathVisible = e.showToolpath.checked !== false;
      e.showToolpath.addEventListener('change', function () { self.setToolpathVisible(this.checked); });
    }
    if (e.scrubber) e.scrubber.addEventListener('input', function (ev) {
      self._setIndex(parseInt(ev.target.value, 10) || 0);
    });
    if (e.playButton) e.playButton.addEventListener('click', function () {
      self.isPlaying ? self._stop() : self._start();
    });
    // Speed is read fresh on every frame and the index lives in one place, so neither of
    // these has to tear down and restart playback the way the old timer did.
    if (e.restartButton) e.restartButton.addEventListener('click', function () { self._setIndex(0); });
    if (e.speedSelect) e.speedSelect.addEventListener('change', function (ev) {
      self.speed = parseInt(ev.target.value, 10) || 40;
    });
    if (e.resetButton) e.resetButton.addEventListener('click', function () { self.resetView(); });
  };

  /* ----------------------------------------------------------- parsing */
  GcodeViewer.prototype._parse = function (gcode) {
    var lines = String(gcode).split('\n');
    var moves = [];
    var cx = 0, cy = 0, cz = 0;
    // Which side of the tube the machine is working on. A tube program cuts face 1,
    // pauses for the operator to flip the tube, then cuts face 2 at the SAME
    // coordinates - the part moved, not the tool. Drawing both literally put two
    // mirrored patterns on top of each other on one wall, which is what made the truss
    // look like overlapping triangles with its mirror image missing.
    var phase = 1;
    var b = { minX: Infinity, maxX: -Infinity, minY: Infinity, maxY: -Infinity, minZ: Infinity, maxZ: -Infinity };
    function track(x, y, z) {
      if (x < b.minX) b.minX = x; if (x > b.maxX) b.maxX = x;
      if (y < b.minY) b.minY = y; if (y > b.maxY) b.maxY = y;
      if (z < b.minZ) b.minZ = z; if (z > b.maxZ) b.maxZ = z;
    }
    for (var li = 0; li < lines.length; li++) {
      var t = lines[li].trim();
      if (t.indexOf('PHASE 2') >= 0) phase = 2;
      if (!t || t.charAt(0) === '(' || t.charAt(0) === ';') continue;
      var gm = t.match(/^(G[0-3])\b/);
      if (!gm) continue;
      var type = gm[1];
      var xm = t.match(/X(-?[\d.]+)/), ym = t.match(/Y(-?[\d.]+)/), zm = t.match(/Z(-?[\d.]+)/);
      // An unparseable word means "unchanged", not NaN: the axis simply was not commanded
      // in a form we understood, and the machine holds its position.
      var nx = xm ? num(xm[1], cx) : cx, ny = ym ? num(ym[1], cy) : cy, nz = zm ? num(zm[1], cz) : cz;

      if (type === 'G2' || type === 'G3') {
        var im = t.match(/I(-?[\d.]+)/), jm = t.match(/J(-?[\d.]+)/);
        if (im && jm) {
          var ai = num(im[1], NaN), aj = num(jm[1], NaN);
          if (isNaN(ai) || isNaN(aj)) continue;
          var ccx = cx + ai, ccy = cy + aj;
          var sa = Math.atan2(cy - ccy, cx - ccx), ea = Math.atan2(ny - ccy, nx - ccx);
          var r = Math.sqrt(ai * ai + aj * aj);
          var sweep = ea - sa, cw = type === 'G2';
          if (cw) { if (sweep > 0) sweep -= 2 * Math.PI; if (Math.abs(sweep) < 0.001) sweep = -2 * Math.PI; }
          else { if (sweep < 0) sweep += 2 * Math.PI; if (Math.abs(sweep) < 0.001) sweep = 2 * Math.PI; }
          if (isNaN(r) || r <= 0 || isNaN(sweep)) continue;
          var sz = cz;
          var segs = Math.max(8, Math.ceil(Math.abs(sweep) * r * 10));
          var zStep = (nz - sz) / segs;
          for (var s = 0; s < segs; s++) {
            var tt = (s + 1) / segs;
            var ang = sa + sweep * tt;
            var px = ccx + r * Math.cos(ang), py = ccy + r * Math.sin(ang), pz = sz + zStep * (s + 1);
            if (!isFinite(px) || !isFinite(py) || !isFinite(pz)) continue;
            moves.push({ type: type, phase: phase, from: { x: cx, y: cy, z: cz }, to: { x: px, y: py, z: pz }, line: t });
            cx = px; cy = py; cz = pz; track(cx, cy, cz);
          }
          continue;
        }
      }
      if (nx !== cx || ny !== cy || nz !== cz) {
        moves.push({ type: type, phase: phase, from: { x: cx, y: cy, z: cz }, to: { x: nx, y: ny, z: nz }, line: t });
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
    var wall = Math.min(num(tube.wall, 0.0625), H / 2);
    var group = new THREE.Group();

    // BOTH machined walls, because both get cut. Face 2 carries the same pattern
    // mirrored in X - the operator flips the tube and the machine repeats the program -
    // so modelling only the near wall left the far half of the truss missing from the
    // part, which is precisely what it looks like on the real tube if you only cut one
    // side.
    function wallShape(mirror) {
      var shape = new THREE.Shape();
      shape.moveTo(0, 0); shape.lineTo(W, 0); shape.lineTo(W, L); shape.lineTo(0, L);
      shape.lineTo(0, 0);
      var mx = function (x) { return mirror ? W - x : x; };
      (tube.holes || []).forEach(function (h) {
        if (!isFinite(h.x) || !isFinite(h.y) || !(h.d > 0)) return;
        var path = new THREE.Path();
        path.absarc(mx(h.x), h.y, h.d / 2, 0, Math.PI * 2, true);
        shape.holes.push(path);
      });
      (tube.pockets || []).forEach(function (ring) {
        if (!ring || ring.length < 3) return;
        var path = new THREE.Path();
        path.moveTo(mx(ring[0][0]), ring[0][1]);
        for (var i = 1; i < ring.length; i++) path.lineTo(mx(ring[i][0]), ring[i][1]);
        path.lineTo(mx(ring[0][0]), ring[0][1]);
        shape.holes.push(path);
      });
      return shape;
    }

    // Two tones. With one material you look down through a cut and see an identically
    // lit surface behind it, so the hole reads as solid metal and the pattern is
    // invisible - the model was correct and looked like an uncut bar. The wall facing
    // away from you is darker, so a cut reads as depth. Which wall that is swaps when
    // the tube is turned over; see _update.
    var brightMetal = new THREE.MeshStandardMaterial({
      color: 0xc3cedd, metalness: 0.6, roughness: 0.38, side: THREE.DoubleSide
    });
    var darkMetal = new THREE.MeshStandardMaterial({
      color: 0x5a6675, metalness: 0.5, roughness: 0.6, side: THREE.DoubleSide
    });

    function machinedWall(mirror, y, material) {
      var mesh = new THREE.Mesh(
        new THREE.ExtrudeGeometry(wallShape(mirror), { depth: wall, bevelEnabled: false }),
        material);
      mesh.rotation.x = -Math.PI / 2;
      mesh.position.y = y;
      return mesh;
    }

    var nearWall = machinedWall(false, H - wall, brightMetal);  // face 1, the near wall
    var farWall = machinedWall(true, 0, darkMetal);             // face 2, mirrored
    group.add(nearWall);
    group.add(farWall);
    group.userData.nearWall = nearWall;
    group.userData.farWall = farWall;

    // The two unmachined side walls. Plain boxes: nothing is cut in them, and drawing
    // them keeps the section reading as a tube rather than two loose plates.
    function slab(w, h, d, x, y, z) {
      var m = new THREE.Mesh(new THREE.BoxGeometry(w, h, d), darkMetal);
      m.position.set(x, y, z);
      return m;
    }
    group.add(slab(wall, H - 2 * wall, L, wall / 2, H / 2, -L / 2));
    group.add(slab(wall, H - 2 * wall, L, W - wall / 2, H / 2, -L / 2));

    // A soft outline makes the edges legible against the toolpath colours.
    var edges = new THREE.LineSegments(
      new THREE.EdgesGeometry(new THREE.BoxGeometry(W, H, L)),
      new THREE.LineBasicMaterial({ color: 0x8fa3bf, transparent: true, opacity: 0.45 }));
    edges.position.set(W / 2, H / 2, -L / 2);
    group.add(edges);

    // Wrap it so the whole tube can be turned over about its own long axis, the way the
    // operator turns it at the pause. Rotating the outer object about Z spins the inner
    // one about the tube's centreline, because the inner one is offset to put that
    // centreline on the origin.
    var pivot = new THREE.Group();
    group.position.set(-W / 2, -H / 2, L / 2);
    pivot.add(group);
    pivot.position.set(W / 2, H / 2, -L / 2);
    pivot.userData.tube = { W: W, H: H, L: L };
    pivot.userData.walls = { near: nearWall, far: farWall, bright: brightMetal, dark: darkMetal };
    pivot.userData.flipped = null;
    return pivot;
  };

  // Show the empty state and put the playback controls away. A program with no moves used
  // to leave the PREVIOUS program's scene on screen, with the scrubber still showing and
  // the label reading "Move 1 of 1" - a stale picture presented as the current one.
  GcodeViewer.prototype._showEmpty = function (message) {
    if (this.els.scrubberContainer) this.els.scrubberContainer.style.display = 'none';
    if (this.els.playbackControls) this.els.playbackControls.style.display = 'none';
    if (this.els.scrubber) { this.els.scrubber.max = 0; this.els.scrubber.value = 0; }
    if (this.els.scrubberLabel) this.els.scrubberLabel.textContent = '';
    if (this.els.scrubberOp) this.els.scrubberOp.textContent = '';
    if (this.els.emptyState) {
      this.els.emptyState.textContent = message || this.emptyText;
      this.els.emptyState.style.display = '';
    }
  };

  /* One vertex per machine position, built once at load. _update then only moves a draw
   * range over it. The old code rebuilt both polylines - up to 4,500 points each - from
   * scratch on every playback frame, which is what made 8x playback fall behind and what
   * leaked the geometries. */
  GcodeViewer.prototype._buildPath = function () {
    var n = this.moves.length;
    var pts = new Float32Array((n + 1) * 3);
    var m0 = this.moves[0];
    // A program that starts at Z0 has not touched the work yet; park the first vertex at
    // the rapid height so the opening move is not drawn through the stock.
    var z0 = (!m0.from.z || m0.from.z === 0) ? this.stockHeight + 0.5 : m0.from.z;
    pts[0] = m0.from.x; pts[1] = z0; pts[2] = -m0.from.y;
    for (var i = 0; i < n; i++) {
      var to = this.moves[i].to;
      pts[(i + 1) * 3] = to.x; pts[(i + 1) * 3 + 1] = to.z; pts[(i + 1) * 3 + 2] = -to.y;
    }
    this.pathPoints = pts;

    // Contiguous runs of one phase. Only the run being cut is drawn: both phases run at
    // the same coordinates - the part moved between them, not the tool - so drawing both
    // at once stacks two mirrored patterns in the same space and reads as nonsense.
    this.runs = [];
    this.runOf = new Int32Array(n);
    var start = 0;
    for (var k = 1; k <= n; k++) {
      if (k === n || (this.moves[k].phase || 1) !== (this.moves[k - 1].phase || 1)) {
        this.runs.push({ a: start, b: k - 1 });
        for (var j = start; j < k; j++) this.runOf[j] = this.runs.length - 1;
        start = k;
      }
    }

    var self = this;
    function line(opts) {
      var geo = new THREE.BufferGeometry();
      geo.setAttribute('position', new THREE.BufferAttribute(pts.slice(), 3));
      geo.setDrawRange(0, 0);
      // The bounding sphere is computed from the whole buffer, not the drawn range, so
      // frustum culling would hide the line whenever the drawn part sat off-centre.
      var l = new THREE.Line(geo, new THREE.LineBasicMaterial(opts));
      l.frustumCulled = false;
      self.scene.add(l);
      return l;
    }
    this.completedLine = line({ color: 0x2ea043 });
    this.upcomingLine = line({ color: 0xfdb515, opacity: 0.8, transparent: true });
  };

  GcodeViewer.prototype.load = function (gcode, opts) {
    opts = opts || {};
    this._stop();   // a regenerated preview must not keep animating the program it replaced
    var parsed = (typeof gcode === 'string' && gcode) ? this._parse(gcode)
                                                     : { moves: [], bounds: null };
    this.moves = parsed.moves;
    this.index = 0;
    this._clearScene();
    this.gridSize = 0;
    if (!this.moves.length) {
      this._showEmpty('No tool moves in this program.');
      return;
    }

    var b = parsed.bounds;
    var maxX = num(b.maxX, 0), maxY = num(b.maxY, 0), maxZ = num(b.maxZ, 0);
    var minX = num(b.minX, 0), minY = num(b.minY, 0);

    // NOTE: phase-2 moves are NOT transformed. The router reaches only the top of the
    // tube; the OPERATOR turns the work over at the M0. Drawing the second face's path
    // under the tube - which an earlier version did, to stop the two phases overlapping -
    // showed the spindle reaching somewhere no router can go. The tube model is rotated
    // instead (see _update), which is what actually happens.

    var tube = opts.tube;
    var isTube = !!(tube && isFinite(tube.face_width) && tube.face_width > 0
                    && isFinite(tube.length) && tube.length > 0 && isFinite(tube.height) && tube.height > 0);

    this.scene.background = new THREE.Color(this._themeColors().bg);
    var maxDim = Math.max(maxX, maxY, maxZ);
    // The floors below (a 15" grid, a 5" axis triad, a 0.15" origin ball) are what keep a
    // normal part from rattling around in an empty scene. On a part smaller than they are
    // they do the opposite: a 0.25 x 0.69" bracket came out as a speck beside a ball twice
    // its width, on a grid twenty times its length. Each floor now gives way once the
    // program is smaller than it, and is untouched for anything bigger.
    this.gridSize = Math.max(maxX * 1.3, maxY * 1.3, Math.min(15, maxDim * 4) || 15);
    // Centre the grid under a tube; the default placement puts most of it off to one
    // side, which is what made the part look lost rather than sitting on a table.
    this.gridPos = isTube ? new THREE.Vector3(tube.face_width / 2, 0, -tube.length / 2)
                          : new THREE.Vector3(this.gridSize / 3, 0, -this.gridSize / 3);
    this._makeGrid();
    this._addAxes(Math.max(maxDim, Math.min(5, maxDim * 3) || 5) * 0.6);
    var ms = Math.max(Math.min(0.15, maxDim * 0.06), maxDim * 0.02) || 0.15;
    this.scene.add(new THREE.Mesh(new THREE.SphereGeometry(ms, 16, 16),
                                  new THREE.MeshBasicMaterial({ color: 0xffffff })));

    // Stock box. Prefer explicit stock dims (multi-part sheet); fall back to toolpath extents.
    var stockW = num(opts.stockWidth, 0) || (maxX - minX) || 1;
    var stockD = num(opts.stockDepth, 0) || (maxY - minY) || 1;
    var stockH = num(opts.stockHeight, 0) || 0.25;
    this.stockHeight = stockH;
    if (isTube) {
      // A real model of the part, not a box standing in for it.
      this.stockHeight = tube.height;
      this.tubeGroup = this._buildTube(tube);
      this.scene.add(this.tubeGroup);
    } else {
      var stockMesh = new THREE.Mesh(
        new THREE.BoxGeometry(stockW, stockH, stockD),
        new THREE.MeshStandardMaterial({
          color: 0xe8f0ff, transparent: true, opacity: 0.15,
          metalness: 0.3, roughness: 0.7, side: THREE.DoubleSide, depthWrite: false
        }));
      stockMesh.position.set(stockW / 2, stockH / 2, -stockD / 2);
      stockMesh.renderOrder = -1;
      this.scene.add(stockMesh);
    }

    var toolDia = num(opts.toolDiameter, 0.157) || 0.157;
    var toolLen = Math.max(maxZ * 1.5, 1.0);
    this.toolMesh = new THREE.Mesh(
      new THREE.CylinderGeometry(toolDia / 2, toolDia / 2, toolLen, 16),
      new THREE.MeshStandardMaterial({ color: 0xc0c0c0, metalness: 0.8, roughness: 0.2, emissive: 0x404040 })
    );
    this.toolMesh.userData.toolLength = toolLen;
    this.scene.add(this.toolMesh);

    this._buildPath();

    // Scrubber + controls. A one-move program still gets them, so the single move can be
    // read; it just has nowhere to scrub to.
    if (this.els.scrubber) { this.els.scrubber.max = this.moves.length - 1; this.els.scrubber.value = 0; }
    if (this.els.scrubberContainer) this.els.scrubberContainer.style.display = 'block';
    if (this.els.playbackControls) this.els.playbackControls.style.display = 'flex';
    if (this.els.emptyState) this.els.emptyState.style.display = 'none';

    this._update(0);

    // Camera.
    if (isTube) {
      // A tube is long and thin, and the plate framing below - look at a third of the
      // extents from twice the longest dimension away - leaves it a sliver in the corner
      // of a huge grid. Frame it on its own bounding sphere instead, and look along the
      // length from above so the pattern on the machined face is what you actually see.
      var tw = tube.face_width, tl = tube.length, th = tube.height;
      var cx = tw / 2, cy = th / 2, cz = -tl / 2;
      var radius = Math.sqrt(tw * tw + th * th + tl * tl) / 2;
      var fov = this.camera.fov * Math.PI / 180;
      var dist = (radius / Math.sin(fov / 2)) * 0.62;
      // Off the long axis and well above it: a tube viewed down its own length shows
      // almost nothing of the face that was machined.
      var dir = new THREE.Vector3(0.55, 0.62, -0.56).normalize();
      this.camera.position.set(cx + dir.x * dist, cy + dir.y * dist, cz + dir.z * dist);
      this.optimalLook = { x: cx, y: cy, z: cz };
    } else {
      var viewDist = Math.max(maxX, maxY, maxZ) * 2 || 10;
      this.camera.position.set(viewDist * 0.7, viewDist * 0.7, viewDist * 0.7);
      this.optimalLook = { x: maxX / 3, y: maxZ / 3, z: -maxY / 3 };
    }
    this.optimalCam = { x: this.camera.position.x, y: this.camera.position.y, z: this.camera.position.z };
    this.controls.target.set(this.optimalLook.x, this.optimalLook.y, this.optimalLook.z);
    this.controls.update();
    this._resize();
  };

  /* ---------------------------------------------------------- playback */
  // The one place the playback position is written. Keeping the scrubber in sync here
  // (rather than treating its value as the state) means playback still works on a page
  // that has no scrubber element, and every caller gets the same clamping.
  GcodeViewer.prototype._setIndex = function (idx) {
    if (!this.moves.length) return;
    idx = Math.max(0, Math.min(this.moves.length - 1, idx | 0));
    this.index = idx;
    if (this.els.scrubber && parseInt(this.els.scrubber.value, 10) !== idx) this.els.scrubber.value = idx;
    this._update(idx);
  };

  GcodeViewer.prototype._update = function (idx) {
    if (!this.moves.length) return;
    idx = Math.max(0, Math.min(this.moves.length - 1, idx | 0));
    this.index = idx;
    var e = this.els;
    var move = this.moves[idx];
    if (e.scrubberLabel) e.scrubberLabel.textContent = 'Move ' + (idx + 1) + ' of ' + this.moves.length;
    if (e.scrubberOp) e.scrubberOp.textContent = (move.type === 'G0' ? 'Rapid' : 'Cut') + ': ' + move.line;

    // The tool sits on the path vertex for this index - vertex 0 before the first move has
    // run, vertex idx+1 once it has - so the marker can never drift from the drawn line.
    if (this.toolMesh && this.pathPoints) {
      var v = (idx === 0 ? 0 : idx + 1) * 3;
      var tl = this.toolMesh.userData.toolLength;
      this.toolMesh.position.set(this.pathPoints[v], this.pathPoints[v + 1] + tl / 2, this.pathPoints[v + 2]);
    }

    // Turn the work over at the pause, exactly as the operator does. The router never
    // moves below the tube; the tube presents its other side to it. Rotating about Z
    // spins the tube on its own long axis, so the wall being machined is always the one
    // facing up - which is why the toolpath can stay where the spindle really is.
    var phase = move.phase || 1;
    if (this.tubeGroup) {
      var flipped = (phase === 2);
      if (this.tubeGroup.userData.flipped !== flipped) {
        this.tubeGroup.userData.flipped = flipped;
        this.tubeGroup.rotation.z = flipped ? Math.PI : 0;
        // Swap the two tones with the tube. The far wall is darker so that a cut reads as
        // depth; turning the tube over without swapping put the DARK wall on top and the
        // bright one underneath, so every cut showed the opposite wall's diagonal through
        // it and the truss read as a row of X's.
        var wl = this.tubeGroup.userData.walls;
        wl.near.material = flipped ? wl.dark : wl.bright;
        wl.far.material = flipped ? wl.bright : wl.dark;
      }
    }

    // Only the run being cut is drawn, and it is drawn by moving a range over a buffer
    // that was built once - no geometry is created or thrown away here.
    // Vertices run.a .. idx+1 are behind the tool, vertices idx .. run.b+1 ahead of it.
    // Index 0 means "about to start move 1", so nothing is complete there; and a
    // single-move program is at its start and its end at once, which is why the upcoming
    // test asks for idx 0 explicitly - without it the one move was drawn in neither
    // colour and a one-move program showed no toolpath at all.
    var run = this.runs[this.runOf[idx]];
    var doneCount = idx > 0 ? (idx - run.a + 2) : 0;
    var upCount = (idx === 0 || idx < this.moves.length - 1) ? (run.b - idx + 2) : 0;
    this._setRange(this.completedLine, run.a, doneCount);
    this._setRange(this.upcomingLine, idx, upCount);
    this.setToolpathVisible(this.toolpathVisible !== false);
  };

  GcodeViewer.prototype._setRange = function (line, start, count) {
    if (!line) return;
    if (count < 2) count = 0;
    line.geometry.setDrawRange(start, count);
    line.userData.drawn = count;
  };

  GcodeViewer.prototype._start = function () {
    // Nothing to play through: a single-move program has no second position to advance to,
    // and the old timer flagged the button "playing" for one tick before stopping itself.
    if (this.moves.length < 2) return;
    // If playback is parked at the end (it auto-pauses there), pressing play should
    // replay from the beginning rather than sit stuck at the last move.
    if (this.index >= this.moves.length - 1) this._setIndex(0);
    this.isPlaying = true;
    if (this.els.playButton) this.els.playButton.classList.add('playing');

    // Driven by the frame clock, not setInterval. A fixed 1000/speed timer cannot fire
    // faster than the browser will schedule it, so 8x playback quietly ran at about
    // three-quarters speed and the backlog grew; advancing by elapsed time honours the
    // speed setting whatever the frame rate.
    var self = this, last = null, carry = 0;
    (function tick(ts) {
      if (!self.isPlaying) return;
      if (last === null) last = ts;
      var dt = Math.min((ts - last) / 1000, 0.25);   // a backgrounded tab must not jump
      last = ts;
      carry += dt * self.speed;
      var steps = Math.floor(carry);
      if (steps > 0) {
        carry -= steps;
        var max = self.moves.length - 1;
        if (self.index + steps >= max) { self._setIndex(max); self._stop(); return; }
        self._setIndex(self.index + steps);
      }
      self.raf = requestAnimationFrame(tick);
    })(performance.now());
  };

  GcodeViewer.prototype._stop = function () {
    this.isPlaying = false;
    if (this.els.playButton) this.els.playButton.classList.remove('playing');
    if (this.raf) { cancelAnimationFrame(this.raf); this.raf = null; }
  };

  /* Show or hide the toolpath, leaving the model. A pocket's clearing passes cover the
     pocket completely, so on a tube the CAM path hides the very geometry it is cutting -
     and the whole point of drawing the tube was to be able to look at the part. */
  GcodeViewer.prototype.setToolpathVisible = function (visible) {
    this.toolpathVisible = visible !== false;
    var on = this.toolpathVisible;
    if (this.completedLine) this.completedLine.visible = on && this.completedLine.userData.drawn > 0;
    if (this.upcomingLine) this.upcomingLine.visible = on && this.upcomingLine.userData.drawn > 0;
    if (this.toolMesh) this.toolMesh.visible = on;
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
