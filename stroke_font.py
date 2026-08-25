"""A single-stroke (engraving) font, for cutting text with an end mill or V-bit.

Why not a normal font: an outline font describes the OUTSIDE of each letter, so
engraving one means pocketing the space between two curves - slow, and illegible at
the sizes a part label needs. A single-stroke font is a set of centreline paths, drawn
the way a person draws a letter with a pen, so the toolpath IS the letter.

The shapes are on a 0..1 grid per glyph (x right, y up), advance width included, and
are deliberately plain: a part label is read across a workbench, not admired.

Only the characters a part name can contain after sanitising are defined. Anything else
becomes a dot, which reads as "something was here" rather than silently vanishing.
"""
from typing import Dict, List, Tuple

Stroke = List[Tuple[float, float]]

#: Nominal glyph box. Cap height 1.0, advance 0.72 including the side bearing.
GLYPH_ADVANCE = 0.72
_W = 0.52          # drawn width of a typical glyph
_M = _W / 2.0      # midline


def _v(*points) -> Stroke:
    return [(float(x), float(y)) for x, y in points]


#: Each glyph is a list of strokes; each stroke is a polyline the tool follows.
GLYPHS: Dict[str, List[Stroke]] = {
    'A': [_v((0, 0), (_M, 1), (_W, 0)), _v((0.1, 0.35), (_W - 0.1, 0.35))],
    'B': [_v((0, 0), (0, 1), (_W - 0.1, 1), (_W, 0.85), (_W, 0.65), (_W - 0.1, 0.55), (0, 0.55)),
          _v((0, 0.55), (_W - 0.06, 0.55), (_W, 0.42), (_W, 0.13), (_W - 0.1, 0), (0, 0))],
    'C': [_v((_W, 0.8), (_W - 0.12, 1), (0.12, 1), (0, 0.8), (0, 0.2), (0.12, 0), (_W - 0.12, 0), (_W, 0.2))],
    'D': [_v((0, 0), (0, 1), (_W - 0.14, 1), (_W, 0.8), (_W, 0.2), (_W - 0.14, 0), (0, 0))],
    'E': [_v((_W, 1), (0, 1), (0, 0), (_W, 0)), _v((0, 0.52), (_W - 0.12, 0.52))],
    'F': [_v((_W, 1), (0, 1), (0, 0)), _v((0, 0.52), (_W - 0.12, 0.52))],
    'G': [_v((_W, 0.8), (_W - 0.12, 1), (0.12, 1), (0, 0.8), (0, 0.2), (0.12, 0), (_W - 0.12, 0),
              (_W, 0.2), (_W, 0.45), (_M, 0.45))],
    'H': [_v((0, 0), (0, 1)), _v((_W, 0), (_W, 1)), _v((0, 0.52), (_W, 0.52))],
    'I': [_v((_M, 0), (_M, 1)), _v((_M - 0.16, 1), (_M + 0.16, 1)), _v((_M - 0.16, 0), (_M + 0.16, 0))],
    'J': [_v((_W, 1), (_W, 0.2), (_W - 0.14, 0), (0.12, 0), (0, 0.2))],
    'K': [_v((0, 0), (0, 1)), _v((_W, 1), (0, 0.48)), _v((0.18, 0.65), (_W, 0))],
    'L': [_v((0, 1), (0, 0), (_W, 0))],
    'M': [_v((0, 0), (0, 1), (_M, 0.45), (_W, 1), (_W, 0))],
    'N': [_v((0, 0), (0, 1), (_W, 0), (_W, 1))],
    'O': [_v((0.12, 0), (0, 0.2), (0, 0.8), (0.12, 1), (_W - 0.12, 1), (_W, 0.8), (_W, 0.2),
              (_W - 0.12, 0), (0.12, 0))],
    'P': [_v((0, 0), (0, 1), (_W - 0.1, 1), (_W, 0.85), (_W, 0.65), (_W - 0.1, 0.52), (0, 0.52))],
    'Q': [_v((0.12, 0), (0, 0.2), (0, 0.8), (0.12, 1), (_W - 0.12, 1), (_W, 0.8), (_W, 0.2),
              (_W - 0.12, 0), (0.12, 0)), _v((_M, 0.3), (_W, 0))],
    'R': [_v((0, 0), (0, 1), (_W - 0.1, 1), (_W, 0.85), (_W, 0.65), (_W - 0.1, 0.52), (0, 0.52)),
          _v((0.2, 0.52), (_W, 0))],
    'S': [_v((_W, 0.85), (_W - 0.12, 1), (0.12, 1), (0, 0.85), (0, 0.62), (0.12, 0.52),
              (_W - 0.12, 0.52), (_W, 0.4), (_W, 0.15), (_W - 0.12, 0), (0.12, 0), (0, 0.15))],
    'T': [_v((0, 1), (_W, 1)), _v((_M, 1), (_M, 0))],
    'U': [_v((0, 1), (0, 0.2), (0.12, 0), (_W - 0.12, 0), (_W, 0.2), (_W, 1))],
    'V': [_v((0, 1), (_M, 0), (_W, 1))],
    'W': [_v((0, 1), (0.13, 0), (_M, 0.6), (_W - 0.13, 0), (_W, 1))],
    'X': [_v((0, 0), (_W, 1)), _v((0, 1), (_W, 0))],
    'Y': [_v((0, 1), (_M, 0.5), (_W, 1)), _v((_M, 0.5), (_M, 0))],
    'Z': [_v((0, 1), (_W, 1), (0, 0), (_W, 0))],
    '0': [_v((0.12, 0), (0, 0.2), (0, 0.8), (0.12, 1), (_W - 0.12, 1), (_W, 0.8), (_W, 0.2),
              (_W - 0.12, 0), (0.12, 0)), _v((0, 0.2), (_W, 0.8))],
    '1': [_v((0.1, 0.8), (_M, 1), (_M, 0)), _v((_M - 0.18, 0), (_M + 0.18, 0))],
    '2': [_v((0, 0.82), (0.12, 1), (_W - 0.12, 1), (_W, 0.82), (_W, 0.62), (0, 0), (_W, 0))],
    '3': [_v((0, 1), (_W, 1), (0.2, 0.55), (_W - 0.08, 0.55), (_W, 0.4), (_W, 0.15),
              (_W - 0.12, 0), (0.12, 0), (0, 0.15))],
    '4': [_v((_W - 0.1, 0), (_W - 0.1, 1), (0, 0.32), (_W, 0.32))],
    '5': [_v((_W, 1), (0, 1), (0, 0.58), (_W - 0.12, 0.58), (_W, 0.44), (_W, 0.15),
              (_W - 0.12, 0), (0.12, 0), (0, 0.13))],
    '6': [_v((_W, 0.85), (_W - 0.12, 1), (0.12, 1), (0, 0.8), (0, 0.15), (0.12, 0),
              (_W - 0.12, 0), (_W, 0.15), (_W, 0.35), (_W - 0.12, 0.5), (0.12, 0.5), (0, 0.36))],
    '7': [_v((0, 1), (_W, 1), (0.18, 0))],
    '8': [_v((0.12, 0.55), (0, 0.7), (0, 0.86), (0.12, 1), (_W - 0.12, 1), (_W, 0.86),
              (_W, 0.7), (_W - 0.12, 0.55), (0.12, 0.55), (0, 0.4), (0, 0.15), (0.12, 0),
              (_W - 0.12, 0), (_W, 0.15), (_W, 0.4), (_W - 0.12, 0.55))],
    '9': [_v((0, 0.15), (0.12, 0), (_W - 0.12, 0), (_W, 0.2), (_W, 0.85), (_W - 0.12, 1),
              (0.12, 1), (0, 0.85), (0, 0.65), (0.12, 0.5), (_W - 0.12, 0.5), (_W, 0.64))],
    '-': [_v((0.06, 0.5), (_W - 0.06, 0.5))],
    '_': [_v((0, 0), (_W, 0))],
    '.': [_v((_M - 0.03, 0), (_M + 0.03, 0))],
    '#': [_v((0.14, 0), (0.2, 1)), _v((_W - 0.2, 0), (_W - 0.14, 1)),
          _v((0, 0.32), (_W, 0.32)), _v((0, 0.68), (_W, 0.68))],
    '/': [_v((0, 0), (_W, 1))],
    '+': [_v((_M, 0.18), (_M, 0.82)), _v((_M - 0.22, 0.5), (_M + 0.22, 0.5))],
    ' ': [],
    # Marks that turn up in real part names: sizes ('1/2" PLATE'), pairings ('L&R'),
    # lists ('A, B'), revisions ('REV=3'), and the leftovers a CAD name can carry.
    # Without a glyph each of these becomes a dash, which quietly renames the part.
    '"': [_v((_M - 0.11, 1), (_M - 0.11, 0.72)), _v((_M + 0.11, 1), (_M + 0.11, 0.72))],
    "'": [_v((_M, 1), (_M, 0.72))],
    ',': [_v((_M + 0.04, 0.08), (_M - 0.06, -0.12))],
    ';': [_v((_M, 0.5), (_M, 0.56)), _v((_M + 0.04, 0.08), (_M - 0.06, -0.12))],
    ':': [_v((_M, 0.5), (_M, 0.56)), _v((_M, 0.06), (_M, 0.12))],
    '!': [_v((_M, 1), (_M, 0.28)), _v((_M, 0.06), (_M, 0.12))],
    '?': [_v((0.06, 0.8), (0.18, 1), (_W - 0.12, 1), (_W, 0.8), (_W, 0.66), (_M, 0.46),
              (_M, 0.3)), _v((_M, 0.06), (_M, 0.12))],
    '=': [_v((0.04, 0.62), (_W - 0.04, 0.62)), _v((0.04, 0.38), (_W - 0.04, 0.38))],
    '&': [_v((_W, 0), (0.16, 0.62), (0.16, 0.84), (0.28, 1), (0.4, 0.86), (0.4, 0.68),
              (0, 0.34), (0, 0.14), (0.14, 0), (0.32, 0.04), (_W, 0.42))],
    '*': [_v((_M, 0.5), (_M, 1)), _v((_M - 0.2, 0.62), (_M + 0.2, 0.88)),
          _v((_M - 0.2, 0.88), (_M + 0.2, 0.62))],
    '%': [_v((_W, 1), (0, 0)), _v((0.02, 0.72), (0.16, 0.72)), _v((0.09, 0.72), (0.09, 1)),
          _v((0.02, 1), (0.16, 1)), _v((_W - 0.16, 0), (_W - 0.02, 0)),
          _v((_W - 0.09, 0), (_W - 0.09, 0.28)), _v((_W - 0.16, 0.28), (_W - 0.02, 0.28))],
    '@': [_v((_W - 0.06, 0.34), (0.34, 0.28), (0.18, 0.36), (0.18, 0.56), (0.34, 0.66),
              (_W - 0.14, 0.6), (_W - 0.14, 0.2), (_W - 0.3, 0.04), (0.14, 0.04),
              (0, 0.24), (0, 0.78), (0.16, 1), (_W - 0.14, 1), (_W, 0.82))],
    '$': [_v((_W, 0.88), (_W - 0.12, 1), (0.12, 1), (0, 0.86), (0, 0.68), (0.12, 0.56),
              (_W - 0.12, 0.5), (_W, 0.36), (_W, 0.16), (_W - 0.12, 0.02), (0.12, 0.02),
              (0, 0.14)), _v((_M, 1.08), (_M, -0.06))],
}

#: What an unmappable character becomes: a visible mark, so a label never silently
#: loses a character and reads as a different part number.
UNKNOWN = [_v((_M - 0.04, 0.44), (_M + 0.04, 0.44)), _v((_M - 0.04, 0.56), (_M + 0.04, 0.56))]


def text_strokes(text: str, height: float = 1.0, spacing: float = 1.0):
    """Centreline paths for `text`, cap height `height`, starting at (0, 0).

    Returns (strokes, width) where each stroke is a list of (x, y) points in inches.
    """
    strokes, pen = [], 0.0
    for char in str(text).upper():
        glyph = GLYPHS.get(char, UNKNOWN if not char.isspace() else [])
        for stroke in glyph:
            strokes.append([(pen + x * height, y * height) for x, y in stroke])
        pen += GLYPH_ADVANCE * height * spacing
    width = max(0.0, pen - (GLYPH_ADVANCE * height * spacing - _W * height)) if strokes else 0.0
    return strokes, width


def text_width(text: str, height: float = 1.0, spacing: float = 1.0) -> float:
    """How wide `text` will be at this height, without building the strokes."""
    return text_strokes(text, height, spacing)[1]
