"""Turn TrueType/OpenType glyph outlines into engraving polylines.

The browser uploads the font with the job.  Nothing here depends on fonts installed on
the CAM server, which is important for the slim Docker image and makes "use this font"
mean the same thing on every machine.
"""

from __future__ import annotations

import io
import math
from typing import Dict, List, Tuple

from fontTools.pens.basePen import BasePen
from fontTools.pens.boundsPen import BoundsPen
from fontTools.ttLib import TTFont

Point = Tuple[float, float]
Stroke = List[Point]


class OutlineFontError(ValueError):
    """A font or requested string cannot safely be converted to toolpaths."""


def _point_line_distance(point: Point, start: Point, end: Point) -> float:
    dx, dy = end[0] - start[0], end[1] - start[1]
    if dx == 0 and dy == 0:
        return math.hypot(point[0] - start[0], point[1] - start[1])
    return abs(dy * point[0] - dx * point[1] + end[0] * start[1]
               - end[1] * start[0]) / math.hypot(dx, dy)


class _FlattenPen(BasePen):
    """A fontTools pen that approximates curves with bounded-error line segments."""

    def __init__(self, glyph_set, tolerance: float):
        super().__init__(glyph_set)
        self.tolerance = max(float(tolerance), 0.01)
        self.contours: List[Stroke] = []
        self._contour: Stroke = []

    def _moveTo(self, point):
        self._finish(False)
        self._contour = [(float(point[0]), float(point[1]))]

    def _lineTo(self, point):
        self._contour.append((float(point[0]), float(point[1])))

    def _curveToOne(self, control1, control2, end):
        start = self._getCurrentPoint()

        def flatten(a, b, c, d, depth=0):
            flat = max(_point_line_distance(b, a, d),
                       _point_line_distance(c, a, d)) <= self.tolerance
            if flat or depth >= 12:
                self._contour.append((float(d[0]), float(d[1])))
                return
            ab = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
            bc = ((b[0] + c[0]) / 2, (b[1] + c[1]) / 2)
            cd = ((c[0] + d[0]) / 2, (c[1] + d[1]) / 2)
            abc = ((ab[0] + bc[0]) / 2, (ab[1] + bc[1]) / 2)
            bcd = ((bc[0] + cd[0]) / 2, (bc[1] + cd[1]) / 2)
            mid = ((abc[0] + bcd[0]) / 2, (abc[1] + bcd[1]) / 2)
            flatten(a, ab, abc, mid, depth + 1)
            flatten(mid, bcd, cd, d, depth + 1)

        flatten(start, control1, control2, end)

    def _qCurveToOne(self, control, end):
        start = self._getCurrentPoint()

        def flatten(a, b, c, depth=0):
            if _point_line_distance(b, a, c) <= self.tolerance or depth >= 12:
                self._contour.append((float(c[0]), float(c[1])))
                return
            ab = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
            bc = ((b[0] + c[0]) / 2, (b[1] + c[1]) / 2)
            mid = ((ab[0] + bc[0]) / 2, (ab[1] + bc[1]) / 2)
            flatten(a, ab, mid, depth + 1)
            flatten(mid, bc, c, depth + 1)

        flatten(start, control, end)

    def _closePath(self):
        self._finish(True)

    def _endPath(self):
        self._finish(False)

    def _finish(self, close: bool):
        if not self._contour:
            return
        if close and self._contour[-1] != self._contour[0]:
            self._contour.append(self._contour[0])
        if len(self._contour) >= 2:
            self.contours.append(self._contour)
        self._contour = []


def _name(font: TTFont) -> str:
    names = font.get('name')
    if names:
        for name_id in (16, 1):       # typographic family, then legacy family
            for record in names.names:
                if record.nameID == name_id:
                    try:
                        value = record.toUnicode().strip()
                    except Exception:
                        continue
                    if value:
                        return value[:100]
    return 'Uploaded font'


def _cap_units(font: TTFont, glyph_set, cmap: Dict[int, str]) -> float:
    os2 = font.get('OS/2')
    cap = getattr(os2, 'sCapHeight', 0) if os2 else 0
    if cap and cap > 0:
        return float(cap)
    # Older fonts do not declare cap height. Measure H, then fall back to the ascender.
    glyph_name = cmap.get(ord('H'))
    if glyph_name:
        bounds = BoundsPen(glyph_set)
        glyph_set[glyph_name].draw(bounds)
        if bounds.bounds:
            _x0, y0, _x1, y1 = bounds.bounds
            if y1 > y0:
                return float(y1 - y0)
    hhea = font.get('hhea')
    ascent = getattr(hhea, 'ascent', 0) if hhea else 0
    return float(ascent if ascent > 0 else font['head'].unitsPerEm)


