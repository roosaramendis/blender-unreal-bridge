# SPDX-License-Identifier: GPL-3.0-or-later
"""Bakes materials that can't be converted node-by-node into plain textures.

When any material on a mesh needs baking, the whole mesh gets a fresh, non-overlapping UV map
("BB_Bake") and every textured material on it is baked into that UV space. The mesh is then
exported with BB_Bake as its first UV map, so Unreal has one consistent UV set and tangent basis.

All work happens on the exporter's temporary mesh and on copies of the materials; the user's
objects and materials are never modified.
"""
import copy
import hashlib
import math
import os

import bpy
import numpy as np

BAKE_UV = "BB_Bake"
# channel -> Principled-style input it is read from, per shader type
SOCKETS = {
    "principled": {"base_color": "Base Color", "metallic": "Metallic", "roughness": "Roughness",
                   "alpha": "Alpha", "emission_color": "Emission Color", "normal": "Normal"},
    "glass": {"base_color": "Color", "roughness": "Roughness", "normal": "Normal"},
    "diffuse": {"base_color": "Color", "normal": "Normal"},
    "emission": {"emission_color": "Color"},
}
EMIT_ORDER = ("base_color", "emission_color", "roughness", "metallic", "alpha")
DEFAULT_MAPPING = {"scale": [1.0, 1.0], "offset": [0.0, 0.0], "rotation": 0.0, "uv_map": None}


def needs_bake(entry):
    return bool(entry.get("bake_warnings"))


