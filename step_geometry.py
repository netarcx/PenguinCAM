"""Convert a single-solid STEP model into PenguinCAM's depth-layered DXF format.

The postprocessor already has a well-tested 2.5D input contract: horizontal faces are
stored as solid HATCH regions on ``Z_...`` layers, with Z measured up from the stock
bottom.  Onshape builds that file by exporting every face parallel to the selected top
face.  This module does the same thing locally with Open CASCADE so an uploaded STEP file
uses the exact same downstream preview and G-code path.

This is intentionally a *2.5D* importer, not a general 3D CAM kernel.  It accepts one
solid whose features are visible from one machining side.  Assemblies, undercuts, side
holes, sloped faces and free-form surfaces are refused rather than projected away.
"""

from dataclasses import dataclass
import io
import math
from typing import Iterable, List, Sequence, Tuple

import ezdxf
from shapely.geometry import Polygon
from shapely.ops import unary_union


MM_PER_INCH = 25.4
DEPTH_TOLERANCE_MM = 0.0254       # one thousandth of an inch
ANGULAR_DOT_TOLERANCE = 1e-5
CURVE_DEFLECTION_MM = 0.0254      # tessellate curves to one-thousandth-inch sag


class StepGeometryError(ValueError):
    """A STEP model cannot safely be represented as a single 2.5D setup."""


@dataclass(frozen=True)
class StepConversion:
    dxf_bytes: bytes
    thickness: float
    layer_depths: Tuple[float, ...]
    machining_normal: Tuple[float, float, float]


