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

from frc_cam_postprocessor import FRCPostProcessor, build_resume_programs
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
    # There is deliberately no general "lateral FEED below material top" key here. One was
    # declared for a long time and never written to or read, which read like coverage that
    # did not exist. It cannot be written generally either: a G1 moving in XY below the
    # material top is what cutting IS, and nothing in the text distinguishes a positioning
    # move at the traverse rate from a real cut. The dangerous specific case - a lateral
    # feed resuming after a pause with no retract - is `unsafe_after_m0` below.
    findings = {'rapid_below_top': [], 'min_z': 1e9,
                'drill_lateral': [], 'g83_without_g80': 0, 'spindle_on': False,
                'unsafe_after_m0': []}
    material_top = None
    z_known = False          # has the program actually said where Z is yet?
    pending_g80 = 0
    just_resumed = False

    # What is in the spindle, not what the section banner is called. Deriving "a drill
    # is loaded" from the banner missed the engraving block entirely: it has a banner of
    # its own, so a program that loaded a twist drill and then wrote the part name with
    # it read as perfectly clean. The header's tool table says which slots are drills;
    # the Load / Install lines and the section banners say which slot is in the spindle.
    drill_slots = set()
    for slot, kind in re.findall(r'\(\s*(T\d+) - .*?, ([a-z0-9 ]+)\)', gcode):
        if 'twist drill' in kind:
            drill_slots.add(slot)
    loaded_slot = None
    banner_drill = False

    for raw in gcode.splitlines():
        line = raw.split(';')[0]
        code = re.sub(r'\(.*?\)', '', line).strip()
        if 'Material top:' in raw:
            m = re.search(r'Z=(-?[\d.]+)', raw)
            if m:
                material_top = float(m.group(1))
        loading = re.search(r'\(\s*(?:Load|Install)\s+(T\d+)\b', raw)
        if loading:
            loaded_slot = loading.group(1)
        if raw.startswith('(===== '):
            banner_drill = 'DRILLING' in raw
            # "(===== PERIMETER - plate - T2 1/8 endmill =====)": the tool is last.
            in_section = re.findall(r'-\s*(T\d+)\b', raw)
            if in_section:
                loaded_slot = in_section[-1]
        # A single-tool program has no tool table; its DRILLING banner is all there is.
        in_drill = banner_drill if not drill_slots else loaded_slot in drill_slots
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
            # `z` is seeded at 0.0 before the program says where the tool is. A cutting
            # program's depths are negative so the seed never dominates, but a dry run's
            # are all positive - and the fake 0.0 then became the reported minimum,
            # contradicting the header on every raised program.
            if 'Z' in words:
                findings['min_z'] = min(findings['min_z'], nz,
                                        z if z_known else nz)
                z_known = True
            elif z_known:
                findings['min_z'] = min(findings['min_z'], z)

            if head == 'G83':
                pending_g80 += 1
            elif in_drill and head in ('G1', 'G2', 'G3') and moved_xy:
                findings['drill_lateral'].append(code)

            if material_top is not None:
                # A rapid that STARTS below the material top and moves in XY drags the
                # cutter, whatever Z it is heading for: a machine interpolates all three
                # axes at once, so `G0 X5 Y3 Z<safe>` climbing out of a cut sweeps the
                # work on the way up. Requiring the destination to be low as well exempted
                # exactly that move.
                if head == 'G0' and moved_xy and (z < material_top - 1e-6
                                                  or nz < material_top - 1e-6):
                    findings['rapid_below_top'].append(code)
                # Any lateral move after a pause, not just a rapid. The generator resumes
                # with `G1 X.. Y.. F<traverse>`, so restricting this to G0 meant it could
                # not fire on the output this project actually produces.
                if (just_resumed and moved_xy
                        and (z < material_top - 1e-6 or nz < material_top - 1e-6)):
                    findings['unsafe_after_m0'].append(code)
            if moved_xy or abs(nz - z) > 1e-9:
                just_resumed = False
            x, y, z = nx, ny, nz
        elif head == 'G80':
            pending_g80 = max(0, pending_g80 - 1)
    findings['g83_without_g80'] = pending_g80
    findings['material_top'] = material_top
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
        # Scan the WHOLE line. Splitting on ';' first assumed a semicolon always starts a
        # trailing remark, but a ';' inside a (...) comment is just text - and truncating
        # there hid every nested paren after it, e.g. "(Generated by: A; B (Mentor))".
        # A ';' outside a comment does start a remark, and a stray '(' inside one is
        # unbalanced on the parsers that read ';' that way, so counting the full line is
        # the stricter reading in both directions.
        d = m = 0
        for c in l:
            if c == '(':
                d += 1
                m = max(m, d)
            elif c == ')':
                d -= 1
        if m > 1:
            fail(name, f'line {n} nested comment: {l[:60]}')
        if d != 0:
            fail(name, f'line {n} unbalanced comment parens: {l[:60]}')
        inp = False
        for c in l:
            if c == '(':
                inp = True
            elif c == ')':
                inp = False
            elif inp and c in '[]':
                fail(name, f'line {n} bracket in comment: {l[:60]}')
                break
        # GRBL-class controllers buffer 80 characters per line and refuse anything
        # longer ("command too long") - mid-run, spindle down, part scrapped. A real
        # program was refused on 2026-09-01 by exactly this. 78 is MAX_LINE_LENGTH
        # in frc_cam_postprocessor; restated here on purpose - the audit checks the
        # claim, not the constant that implements it.
        if len(l) > 78:
            fail(name, f'line {n} is {len(l)} chars, over the 78-char controller '
                       f'line limit: {l[:60]}')

    # --- no ATC codes ---------------------------------------------------------------
    for n, l in enumerate(lines, 1):
        code = re.sub(r'\(.*?\)', '', l).split(';')[0].strip()
        for tok in code.split():
            if tok in ('M6', 'M06') or tok.startswith('G43') or re.fullmatch(r'T\d+', tok):
                fail(name, f'line {n} emits an ATC word: {tok}')
            # GRBL 1.1 does not implement canned cycles; ASSUMPTIONS.md targets GRBL.
            if re.fullmatch(r'G8[1-9]', tok):
                fail(name, f'line {n} emits canned cycle {tok}, unsupported on GRBL')