def _sha1(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _unwrap(context, obj):
    mesh = obj.data
    layer = mesh.uv_layers.new(name=BAKE_UV)
    if layer is None:
        raise RuntimeError("the mesh already has the maximum number of UV maps")
    mesh.uv_layers.active = layer          # unwrap target AND the tangent basis of baked normals
    # Select every face up front (no edit-mode select operator needed).
    for items in (mesh.vertices, mesh.edges, mesh.polygons):
        items.foreach_set("select", np.ones(len(items), dtype=bool))
    # The object lives alone in the exporter's work scene; the context is pointed at it explicitly.
    obj.select_set(True)
    context.view_layer.objects.active = obj
    with context.temp_override(active_object=obj, object=obj, selected_objects=[obj],
                               selected_editable_objects=[obj]):
        bpy.ops.object.mode_set(mode='EDIT')
        try:
            with context.temp_override(edit_object=obj, active_object=obj, object=obj):
                bpy.ops.uv.smart_project(angle_limit=math.radians(66.0), island_margin=0.01)
        finally:
            bpy.ops.object.mode_set(mode='OBJECT')
    return BAKE_UV


def _texture_ref(key, channel):
    return {"image": key, "channel": channel, "mapping": dict(DEFAULT_MAPPING),
            "extension": "REPEAT", "interpolation": "Linear"}


class _Slot:
    """One material slot being baked: a private copy of the material plus what to bake from it."""

    def __init__(self, material, entry, dummy):
        self.entry = entry
        self.mat = material.copy()
        self.tree = self.mat.node_tree
        self.dummy = dummy
        self.out = self.tree.get_output_node('CYCLES') or self.tree.get_output_node('ALL')
        self.shader = self.tree.nodes.get(entry.get("_shader") or "") if entry.get("_top") else None
        self.surface_from = None
        if self.out is not None and self.out.inputs["Surface"].is_linked:
            self.surface_from = self.out.inputs["Surface"].links[0].from_socket
        self.target = self.tree.nodes.new("ShaderNodeTexImage")
        self.emission = self.tree.nodes.new("ShaderNodeEmission")
        self.images = {}        # channel -> baked bpy image
        self.wants = {}         # channel -> input socket (emit mode)
        self.generic = False
        sockets = SOCKETS.get(entry.get("shader"), {})
        if self.shader is not None and sockets:
            for channel, name in sockets.items():
                sock = self.shader.inputs.get(name)
                if sock is not None and sock.is_linked:
                    self.wants[channel] = sock
        elif self.out is not None and self.surface_from is not None:
            self.generic = True     # unknown shader network: use Blender's own bake passes

    def aim(self, image):
        self.target.image = image or self.dummy
        for n in self.tree.nodes:
            n.select = False
        self.target.select = True
        self.tree.nodes.active = self.target

    def restore_surface(self):
        if self.out is not None and self.surface_from is not None:
            self.tree.links.new(self.surface_from, self.out.inputs["Surface"])

    def emit(self, channel):
        src = self.wants[channel].links[0].from_socket
        self.tree.links.new(src, self.emission.inputs["Color"])
        self.tree.links.new(self.emission.outputs[0], self.out.inputs["Surface"])


def bake_mesh(context, obj, slots, entries, variant_names, tex_dir, resolution, names, report):
    """Bake every textured material on `obj`.

    slots: real materials per slot (or None); entries: their resolved manifest entries.
    variant_names: asset-name stems per slot. Returns (variants, textures):
      variants[i] = new manifest entry for slot i, or None to keep the plain material
      textures    = {texture key: manifest texture info} for the baked files
    """
    scene = context.scene
    saved = (scene.render.engine, scene.cycles.samples, scene.cycles.use_denoising)
    made_images, made_slots, textures = [], [], {}
    variants = [None] * len(slots)
    dummy = bpy.data.images.new("BB_bake_dummy", 4, 4)
    made_images.append(dummy)

    def new_image(color):
        img = bpy.data.images.new("BB_bake", resolution, resolution, alpha=False)
        img.colorspace_settings.name = 'sRGB' if color else 'Non-Color'
        made_images.append(img)
        return img

    def run(bake_type, targets, **kw):
        for i, slot in enumerate(made_slots):
            if slot is not None:
                slot.aim(targets.get(i))
        obj.select_set(True)            # the only object in the work scene
        context.view_layer.objects.active = obj
        with context.temp_override(active_object=obj, object=obj, selected_objects=[obj],
                                   selected_editable_objects=[obj]):
            bpy.ops.object.bake(type=bake_type, uv_layer=BAKE_UV, margin=max(2, resolution // 256),
                                use_clear=True, target='IMAGE_TEXTURES', save_mode='INTERNAL', **kw)

    try:
        scene.render.engine = 'CYCLES'
        scene.cycles.samples = 8
        scene.cycles.use_denoising = False
        _unwrap(context, obj)
        mesh = obj.data
        for i, (mat, entry) in enumerate(zip(slots, entries)):
            slot = _Slot(mat, entry, dummy) if mat is not None and entry is not None else None
            made_slots.append(slot)
            if slot is not None:
                mesh.materials[i] = slot.mat

        # 1. normals, with the original shading
        targets = {}
        for i, slot in enumerate(made_slots):
            if slot is not None and ("normal" in slot.wants or slot.generic):
                targets[i] = slot.images["normal"] = new_image(False)
        if targets:
            run('NORMAL', targets, normal_space='TANGENT')

        # 2. one input at a time, routed through an Emission shader (exact values, no lighting)
        for channel in EMIT_ORDER:
            targets = {}
            for i, slot in enumerate(made_slots):
                if slot is not None and channel in slot.wants:
                    slot.emit(channel)
                    targets[i] = slot.images[channel] = new_image(channel in ("base_color", "emission_color"))
            if targets:
                run('EMIT', targets)
                for slot in made_slots:
                    if slot is not None:
                        slot.restore_surface()

        # 3. shader networks we can't take apart: Blender's diffuse-colour and roughness passes
        for bake_type, channel, kw in (('DIFFUSE', "base_color", {"pass_filter": {'COLOR'}}),
                                       ('ROUGHNESS', "roughness", {})):
            targets = {}
            for i, slot in enumerate(made_slots):
                if slot is not None and slot.generic:
                    targets[i] = slot.images[channel] = new_image(channel == "base_color")
            if targets:
                run(bake_type, targets, **kw)

        # 4. write files and build the baked material entries
        for i, slot in enumerate(made_slots):
            if slot is None or not slot.images:
                continue
            entry = copy.deepcopy({k: v for k, v in slot.entry.items() if k != "node_tree"})
            entry["node_tree"] = None
            entry["source_material"] = slot.entry["name"]
            entry["baked"] = sorted(slot.images)
            entry["bake_warnings"] = []
            ch = entry["channels"]
            stem = variant_names[i]

            def save(img, suffix, usage, color):
                asset = names.make("T_", f"{stem}_{suffix}")
                path = os.path.join(tex_dir, asset + ".png")
                img.filepath_raw = path
                img.file_format = 'PNG'
                img.save()
                textures[asset] = {
                    "asset_name": asset, "file": "textures/" + asset + ".png", "sha1": _sha1(path),
                    "colorspace": 'sRGB' if color else 'Non-Color', "is_data": not color, "is_float": False,
                    "usage": [usage], "size": [resolution, resolution], "alpha_mode": "NONE",
                    "normal_directx": False, "baked": True,
                }
                return asset

            def plain(channel, texture=None, **extra):
                old = ch.get(channel) or {}
                ch[channel] = {"value": old.get("value"), "texture": texture, "unsupported": None, **extra}

            if "base_color" in slot.images:
                key = save(slot.images["base_color"], "BaseColor", "color", True)
                plain("base_color", _texture_ref(key, "rgb"), adjust=None, tint=None, ao=None)
            if "emission_color" in slot.images:
                key = save(slot.images["emission_color"], "Emissive", "color", True)
                plain("emission_color", _texture_ref(key, "rgb"), adjust=None, tint=None, ao=None)
            if "normal" in slot.images:
                key = save(slot.images["normal"], "Normal", "normal", False)
                plain("normal", _texture_ref(key, "rgb"), strength=1.0)
            if "alpha" in slot.images:
                key = save(slot.images["alpha"], "Alpha", "data", False)
                plain("alpha", _texture_ref(key, "r"), invert=False, scale=1.0)
            rough, metal = slot.images.get("roughness"), slot.images.get("metallic")
            if rough is not None or metal is not None:
                # pack into one texture: R = 1 (no AO), G = roughness, B = metallic
                n = resolution * resolution
                packed = np.ones((n, 4), dtype=np.float32)
                for img, column, channel, default in ((rough, 1, "roughness", 0.5), (metal, 2, "metallic", 0.0)):
                    if img is not None:
                        px = np.empty(n * 4, dtype=np.float32)
                        img.pixels.foreach_get(px)
                        packed[:, column] = px.reshape(n, 4)[:, 0]
                    else:
                        v = (ch.get(channel) or {}).get("value")
                        packed[:, column] = v if isinstance(v, (int, float)) else default
                orm = new_image(False)
                orm.pixels.foreach_set(packed.ravel())
                key = save(orm, "ORM", "data", False)
                plain("roughness", _texture_ref(key, "g"), invert=False, scale=1.0)
                plain("metallic", _texture_ref(key, "b"), invert=False, scale=1.0)
            if slot.generic:
                entry["shader"] = "baked"
                entry["warnings"] = list(entry.get("warnings", [])) + [
                    "baked from a shader network that can't be taken apart: colour, roughness and "
                    "normal only (no metallic)"]
            variants[i] = entry
        return variants, textures, True
    except Exception as exc:
        report.warn(f"Baking failed on '{obj.name}': {exc}")
        return [None] * len(slots), {}, False
    finally:
        mesh = obj.data
        for i, mat in enumerate(slots):         # put the real materials back on the temp mesh
            if i < len(mesh.materials):
                mesh.materials[i] = mat
        for slot in made_slots:
            if slot is not None:
                bpy.data.materials.remove(slot.mat)
        for img in made_images:
            bpy.data.images.remove(img)
        scene.render.engine, scene.cycles.samples, scene.cycles.use_denoising = saved
