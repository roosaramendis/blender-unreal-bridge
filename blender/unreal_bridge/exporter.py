# SPDX-License-Identifier: GPL-3.0-or-later
"""Core export: per-mesh FBX files + textures + manifest.json."""
import hashlib
import json
import os
import re
import shutil
import time

import bmesh
import bpy
import numpy as np
import math

from mathutils import Matrix, Vector

from . import baker, hierarchy
from .materials import MaterialExporter

FORMAT_VERSION = 3
ADDON_VERSION = "0.15.0"
MESH_TYPES = {'MESH', 'CURVE', 'SURFACE', 'FONT', 'META'}
POINTER_FILE = os.path.join(os.path.expanduser("~"), ".config", "blender_bridge", "last_export.json")
# Blender image format -> extension Unreal can import
IMAGE_EXT = {'PNG': '.png', 'JPEG': '.jpg', 'TARGA': '.tga', 'TARGA_RAW': '.tga', 'BMP': '.bmp',
             'TIFF': '.tif', 'OPEN_EXR': '.exr', 'HDR': '.hdr'}
COPY_EXT = {'.png', '.jpg', '.jpeg', '.tga', '.bmp', '.tif', '.tiff', '.exr', '.hdr'}


class ExportError(Exception):
    pass


class Report:
    def __init__(self):
        self.warnings = []

    def warn(self, msg):
        if msg not in self.warnings:
            self.warnings.append(msg)


class Names:
    """Unreal-safe, unique asset names."""

    def __init__(self):
        self.used = set()

    def make(self, prefix, raw):
        base = prefix + (re.sub(r'[^A-Za-z0-9_]+', '_', raw).strip('_') or "Unnamed")
        name, i = base, 1
        while name.lower() in self.used:
            i += 1
            name = f"{base}_{i}"
        self.used.add(name.lower())
        return name


def content_path(raw):
    """Accept '/Game/Foo' or a real folder inside a project's Content directory."""
    p = raw.strip().replace("\\", "/")
    if not p.startswith("/Game"):
        i = p.find("/Content")
        rest = p[i + len("/Content"):] if i >= 0 else p
        p = "/Game/" + rest.strip("/")
    parts = [re.sub(r'[^A-Za-z0-9_]+', '_', x) for x in p.split("/") if x]
    return "/" + "/".join(parts)


def safe_name(raw):
    return re.sub(r'[^A-Za-z0-9_]+', '_', raw).strip('_') or "Untitled"


def to_unreal(matrix, unit):
    """Blender world matrix -> Unreal location (cm) / quaternion (x,y,z,w) / scale.
    Blender is right-handed, Unreal left-handed: the Y axis is mirrored."""
    loc, rot, scale = hierarchy.decompose(matrix)     # mirrors as one negative axis
    return {
        "location": [loc.x * unit, -loc.y * unit, loc.z * unit],
        "rotation_quat": [-rot.x, rot.y, -rot.z, rot.w],
        "scale": [scale.x, scale.y, scale.z],
    }


def to_unreal_aim(matrix, unit):
    """Lights and cameras: Blender looks down local -Z with +Y up. Unreal gets the world-space
    forward and up vectors and builds its own rotation from them."""
    rot = matrix.to_3x3().normalized()
    f, u, loc = rot @ Vector((0.0, 0.0, -1.0)), rot @ Vector((0.0, 1.0, 0.0)), matrix.translation
    return {"location": [loc.x * unit, -loc.y * unit, loc.z * unit],
            "forward": [f.x, -f.y, f.z], "up": [u.x, -u.y, u.z]}


def _collection_paths(scene):
    """object name -> 'Collection/Sub collection' path for Outliner folders."""
    paths = {}

    def walk(col, prefix):
        for child in col.children:
            path = f"{prefix}/{child.name}" if prefix else child.name
            for o in child.objects:
                paths.setdefault(o.name, path)
            walk(child, path)
    walk(scene.collection, "")
    return paths


def _light_entry(obj, matrix, unit):
    """Intensities use measured Cycles behaviour, on the scale 'Blender value 1.0 = 1 nit':
    sun W/m2 -> lux 1:1, point/spot W -> candela W/(4 pi), area W -> candela W/pi."""
    l = obj.data
    scale = matrix.to_scale()
    e = {"type": l.type, "color": list(l.color), "energy": l.energy, "cast_shadow": bool(l.use_shadow),
         "unreal": to_unreal_aim(matrix, unit)}
    if getattr(l, "use_temperature", False):
        e["temperature"] = l.temperature
    if l.type == 'SUN':
        e.update(intensity=l.energy, units="lux", angle=math.degrees(l.angle))
    elif l.type in {'POINT', 'SPOT'}:
        e.update(intensity=l.energy / (4.0 * math.pi), units="candela", radius=l.shadow_soft_size * unit)
        if l.type == 'SPOT':
            outer = math.degrees(l.spot_size) / 2.0
            e.update(outer_cone=outer, inner_cone=outer * (1.0 - l.spot_blend))
    elif l.type == 'AREA':
        sx = l.size * abs(scale.x)
        sy = (l.size_y if l.shape in {'RECTANGLE', 'ELLIPSE'} else l.size) * abs(scale.y)
        e.update(intensity=l.energy / math.pi, units="candela", shape=l.shape,
                 width=sx * unit, height=sy * unit)
    return e