def validate_font(path: str) -> str:
    """Validate an uploaded font and return a displayable family name."""
    try:
        # Own the file handle explicitly. TTFont can raise halfway through parsing a
        # damaged upload; when it opened the path itself that half-built object left
        # the descriptor open until garbage collection.
        with open(path, 'rb') as source:
            font = TTFont(source, lazy=False)
        try:
            if not font.getBestCmap():
                raise OutlineFontError('The font has no usable Unicode character map.')
            font.getGlyphSet()
            return _name(font)
        finally:
            font.close()
    except OutlineFontError:
        raise
    except Exception as exc:
        raise OutlineFontError(f'The uploaded font could not be read: {exc}') from exc


def validate_font_bytes(data: bytes) -> str:
    """Validate an in-memory font from the saved-job API."""
    try:
        font = TTFont(io.BytesIO(data), lazy=False)
        try:
            if not font.getBestCmap():
                raise OutlineFontError('The font has no usable Unicode character map.')
            font.getGlyphSet()
            return _name(font)
        finally:
            font.close()
    except OutlineFontError:
        raise
    except Exception as exc:
        raise OutlineFontError(f'The uploaded font could not be read: {exc}') from exc


def text_strokes(text: str, cap_height: float, path: str,
                 max_points: int = 100_000) -> Tuple[List[Stroke], float, str]:
    """Return glyph-outline polylines, their width, and the font family name.

    ``cap_height`` is inches in the returned coordinates. Curves are flattened to about
    0.002 in chord error, with a smaller error for very small type.
    """
    if not text or not text.strip():
        raise OutlineFontError('Enter text to engrave.')
    if not math.isfinite(cap_height) or cap_height <= 0:
        raise OutlineFontError('Engraving height must be a positive number.')
    if '\n' in text or '\r' in text:
        raise OutlineFontError('Use one line of text per part.')

    try:
        with open(path, 'rb') as source:
            font = TTFont(source, lazy=False)
    except Exception as exc:
        raise OutlineFontError(f'The uploaded font could not be read: {exc}') from exc
    try:
        cmap = font.getBestCmap() or {}
        glyph_set = font.getGlyphSet()
        hmtx = font['hmtx'].metrics
        scale = cap_height / _cap_units(font, glyph_set, cmap)
        # Work in font units. At the requested scale this is no more than 0.002 inch.
        tolerance = max(0.5, min(8.0, 0.002 / scale))

        kern: Dict[Tuple[str, str], float] = {}
        if 'kern' in font:
            for table in font['kern'].kernTables:
                if getattr(table, 'format', None) == 0:
                    kern.update(table.kernTable)

        contours: List[Stroke] = []
        pen_x = 0.0
        previous = None
        missing = []
        for char in text:
            glyph_name = cmap.get(ord(char))
            if glyph_name is None:
                missing.append(char)
                continue
            if previous is not None:
                pen_x += kern.get((previous, glyph_name), 0.0)
            pen = _FlattenPen(glyph_set, tolerance)
            glyph_set[glyph_name].draw(pen)
            pen._finish(False)
            for contour in pen.contours:
                contours.append([((pen_x + x) * scale, y * scale) for x, y in contour])
            pen_x += hmtx.get(glyph_name, (font['head'].unitsPerEm, 0))[0]
            previous = glyph_name
            if sum(len(c) for c in contours) > max_points:
                raise OutlineFontError('That text/font combination creates too many toolpath points.')

        if missing:
            chars = ''.join(dict.fromkeys(missing))
            raise OutlineFontError(f'The selected font has no glyph for {chars!r}.')
        points = [point for contour in contours for point in contour]
        if not points:
            raise OutlineFontError('The selected text has no drawable glyph outlines.')
        min_x = min(p[0] for p in points)
        min_y = min(p[1] for p in points)
        max_x = max(p[0] for p in points)
        shifted = [[(x - min_x, y - min_y) for x, y in contour]
                   for contour in contours]
        return shifted, max_x - min_x, _name(font)
    finally:
        font.close()