def check_offset_reset_before_motion(name, lines):
    """A stale G92 must be cancelled before the first programmed move."""
    reset_at = None
    first_motion = None
    for index, line in enumerate(lines):
        code = re.sub(r'\(.*?\)', '', line).split(';')[0].strip()
        if code.startswith('G92.1'):
            reset_at = index
        if re.match(r'^G0?[0-3]\b', code):
            first_motion = index
            break
    if reset_at is None or (first_motion is not None and reset_at > first_motion):
        fail(name, 'does not cancel temporary G92 offset before first motion')



#: Where a facing / cut-to-length block starts, and what ends it.
TUBE_BLOCK_START = re.compile(r'^\(\s*(?:Tube facing:|Cut to length at Y=)')
TUBE_BLOCK_END = re.compile(
    r'^\(\s*(?:Machine pattern|=== PHASE|=== CUT TUBE|Square tube end)|^M[0-9]')


def check_tube_wall_rapids(name, lines, tube_width, tube_height, wall, tool_diameter):
    """Simulate the tube cross-section and flag rapids that enter standing material.

    This is the audit gap that let the walls-only rapid-plunge bug ship. The generator
    used to treat every pass after the first as "the middle is hollow" - but the TOP
    WALL of box tube spans the full width, and with a small cutter the first pass does
    not necessarily reach past it. The rapid then went through solid 6061 at mid-span.

    The model is a slice through the tube at the cutting plane:

      * columns outside 0..tube_width are air;
      * the two side-wall columns are solid all the way down;
      * every other column carries the top wall, solid from the top of the tube down
        to `tube_height - wall`, and nothing below that.

    Cutting moves lower the floor of each column they sweep (tool radius included).
    A rapid may not put the tool tip below a column's floor while material still
    stands there.

    The program's own claim is checked too: a pass labelled "walls only" is asserting
    that the middle is open, and that assertion has to be earned by a previous pass
    having reached past the wall.
    """
    radius = tool_diameter / 2.0
    step = 0.005
    eps = 1e-6

    for kind, count, depth in re.findall(
            r'\( (Roughing|Finishing): (\d+) passes of ([\d.]+)" each', '\n'.join(lines)):
        depth = float(depth)
        for raw in lines:
            m = re.search(rf'\( {kind} pass (\d+)/{count} .*- walls only \)', raw)
            if not m:
                continue
            cleared = (int(m.group(1)) - 1) * depth
            if cleared < wall + eps:
                fail(name,
                     f'{kind.lower()} pass {m.group(1)} claims walls only after '
                     f'{cleared:.4f}" of depth, but the top wall is {wall:.4f}" thick')

    blocks = []
    current = None
    for raw in lines:
        text = raw.strip()
        if TUBE_BLOCK_START.match(text):
            current = []
            blocks.append(current)
            continue
        if current is not None and TUBE_BLOCK_END.match(text):
            current = None
            continue
        if current is not None:
            current.append(raw)

    for block in blocks:
        moves = []
        x = z = None
        max_z = -1e9
        for raw in block:
            code = re.sub(r'\(.*?\)', '', raw).split(';')[0].strip()
            if not code:
                continue
            head = code.split()[0]
            if head not in ('G0', 'G1', 'G2', 'G3'):
                continue
            words = dict((w[0], float(w[1])) for w in NUM.findall(code))
            nx = words.get('X', x if x is not None else 0.0)
            nz = words.get('Z', z if z is not None else 0.0)
            max_z = max(max_z, nz)
            moves.append((head, nx, nz, code))
            x, z = nx, nz
        if not moves:
            continue

        # z_safe is the block's own retract height, 0.25" above the tube. Deriving the
        # tube top from the program keeps this honest for dry runs, where the whole
        # tube frame is lifted.
        z_top = max_z - 0.25
        wall_bottom = z_top - wall

        def columns(a, b):
            lo, hi = min(a, b) - radius, max(a, b) + radius
            lo = max(lo, 0.0)
            hi = min(hi, tube_width)
            if hi < lo:
                return []
            n = int((hi - lo) / step) + 1
            return [lo + i * step for i in range(n)]

        floors = {}

        def floor_of(c):
            return floors.get(round(c / step), z_top)

        def set_floor(c, value):
            key = round(c / step)
            floors[key] = min(floors.get(key, z_top), value)

        x, z = moves[0][1], max_z
        for head, nx, nz, code in moves:
            tip = min(z, nz)
            if head == 'G0':
                for c in columns(x, nx):
                    f = floor_of(c)
                    side_wall = c <= wall + eps or c >= tube_width - wall - eps
                    standing = f > wall_bottom + eps
                    if tip < f - eps and (side_wall or standing):
                        fail(name,
                             f'rapid to Z{nz:.4f} at X{nx:.4f} enters standing tube '
                             f'material, that column is only cleared to Z{f:.4f}: {code}')
                        break
            else:
                for c in columns(x, nx):
                    set_floor(c, tip)
            x, z = nx, nz


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
    _check_tube_program(name, result, face_width, tube_height, wall, tool)