def _camera_entry(obj, matrix, unit, scene):
    c = obj.data
    r = scene.render
    rx, ry = r.resolution_x * r.pixel_aspect_x, r.resolution_y * r.pixel_aspect_y
    if c.sensor_fit == 'VERTICAL' or (c.sensor_fit == 'AUTO' and ry > rx):
        sh = c.sensor_height if c.sensor_fit == 'VERTICAL' else c.sensor_width
        sw = sh * rx / ry
    else:
        sw = c.sensor_width
        sh = sw * ry / rx
    e = {"type": c.type, "lens": c.lens, "sensor_width": sw, "sensor_height": sh,
         "active": scene.camera == obj, "unreal": to_unreal_aim(matrix, unit),
         "use_dof": bool(c.dof.use_dof), "fstop": c.dof.aperture_fstop}
    focus = c.dof.focus_distance
    if c.dof.focus_object is not None:
        focus = (c.dof.focus_object.matrix_world.translation - matrix.translation).length
    e["focus_distance"] = focus * unit
    return e


def _export_world(scene, out_dir, names, report):
    """Background colour or equirectangular HDRI of the scene's world."""
    from .materials import MaterialExporter, _plain
    world = scene.world
    if world is None:
        return {"type": "none"}
    tree = world.node_tree
    if tree is None:
        return {"type": "color", "color": list(world.color), "strength": 1.0}
    mx = MaterialExporter(report)      # separate instance: its image list is not exported as textures
    info = {"type": "color", "color": [0.05, 0.05, 0.05], "strength": 1.0, "node_tree": mx.dump_tree(tree)}
    out = None
    for target in ('ALL', 'CYCLES', 'EEVEE'):
        out = tree.get_output_node(target)
        if out:
            break
    node, _, ctx, _ = mx._src(out.inputs["Surface"], ()) if out else (None, None, (), None)
    if node is None or node.type != 'BACKGROUND':
        report.warn("World: only a plain Background shader is supported; a dark grey sky is used")
        return info
    strength = mx._const(node.inputs["Strength"], ctx)
    info["strength"] = strength if isinstance(strength, (int, float)) else 1.0
    src, _, sctx, term = mx._src(node.inputs["Color"], ctx)
    if src is None:
        if term is not None:
            info["color"] = list(_plain(term.default_value))[:3]
        return info
    if src.type == 'TEX_ENVIRONMENT' and src.image is not None:
        if src.projection != 'EQUIRECTANGULAR':
            report.warn("World: only equirectangular HDRIs are supported")
            return info
        m, _, mctx, _ = mx._src(src.inputs["Vector"], sctx)
        rot_z = 0.0
        if m is not None and m.type == 'MAPPING':
            r = mx._const(m.inputs["Rotation"], mctx)
            rot_z = r[2] if r else 0.0
        img = src.image
        hdri_dir = os.path.join(out_dir, "hdri")
        os.makedirs(hdri_dir, exist_ok=True)
        clean = re.sub(r'\.(hdr|exr|png|jpe?g|tiff?)$', '', re.sub(r'\.\d{3}$', '', img.name), flags=re.IGNORECASE)
        asset = names.make("HDR_", clean)
        dest = os.path.join(hdri_dir, asset + ".hdr")
        path = bpy.path.abspath(img.filepath_raw, library=img.library) if img.filepath_raw else ""
        try:
            if path.lower().endswith(".hdr") and os.path.isfile(path) and not img.packed_file:
                shutil.copy2(path, dest)
            else:  # Unreal only builds a cubemap from .hdr, so convert (EXR etc.)
                st = scene.render.image_settings
                old_format = st.file_format
                st.file_format = 'HDR'
                try:
                    img.save_render(dest, scene=scene)
                finally:
                    st.file_format = old_format
        except Exception as exc:
            report.warn(f"World: could not export HDRI '{img.name}': {exc}")
            return info
        info.update(type="hdri", asset_name=asset, file="hdri/" + asset + ".hdr", sha1=_sha1(dest),
                    rotation_z=rot_z, image=img.name)
        return info
    if src.type == 'TEX_SKY':
        info["type"] = "sky"
        report.warn("World: the procedural Sky Texture is not converted yet; a dark grey sky is used")
        return info
    report.warn(f"World: colour comes from '{src.bl_idname}', which is not supported; a dark grey sky is used")
    return info


