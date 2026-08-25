"""Independent audit of generated G-code.

    uv run python gcode_audit.py

Deliberately NOT built from the same assumptions as the unit tests. It *simulates* the
program - walking every move, tracking modal position - and checks physical claims:

* does the header's ZMIN match how deep the program actually goes?
* does any rapid traverse through material?
* does a drilling operation ever feed sideways? (a twist drill cannot)
* are there canned cycles? (GRBL 1.1 does not implement G81-G89)
* do the comments obey the ASCII / no-nested-parens / no-brackets rules?

This exists because the unit tests encode the same assumptions the code does, so they
stayed green through a bug that made a drilling operation emit end-mill toolpaths. An
audit built from different premises catches what a test written by the same hand does
not. It found the drill-depth ZMIN under-report that the whole suite missed.

Run it after any change to toolpath generation. Zero problems is the expected result;
a finding is a real defect, not a style note.
"""
import io
import math
import re

from frc_cam_postprocessor import FRCPostProcessor
import sys
import tempfile
from contextlib import redirect_stdout

import ezdxf

import tooling
from tooling import MultiToolJob, Operation, PartOps, Tool
from team_config import TeamConfig

problems = []
checked = 0


def fail(job_name, msg):
    problems.append(f'{job_name}: {msg}')


def plate(holes=(), pockets=(), size=(6, 4)):
    doc = ezdxf.new('R2010')
    msp = doc.modelspace()
    w, h = size
    msp.add_lwpolyline([(0, 0), (w, 0), (w, h), (0, h)], close=True)
    for (x, y, d) in holes:
        msp.add_circle((x, y), d / 2.0)
    for (x0, y0, x1, y1) in pockets:
        msp.add_lwpolyline([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], close=True)
    p = tempfile.mktemp(suffix='.dxf')
    doc.saveas(p)
    return p


def run(job):
    with redirect_stdout(io.StringIO()):
        return tooling.generate_multitool_job(job, timestamp='2026-08-20 18:00:00')


NUM = re.compile(r'([XYZQRF])(-?\d*\.?\d+)')


def simulate(gcode):
    """Walk the program tracking modal position. Returns findings."""
    x = y = z = 0.0
    findings = {'lateral_feed_below_top': [], 'rapid_below_top': [], 'min_z': 1e9,
                'drill_lateral': [], 'g83_without_g80': 0, 'spindle_on': False,
                'unsafe_after_m0': []}
    material_top = None
    in_drill = False
    pending_g80 = 0
    just_resumed = False

    for raw in gcode.splitlines():
        line = raw.split(';')[0]
        code = re.sub(r'\(.*?\)', '', line).strip()
        if 'Material top:' in raw:
            m = re.search(r'Z=(-?[\d.]+)', raw)
            if m:
                material_top = float(m.group(1))
        if raw.startswith('(===== '):
            in_drill = 'DRILLING' in raw
        if not code:
            continue
        words = dict((w[0], float(w[1])) for w in NUM.findall(code))
        head = code.split()[0]

        if head == 'M0':
            just_resumed = True
            continue
        if head.startswith(('G0', 'G1', 'G2', 'G3', 'G83')):
            nx, ny = words.get('X', x), words.get('Y', y)
            nz = words.get('Z', z)
            moved_xy = abs(nx - x) > 1e-9 or abs(ny - y) > 1e-9
            findings['min_z'] = min(findings['min_z'], nz, z)

            if head == 'G83':
                pending_g80 += 1
            elif in_drill and head in ('G1', 'G2', 'G3') and moved_xy:
                findings['drill_lateral'].append(code)

            if material_top is not None:
                if head == 'G0' and moved_xy and (z < material_top - 1e-6
                                                  and nz < material_top - 1e-6):
                    findings['rapid_below_top'].append(code)
                if just_resumed and head == 'G0' and moved_xy and nz < material_top:
                    findings['unsafe_after_m0'].append(code)
            if moved_xy or abs(nz - z) > 1e-9:
                just_resumed = False
            x, y, z = nx, ny, nz
        elif head == 'G80':
            pending_g80 = max(0, pending_g80 - 1)
    findings['g83_without_g80'] = pending_g80
    return findings