def _dot(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def _cross(a: Sequence[float], b: Sequence[float]) -> Tuple[float, float, float]:
    return (a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def _unit(v: Sequence[float]) -> Tuple[float, float, float]:
    mag = math.sqrt(_dot(v, v))
    if mag <= 1e-12:
        raise StepGeometryError("STEP model contains a face with no usable direction.")
    return tuple(x / mag for x in v)


def _canonical_axis(v: Sequence[float]) -> Tuple[float, float, float]:
    result = _unit(v)
    dominant = max(range(3), key=lambda i: abs(result[i]))
    if result[dominant] < 0:
        result = tuple(-x for x in result)
    return result


def _point_tuple(point) -> Tuple[float, float, float]:
    return point.X(), point.Y(), point.Z()


def _load_step(path: str):
    try:
        from OCP.IFSelect import IFSelect_RetDone
        from OCP.STEPControl import STEPControl_Reader
    except ImportError as exc:  # pragma: no cover - deployment/configuration failure
        raise StepGeometryError(
            "STEP import is not installed on this server. Install cadquery-ocp-novtk."
        ) from exc

    reader = STEPControl_Reader()
    if reader.ReadFile(str(path)) != IFSelect_RetDone:
        raise StepGeometryError("Could not read that STEP file. It may be damaged or unsupported.")
    if reader.TransferRoots() <= 0:
        raise StepGeometryError("The STEP file did not contain a transferable solid.")
    shape = reader.OneShape()
    if shape.IsNull():
        raise StepGeometryError("The STEP file did not contain any geometry.")
    return shape


def _explore(shape, kind, caster) -> Iterable:
    from OCP.TopExp import TopExp_Explorer

    explorer = TopExp_Explorer(shape, kind)
    while explorer.More():
        yield caster(explorer.Current())
        explorer.Next()


def _planar_faces(shape):
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepGProp import BRepGProp
    from OCP.GeomAbs import GeomAbs_Plane
    from OCP.GProp import GProp_GProps
    from OCP.TopAbs import TopAbs_FACE, TopAbs_REVERSED
    from OCP.TopoDS import TopoDS

    records = []
    for face in _explore(shape, TopAbs_FACE, TopoDS.Face_s):
        surface = BRepAdaptor_Surface(face)
        if surface.GetType() != GeomAbs_Plane:
            continue
        plane = surface.Plane()
        direction = plane.Axis().Direction()
        normal = (direction.X(), direction.Y(), direction.Z())
        if face.Orientation() == TopAbs_REVERSED:
            normal = tuple(-x for x in normal)
        props = GProp_GProps()
        BRepGProp.SurfaceProperties_s(face, props)
        records.append({
            'face': face,
            'normal': _unit(normal),
            'origin': _point_tuple(plane.Location()),
            'area': float(props.Mass()),
        })
    return records


def _choose_machining_axis(records):
    if not records:
        raise StepGeometryError("The STEP model has no planar face to use as the stock top.")

    # A plate's broad top/bottom face is the reliable machining-axis signal.  Group
    # parallel faces modulo direction so the larger bottom face of a counterbored plate
    # still selects the same axis as its interrupted top face.
    seed = max(records, key=lambda r: r['area'])
    axis = _canonical_axis(seed['normal'])
    parallel = [r for r in records if abs(_dot(r['normal'], axis)) >= 1 - ANGULAR_DOT_TOLERANCE]
    for record in parallel:
        record['axis_position'] = _dot(record['origin'], axis)

    low = min(r['axis_position'] for r in parallel)
    high = max(r['axis_position'] for r in parallel)
    if high - low <= DEPTH_TOLERANCE_MM:
        raise StepGeometryError("The STEP model has no measurable stock thickness.")

    internal = [r for r in parallel
                if r['axis_position'] > low + DEPTH_TOLERANCE_MM
                and r['axis_position'] < high - DEPTH_TOLERANCE_MM]

    def score(top_direction):
        upward = sum(r['area'] for r in internal
                     if _dot(r['normal'], top_direction) > 1 - ANGULAR_DOT_TOLERANCE)
        downward = sum(r['area'] for r in internal
                       if _dot(r['normal'], top_direction) < -1 + ANGULAR_DOT_TOLERANCE)
        return downward, -upward

    positive = axis
    negative = tuple(-x for x in axis)
    machining_normal = min((positive, negative), key=score)
    bad_area, _ = score(machining_normal)
    if bad_area > (0.001 * MM_PER_INCH) ** 2:
        raise StepGeometryError(
            "This part has features opening from both sides or an undercut. "
            "PenguinCAM STEP import supports one top-down 2.5D setup at a time."
        )
    return machining_normal, parallel


def _validate_surfaces(shape, machining_normal):
    """Refuse surfaces that would disappear in a top projection.

    Planar walls must be horizontal or vertical.  Cylinders/cones/extrusions must run
    along Z; a horizontal cylinder is a side hole.  Free-form surfaces and fillets need
    real 3D toolpaths, which this importer deliberately does not invent.
    """
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.GeomAbs import (GeomAbs_BSplineSurface, GeomAbs_BezierSurface,
                            GeomAbs_Cone, GeomAbs_Cylinder, GeomAbs_OffsetSurface,
                            GeomAbs_OtherSurface, GeomAbs_Plane, GeomAbs_Sphere,
                            GeomAbs_SurfaceOfExtrusion, GeomAbs_SurfaceOfRevolution,
                            GeomAbs_Torus)
    from OCP.TopAbs import TopAbs_FACE
    from OCP.TopoDS import TopoDS

    unsupported = {
        GeomAbs_Sphere: 'spherical surface', GeomAbs_Torus: 'fillet/torus',
        GeomAbs_BezierSurface: 'Bezier surface', GeomAbs_BSplineSurface: 'free-form surface',
        GeomAbs_SurfaceOfRevolution: 'revolved surface', GeomAbs_OffsetSurface: 'offset surface',
        GeomAbs_OtherSurface: 'unsupported surface',
    }
    for face in _explore(shape, TopAbs_FACE, TopoDS.Face_s):
        surface = BRepAdaptor_Surface(face)
        kind = surface.GetType()
        if kind == GeomAbs_Plane:
            direction = surface.Plane().Axis().Direction()
            alignment = abs(_dot((direction.X(), direction.Y(), direction.Z()), machining_normal))
            if alignment > 1 - ANGULAR_DOT_TOLERANCE or alignment < ANGULAR_DOT_TOLERANCE:
                continue
            raise StepGeometryError(
                "The STEP model contains a sloped planar face. Only vertical walls and "
                "flat depth levels are supported in 2.5D mode."
            )
        if kind in (GeomAbs_Cylinder, GeomAbs_Cone):
            primitive = surface.Cylinder() if kind == GeomAbs_Cylinder else surface.Cone()
            direction = primitive.Axis().Direction()
            alignment = abs(_dot((direction.X(), direction.Y(), direction.Z()), machining_normal))
            if alignment > 1 - ANGULAR_DOT_TOLERANCE:
                continue
            raise StepGeometryError(
                "The STEP model contains a side-facing hole or curved wall. "
                "Only holes parallel to the machining axis are supported."
            )
        if kind == GeomAbs_SurfaceOfExtrusion:
            direction = surface.Direction()
            alignment = abs(_dot((direction.X(), direction.Y(), direction.Z()), machining_normal))
            if alignment > 1 - ANGULAR_DOT_TOLERANCE:
                continue
        label = unsupported.get(kind, 'unsupported curved surface')
        raise StepGeometryError(
            f"The STEP model contains a {label}. Fillets and free-form 3D surfaces "
            "are not supported by the 2.5D importer."
        )


def _sample_wire(wire, face, basis_u, basis_v):
    from OCP.BRep import BRep_Tool
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.BRepTools import BRepTools_WireExplorer
    from OCP.GCPnts import GCPnts_QuasiUniformDeflection
    from OCP.TopoDS import TopoDS

    points_3d = []
    explorer = BRepTools_WireExplorer(wire, face)
    while explorer.More():
        edge = TopoDS.Edge_s(explorer.Current())
        curve = BRepAdaptor_Curve(edge)
        edge_points = []
        try:
            sampler = GCPnts_QuasiUniformDeflection(curve, CURVE_DEFLECTION_MM)
            if sampler.IsDone():
                edge_points = [_point_tuple(sampler.Value(i))
                               for i in range(1, sampler.NbPoints() + 1)]
        except Exception:
            edge_points = []
        if len(edge_points) < 2:
            edge_points = [_point_tuple(curve.Value(curve.FirstParameter())),
                           _point_tuple(curve.Value(curve.LastParameter()))]

        # WireExplorer exposes the topological start vertex, which is more reliable than
        # assuming the underlying curve parameter follows the edge's orientation.
        vertex_point = _point_tuple(BRep_Tool.Pnt_s(explorer.CurrentVertex()))
        start_distance = sum((a - b) ** 2 for a, b in zip(edge_points[0], vertex_point))
        end_distance = sum((a - b) ** 2 for a, b in zip(edge_points[-1], vertex_point))
        if end_distance < start_distance:
            edge_points.reverse()
        if points_3d and sum((a - b) ** 2 for a, b in zip(points_3d[-1], edge_points[0])) < 1e-12:
            edge_points = edge_points[1:]
        points_3d.extend(edge_points)
        explorer.Next()

    if len(points_3d) < 3:
        return []
    points = [(_dot(p, basis_u) / MM_PER_INCH, _dot(p, basis_v) / MM_PER_INCH)
              for p in points_3d]
    if math.dist(points[0], points[-1]) > 1e-8:
        points.append(points[0])
    return points


def _nest_loops(loops: List[List[Tuple[float, float]]]) -> List[Polygon]:
    candidates = []
    for loop in loops:
        if len(loop) < 4:
            continue
        polygon = Polygon(loop)
        if not polygon.is_valid:
            polygon = polygon.buffer(0)
        if isinstance(polygon, Polygon) and not polygon.is_empty and polygon.area > 1e-10:
            candidates.append(polygon)
    candidates.sort(key=lambda p: p.area, reverse=True)
    if not candidates:
        return []

    parent = [None] * len(candidates)
    for i, inner in enumerate(candidates):
        probe = inner.representative_point()
        containers = [(outer.area, j) for j, outer in enumerate(candidates)
                      if outer.area > inner.area and outer.contains(probe)]
        if containers:
            parent[i] = min(containers)[1]

    def depth(index):
        result = 0
        while parent[index] is not None:
            result += 1
            index = parent[index]
        return result

    depths = [depth(i) for i in range(len(candidates))]
    solids = []
    for i, outer in enumerate(candidates):
        if depths[i] % 2:
            continue
        holes = [list(candidates[j].exterior.coords) for j in range(len(candidates))
                 if parent[j] == i]
        polygon = Polygon(outer.exterior.coords, holes)
        if polygon.is_valid and not polygon.is_empty:
            solids.append(polygon)
    return solids


def _face_polygons(face, basis_u, basis_v) -> List[Polygon]:
    from OCP.TopAbs import TopAbs_WIRE
    from OCP.TopoDS import TopoDS

    loops = [_sample_wire(wire, face, basis_u, basis_v)
             for wire in _explore(face, TopAbs_WIRE, TopoDS.Wire_s)]
    return _nest_loops([loop for loop in loops if loop])


def _layer_name(depth_inches: float) -> str:
    mils = max(0, int(round(depth_inches * 1000)))
    return f"Z_{mils // 1000}p{mils % 1000:03d}"


def _add_polygon_hatch(modelspace, polygon: Polygon, layer_name: str):
    hatch = modelspace.add_hatch(color=7, dxfattribs={'layer': layer_name})
    hatch.paths.add_polyline_path(list(polygon.exterior.coords), is_closed=True)
    for interior in polygon.interiors:
        hatch.paths.add_polyline_path(list(interior.coords), is_closed=True, flags=0)


def convert_step_to_multilayer_dxf(path: str) -> StepConversion:
    """Read ``path`` and return a depth-layered DXF suitable for ``FRCPostProcessor``."""
    from OCP.TopAbs import TopAbs_SOLID
    from OCP.TopoDS import TopoDS

    shape = _load_step(path)
    solids = list(_explore(shape, TopAbs_SOLID, TopoDS.Solid_s))
    if len(solids) != 1:
        raise StepGeometryError(
            f"STEP import requires exactly one solid; this file contains {len(solids)}. "
            "Export one part at a time."
        )
    shape = solids[0]
    records = _planar_faces(shape)
    machining_normal, parallel = _choose_machining_axis(records)
    _validate_surfaces(shape, machining_normal)

    # Stable right-handed XY basis perpendicular to the selected top direction.
    helper = (1.0, 0.0, 0.0) if abs(machining_normal[0]) < 0.9 else (0.0, 1.0, 0.0)
    basis_u = _unit(_cross(helper, machining_normal))
    basis_v = _unit(_cross(machining_normal, basis_u))

    for record in parallel:
        record['position'] = _dot(record['origin'], machining_normal)
    bottom = min(r['position'] for r in parallel)
    top = max(r['position'] for r in parallel)
    thickness = (top - bottom) / MM_PER_INCH
    if thickness <= 0:
        raise StepGeometryError("The STEP model has no measurable stock thickness.")

    bins = []
    for record in sorted(parallel, key=lambda r: r['position']):
        existing = next((entry for entry in bins
                         if abs(entry['position'] - record['position']) <= DEPTH_TOLERANCE_MM), None)
        if existing is None:
            existing = {'position': record['position'], 'faces': []}
            bins.append(existing)
        existing['faces'].append(record['face'])

    if len(bins) < 2:
        raise StepGeometryError("The STEP model did not expose both a top and bottom face.")

    document = ezdxf.new('R2010', setup=True)
    modelspace = document.modelspace()
    layer_depths = []
    for entry in bins:
        depth = (entry['position'] - bottom) / MM_PER_INCH
        layer_depths.append(depth)
        name = _layer_name(depth)
        if name not in document.layers:
            document.layers.add(name)
        face_polygons = []
        for face in entry['faces']:
            face_polygons.extend(_face_polygons(face, basis_u, basis_v))
        if not face_polygons:
            raise StepGeometryError(
                f"Could not recover the face boundaries at depth {depth:.4f} inches."
            )
        combined = unary_union(face_polygons)
        polygons = [combined] if isinstance(combined, Polygon) else list(combined.geoms)
        for polygon in polygons:
            if isinstance(polygon, Polygon) and not polygon.is_empty:
                _add_polygon_hatch(modelspace, polygon, name)

    stream = io.StringIO()
    document.write(stream)
    return StepConversion(
        dxf_bytes=stream.getvalue().encode(document.output_encoding),
        thickness=thickness,
        layer_depths=tuple(layer_depths),
        machining_normal=tuple(machining_normal),
    )