def _sha1(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# Modifiers whose result depends only on the mesh and their own settings. Anything else (a
# Boolean cutter, Geometry Nodes, Shrinkwrap, physics...) can differ per copy.
_LOCAL_MODIFIERS = {
    'BEVEL', 'SUBSURF', 'MULTIRES', 'WEIGHTED_NORMAL', 'TRIANGULATE', 'EDGE_SPLIT', 'SOLIDIFY',
    'DECIMATE', 'WELD', 'MIRROR', 'ARRAY', 'SMOOTH', 'LAPLACIANSMOOTH', 'REMESH', 'SCREW', 'SKIN',
    'WIREFRAME', 'DISPLACE', 'SIMPLE_DEFORM', 'CAST', 'MASK', 'BUILD', 'NORMAL_EDIT'}
_VOLATILE_PROPS = {"name", "show_expanded", "is_active", "persistent_uid", "is_override_data",
                   "show_on_cage", "show_in_editmode", "use_pin_to_last", "execution_time",
                   "show_render", "use_apply_on_spline"}


def _modifier_signature(obj):
    """Text describing the modifier stack, or None if copies could come out different."""
    parts = []
    for mod in obj.modifiers:
        if not mod.show_viewport:
            continue
        if mod.type not in _LOCAL_MODIFIERS:
            return None
        if mod.type == 'DISPLACE' and mod.texture_coords in {'GLOBAL', 'OBJECT'}:
            return None
        props = []
        for p in mod.bl_rna.properties:
            pid = p.identifier
            if pid in _VOLATILE_PROPS or pid == "rna_type":
                continue
            val = getattr(mod, pid, None)
            if isinstance(val, bpy.types.Object):
                return None                     # mirror object, array offset object, ...
            if isinstance(val, bpy.types.ID):
                val = "ID:" + val.name
            elif p.type in {'BOOLEAN', 'INT', 'FLOAT', 'STRING', 'ENUM'}:
                try:
                    val = tuple(val) if p.type != 'STRING' and getattr(p, "array_length", 0) else val
                except TypeError:
                    pass
                if isinstance(val, set):
                    val = tuple(sorted(val))
            else:
                continue
            props.append((pid, val))
        parts.append((mod.type, tuple(props)))
    return repr(parts)


def _mesh_key(obj, bake_scale=False):
    """Objects with the same key share one exported mesh."""
    if obj.type != 'MESH' or any(s.link != 'DATA' for s in obj.material_slots):
        return "object:" + obj.name
    key = "data:" + obj.data.name
    if any(m.show_viewport for m in obj.modifiers):
        sig = _modifier_signature(obj)
        if sig is None:
            return "object:" + obj.name
        key += "#" + hashlib.sha1(sig.encode()).hexdigest()[:10]
    if bake_scale:      # Assets mode bakes each object's scale into its mesh
        sc = obj.matrix_world.to_scale()
        if any(abs(c - 1.0) > 1e-6 for c in sc):
            key += "@" + ",".join(f"{c:.5f}" for c in sc)
    return key


def _build_temp_mesh(obj, depsgraph, bake_scale):
    """Evaluated copy of the object's geometry. Material slots are kept exactly as in Blender:
    same count, same order, empty and unused slots included."""
    eval_obj = obj.evaluated_get(depsgraph)
    mesh = bpy.data.meshes.new_from_object(eval_obj, preserve_all_data_layers=True, depsgraph=depsgraph)
    if mesh is None or len(mesh.polygons) == 0:
        if mesh is not None:
            bpy.data.meshes.remove(mesh)
        return None, []
    for i, slot in enumerate(obj.material_slots):
        if slot.link == 'OBJECT' and i < len(mesh.materials):
            mesh.materials[i] = slot.material
    slots = list(mesh.materials)
    if slots:
        # Blender draws a face whose slot index is past the end with the last slot.
        idx = np.zeros(len(mesh.polygons), dtype=np.int32)
        mesh.polygons.foreach_get("material_index", idx)
        if idx.size and idx.max() >= len(slots):
            mesh.polygons.foreach_set("material_index", np.clip(idx, 0, len(slots) - 1))
    else:
        slots = [None]
    if bake_scale:
        s = obj.matrix_world.to_scale()
        if any(abs(c - 1.0) > 1e-6 for c in s):
            mesh.transform(Matrix.Diagonal((s.x, s.y, s.z, 1.0)))
            if s.x * s.y * s.z < 0:
                mesh.flip_normals()
    mesh.update()
    return mesh, slots


def triangulate_keep_normals(mesh):
    """Triangulate `mesh` in place (Beauty, like Ctrl+T) and give every new corner exactly the
    normal its original corner had, so the shading can't change. Works for custom normals,
    sharp edges and flat-shaded faces alike. Returns False if the mesh was already all triangles."""
    sizes = np.empty(len(mesh.polygons), dtype=np.int32)
    mesh.polygons.foreach_get("loop_total", sizes)
    if sizes.size == 0 or sizes.max() <= 3:
        return False
    normals = np.empty(len(mesh.loops) * 3, dtype=np.float32)
    mesh.corner_normals.foreach_get("vector", normals)
    normals = normals.reshape(-1, 3)
    bm = bmesh.new()
    try:
        bm.from_mesh(mesh)
        layer = bm.loops.layers.int.new("bb_corner")
        index = 0
        for face in bm.faces:                   # same face and corner order as the mesh
            for loop in face.loops:
                loop[layer] = index
                index += 1
        bmesh.ops.triangulate(bm, faces=bm.faces[:], quad_method='BEAUTY', ngon_method='BEAUTY')
        bm.to_mesh(mesh)
    finally:
        bm.free()
    attr = mesh.attributes.get("bb_corner")
    origin = np.empty(len(mesh.loops), dtype=np.int32)
    attr.data.foreach_get("value", origin)
    mesh.attributes.remove(attr)
    # Store the normals uncompressed ("free" custom normals). The usual compressed form is tied to
    # each face, so re-storing it on the new triangles would shift normals by up to ~0.7 degrees.
    old_attr = mesh.attributes.get("custom_normal")
    if old_attr is not None:
        mesh.attributes.remove(old_attr)
    try:
        free = mesh.attributes.new("custom_normal", 'FLOAT_VECTOR', 'CORNER')
        free.data.foreach_set("vector", normals[origin].ravel())
    except Exception:                       # older Blender: compressed custom normals
        mesh.normals_split_custom_set(normals[origin].tolist())
    mesh.update()
    return True


def bake_linear(mesh, linear):
    """Apply a 3x3 transform (e.g. a slant) to the mesh itself, keeping shading exact: every corner
    normal is transformed properly and stored uncompressed; a mirror also flips the faces."""
    n = np.empty(len(mesh.loops) * 3, dtype=np.float32)
    mesh.corner_normals.foreach_get("vector", n)
    inv_t = np.array(linear.inverted_safe().transposed(), dtype=np.float64)
    n = n.reshape(-1, 3).astype(np.float64) @ inv_t.T
    n /= np.maximum(np.linalg.norm(n, axis=1), 1e-12)[:, None]
    keep = mesh.attributes.new("bb_normal", 'FLOAT_VECTOR', 'CORNER')
    keep.data.foreach_set("vector", n.astype(np.float32).ravel())
    mesh.transform(linear.to_4x4())
    if linear.determinant() < 0:
        mesh.flip_normals()                     # corner attributes follow the flipped corners
    keep = mesh.attributes["bb_normal"]
    out = np.empty(len(mesh.loops) * 3, dtype=np.float32)
    keep.data.foreach_get("vector", out)
    mesh.attributes.remove(keep)
    old = mesh.attributes.get("custom_normal")
    if old is not None:
        mesh.attributes.remove(old)
    try:
        mesh.attributes.new("custom_normal", 'FLOAT_VECTOR', 'CORNER').data.foreach_set("vector", out)
    except Exception:
        mesh.normals_split_custom_set(out.reshape(-1, 3).tolist())
    mesh.update()


def _first_uv(mesh, name=None):
    """Unreal uses the FIRST UV map. Put `name` there, or else the map Blender renders with."""
    uvs = mesh.uv_layers
    if name is not None and name in uvs:
        render = list(uvs.keys()).index(name)
    else:
        render = next((i for i, l in enumerate(uvs) if l.active_render), 0)
    if render > 0:
        n = len(mesh.loops) * 2
        a, b = np.empty(n, dtype=np.float32), np.empty(n, dtype=np.float32)
        uvs[0].uv.foreach_get("vector", a)
        uvs[render].uv.foreach_get("vector", b)
        uvs[0].uv.foreach_set("vector", b)
        uvs[render].uv.foreach_set("vector", a)
        name0, name1 = uvs[0].name, uvs[render].name
        uvs[0].name = "__bb_swap__"
        uvs[render].name = name0
        uvs[0].name = name1
    if len(uvs):
        uvs[0].active_render = True
        uvs.active_index = 0


def empty_entry():
    """Neutral grey material for empty slots."""
    plain = lambda v: {"value": v, "texture": None, "unsupported": None}
    return {"name": "EmptySlot", "shader": "principled", "blend_mode": "opaque", "two_sided": False,
            "channels": {"base_color": plain([0.8, 0.8, 0.8, 1.0]), "metallic": plain(0.0),
                         "roughness": plain(0.5)},
            "extra": {}, "warnings": [], "bake_warnings": [], "node_tree": None}


def _work_scene(scene):
    """An empty scene for the temporary export objects, so the user's scene is never re-evaluated."""
    work = bpy.data.scenes.new("BB_export_work")
    us, ws = scene.unit_settings, work.unit_settings
    ws.system, ws.scale_length, ws.length_unit = us.system, us.scale_length, us.length_unit
    return work


def _export_fbx(context, work, obj, filepath, custom_normals=False):
    """custom_normals: the mesh is already triangulated with its normals kept; write those normals
    with smoothing groups and Blender's (MikkTSpace) tangents, colours as sRGB."""
    view_layer = work.view_layers[0]
    tangents = custom_normals and len(obj.data.uv_layers) > 0
    with context.temp_override(scene=work, view_layer=view_layer, selected_objects=[obj],
                               active_object=obj, object=obj):
        obj.select_set(True, view_layer=view_layer)
        view_layer.objects.active = obj
        bpy.ops.export_scene.fbx(
            filepath=filepath, use_selection=True, object_types={'MESH'},
            use_mesh_modifiers=True, mesh_smooth_type='SMOOTH_GROUP' if custom_normals else 'FACE',
            use_tspace=tangents, use_triangles=True, colors_type='SRGB',
            bake_anim=False, add_leaf_bones=False, embed_textures=False,
            axis_forward='-Z', axis_up='Y', apply_scale_options='FBX_SCALE_NONE',
            global_scale=1.0, use_custom_props=False)
    return tangents


def _export_image(img, tex_dir, asset_name, report):
    """Returns (file name, copy job or None). A copy job (src, dest) is run later on a thread;
    images that must be written by Blender (packed, generated, edited) are saved right away."""
    if img.source == 'TILED':
        report.warn(f"Image '{img.name}' is a UDIM set; UDIMs are not supported yet")
        return None, None
    if img.source not in {'FILE', 'GENERATED'}:
        report.warn(f"Image '{img.name}' ({img.source}) is not a still image; skipped")
        return None, None
    src = bpy.path.abspath(img.filepath_raw, library=img.library) if img.filepath_raw else ""
    ext = os.path.splitext(src)[1].lower()
    if (img.source == 'FILE' and not img.packed_file and not img.is_dirty
            and os.path.isfile(src) and ext in COPY_EXT):
        dest = os.path.join(tex_dir, asset_name + ext)
        return os.path.basename(dest), (src, dest)
    ext = IMAGE_EXT.get(img.file_format)
    if ext is None:
        report.warn(f"Image '{img.name}' has format {img.file_format}, which Unreal can't import; skipped")
        return None, None
    if not img.has_data:
        try:
            img.pixels[0]  # forces a load
        except Exception:
            pass
    if not img.has_data:
        report.warn(f"Image '{img.name}' has no pixel data (missing file?); skipped")
        return None, None
    dest = os.path.join(tex_dir, asset_name + ext)
    try:
        img.save(filepath=dest, save_copy=True)
    except Exception as exc:
        report.warn(f"Could not save image '{img.name}': {exc}")
        return None, None
    return os.path.basename(dest), None


def _copy_and_hash(job, path):
    """Thread worker: copy (if needed) then SHA-1. Both release the GIL."""
    if job is not None:
        src, dest = job
        if not (os.path.isfile(dest) and os.path.samefile(src, dest)):
            shutil.copy2(src, dest)
    return _sha1(path)


def _placements(context, settings, depsgraph, report):
    """Everything to export as (object, world matrix): the chosen objects themselves plus every
    instance they generate (collection instances, Geometry Nodes object instances, particles)."""
    scene = context.scene
    if settings.mode == 'ASSETS' or settings.selected_only:
        chosen = list(context.selected_objects)
        if not chosen:
            raise ExportError("Nothing is selected. Select the objects to export.")
    else:
        chosen = [o for o in scene.objects if not o.hide_render]
    paths = _collection_paths(scene)
    chosen_names = {o.name for o in chosen}
    out = [{"obj": o, "matrix": o.matrix_world.copy(), "name": o.name,
            "collection": paths.get(o.name), "instancer": None,
            "parent": o.parent.name if o.parent is not None and o.parent.name in chosen_names else None}
           for o in sorted(chosen, key=lambda o: o.name)]
    counts, generated = {}, 0
    for inst in depsgraph.object_instances:
        if not inst.is_instance or inst.parent is None:
            continue
        parent = inst.parent.original
        if parent.name not in chosen_names:
            continue
        src = inst.object.original
        if src.name == parent.name:
            continue            # text / curve objects list their own mesh as an "instance"
        if bpy.data.objects.get(src.name) != src:
            generated += 1      # geometry made inside Geometry Nodes, not a real object
            continue
        n = counts[parent.name] = counts.get(parent.name, 0) + 1
        out.append({"obj": src, "matrix": inst.matrix_world.copy(),
                    "name": f"{parent.name}.{src.name}.{n:03d}",
                    "collection": paths.get(parent.name), "instancer": parent.name,
                    "parent": parent.name})
    if generated:
        report.warn(f"{generated} Geometry Nodes instance(s) of generated geometry were skipped "
                    f"(only instances of real objects are exported)")
    if settings.mode == 'SCENE' and getattr(settings, "hierarchy", 'AS_IS') != 'AS_IS':
        # Parents that aren't exported themselves (hidden for render, or not selected) still hold
        # the hierarchy together: add them as structure only.
        have = {p["name"] for p in out}
        for p in list(out):
            o = p["obj"] if p["instancer"] is None else None
            while o is not None and o.parent is not None and o.parent.name not in have:
                par = o.parent
                have.add(par.name)
                out.append({"obj": par, "matrix": par.matrix_world.copy(), "name": par.name,
                            "collection": paths.get(par.name), "instancer": None, "structural": True,
                            "parent": par.parent.name if par.parent is not None else None})
                o = par
        for p in out:     # a parent added above now exists for its children
            if p["parent"] is None and p["instancer"] is None and p["obj"].parent is not None:
                p["parent"] = p["obj"].parent.name if p["obj"].parent.name in have else None
    if not any(p["obj"].type in MESH_TYPES and not p.get("structural") for p in out):
        raise ExportError("No mesh objects to export.")
    return out


def run_export(context, settings):
    """Run the whole export in one go. Returns (manifest_path, Report)."""
    result = {}
    for _ in export_steps(context, settings, result):
        pass
    return result["manifest"], result["report"]


def export_steps(context, settings, result):
    """The export as a generator yielding (fraction 0..1, status text) between small steps, so the
    UI can redraw and show progress. Fills result["manifest"] and result["report"] at the end.
    Closing the generator early (cancel) still cleans up everything temporary."""
    t0 = time.time()
    report = Report()
    scene = context.scene

    if settings.export_dir.startswith("//") and not bpy.data.filepath:
        raise ExportError("Save the .blend file first, or pick an absolute export folder.")
    export_dir = bpy.path.abspath(settings.export_dir)
    blend_stem = os.path.splitext(os.path.basename(bpy.data.filepath))[0] if bpy.data.filepath else ""
    name = safe_name(settings.export_name or blend_stem or scene.name)
    out_dir = os.path.join(export_dir, name)
    mesh_dir = os.path.join(out_dir, "meshes")
    tex_dir = os.path.join(out_dir, "textures")
    for d in (mesh_dir, tex_dir):
        os.makedirs(d, exist_ok=True)

    if context.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    asset_mode = settings.mode == 'ASSETS'
    unit = scene.unit_settings.scale_length * 100.0
    names = Names()
    mat_exp = MaterialExporter(report)
    depsgraph = context.evaluated_depsgraph_get()
    yield 0.0, "Collecting objects"
    placements = _placements(context, settings, depsgraph, report)
    lights, cameras = [], []
    mesh_total = max(1, len({_mesh_key(p["obj"], asset_mode) for p in placements
                             if p["obj"].type in MESH_TYPES and not p.get("structural")}))
    work = _work_scene(scene)
    group_mode = 'AS_IS' if asset_mode else getattr(settings, "hierarchy", 'AS_IS')
    group_by = getattr(settings, "group_by", 'PARENT')
    nodes = []
    skew = {}
    pivots = None
    if not asset_mode:
        yield 0.0, "Checking transforms"
        plan = []
        for p in placements:
            k = hierarchy.node_kind(p["obj"].type, MESH_TYPES)
            plan.append({"name": p["name"], "parent": p.get("parent"), "kind": "empty" if p.get("structural") else k,
                         "label": safe_name(p["name"]), "ref": 0 if k in ("light", "camera") else None,
                         "collection": p["collection"], "structural": p.get("structural", False),
                         "matrix": hierarchy.aim_matrix(p["matrix"]) if k in ("light", "camera") else p["matrix"],
                         "corners": [p["matrix"] @ Vector(c) for c in p["obj"].bound_box]
                         if k == "mesh" and not p.get("structural") else None})
        if group_mode != 'AS_IS' and group_by == 'COLLECTION':
            pivots = hierarchy.collection_pivots(plan, report)
        skew = hierarchy.plan_skew(plan, group_mode, group_by, pivots, to_unreal, unit)

    meshes, objects = {}, []
    temp_objs, temp_meshes = [], []
    base_entries = {}                   # Blender material name -> resolved entry (before baking)
    materials = {}                      # manifest key -> entry
    baked_textures = {}
    bake_on = bool(getattr(settings, "bake", True))
    keep_normals = bool(getattr(settings, "custom_normals", True))
    no_tangents = []
    resolution = int(getattr(settings, "bake_resolution", "2048"))

    canonical = {}          # content signature -> manifest key of the first material with it
    canon_of = {}           # Blender material name -> manifest key it shares
    stand_ins = {}          # slot name -> stand-in material written into the FBX

    def resolve(mat):
        if mat.name not in base_entries:
            base_entries[mat.name] = mat_exp.export_material(mat)
        return base_entries[mat.name]

    def material_key(mat, entry):
        """Materials identical in every node, link and value share one Unreal material."""
        if mat.name not in canon_of:
            sig = mat_exp.signature(mat, entry)
            if sig not in canonical:
                canonical[sig] = mat.name
            canon_of[mat.name] = canonical[sig]
        return canon_of[mat.name]

    def register(key, entry, raw_name):
        """Give a manifest material its Unreal Material Instance name."""
        if key not in materials:
            entry["asset_name"] = names.make("MI_", re.sub(r'\.\d{3}$', '', raw_name))
            materials[key] = entry
        return materials[key]["asset_name"]

    def stand_in(slot_name):
        """A material datablock named `slot_name` for one FBX slot. Every slot of a mesh gets its own,
        so the FBX exporter can't merge two slots that use the same material."""
        if slot_name not in stand_ins:
            mat = bpy.data.materials.new(slot_name)
            stand_ins[slot_name] = mat      # its real name may differ if the name was taken
        return stand_ins[slot_name]

    try:
        for place in placements:
            obj, matrix = place["obj"], place["matrix"]
            original = matrix
            fix = skew.get(place["name"])
            if fix is not None:
                matrix = fix[1]                 # the slant goes into the mesh instead
            kind = hierarchy.node_kind(obj.type, MESH_TYPES)
            node = {"name": place["name"], "parent": place.get("parent"), "kind": kind, "ref": None,
                    "label": safe_name(place["name"]), "collection": place["collection"],
                    "matrix": hierarchy.aim_matrix(matrix) if kind in ("light", "camera") else matrix}
            if kind == "mesh" and not place.get("structural"):
                node["corners"] = [original @ Vector(c) for c in obj.bound_box]
            nodes.append(node)
            if place.get("structural"):
                node["kind"] = "empty"
                continue
            if obj.type not in MESH_TYPES:
                if not asset_mode and obj.type in {'LIGHT', 'CAMERA'}:
                    entry = (_light_entry(obj, matrix, unit) if obj.type == 'LIGHT'
                             else _camera_entry(obj, matrix, unit, scene))
                    entry.update(name=place["name"], label=safe_name(place["name"]),
                                 collection=place["collection"])
                    target = lights if obj.type == 'LIGHT' else cameras
                    node["ref"] = len(target)
                    target.append(entry)
                continue
            key = _mesh_key(obj, asset_mode)
            if fix is not None:
                lin = fix[0]
                key += "~" + hashlib.sha1(repr([round(v, 5) + 0.0 for row in lin for v in row]).encode()).hexdigest()[:10]
            if key not in meshes:
                yield 0.75 * len(meshes) / mesh_total, f"Mesh {len(meshes) + 1} of {mesh_total}: {obj.name}"
                mesh, slots = _build_temp_mesh(obj, depsgraph, bake_scale=asset_mode)
                if mesh is None:
                    report.warn(f"'{obj.name}' has no faces; skipped")
                    meshes[key] = None
                    continue
                temp_meshes.append(mesh)
                if fix is not None:
                    bake_linear(mesh, fix[0])
                if keep_normals:
                    triangulate_keep_normals(mesh)     # before baking, so bakes use the final triangles
                raw = obj.data.name if key.startswith("data:") else obj.name
                if fix is not None:
                    raw += "_slant"
                asset = names.make("SM_", raw)
                mesh.name = asset
                tmp = bpy.data.objects.new(asset, mesh)
                work.collection.objects.link(tmp)
                temp_objs.append(tmp)
                fbx_path = os.path.join(mesh_dir, asset + ".fbx")
                entries = [resolve(m) if m is not None else None for m in slots]
                variants = [None] * len(slots)
                if bake_on and any(e is not None and baker.needs_bake(e) for e in entries):
                    stems = [f"{m.name}_{asset[3:]}" if m is not None else "" for m in slots]
                    yield 0.75 * len(meshes) / mesh_total, f"Baking materials of {obj.name}"
                    with context.temp_override(scene=work, view_layer=work.view_layers[0]):
                        variants, new_tex, ok = baker.bake_mesh(context, tmp, slots, entries, stems, tex_dir,
                                                                resolution, names, report)
                    baked_textures.update(new_tex)
                    if ok:
                        _first_uv(mesh, baker.BAKE_UV)
                if not any(v is not None for v in variants):
                    _first_uv(mesh)
                # One stand-in material per slot, named after the Unreal Material Instance it gets.
                # A material used in two slots gives two stand-ins (MI_X, MI_X__slot2) so both slots
                # survive the FBX export; both point to the same Unreal material.
                repeats = {}
                for m in slots:
                    if m is not None:
                        repeats[m.name] = repeats.get(m.name, 0) + 1
                slot_keys, slot_names, seen = [], [], {}
                for i, m in enumerate(slots):
                    if m is None:
                        mat_key = "__empty__"
                        mi_name = register(mat_key, empty_entry(), "EmptySlot")
                    elif variants[i] is not None:
                        mat_key = f"{material_key(m, entries[i])}@{asset}"
                        if repeats[m.name] > 1:
                            mat_key += f"#{i}"      # each slot was baked on its own
                        mi_name = register(mat_key, variants[i], f"{m.name}_{asset[3:]}")
                    else:
                        mat_key = material_key(m, entries[i])
                        mi_name = register(mat_key, base_entries[mat_key], mat_key)
                    n = seen[mi_name] = seen.get(mi_name, 0) + 1
                    stand = stand_in(mi_name if n == 1 else f"{mi_name}__slot{n}")
                    if i < len(mesh.materials):
                        mesh.materials[i] = stand
                    else:                   # a mesh with no slots at all gets one neutral slot
                        mesh.materials.append(stand)
                    slot_keys.append(mat_key)
                    slot_names.append(stand.name)
                fidx = np.empty(len(mesh.polygons), dtype=np.int32)
                mesh.polygons.foreach_get("material_index", fidx)
                slot_faces = np.bincount(fidx, minlength=len(slot_names))[:len(slot_names)].tolist()
                tangents = _export_fbx(context, work, tmp, fbx_path, keep_normals)
                if keep_normals and not tangents:
                    no_tangents.append(asset)
                bb = [v[:] for v in obj.bound_box]
                meshes[key] = {
                    "asset_name": asset, "file": "meshes/" + asset + ".fbx", "sha1": _sha1(fbx_path),
                    "material_slots": slot_keys,
                    "baked": any(v is not None for v in variants),
                    "custom_normals": keep_normals, "tangents": keep_normals and asset not in no_tangents,
                    "slot_names": slot_names,
                    "slot_faces": slot_faces,
                    "vertices": len(mesh.vertices), "polygons": len(mesh.polygons),
                    "bounds_min": [min(c[i] for c in bb) for i in range(3)],
                    "bounds_max": [max(c[i] for c in bb) for i in range(3)],
                    "source": raw,
                }
                bpy.data.objects.remove(tmp, do_unlink=True)
                temp_objs.remove(tmp)
            if meshes[key] is None:
                continue
            node["ref"] = len(objects)
            objects.append({
                "name": place["name"], "label": safe_name(place["name"]), "type": obj.type, "mesh": key,
                "parent": obj.parent.name if obj.parent else None,
                "collection": place["collection"], "instancer": place["instancer"],
                "matrix_world": [list(r) for r in matrix],
                "unreal": to_unreal(matrix, unit),
            })
    finally:
        for tmp in temp_objs:
            bpy.data.objects.remove(tmp, do_unlink=True)
        for mesh in temp_meshes:
            bpy.data.meshes.remove(mesh)
        for mat in stand_ins.values():
            bpy.data.materials.remove(mat)
        bpy.data.scenes.remove(work)

    meshes = {k: v for k, v in meshes.items() if v is not None}
    if not meshes:
        raise ExportError("None of the objects had any geometry to export.")

    used_images = {}        # image name -> set of usages, from the materials that are really exported
    for key in sorted(materials):
        entry = materials[key]
        for w in entry.get("warnings", []) + entry.get("bake_warnings", []):
            report.warn(f"Material '{entry['name']}': {w}")
        for channel, c in entry["channels"].items():
            refs = [(c.get("texture"), "normal" if channel == "normal" else
                     "color" if channel in ("base_color", "emission_color") else "data")]
            if c.get("ao"):
                refs.append((c["ao"]["texture"], "data"))
            for tex, usage in refs:
                if tex and tex["image"] not in baked_textures:
                    used_images.setdefault(tex["image"], set())
                    if tex["channel"] != "a":
                        used_images[tex["image"]].add(usage)
        for k in [k for k in entry if k.startswith("_")]:
            del entry[k]

    textures, by_source, jobs = dict(baked_textures), {}, []
    tex_total = max(1, len(used_images))
    for ti, iname in enumerate(sorted(used_images)):
        yield 0.75 + 0.1 * ti / tex_total, f"Texture {ti + 1} of {tex_total}"
        info = mat_exp.images.get(iname)
        if info is None:
            continue
        info = dict(info, usage=used_images[iname])
        img = info["image"]
        # Image.001 / Image.002 copies of one file become a single Unreal texture.
        source = None
        if img.source == 'FILE' and not img.packed_file and not img.is_dirty and img.filepath_raw:
            source = (os.path.normpath(bpy.path.abspath(img.filepath_raw, library=img.library)),
                      img.colorspace_settings.name)
        if source in by_source:
            shared = textures[by_source[source]]
            shared["usage"] = sorted(set(shared["usage"]) | info["usage"])
            shared["normal_directx"] = shared["normal_directx"] or bool(info.get("directx"))
            textures[iname] = shared
            continue
        clean = re.sub(r'\.\d{3}$', '', iname)
        clean = re.sub(r'\.(png|jpe?g|tga|bmp|tiff?|exr|hdr)$', '', clean, flags=re.IGNORECASE)
        asset = names.make("T_", clean)
        fname, job = _export_image(img, tex_dir, asset, report)
        if fname is None:
            continue
        jobs.append((iname, job, os.path.join(tex_dir, fname)))
        textures[iname] = {
            "asset_name": asset, "file": "textures/" + fname, "sha1": None,
            "colorspace": img.colorspace_settings.name, "is_data": bool(img.colorspace_settings.is_data),
            "is_float": bool(img.is_float), "usage": sorted(info["usage"]), "size": list(img.size),
            "alpha_mode": img.alpha_mode, "normal_directx": bool(info.get("directx")),
        }
        if source:
            by_source[source] = iname

    if jobs:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(8, os.cpu_count() or 4)) as pool:
            futures = [(iname, pool.submit(_copy_and_hash, job, path)) for iname, job, path in jobs]
            for done, (iname, fut) in enumerate(futures):
                while not fut.done():
                    yield 0.85 + 0.1 * done / len(futures), f"Copying textures ({done} of {len(futures)})"
                    time.sleep(0.02)
                try:
                    textures[iname]["sha1"] = fut.result()
                except OSError as exc:
                    report.warn(f"Could not copy texture '{iname}': {exc}")
                    del textures[iname]
        for iname, t in list(textures.items()):      # shared entries point at a dropped texture
            if t.get("sha1") is None and not t.get("baked"):
                del textures[iname]

    yield 0.95, "World, groups and manifest"
    if no_tangents:
        shown = ", ".join(no_tangents[:5]) + (f" and {len(no_tangents) - 5} more" if len(no_tangents) > 5 else "")
        report.warn(f"{len(no_tangents)} mesh(es) have no UV map, so no tangents were exported for them "
                    f"(Unreal computes their own): {shown}")
    world = {"type": "none"} if asset_mode else _export_world(scene, out_dir, names, report)
    groups = {"mode": "AS_IS", "units": [], "blueprints": {}}
    if group_mode != 'AS_IS':
        groups = hierarchy.build(nodes, objects, lights, cameras, to_unreal, unit, group_mode,
                                 getattr(settings, "instance_repeats", True), names, report, group_by, pivots)
        inside = sum(1 for o in objects if o.get("unit") is not None)
        outside = [o for o in objects if o.get("unit") is None]
        scene_coll = sum(1 for o in outside if not o.get("collection"))
        report.warn(f"Placement: {inside} piece(s) inside groups/Blueprints, {len(outside)} on their own"
                    + (f" ({scene_coll} directly in Scene Collection -> folder 'Scene Collection')" if scene_coll else "")
                    + (f"; {sum(1 for p in placements if p['name'] in skew)} slanted piece(s) kept exact by "
                       f"baking the slant into {sum(1 for k, v in meshes.items() if v and '~' in k)} mesh copy(ies)"
                       if skew else ""))
    elif skew:
        report.warn(f"{len(skew)} slanted piece(s) kept exact by baking the slant into "
                    f"{sum(1 for k, v in meshes.items() if v and '~' in k)} mesh copy(ies)")
    r = scene.render

    manifest = {
        "format": "blender_bridge", "version": FORMAT_VERSION,
        "generator": {"blender": bpy.app.version_string, "addon": ADDON_VERSION},
        "exported": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "mode": settings.mode, "name": name,
        "bake": {"enabled": bake_on, "resolution": resolution},
        "custom_normals": keep_normals,
        "unreal_root": content_path(settings.unreal_root) + "/" + name,
        "source_blend": bpy.data.filepath,
        "units": {"scale_length": scene.unit_settings.scale_length, "to_unreal": unit},
        "textures": textures, "materials": materials, "node_groups": mat_exp.node_groups,
        "meshes": meshes, "objects": objects, "lights": lights, "cameras": cameras, "world": world,
        "hierarchy": groups,
        "render": {"resolution": [r.resolution_x, r.resolution_y], "fps": r.fps / r.fps_base},
        "warnings": report.warnings,
    }
    manifest_path = os.path.join(out_dir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=1, default=str)

    try:
        os.makedirs(os.path.dirname(POINTER_FILE), exist_ok=True)
        with open(POINTER_FILE, "w", encoding="utf-8") as f:
            json.dump({"manifest": manifest_path, "exported": manifest["exported"]}, f)
    except OSError as exc:
        report.warn(f"Could not write {POINTER_FILE}: {exc}")

    report.summary = (f"{len(meshes)} meshes, {len(materials)} materials, "
                      f"{len({t['asset_name'] for t in textures.values()})} textures, "
                      f"{len(objects)} objects"
                      + (f", {sum(1 for m in meshes.values() if m.get('baked'))} baked" if baked_textures else "")
                      + (f", {len(lights)} lights, {len(cameras)} cameras" if not asset_mode else "")
                      + (f", {len(groups['units'])} groups" if group_mode == 'GROUPED' else "")
                      + (f", {len(groups['units'])} houses as {len(groups['blueprints'])} Blueprints"
                         if group_mode == 'BLUEPRINT' else "")
                      + f" in {time.time() - t0:.1f}s")
    result["manifest"], result["report"] = manifest_path, report
    yield 1.0, "Done"
