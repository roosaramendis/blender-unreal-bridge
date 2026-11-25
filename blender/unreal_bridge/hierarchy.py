# SPDX-License-Identifier: GPL-3.0-or-later
"""Scene mode: turns Blender parenting / collection instances into Unreal groups or Blueprints.

A *unit* is a top-level object that has children (usually an empty holding a kitbashed house)
plus everything under it. Each node keeps its transform relative to its nearest kept ancestor.

Unreal composes child transforms as location/rotation/scale and can't represent the skew a
non-uniformly scaled parent gives a rotated child. Such pieces are left out of the unit and
placed at their exact world position instead, so nothing is silently distorted.
"""
import json
import math
from collections import defaultdict

from mathutils import Matrix, Quaternion, Vector

# Blender lights / cameras look down local -Z with +Y up; Unreal ones look down +X with +Z up.
_AIM = Matrix(((0.0, -1.0, 0.0), (0.0, 0.0, 1.0), (-1.0, 0.0, 0.0))).to_4x4()


def aim_matrix(matrix):
    """World matrix of a light / camera in 'Unreal axes' (scale kept, so a light inside a scaled
    house has the same relative transform as in an unscaled copy)."""
    return matrix @ _AIM


_FLIPS = [Matrix.Diagonal((-1.0, 1.0, 1.0, 1.0)), Matrix.Diagonal((1.0, -1.0, 1.0, 1.0)),
          Matrix.Diagonal((1.0, 1.0, -1.0, 1.0))]
MIRROR_X = _FLIPS[0]


def decompose(matrix):
    """Location, rotation, scale like Matrix.decompose(), except a mirror (negative determinant) is
    written as ONE negative scale axis plus a rotation. Blender's own decompose gives scale -1 on all
    three axes plus a 180 degree turn, which Unreal mishandles (it drops the sign when the piece is
    edited). The mirror axis that needs the least rotation is used, which is usually the axis the
    object was actually mirrored on."""
    if matrix.to_3x3().determinant() >= 0:
        return matrix.decompose()
    best = None
    for axis, flip in enumerate(_FLIPS):
        loc, rot, scale = (matrix @ flip).decompose()      # positive determinant now
        scale = scale.copy()
        scale[axis] = -scale[axis]
        angle = rot.angle
        if best is None or angle < best[0] - 1e-6:
            best = (angle, loc, rot, scale)
    return best[1], best[2], best[3]


def is_mirrored(matrix):
    return matrix.to_3x3().determinant() < 0


def node_kind(obj_type, mesh_types):
    if obj_type in mesh_types:
        return "mesh"
    if obj_type in ("LIGHT", "CAMERA"):
        return obj_type.lower()
    return "empty"


def _representable(parent, local, actual):
    """Does Unreal's composition of parent * local reproduce `actual`?

    Unreal composes two ways. Without negative scales: location/rotation/scale are combined
    piecewise (skew from a non-uniformly scaled parent is lost). With a negative scale anywhere
    (a mirror): the exact matrix product is taken and re-split using scale = parent * child scale,
    which is exact whenever that split exists. Both are modelled here."""
    pl, pr, ps = decompose(parent)
    ll, lr, ls = decompose(local)
    scale = Vector((ps.x * ls.x, ps.y * ls.y, ps.z * ls.z))
    size = max(1.0, max(abs(v) for row in actual for v in row))
    if min(ps) < 0 or min(ls) < 0:
        if min(abs(v) for v in scale) < 1e-9:
            return False
        rot = actual.to_3x3() @ Matrix.Diagonal((1.0 / scale.x, 1.0 / scale.y, 1.0 / scale.z))
        ortho = rot @ rot.transposed()
        return (all(abs(ortho[i][j] - (1.0 if i == j else 0.0)) < 1e-4 for i in range(3) for j in range(3))
                and rot.determinant() > 0)
    loc = pl + pr @ Vector((ps.x * ll.x, ps.y * ll.y, ps.z * ll.z))
    composed = Matrix.LocRotScale(loc, pr @ lr, scale)
    return all(abs(a - b) <= 1e-4 * size for ra, rb in zip(composed, actual) for a, b in zip(ra, rb))


def _rounded(t):
    """Comparable form of a transform: 0.1 mm / ~0.01 degree steps, no -0.0, one quaternion sign."""
    q = list(t["rotation_quat"])
    lead = next((v for v in (q[3], q[0], q[1], q[2]) if abs(v) > 1e-6), 1.0)
    if lead < 0:
        q = [-v for v in q]
    return ([round(v, 2) + 0.0 for v in t["location"]], [round(v, 4) + 0.0 for v in q],
            [round(v, 4) + 0.0 for v in t["scale"]])