def _check_tube_program(name, result, tube_width=None, tube_height=None, wall=None,
                        tool_diameter=None):
    """The frame-independent checks every tube program must pass, whichever path built
    it. Shared by the fixed patterns and by custom designs so neither can drift into
    being audited less than the other."""
    g = result.gcode
    lines = g.splitlines()

    check_text_rules(name, lines)
    check_offset_reset_before_motion(name, lines)
    if None not in (tube_width, tube_height, wall, tool_diameter):
        check_tube_wall_rapids(name, lines, tube_width, tube_height, wall,
                               tool_diameter)
    if 'REQUIRED ALUMINUM PREFLIGHT' not in g:
        fail(name, 'tube aluminum program has no mandatory preflight')
    if 'continuous manual air blast' not in g and 'flow is aimed and chips can escape' not in g:
        fail(name, 'tube aluminum program has no chip-evacuation disposition')

    tool_match = re.search(r'\( Tool: ([\d.]+)"(?: \d+-flute)? end mill \)', g)
    if tool_match:
        diameter = float(tool_match.group(1))
        for phase, depth in re.findall(
                r'\( (Roughing|Finishing): \d+ passes of ([\d.]+)" each', g):
            if float(depth) > diameter + 0.0006:
                fail(name, f'tube {phase.lower()} axial level {depth}" exceeds '
                           f'the {diameter:.3f}" cutter diameter')

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

    _check_tube_program(name, result, face_width, tube_height, wall, tool)

    g = result.gcode
    if 'twist drill' in g:
        fail(name, 'a custom design claims a twist drill in its header; it is milled')
    if 'Custom design:' not in g:
        fail(name, 'header does not say what the custom design contains')
    # A bore big enough to need helical entry must actually get one - a straight plunge
    # with a 0.157 end mill into 1.125 of bore is the bug this path exists to avoid.
    if any(abs(h['diameter'] - 1.125) < 1e-6 for h in pp.holes) and 'Helical entry' not in g:
        fail(name, 'a 1.125" bearing bore was cut without a helical entry')