def check_text_rules(name, lines):
    """Rules that hold for EVERY program regardless of the coordinate frame it works in.

    Split out so tube programs can be audited too. The checks below this point in audit()
    reason about the plate Z-frame and about M0 meaning a tool change, neither of which is
    true of a tube program, so those stay where they are.
    """
    # --- comment rules (CLAUDE.md) -------------------------------------------------
    for n, l in enumerate(lines, 1):
        try:
            l.encode('ascii')
        except UnicodeEncodeError:
            fail(name, f'line {n} non-ASCII: {l[:60]}')
        d = m = 0
        for c in l.split(';')[0]:
            if c == '(':
                d += 1
                m = max(m, d)
            elif c == ')':
                d -= 1
        if m > 1:
            fail(name, f'line {n} nested comment: {l[:60]}')
        inp = False
        for c in l:
            if c == '(':
                inp = True
            elif c == ')':
                inp = False
            elif inp and c in '[]':
                fail(name, f'line {n} bracket in comment: {l[:60]}')
                break

    # --- no ATC codes ---------------------------------------------------------------
    for n, l in enumerate(lines, 1):
        code = re.sub(r'\(.*?\)', '', l).split(';')[0].strip()
        for tok in code.split():
            if tok in ('M6', 'M06') or tok.startswith('G43') or re.fullmatch(r'T\d+', tok):
                fail(name, f'line {n} emits an ATC word: {tok}')
            # GRBL 1.1 does not implement canned cycles; ASSUMPTIONS.md targets GRBL.
            if re.fullmatch(r'G8[1-9]', tok):
                fail(name, f'line {n} emits canned cycle {tok}, unsupported on GRBL')



def audit_tube(name, face_width, tube_length, tube_height, square_end=True,
               cut_to_length=False, mode='holes', tool=None, wall=0.0625):
    """Audit a pre-designed tube pattern program.

    Tube programs are generated by a different path from the multi-tool jobs above and
    were previously not audited at all - which is how bracketed comments survived in the
    tube header for as long as they did.

    Only the frame-independent checks run here. The ZMIN / rapid-below-top checks in
    audit() assume the plate frame, where material top is a fixed Z; a tube program works
    at Z = tube_height - wall and pauses to flip rather than to change tools, so those
    checks would report differences in convention as if they were faults. Physical-depth
    auditing of tube programs is NOT covered - see MULTI_TOOL_STATUS.md.
    """
    global checked
    checked += 1
    import tube_patterns
    # A drilled hole pattern REQUIRES the tool to be the drill; a lightening pattern is
    # milled with an end mill. load_tube_pattern refuses any other combination.
    if tool is None:
        tool = tube_patterns.HOLE_DIAMETER if mode == 'holes' else 0.157
    pp = FRCPostProcessor(wall, tool)
    pp.apply_material_preset('aluminum_tube')
    pp.tube_height = tube_height
    pp.load_tube_pattern(face_width, tube_length, mode=mode)
    result = pp.generate_tube_pattern_gcode(
        tube_height=tube_height, square_end=square_end, cut_to_length=cut_to_length,
        tube_width=face_width, tube_length=tube_length)
    if not result.success:
        fail(name, 'FAILED TO GENERATE: ' + '; '.join(result.errors)[:120])
        return
    _check_tube_program(name, result)