def build(nodes, objects, lights, cameras, to_unreal, unit, mode, instanced, names, report,
          group_by='PARENT', pivots=None, failures=None):
    """nodes: [{name, parent, kind, matrix, ref, label, collection, structural}] for every placement.
    group_by 'PARENT': each top-level object with children is a unit.
    group_by 'COLLECTION': each collection (Outliner folder) that directly holds objects is a unit,
    with its origin at pivots[collection path].
    Marks member entries in objects / lights / cameras with "unit" and returns the manifest block."""
    by_name = {n["name"]: n for n in nodes}
    by_coll = group_by == 'COLLECTION'
    children = defaultdict(list)
    roots = []
    for n in nodes:
        if n["parent"] in by_name and n["parent"] != n["name"]:
            children[n["parent"]].append(n["name"])
        else:
            roots.append(n["name"])

    def kids(name):
        if not by_coll:
            return children[name]
        coll = by_name[name]["collection"]
        return [c for c in children[name] if by_name[c]["collection"] == coll]

    content = {}

    def has_content(name):
        if name not in content:
            content[name] = by_name[name]["ref"] is not None or any(has_content(c) for c in kids(name))
        return content[name]

    def content_key(n):
        if n["ref"] is None:
            return "empty"
        if n["kind"] == "mesh":
            return "mesh:" + objects[n["ref"]]["mesh"]
        entry = (lights if n["kind"] == "light" else cameras)[n["ref"]]
        keep = {k: v for k, v in entry.items() if k not in ("name", "label", "collection", "unreal", "unit")}
        return n["kind"] + ":" + json.dumps(keep, sort_keys=True, default=str)

    identity = to_unreal(Matrix.Identity(4), unit)
    units, blueprints, signatures, loose = [], {}, {}, []

    def make_unit(root_m, root_label, root_node, top, folder, name):
        """root_node: the node standing at the origin (None for a collection's pivot)."""
        ui = len(units)
        unit_nodes = [{"id": 0, "parent": None, "kind": "empty", "ref": None, "label": root_label,
                       "local": identity, "world": to_unreal(root_m, unit), "in_root": identity,
                       "leaf": False, "key": "empty"}]
        members = []
        if root_node is not None and root_node["ref"] is not None:
            unit_nodes[0].update(kind=root_node["kind"], ref=root_node["ref"], key=content_key(root_node))
            members.append(root_node)

        def walk(names_, anchor_id, anchor_m):
            entries = []
            for c in names_:
                if not has_content(c):
                    continue
                cn = by_name[c]
                local = anchor_m.inverted_safe() @ cn["matrix"]
                entries.append((content_key(cn), _rounded(to_unreal(local, unit)), c, local))
            entries.sort(key=lambda k: (k[0], k[1], k[2]))   # canonical order: copies match
            for key, _, c, local in entries:
                cn = by_name[c]
                if _representable(anchor_m, local, cn["matrix"]):
                    nid = len(unit_nodes)
                    in_root = root_m.inverted_safe() @ cn["matrix"]
                    unit_nodes.append({
                        "id": nid, "parent": anchor_id, "kind": cn["kind"] if cn["ref"] is not None else "empty",
                        "ref": cn["ref"], "label": cn["label"], "key": key,
                        "local": to_unreal(local, unit), "world": to_unreal(cn["matrix"], unit),
                        "in_root": to_unreal(in_root, unit) if _representable(root_m, in_root, cn["matrix"]) else None,
                        "mirrored": is_mirrored(in_root),
                        # transform inside an instanced component that is itself mirrored on X
                        "in_root_unmirrored": to_unreal(MIRROR_X @ in_root, unit) if is_mirrored(in_root) else None,
                        "leaf": True})
                    unit_nodes[anchor_id]["leaf"] = False
                    members.append(cn)
                    walk(kids(c), nid, cn["matrix"])
                else:
                    if failures is not None:
                        failures[c] = (anchor_m.copy(), local.copy())
                    if cn["ref"] is not None:
                        loose.append(cn["name"])
                    walk(kids(c), anchor_id, anchor_m)   # its children attach one level up

        walk(top, 0, root_m)
        if len(unit_nodes) < 2 and unit_nodes[0]["ref"] is None:
            return
        for n in members:
            if n["ref"] is not None:
                {"mesh": objects, "light": lights, "camera": cameras}[n["kind"]][n["ref"]]["unit"] = ui
        entry = {"name": name, "label": root_label, "collection": folder,
                 "world": to_unreal(root_m, unit), "nodes": unit_nodes, "blueprint": None}
        if mode == 'BLUEPRINT':
            sig = json.dumps([(n["parent"], n["key"], _rounded(n["local"])) for n in unit_nodes])
            if sig not in signatures:
                signatures[sig] = names.make("BP_", root_label)
                blueprints[signatures[sig]] = {"source_unit": ui, "uses": 0}
            entry["blueprint"] = signatures[sig]
            blueprints[signatures[sig]]["uses"] += 1
        units.append(entry)

    if by_coll:
        tops = defaultdict(list)
        for n in nodes:
            coll = n["collection"]
            if not coll:
                continue            # objects directly in the Scene Collection stay loose
            parent = by_name.get(n["parent"])
            if parent is None or parent["collection"] != coll:
                tops[coll].append(n["name"])
        for coll in sorted(tops):
            if not any(has_content(t) for t in tops[coll]):
                continue
            leaf = coll.rsplit("/", 1)
            folder = leaf[0] if len(leaf) > 1 else ""     # "" = directly in the Blender folder
            make_unit(pivots[coll], _safe(leaf[-1]), None, tops[coll], folder, coll)
    else:
        for root_name in roots:
            root = by_name[root_name]
            if any(has_content(c) for c in kids(root_name)):
                make_unit(root["matrix"], root["label"], root, kids(root_name), root["collection"], root_name)

    if loose:
        shown = ", ".join(loose[:5]) + (f" and {len(loose) - 5} more" if len(loose) > 5 else "")
        report.warn(f"{len(loose)} piece(s) left out of their group: their parent is scaled differently "
                    f"along each axis and they are rotated, which Unreal can't reproduce. They are placed "
                    f"on their own at their exact position: {shown}")
    for u in units:
        for n in u["nodes"]:
            n.pop("key", None)
    return {"mode": mode, "group_by": group_by, "instanced": bool(instanced), "units": units,
            "blueprints": blueprints}