def _point_to_segment(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def check_standing_tabs(name, gcode):
    """Do the tabs really stand as tall as the program says they do?

    A tab is the only thing holding a profiled part while the machine keeps cutting, and
    the program states its own claim out loud: every `; Tab N start` lifts the cutter to
    the top of the standing tab. The claim is checkable - nothing before the removal
    pass may cut BELOW that Z at a tab.

    It did not hold. Only the final pass lifted, so on 5-pass 1/4" aluminum the four
    intermediate passes milled straight through the tab zones and the tabs ended up one
    pass-depth tall - 0.054" of a designed 0.15". This audits the emitted program rather
    than the generator, which is how it can disagree with the code that wrote it.
    """
    lines = gcode.splitlines()
    removal = next((i for i, l in enumerate(lines) if 'TAB REMOVAL PASS' in l), None)
    if removal is None:
        return

    # Where each tab is, from the removal pass's own moves.
    paths, current = [], None
    for raw in lines[removal:]:
        mx = re.search(r'X(-?[\d.]+)', raw)
        my = re.search(r'Y(-?[\d.]+)', raw)
        if 'tab start (in kerf)' in raw and mx and my:
            if current and len(current) > 1:
                paths.append(current)
            current = [(float(mx.group(1)), float(my.group(1)))]
        elif 'Cut through tab' in raw and current is not None and mx and my:
            point = (float(mx.group(1)), float(my.group(1)))
            if point != current[-1]:
                current.append(point)
    if current and len(current) > 1:
        paths.append(current)
    unique = {}
    for path in paths:
        unique.setdefault(path[0], path)
    paths = list(unique.values())
    if not paths:
        return

    centres = []
    for path in paths:
        spans = [math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(path, path[1:])]
        half = sum(spans) / 2.0
        walked = 0.0
        for (a, b), span in zip(zip(path, path[1:]), spans):
            if span and walked + span >= half:
                t = (half - walked) / span
                centres.append((a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])))
                break
            walked += span
        else:
            centres.append(path[len(path) // 2])

    # The tab top the program itself announces.
    tops = [float(re.search(r'Z(-?[\d.]+)', l).group(1)) for l in lines[:removal]
            if re.search(r';\s*Tab (?:\d+ start|lift during ramp)', l)
            and re.search(r'Z(-?[\d.]+)', l)]
    if not tops:
        fail(name, 'a tab-removal pass exists but no pass ever lifted over a tab')
        return
    declared_top = min(tops)

    floors = {c: 1e9 for c in centres}
    radius = 0.0
    in_chamfer = False
    x = y = z = None
    previous = None
    for raw in lines[:removal]:
        found = (re.search(r'\(Tool: ([\d.]+)"', raw)
                 or re.search(r'\(Tool ([\d.]+) in diameter', raw))
        if found:
            radius = float(found.group(1)) / 2.0
        if raw.startswith('(===== '):
            # A chamfer deliberately breaks the top edge all the way round, tabs
            # included. It is a few thou deep by design and says nothing about whether
            # the tab still holds the part.
            in_chamfer = 'CHAMFER' in raw.upper() or 'DEBURR' in raw.upper()
        if in_chamfer:
            previous = None
            continue
        code = re.sub(r'\(.*?\)', '', raw.split(';')[0]).strip()
        m = re.match(r'^(G0|G1|G2|G3)\b', code)
        if not m:
            continue
        words = dict((w[0], float(w[1])) for w in NUM.findall(code))
        x, y, z = words.get('X', x), words.get('Y', y), words.get('Z', z)
        if None not in (x, y, z) and previous is not None and None not in previous:
            if m.group(1) != 'G0':
                for centre in centres:
                    # Strictly inside the cutter's sweep. A tool tangent at the tab
                    # centre is the boundary case that says the tab is exactly as wide
                    # as the cutter, which is a tab-width question, not a height one.
                    if _point_to_segment(centre[0], centre[1], previous[0], previous[1],
                                         x, y) < radius - 1e-4:
                        floors[centre] = min(floors[centre], previous[2], z)
        previous = (x, y, z)

    for centre, floor in floors.items():
        if floor < 1e8 and floor < declared_top - 1e-4:
            fail(name,
                 f'the tab at ({centre[0]:.3f}, {centre[1]:.3f}) is cut to Z{floor:.4f} '
                 f'but the program lifts to Z{declared_top:.4f} over it - it stands '
                 f'{declared_top - floor:.4f}" less than the program claims')
            return


def audit_island_pocket(name, outer, island, tool=0.25, thickness=0.25):
    """Audit the island-aware pocket path: does anything cut through the island?

    The pocket generator that handles islands linked its contour rings with a bare feed
    move at full depth. Only CIRCULAR islands were diverted to the spiral clearer, so a
    rectangular one was fed straight across. This drives the generator directly with a
    known island and checks every cutting move against it - the audit cannot infer where
    an island is from the G-code, so the geometry has to be supplied here.
    """
    from shapely.geometry import LineString, Polygon

    global checked
    checked += 1
    with redirect_stdout(io.StringIO()):
        pp = FRCPostProcessor(thickness, tool)
        pp.apply_material_preset('plywood')
        pp.material_top = thickness
        pp.cut_depth = -0.008
        lines = pp._generate_pocket_gcode_from_polygon(Polygon(outer, [island]))
    if not lines:
        fail(name, 'the island-aware pocket produced no toolpath')
        return
    check_text_rules(name, lines)

    keep = Polygon(island).buffer(pp.tool_radius - 1e-3)
    x = y = None
    z = thickness + 1.0
    previous = None
    crossings = 0
    first = ''
    for raw in lines:
        code = re.sub(r'\(.*?\)', '', raw.split(';')[0]).strip()
        m = re.match(r'^(G0|G1|G2|G3)\b', code)
        if not m:
            continue
        words = dict((w[0], float(w[1])) for w in NUM.findall(code))
        x, y, z = words.get('X', x), words.get('Y', y), words.get('Z', z)
        if (m.group(1) != 'G0' and previous is not None and None not in previous
                and None not in (x, y) and min(previous[2], z) < pp.material_top - 1e-9
                and keep.intersects(LineString([(previous[0], previous[1]), (x, y)]))):
            crossings += 1
            first = first or code
        previous = (x, y, z)
    if crossings:
        fail(name, f'{crossings} cutting move(s) cross the island: {first[:60]}')

    # ...and it still has to CLEAR the pocket, or "no gouges" is trivially satisfied.
    xs = [float(v) for raw in lines
          for v in re.findall(r'\bX(-?[\d.]+)', raw.split(';')[0])]
    left, right = min(p[0] for p in island), max(p[0] for p in island)
    if not any(v < left for v in xs) or not any(v > right for v in xs):
        fail(name, 'the pocket was not cleared on both sides of the island')


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
    check_offset_reset_before_motion(name, lines)
    _audit_resume_programs(name, result)

    aluminum_program = (('Material: Aluminum' in g or 'Material: 6061 Aluminum' in g
                         or 'Material: 6063 Aluminum' in g)
                        and 'DRY RUN' not in g)
    if aluminum_program:
        if 'REQUIRED ALUMINUM PREFLIGHT' not in g:
            fail(name, 'aluminum program has no mandatory chip-evacuation preflight')
        if 'continuous manual air blast' not in g and 'flow is aimed and chips can escape' not in g:
            fail(name, 'aluminum program has no explicit continuous chip-evacuation disposition')
        for change in g.split('( === TOOL CHANGE')[1:]:
            before_pause = change.split('M0', 1)[0]
            if 'Clean collet, minimize stickout, and verify low runout' not in before_pause:
                fail(name, 'aluminum tool change does not recheck collet and runout')
            if 'continuous directed air and a clear chip escape path' not in before_pause:
                fail(name, 'aluminum tool change does not recheck chip evacuation')

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

    check_standing_tabs(name, g)

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
    expected_pauses = (g.count('=== TOOL CHANGE') + g.count('PAUSE FOR FIXTURING')
                       + g.count('REQUIRED ALUMINUM PREFLIGHT'))
    if g.count('M0') != expected_pauses:
        fail(name, f'M0 count {g.count("M0")} does not match pauses '
                   f'({g.count("=== TOOL CHANGE")} changes + '
                   f'{g.count("PAUSE FOR FIXTURING")} fixturing + '
                   f'{g.count("REQUIRED ALUMINUM PREFLIGHT")} aluminum preflight)')
    dry = 'DRY RUN' in g
    if dry:
        # The whole claim of a dry run, checked independently: nothing reaches the work.
        # The header's "material top" is the LIFTED, fictional one - the program does
        # descend within the raised frame, which is the point of "the same program,
        # raised". The real work sits a lift below that, and that is what must be clear.
        lift = re.search(r'raised (\d+\.?\d*) in above the work', g)
        if sim['material_top'] is not None and lift:
            real_top = sim['material_top'] - float(lift.group(1))
            if sim['min_z'] < real_top - 1e-6:
                fail(name, f'dry run reaches Z{sim["min_z"]:.4f}, into work whose top '
                           f'is at {real_top:.4f}')
        elif sim['material_top'] is not None:
            fail(name, 'dry run header does not state the lift')
        if re.search(r'^S\d+ M3', g, re.M):
            fail(name, 'dry run starts the spindle')
    for i, l in enumerate(lines):
        if l.startswith('M0'):
            after = '\n'.join(lines[i:i + 12])
            # A dry run deliberately violates "every pause restarts the spindle" - the
            # whole point is that it never starts. Inverted rather than skipped, so the
            # property is still checked: after a dry run's pause, M3 must be ABSENT.
            if dry:
                if re.search(r'^S\d+ M3', after, re.M):
                    fail(name, 'spindle started after a pause in a dry run')
            elif 'M3' not in after:
                fail(name, 'spindle not restarted after a pause')


def _audit_resume_programs(name, result):
    """Independently verify every tool-boundary tail can start with no prior modal state."""
    programs = build_resume_programs(result.gcode, result.filename)
    material_header = next((line.lower() for line in result.gcode.splitlines()
                            if line.startswith('(Material:')), '')
    aluminum_program = 'aluminum' in material_header
    expected = result.gcode.count('=== TOOL CHANGE')
    if len(programs) != expected:
        fail(name, f'{expected} tool changes produced {len(programs)} resume programs')
        return
    for program in programs:
        resume_name = f'{name}/{program["checkpoint"]}'
        gcode = program['gcode']
        lines = gcode.splitlines()
        check_text_rules(resume_name, lines)
        checkpoint = next((i for i, line in enumerate(lines)
                           if '=== RESUME CHECKPOINT' in line), None)
        confirm = next((i for i, line in enumerate(lines)
                        if line.startswith('M0')), None)
        if checkpoint is None or confirm is None or confirm >= checkpoint:
            fail(resume_name, 'operator confirmation does not precede checkpoint motion')
            continue
        before = '\n'.join(lines[:checkpoint])
        if re.search(r'^G[0-3]\b', before, re.M):
            fail(resume_name, 'moves the machine before the standalone resume confirmation')
        if aluminum_program:
            if 'clean collet, low runout, continuous directed air' not in before:
                fail(resume_name, 'standalone aluminum setup omits collet, air, or runout check')
        after = '\n'.join(lines[checkpoint:checkpoint + 18])
        for required in ('G90 G94 G91.1 G40 G49 G17', 'G20', 'G92.1', 'G54',
                         'Safe Z before resumed XY motion'):
            if required not in after:
                fail(resume_name, f'missing restart state {required}')
        if 'DRY RUN' not in result.gcode and 'M3' not in after:
            fail(resume_name, 'does not start the incoming tool after the state reset')
        if not gcode.rstrip().endswith('M30  ; Program end'):
            fail(resume_name, 'does not carry the original program through M30')


def audit_refusal(name, job, expected):
    """An unsafe request is a passing case only when no runnable program is returned."""
    global checked
    checked += 1
    result = run(job)
    if result.success:
        fail(name, 'UNSAFE REQUEST GENERATED RUNNABLE G-CODE')
    elif not any(expected.lower() in e.lower() for e in result.errors):
        fail(name, f'refusal did not explain {expected!r}: {result.errors[:1]}')


def audit_bed_leveling(name):
    """Independently walk the standalone spoilboard raster.

    It does not use FRCPostProcessor or the plate material frame, so feeding it through
    audit() would make that function validate assumptions this program intentionally does
    not have. This small simulator checks the physical promises unique to surfacing.
    """
    global checked
    checked += 1
    from bed_leveling import generate_bed_leveling, parse_spec

    spec = parse_spec({
        'width': 23.5, 'height': 17.25, 'tool_diameter': 1.0,
        'stepover_percent': 63, 'depth': 0.012, 'feed_rate': 95,
        'plunge_rate': 18, 'spindle_speed': 18000, 'safe_z': 0.3,
    }, machine_width=24, machine_height=18, machine_z=8)
    result = generate_bed_leveling(spec)
    lines = result.gcode.splitlines()
    check_text_rules(name, lines)
    check_offset_reset_before_motion(name, lines)

    pause = next((i for i, line in enumerate(lines) if line.startswith('M0')), None)
    spindle = next((i for i, line in enumerate(lines)
                    if re.match(r'^S\d+ M3\b', line)), None)
    if pause is None or spindle is None or pause >= spindle:
        fail(name, 'verification pause does not precede spindle start')
    if any(re.match(r'^M[789]\b', line) for line in lines):
        fail(name, 'emits a coolant code even though bed leveling has no coolant config')
    if 'G20  ; Inches' not in lines:
        fail(name, 'does not establish inch units')
    if sum(1 for line in lines if line.startswith('M30')) != 1:
        fail(name, 'does not contain exactly one program end')

    radius = spec.tool_diameter / 2.0
    x = y = z = 0.0
    saw_cut = False
    last_cut_line = -1
    last_spindle_off = -1
    for line_number, raw in enumerate(lines):
        code = re.sub(r'\(.*?\)', '', raw).split(';')[0].strip()
        if not code:
            continue
        if re.match(r'^M0?5\b', code):
            last_spindle_off = line_number
        head = code.split()[0]
        if head not in ('G0', 'G1'):
            continue
        words = dict((word, float(value)) for word, value in NUM.findall(code))
        nx, ny, nz = words.get('X', x), words.get('Y', y), words.get('Z', z)
        lateral = abs(nx - x) > 1e-9 or abs(ny - y) > 1e-9
        if head == 'G0' and lateral and min(z, nz) < -1e-9:
            fail(name, f'rapid XY move below the spoilboard top: {raw}')
        if head == 'G1' and lateral:
            saw_cut = True
            last_cut_line = line_number
            if abs(z + spec.depth) > 1e-6 or abs(nz + spec.depth) > 1e-6:
                fail(name, f'lateral feed is not at the declared cut depth: {raw}')
            if abs(nx - x) > 1e-9 and abs(ny - y) > 1e-9:
                fail(name, f'raster contains a diagonal cutting move: {raw}')
            if not (radius - 1e-6 <= nx <= spec.width - radius + 1e-6 and
                    radius - 1e-6 <= ny <= spec.height - radius + 1e-6):
                fail(name, f'cutting center leaves the cutter-radius envelope: {raw}')
        x, y, z = nx, ny, nz

    if not saw_cut:
        fail(name, 'contains no lateral surfacing cuts')
    if last_spindle_off <= last_cut_line:
        fail(name, 'does not stop the spindle after the final cut')


def main():
    HOLES = [(1, 1, 0.196), (5, 1, 0.196), (1, 3, 0.196), (5, 3, 0.196)]
    BORE = [(3, 2, 0.75)]
    POCKET = [(2.2, 2.4, 3.8, 3.2)]

    mill = [Tool(1, '1/8 endmill', 0.125, 1), Tool(2, '1/4 endmill', 0.25, 2),
            Tool(3, '1/2 V-bit', 0.5, 2, type='vbit', included_angle=90)]
    drill_set = [Tool(1, '#10 drill', 0.1935, 2, type='drill'),
                 Tool(2, '1/4 endmill', 0.25, 2),
                 Tool(3, '1/2 V-bit', 0.5, 2, type='vbit', included_angle=90)]

    audit_bed_leveling('bed-leveling/raster')

    # Adversarial requests that used to produce executable bit-breaking programs.
    # Every public alloy spelling must take the aluminum path, never plywood.
    hostile_dxf = plate(HOLES, POCKET)
    for alias in ('aluminum', 'aluminum_tube', '6061', '6061-T6',
                  'aluminum_6061', '6063', '6063-T5', 'aluminum_6063'):
        audit_refusal(f'refuse/4F/{alias}', MultiToolJob(
            material=alias, thickness=0.25, machine_id='omio_x8',
            tools=[Tool(1, '1/8 4F', 0.125, 4)],
            parts=[PartOps(dxf_path=hostile_dxf, name='p', operations=[
                Operation('holes', 1), Operation('pockets', 1),
                Operation('perimeter', 1)])]), '1- or 2-flute')

    audit_refusal('refuse/rpm-below-machine', MultiToolJob(
        material='6063', thickness=0.25, machine_id='omio_x8',
        tools=[Tool(1, '1/8 1F at 1000 RPM', 0.125, 1, spindle_speed=1000)],
        parts=[PartOps(dxf_path=plate(), name='p', operations=[
            Operation('perimeter', 1)])]), 'RPM')
    audit_refusal('refuse/2000-ipm-drill', MultiToolJob(
        material='6061', thickness=0.25, machine_id='omio_x8',
        tools=[Tool(1, '#10 drill at 2000 IPM', 0.1935, 2, type='drill',
                    plunge_rate=2000), Tool(2, '1/8 endmill', 0.125, 1)],
        parts=[PartOps(dxf_path=plate(HOLES), name='p', operations=[
            Operation('holes', 1, 'Drill'), Operation('perimeter', 2)])]), 'plunge')

    for material in ('plywood', 'aluminum', 'polycarbonate'):
        for thickness in (0.125, 0.25, 0.5):
            dxf = plate(HOLES + BORE, POCKET)
            audit(f'mill/{material}/{thickness}', MultiToolJob(
                material=material, thickness=thickness, tools=mill, machine_id='omio_x8',
                max_pass_depth=1 / 32,
                parts=[PartOps(dxf_path=dxf, name='p', operations=[
                    Operation('holes', 1, scope={'max_diameter': 0.4}),
                    Operation('holes', 2, scope={'min_diameter': 0.4}),
                    Operation('pockets', 2), Operation('perimeter', 2),
                    Operation('chamfer', 3, scope={'targets': ['perimeter'], 'width': 0.02})])]),
                  max_engagement=1 / 32)

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

    # The 2026-08-24 field-failure geometry, audited forever with the safe cutter the
    # program now requires: thin aluminum and an operator depth-per-pass ceiling. The
    # original bug hid in tab removal (full plate thickness in one move while the
    # profile politely stepped down), which no other case exercised. The physical 4F
    # tool from that failure is separately covered by refusal tests.
    audit('thin-al/ceiling', MultiToolJob(
        material='aluminum', thickness=0.125, machine_id='omio_x8',
        max_pass_depth=1 / 32,
        tools=[Tool(1, '1/8 1F aluminum', 0.125, 1)],
        parts=[PartOps(dxf_path=plate(HOLES), name='p', operations=[
            Operation('holes', 1), Operation('perimeter', 1)])]),
          max_engagement=1 / 32)

    # The same job WITH a cleared pocket. Until the pocket clearing learned to step
    # down, this bit 0.133" per pass against a 0.031" ceiling - four times over, on the
    # exact job shape whose profile the ceiling was added to protect.
    audit('thin-al/ceiling+pocket', MultiToolJob(
        material='aluminum', thickness=0.125, machine_id='omio_x8',
        max_pass_depth=1 / 32,
        tools=[Tool(1, '1/8 1F aluminum', 0.125, 1)],
        parts=[PartOps(dxf_path=plate(HOLES, POCKET), name='p', operations=[
            Operation('holes', 1), Operation('pockets', 1), Operation('perimeter', 1)])]),
          max_engagement=1 / 32)

    # A big bore in thick stock: the circular-spiral clearing strategy, which is a
    # different generator again from the contour-parallel one above.
    audit('thick-al/bore', MultiToolJob(
        material='aluminum', thickness=0.5, machine_id='omio_x8', max_pass_depth=1 / 32,
        tools=[Tool(1, '1/4 2F', 0.25, 2)],
        parts=[PartOps(dxf_path=plate([(3, 2, 1.5)], POCKET), name='p', operations=[
            Operation('holes', 1), Operation('pockets', 1), Operation('perimeter', 1)])]),
          max_engagement=1 / 32)

    # Partial-depth work on the BOARD datum (the stock-top case is audited above), where
    # a pocket that stops short must still be cleared rather than contoured.
    audit('mill/partial-depth', MultiToolJob(
        material='aluminum', thickness=0.25, machine_id='omio_x8', max_pass_depth=1 / 32,
        tools=drill_set,
        parts=[PartOps(dxf_path=plate(HOLES + BORE, POCKET), name='p', operations=[
            Operation('holes', 1, 'Drill', scope={'max_diameter': 0.4}),
            Operation('holes', 2, 'Bore', scope={'min_diameter': 0.4}),
            Operation('pockets', 2, 'Relief', depth=0.1),
            Operation('perimeter', 2)])]),
          expect_drill=True, max_engagement=1 / 32)

    # Engraved part names: user text reaching a G-code comment, and a light cut in the
    # middle of a face. The audit's text rules (ASCII, no nested parens, no brackets)
    # are exactly what a part name can break.
    def engraved_run(name):
        def _go():
            with redirect_stdout(io.StringIO()):
                pp = FRCPostProcessor(material_thickness=0.25, tool_diameter=0.0625,
                                      units='inch')
                pp.apply_material_preset('plywood')
                pp.engrave = {'text': name, 'height': 0.18, 'depth': 0.01}
                pp.load_dxf(plate(HOLES, POCKET))
                pp.transform_coordinates('bottom-left', 0)
                pp.identify_perimeter_and_pockets()
                pp.classify_holes()
                return pp.generate_gcode(suggested_filename='engraved',
                                         timestamp='2026-08-25 03:00:00')
        return _go

    for label in ('GEARBOX-L', 'Bracket (left) [v2]', 'ARM_2129#3'):
        audit(f'engrave/{label[:12]}', engraved_run(label))

    # Engraving in a job whose FIRST operation is drilled. The name has to be cut by a
    # milling tool, and the audit follows the tool that is actually in the spindle
    # rather than the section banner - the engrave block has a banner of its own, which
    # is precisely how a twist drill being fed sideways at 75 IPM read as clean.
    audit('engrave/after-drilling', MultiToolJob(
        material='plywood', thickness=0.25, engrave=True, machine_id='omio_x8',
        tools=[Tool(1, '#7 drill', 0.201, 2, type='drill'),
               Tool(2, '1/8 in endmill', 0.125, 2)],
        parts=[PartOps(dxf_path=plate(holes=[(1.0, 1.0, 0.201), (5.0, 1.0, 0.201)]),
                       name='GEARBOX-L', operations=[
                           Operation('holes', 1, 'Drill'),
                           Operation('perimeter', 2)])]),
          expect_drill=True)

    # Dry runs. Nothing in the corpus audited one, so the audit had no independent
    # opinion on the feature whose entire job is to be trustworthy: 13 separate
    # mutations of the dry-run code produced no audit finding at all. The frame here is
    # entirely positive, which is what caught the simulator seeding min_z at a Z the
    # program never visits.
    def dry_run_single(thickness, tool=0.125):
        def _go():
            with redirect_stdout(io.StringIO()):
                pp = FRCPostProcessor(material_thickness=thickness, tool_diameter=tool,
                                      units='inch')
                pp.apply_material_preset('aluminum', 'omio_x8')
                pp.set_dry_run(2.0)
                pp.load_dxf(plate(HOLES, POCKET))
                pp.transform_coordinates('bottom-left', 0)
                pp.identify_perimeter_and_pockets()
                pp.classify_holes()
                return pp.generate_gcode(suggested_filename='dry',
                                         timestamp='2026-08-25 03:00:00')
        return _go

    # Thin stock, where the requested 2" lift dominates, and thick stock, where it does
    # not - a fixed lift over a 2.5" block cut 0.5" deep with a stationary cutter.
    audit('dryrun/thin', dry_run_single(0.25))
    audit('dryrun/thick', dry_run_single(2.5))

    audit('dryrun/multitool', MultiToolJob(
        material='aluminum', thickness=0.25, machine_id='omio_x8', max_pass_depth=1 / 32,
        dry_run_lift=2.0, tools=drill_set,
        parts=[PartOps(dxf_path=plate(HOLES + BORE, POCKET), name='p', operations=[
            Operation('holes', 1, 'Drill', scope={'max_diameter': 0.4}),
            Operation('holes', 2, 'Bore', scope={'min_diameter': 0.4}),
            Operation('pockets', 2),
            Operation('perimeter', 2)])]),
          expect_drill=True, max_engagement=1 / 32)

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
    # 22", not 24": cutting a tube to length puts the cut plane past the tube's end,
    # and the roughing arc reaches past that again, so a 24" tube genuinely does not fit
    # 24" of Y travel. The envelope check refuses it now, which is the point.
    audit_tube('tube/2x1-flat/cut-to-length', 2.0, 22.0, 1.0, mode='lightening',
               square_end=True, cut_to_length=True)
    # Thick-wall 1x1 with a small cutter: the per-pass depth (0.101") is LESS than the
    # 0.125" wall, so the first pass does not clear the top wall. This is the geometry
    # that made the walls-only branch rapid through solid 6061 at mid-span, and it is
    # here so the cross-section check has something real to prove.
    audit_tube('tube/1x1/thick-wall/facing+cut', 1.0, 12.0, 1.0, mode='lightening',
               tool=0.125, wall=0.125, square_end=True, cut_to_length=True)

    # Pockets with a standing island, square and rectangular - the shapes the circular
    # ring detector does NOT divert, and so the ones that were gouged.
    audit_island_pocket('pocket/island/square',
                        [(0, 0), (4, 0), (4, 3), (0, 3)],
                        [(1.5, 1.0), (2.5, 1.0), (2.5, 2.0), (1.5, 2.0)])
    audit_island_pocket('pocket/island/long',
                        [(0, 0), (6, 0), (6, 3), (0, 3)],
                        [(1.0, 1.2), (5.0, 1.2), (5.0, 1.8), (1.0, 1.8)], tool=0.157)

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
    print('  note: tube programs also enforce <=1D axial facing levels; their ZMIN and '
          'rapid checks use separate tube-frame tests')
    print(f'{len(problems)} problem(s)')
    for p in problems:
        print('  *', p)
    sys.exit(1 if problems else 0)


if __name__ == '__main__':
    main()