def _check_tube_program(name, result):
    """The frame-independent checks every tube program must pass, whichever path built
    it. Shared by the fixed patterns and by custom designs so neither can drift into
    being audited less than the other."""
    g = result.gcode
    lines = g.splitlines()

    check_text_rules(name, lines)

    # Substance, not the plate path's exact wording: the tube generator ends with a bare
    # `M30` where the plate generator writes `M30  ; Program end`. Both are valid; what
    # matters is that the program really does stop the spindle and end.
    tail = [l for l in lines if l.strip()][-3:]
    codes = [l.split(';')[0].strip() for l in tail]
    if not codes or codes[-1] != 'M30':
        fail(name, f'program does not end with M30, ends with {codes[-1:] or "nothing"}')
    if 'M5' not in codes:
        fail(name, 'spindle not stopped before program end')
    # Every pause on a tube job is a flip the operator performs by hand; the spindle must
    # come back on afterwards or the next phase cuts with a stopped tool.
    for i, l in enumerate(lines):
        if l.startswith('M0'):
            if 'M3' not in '\n'.join(lines[i:i + 14]):
                fail(name, f'spindle not restarted after the pause at line {i + 1}')
    if 'PHASE 1' not in g or 'PHASE 2' not in g:
        fail(name, 'tube program does not machine both faces')

    # The first motion must lift clear before anything moves in XY: at program start the
    # tool is wherever the last job left it, and a rapid across the tube at that height
    # drags the cutter through it.
    import re as _re
    for l in lines:
        code = _re.sub(r'\(.*?\)', '', l).split(';')[0].strip()
        if not code or not _re.match(r'G0?[0-3]\b', code):
            continue
        has_z = any(t.startswith('Z') for t in code.split())
        has_xy = any(t[0] in 'XY' for t in code.split() if t[:1] in ('X', 'Y'))
        if has_xy and not has_z:
            fail(name, f'first motion moves in XY before retracting: {code}')
        break


def audit_tube_design(name, design, face_width, tube_length, tube_height,
                      tool=0.157, wall=0.0625, square_end=False, cut_to_length=False):
    """Audit a CUSTOM tube design - features the user placed themselves.

    Same checks as audit_tube, plus the two claims that are specific to this path: the
    header must not offer a twist drill (a design mixing clearance sizes and a 1.125"
    bore is machined with the end mill, and the operator loads what the header says),
    and squaring/cutting to length must be ALLOWED here, unlike the drilled pattern -
    there is a milling cutter in the spindle, so those are ordinary operations.
    """
    global checked
    checked += 1
    with redirect_stdout(io.StringIO()):
        pp = FRCPostProcessor(wall, tool)
        pp.apply_material_preset('aluminum_tube')
        pp.tube_height = tube_height
        pp.load_tube_design(design, face_width, tube_length)
        result = pp.generate_tube_pattern_gcode(
            tube_height=tube_height, square_end=square_end,
            cut_to_length=cut_to_length, tube_width=face_width, tube_length=tube_length)
    if not result.success:
        fail(name, 'FAILED TO GENERATE: ' + '; '.join(result.errors)[:120])
        return

    _check_tube_program(name, result)

    g = result.gcode
    if 'twist drill' in g:
        fail(name, 'a custom design claims a twist drill in its header; it is milled')
    if 'Custom design:' not in g:
        fail(name, 'header does not say what the custom design contains')
    # A bore big enough to need helical entry must actually get one - a straight plunge
    # with a 0.157 end mill into 1.125 of bore is the bug this path exists to avoid.
    if any(abs(h['diameter'] - 1.125) < 1e-6 for h in pp.holes) and 'Helical entry' not in g:
        fail(name, 'a 1.125" bearing bore was cut without a helical entry')