def _safe(raw):
    import re
    return re.sub(r'[^A-Za-z0-9_]+', '_', raw).strip('_') or "Group"


def collection_pivots(nodes, report):
    """Origin of each collection's group: its Instance Offset if set, else the bottom-centre of the
    bounds of what it holds."""
    import bpy
    pts = defaultdict(list)
    for n in nodes:
        if n["collection"] and n.get("corners"):
            pts[n["collection"]].extend(n["corners"])
        elif n["collection"] and n["ref"] is not None:
            pts[n["collection"]].append(n["matrix"].translation.copy())
    out = {}
    for coll in {n["collection"] for n in nodes if n["collection"]}:
        col = bpy.data.collections.get(coll.rsplit("/", 1)[-1])
        offset = Vector(col.instance_offset) if col is not None else Vector()
        if offset.length > 1e-6 or not pts[coll]:
            out[coll] = Matrix.Translation(offset)
        else:
            p = pts[coll]
            lo = Vector((min(v.x for v in p), min(v.y for v in p), min(v.z for v in p)))
            hi = Vector((max(v.x for v in p), max(v.y for v in p), max(v.z for v in p)))
            out[coll] = Matrix.Translation(Vector(((lo.x + hi.x) / 2, (lo.y + hi.y) / 2, lo.z)))
    return out


class _Quiet:
    def warn(self, msg):
        pass


class _Names:
    def __init__(self):
        self.n = 0

    def make(self, prefix, raw):
        self.n += 1
        return f"{prefix}{self.n}"


def world_representable(matrix):
    """Can a single Unreal location/rotation/scale reproduce this matrix (no slant)?"""
    loc, rot, scale = decompose(matrix)
    back = Matrix.LocRotScale(loc, rot, scale)
    size = max(1.0, max(abs(v) for row in matrix for v in row))
    return all(abs(a - b) <= 1e-4 * size for ra, rb in zip(back, matrix) for a, b in zip(ra, rb))


def plan_skew(nodes, mode, group_by, pivots, to_unreal, unit):
    """Find mesh pieces whose transform Unreal can't reproduce (a slant from an unevenly scaled
    parent plus a rotation) and plan how to keep them exact:
      returns {node name: (B, placed)} where B (3x3) is baked into a copy of the mesh and `placed`
      is the new world matrix, which Unreal can reproduce.
    Inside a group the piece keeps only its position relative to its parent (the parent's uneven
    scale then applies to the already-slanted mesh, exactly like in Blender); a piece on its own
    keeps position and rotation."""
    fixes = {}
    current = {n["name"]: n["matrix"] for n in nodes}
    members = set()
    if mode != 'AS_IS':
        for _ in range(16):
            probe = [dict(n, matrix=current[n["name"]],
                          ref=i if n["kind"] in ("mesh", "light", "camera") else None)
                     for i, n in enumerate(nodes)]
            dummy = [{"mesh": n["name"]} for n in nodes]
            fails = {}
            build(probe, dummy, [{} for _ in nodes], [{} for _ in nodes], to_unreal, unit, mode, False,
                  _Names(), _Quiet(), group_by, pivots, failures=fails)
            kinds = {n["name"]: n["kind"] for n in probe}
            fails = {k: v for k, v in fails.items() if kinds[k] == "mesh"}
            members = {nodes[i]["name"] for i, d in enumerate(dummy) if "unit" in d}
            if not fails:
                break
            for name, (anchor_m, local) in fails.items():
                linear = local.to_3x3()
                prev = fixes.get(name)
                base = prev[0] if prev else Matrix.Identity(3)
                fixes[name] = (linear @ base, anchor_m @ Matrix.Translation(local.translation))
                current[name] = fixes[name][1]
    # pieces on their own (As Is, or not in any group): their world transform must be reproducible
    for n in nodes:
        if n["kind"] != "mesh" or n.get("structural") or n["name"] in members:
            continue
        m = current[n["name"]]
        if world_representable(m):
            continue
        loc, rot, _ = decompose(m)
        r3 = rot.to_matrix()
        linear = r3.inverted() @ m.to_3x3()
        prev = fixes.get(n["name"])
        base = prev[0] if prev else Matrix.Identity(3)
        fixes[n["name"]] = (linear @ base, Matrix.Translation(loc) @ r3.to_4x4())
    return fixes