def max_lateral_engagement(gcode):
    """Independently measure the deepest bite any straight feed move takes.

    Walks the program with a coarse occupancy grid of cut floors: for every lateral G1
    below the material top, the axial engagement is how far the move's Z sits below the
    lowest floor previously cut along its path. This is the premise the unit tests did
    not have - they checked that the PASS COMMENTS obeyed the depth limit, while the
    tab-removal pass quietly slotted the full plate thickness in one move with no pass
    comment at all. Arcs are skipped (bores are axially fed by design); straight moves
    are where profiles, pockets and tabs live.

    Returns (engagement, offending line) for the worst move found.
    """
    cell = 0.04
    floors = {}
    x = y = z = 10.0
    material_top = None
    worst = (0.0, '')

    def cells_on(x0, y0, x1, y1, z0=None, z1=None):
        """Cells along the move, each with the interpolated Z there - a ramp cuts a
        sloped floor, and marking only its endpoints made a pass's closing move over
        its own just-ramped path read as a deep bite into virgin stock."""
        length = math.hypot(x1 - x0, y1 - y0)
        steps = max(1, int(length / cell))
        for i in range(steps + 1):
            t = i / steps
            zt = z0 + (z1 - z0) * t if z0 is not None else None
            yield (round((x0 + (x1 - x0) * t) / cell),
                   round((y0 + (y1 - y0) * t) / cell)), zt

    for raw in gcode.splitlines():
        if 'Material top:' in raw:
            m = re.search(r'Z=(-?[\d.]+)', raw)
            if m:
                material_top = float(m.group(1))
        code = re.sub(r'\(.*?\)', '', raw.split(';')[0]).strip()
        if not code:
            continue
        words = dict((w[0], float(w[1])) for w in NUM.findall(code))
        head = code.split()[0]
        if not head.startswith(('G0', 'G1', 'G2', 'G3')):
            continue
        nx, ny, nz = words.get('X', x), words.get('Y', y), words.get('Z', z)
        if head.startswith(('G1', 'G2', 'G3')) and material_top is not None:
            samples = list(cells_on(x, y, nx, ny, z, nz))
            flat_lateral = ((abs(nx - x) > 1e-9 or abs(ny - y) > 1e-9)
                            and nz < material_top - 1e-6 and abs(nz - z) < 1e-9
                            and head.startswith('G1'))
            if flat_lateral:
                # A sample counts as previously cut if ANY neighboring cell was -
                # grid quantization otherwise flags a re-traced kerf whose samples
                # round into the next cell over. A genuinely virgin span (a tab) has
                # interior samples with fully-uncut neighborhoods and gets caught.
                def floor_near(k):
                    kx, ky = k
                    return min(floors.get((kx + dx, ky + dy), material_top)
                               for dx in (-1, 0, 1) for dy in (-1, 0, 1))
                bite = max(floor_near(k) for k, _ in samples) - nz
                if bite > worst[0]:
                    worst = (bite, raw.strip())
            for k, zt in samples:
                floors[k] = min(floors.get(k, material_top), zt)
        x, y, z = nx, ny, nz
    return worst


def audit(name, job, expect_drill=False, max_engagement=None):
    """Audit one program. `job` is a MultiToolJob, or a zero-arg callable returning a
    PostProcessorResult (used for standard-mode programs such as the deburr pass).
    `max_engagement`, when given, is the deepest axial bite any single straight feed
    move is allowed to take - the check that catches a pass ignoring the depth limit."""
    global checked
    checked += 1
    result = job() if callable(job) else run(job)
    if not result.success:
        fail(name, 'FAILED TO GENERATE: ' + '; '.join(result.errors)[:120])
        return
    g = result.gcode
    lines = g.splitlines()

    check_text_rules(name, lines)

    if max_engagement is not None:
        # The grid checker is a coarse independent instrument with ~25% reading error
        # on legitimate re-traced kerfs (measured against real programs). The failure
        # it exists to catch - the tab-removal pass slotting a full 0.133" plate under
        # a 0.031" ceiling - reads 4x over, so a 1.35x acceptance band rejects the bug
        # class without false alarms on quantization noise.
        bite, line = max_lateral_engagement(g)
        if bite > max_engagement * 1.35 + 1e-6:
            fail(name, f'a straight feed move bites {bite:.4f}" of material, far over '
                       f'the {max_engagement:.4f}" per-pass limit: {line[:60]}')

    # --- header claims vs reality ---------------------------------------------------
    sim = simulate(g)
    zmin = [l for l in lines if l.startswith('(ZMIN:')]
    if zmin:
        declared = float(zmin[0].split(':')[1].strip().strip('")'))
        if declared > sim['min_z'] + 1e-6:
            fail(name, f'header ZMIN {declared:.4f} but program reaches {sim["min_z"]:.4f}')
    if sim['g83_without_g80']:
        fail(name, f'{sim["g83_without_g80"]} canned cycle(s) never cancelled by G80')
    if sim['rapid_below_top']:
        fail(name, f'{len(sim["rapid_below_top"])} rapid(s) traverse below material top: '
                   f'{sim["rapid_below_top"][0][:50]}')
    if sim['unsafe_after_m0']:
        fail(name, f'traverse below material top immediately after M0: '
                   f'{sim["unsafe_after_m0"][0][:50]}')

    # --- drilling specifics ---------------------------------------------------------
    if expect_drill:
        if sim['drill_lateral']:
            fail(name, f'DRILL MADE {len(sim["drill_lateral"])} LATERAL CUT(S): '
                       f'{sim["drill_lateral"][0][:50]}')
        if 'Peck 1 of' not in g:
            fail(name, 'drill operation emitted no pecking moves')
        if 'No straight plunges' in g:
            fail(name, 'header claims "No straight plunges" on a drilling program')

    # --- program structure ----------------------------------------------------------
    if not g.rstrip().endswith('M30  ; Program end'):
        fail(name, 'program does not end with M30')
    if g.count('M0') != g.count('=== TOOL CHANGE') + g.count('PAUSE FOR FIXTURING'):
        fail(name, f'M0 count {g.count("M0")} does not match pauses '
                   f'({g.count("=== TOOL CHANGE")} changes + '
                   f'{g.count("PAUSE FOR FIXTURING")} fixturing)')
    for i, l in enumerate(lines):
        if l.startswith('M0'):
            after = '\n'.join(lines[i:i + 12])
            if 'M3' not in after:
                fail(name, 'spindle not restarted after a pause')
            break


def main():
    HOLES = [(1, 1, 0.196), (5, 1, 0.196), (1, 3, 0.196), (5, 3, 0.196)]
    BORE = [(3, 2, 0.75)]
    POCKET = [(2.2, 2.4, 3.8, 3.2)]

    mill = [Tool(1, '1/8 endmill', 0.125, 1), Tool(2, '1/4 endmill', 0.25, 2),
            Tool(3, '1/2 V-bit', 0.5, 2, type='vbit', included_angle=90)]
    drill_set = [Tool(1, '#10 drill', 0.1935, 2, type='drill'),
                 Tool(2, '1/4 endmill', 0.25, 2),
                 Tool(3, '1/2 V-bit', 0.5, 2, type='vbit', included_angle=90)]

    for material in ('plywood', 'aluminum', 'polycarbonate'):
        for thickness in (0.125, 0.25, 0.5):
            dxf = plate(HOLES + BORE, POCKET)
            audit(f'mill/{material}/{thickness}', MultiToolJob(
                material=material, thickness=thickness, tools=mill, machine_id='omio_x8',
                parts=[PartOps(dxf_path=dxf, name='p', operations=[
                    Operation('holes', 1, scope={'max_diameter': 0.4}),
                    Operation('holes', 2, scope={'min_diameter': 0.4}),
                    Operation('pockets', 2), Operation('perimeter', 2),
                    Operation('chamfer', 3, scope={'targets': ['perimeter'], 'width': 0.02})])]))

            audit(f'drill/{material}/{thickness}', MultiToolJob(
                material=material, thickness=thickness, tools=drill_set, machine_id='omio_x8',
                parts=[PartOps(dxf_path=dxf, name='p', operations=[
                    Operation('holes', 1, 'Drill', scope={'max_diameter': 0.4}),
                    Operation('holes', 2, 'Bore', scope={'min_diameter': 0.4}),
                    Operation('pockets', 2), Operation('perimeter', 2)])]),
                  expect_drill=True)

    # The stock-top Z datum, on the same shapes: every cutting move is below zero
    # here, so the "no rapid below the material top" and engagement checks are read
    # against a negative top face. The audit takes the top face from the header, so it
    # is not told which datum it is looking at - which is the point.
    for thickness in (0.125, 0.5):
        audit(f'mill/stock-top/{thickness}', MultiToolJob(
            material='aluminum', thickness=thickness, tools=mill, machine_id='omio_x8',
            z_datum='stock_top',
            parts=[PartOps(dxf_path=plate(HOLES + BORE, POCKET), name='p', operations=[
                Operation('holes', 1, scope={'max_diameter': 0.4}),
                Operation('holes', 2, scope={'min_diameter': 0.4}),
                Operation('pockets', 2), Operation('perimeter', 2),
                Operation('chamfer', 3, scope={'targets': ['perimeter'], 'width': 0.02})])]))

    # Partial-depth work on the stock-top datum, where every Z is negative and
    # "is this through the stock?" cannot be read off the sign. A pocket that stops
    # short must still be cleared, and a drilled hole must only break through when it
    # is meant to.
    audit('mill/stock-top/partial-depth', MultiToolJob(
        material='aluminum', thickness=0.25, tools=drill_set, machine_id='omio_x8',
        z_datum='stock_top',
        parts=[PartOps(dxf_path=plate(HOLES + BORE, POCKET), name='p', operations=[
            Operation('holes', 1, 'Drill', scope={'max_diameter': 0.4}),
            Operation('holes', 2, 'Bore', scope={'min_diameter': 0.4}),
            Operation('pockets', 2, 'Relief', depth=0.1),
            Operation('perimeter', 2)])]),
          expect_drill=True)

    # multi-part, with the fixturing pause on
    pause_cfg = TeamConfig({'machining': {'fixturing': {'pause_before_perimeter': True}}})
    dxf = plate(HOLES + BORE, POCKET)
    audit('multipart+pause', MultiToolJob(
        material='plywood', thickness=0.25, tools=drill_set, config=pause_cfg,
        machine_id='omio_x8', parts=[
            PartOps(dxf_path=dxf, name='a', place_x=0, operations=[
                Operation('holes', 1, 'Drill', scope={'max_diameter': 0.4}),
                Operation('holes', 2, 'Bore', scope={'min_diameter': 0.4}),
                Operation('pockets', 2), Operation('perimeter', 2)]),
            PartOps(dxf_path=dxf, name='b', place_x=7, operations=[
                Operation('holes', 1, 'Drill', scope={'max_diameter': 0.4}),
                Operation('holes', 2, 'Bore', scope={'min_diameter': 0.4}),
                Operation('pockets', 2), Operation('perimeter', 2)])]),
          expect_drill=True)

    # park position configured -> G53 should appear and be the only machine motion
    park_cfg = TeamConfig({'machine': {'park_position': {'x': 0.5, 'y': 19.0, 'z': -0.5}}})
    audit('with-park', MultiToolJob(
        material='plywood', thickness=0.25, tools=mill, config=park_cfg, machine_id='omio_x8',
        parts=[PartOps(dxf_path=plate(HOLES + BORE, POCKET), name='p', operations=[
            Operation('holes', 1, scope={'max_diameter': 0.4}),
            Operation('holes', 2, scope={'min_diameter': 0.4}),
            Operation('pockets', 2), Operation('perimeter', 2)])]))

    # The 2026-08-24 field-failure shape, audited forever: thin aluminum, a small
    # multi-flute cutter, and an operator depth-per-pass ceiling. The original bug hid
    # in the tab-removal pass (full plate thickness in one move while the profile
    # politely stepped down), which no other case exercised.
    audit('thin-al/ceiling', MultiToolJob(
        material='aluminum', thickness=0.125, machine_id='omio_x8',
        max_pass_depth=1 / 32,
        tools=[Tool(1, '1/8 4F', 0.125, 4)],
        parts=[PartOps(dxf_path=plate(HOLES), name='p', operations=[
            Operation('holes', 1), Operation('perimeter', 1)])]),
          max_engagement=1 / 32)

    # Standard-mode (single-tool) programs with the deburr / chamfer pass appended:
    # the same physical checks, on the non-multitool path that generates them. Two
    # angles, because the depth follows from the angle and a wrong tangent would move
    # real metal.
    from frc_cam_postprocessor import parse_chamfer_spec

    def standard_chamfer_run(material, thickness, angle, z_datum=None):
        def _go():
            with redirect_stdout(io.StringIO()):
                pp = FRCPostProcessor(material_thickness=thickness, tool_diameter=0.157,
                                      units='inch', z_datum=z_datum)
                pp.apply_material_preset(material)
                pp.load_dxf(plate(HOLES + BORE, POCKET))
                pp.transform_coordinates('bottom-left', 0)
                pp.identify_perimeter_and_pockets()
                pp.classify_holes()
                pp.chamfer_pass = parse_chamfer_spec({
                    'width': 0.02, 'bit_diameter': 0.5, 'bit_angle': angle,
                    'targets': ['perimeter', 'holes', 'pockets']})
                return pp.generate_gcode(suggested_filename='std_chamfer',
                                         timestamp='2026-08-20 18:00:00')
        return _go

    for material in ('plywood', 'aluminum'):
        for angle in (60, 90):
            audit(f'std-chamfer/{material}/{angle}deg',
                  standard_chamfer_run(material, 0.25, angle))

    # The single-tool path on the stock-top datum, deburr pass and all.
    audit('std-chamfer/stock-top/90deg',
          standard_chamfer_run('aluminum', 0.25, 90, z_datum='stock_top'))

    # Pre-designed tube patterns (1x1 and 2x1, with and without lightening).
    for face_width, height, label in ((1.0, 1.0, '1x1'), (2.0, 1.0, '2x1-flat'),
                                      (1.0, 2.0, '2x1-standing')):
        for length in (6.0, 24.0):
            for mode in ('holes', 'lightening'):
                # Squaring and cutting to length are MILLING operations. They are audited
                # on the milled pattern only: with a twist drill loaded the generator now
                # refuses them, because it used to emit them and feed the drill sideways.
                mill = (mode == 'lightening')
                audit_tube(f'tube/{label}/{length:g}in/{mode}', face_width, length,
                           height, mode=mode, square_end=mill)
    audit_tube('tube/2x1-flat/cut-to-length', 2.0, 24.0, 1.0, mode='lightening',
               square_end=True, cut_to_length=True)

    # Custom designs: a mixed-size face (which no single drill could make), the same
    # design on a narrow face, and one that also squares the end - allowed here because
    # the tool is an end mill, unlike the drilled pattern.
    MIXED = {'version': 1, 'features': [
        {'type': 'hole-run', 'x': 0.5, 'y': 1.0, 'pitch': 0.5, 'count': 6,
         'axis': 'y', 'size': '8-32'},
        {'type': 'hole-array', 'x': 1.5, 'y': 1.0, 'pitch_x': 0.0, 'pitch_y': 1.0,
         'cols': 1, 'rows': 3, 'size': '10-32'},
        {'type': 'hole', 'x': 1.0, 'y': 2.0, 'size': '1/4-20'},
        {'type': 'bearing', 'x': 1.0, 'y': 5.0},
        {'type': 'pocket', 'x': 1.0, 'y': 8.0, 'w': 1.2, 'h': 2.0,
         'corner_radius': 0.25},
    ]}
    NARROW = {'version': 1, 'features': [
        {'type': 'hole-run', 'x': 0.5, 'y': 1.0, 'pitch': 0.75, 'count': 8,
         'axis': 'y', 'size': '10-32'},
        {'type': 'pocket', 'x': 0.5, 'y': 8.0, 'w': 0.5, 'h': 1.5,
         'corner_radius': 0.25},
    ]}
    audit_tube_design('tube/custom/2x1-flat/mixed', MIXED, 2.0, 12.0, 1.0)
    audit_tube_design('tube/custom/2x1-flat/mixed+square', MIXED, 2.0, 12.0, 1.0,
                      square_end=True)
    audit_tube_design('tube/custom/1x1/narrow', NARROW, 1.0, 12.0, 1.0)

    # The refusal is part of the contract, so audit it too: a drilled pattern combined
    # with a milling operation must produce NO program rather than a dangerous one.
    global checked
    for square, cut in ((True, False), (False, True)):
        checked += 1
        import tube_patterns as _tp
        pp = FRCPostProcessor(0.0625, _tp.HOLE_DIAMETER)
        pp.apply_material_preset('aluminum_tube')
        pp.tube_height = 1.0
        pp.load_tube_pattern(2.0, 12.0, mode='holes')
        res = pp.generate_tube_pattern_gcode(tube_height=1.0, square_end=square,
                                             cut_to_length=cut, tube_width=2.0,
                                             tube_length=12.0)
        if res.success:
            fail(f'tube/drill+mill/{square}{cut}',
                 'a drilled pattern was allowed to run a milling operation')

    print(f'audited {checked} generated programs')
    print('  note: tube programs get the text/structure rules only - the ZMIN and '
          'rapid-below-top checks assume the plate Z-frame')
    print(f'{len(problems)} problem(s)')
    for p in problems:
        print('  *', p)
    sys.exit(1 if problems else 0)


if __name__ == '__main__':
    main()
