"""Imports a manifest.json written by the Blender 'Unreal Bridge' addon."""
import json
import math
import os
import re

import unreal

from . import blueprints, masters
from .util import Log, asset_tools, exists, get_prop, load, set_prop

POINTER_FILE = os.path.join(os.path.expanduser("~"), ".config", "blender_bridge", "last_export.json")
OVERVIEW_MAP = "/Game/BlenderBridge/Maps/BB_AssetOverview"
TEMPLATE_MAP = "/Engine/Maps/Templates/Template_Default"
TAG = "BlenderBridge"
SUPPORTED_VERSION = 3
mel = unreal.MaterialEditingLibrary
CHANNEL_MASK = {"r": (1, 0, 0, 0), "g": (0, 1, 0, 0), "b": (0, 0, 1, 0), "a": (0, 0, 0, 1), "rgb": (1, 0, 0, 0)}


# --------------------------------------------------------------------------- entry points
def import_latest():
    """Menu entry: import whatever Blender exported last."""
    if not os.path.isfile(POINTER_FILE):
        _dialog("Nothing to import", f"No export found.\nExport from Blender first.\n\n(looked for {POINTER_FILE})")
        return
    with open(POINTER_FILE, encoding="utf-8") as f:
        path = json.load(f).get("manifest", "")
    import_manifest(path)


def import_manifest(manifest_path):
    """Import one export. manifest_path = .../<name>/manifest.json"""
    log = Log()
    if not os.path.isfile(manifest_path):
        _dialog("Import failed", f"Manifest not found:\n{manifest_path}")
        return
    with open(manifest_path, encoding="utf-8") as f:
        man = json.load(f)
    if man.get("format") != "blender_bridge":
        _dialog("Import failed", "That file is not a Blender Bridge manifest.")
        return
    if man.get("version", 0) > SUPPORTED_VERSION:
        log.warn(f"manifest version {man.get('version')} is newer than this plugin understands")

    # Switching maps discards unsaved work, so offer to save first.
    if not unreal.EditorLoadingAndSavingUtils.save_dirty_packages_with_dialog(True, True):
        log.info("import cancelled")
        return

    base_dir = os.path.dirname(manifest_path)
    root = man["unreal_root"].rstrip("/")
    steps = len(man["textures"]) + len(man["materials"]) + len(man["meshes"]) + 2
    with unreal.ScopedSlowTask(steps, "Importing from Blender...") as task:
        task.make_dialog(True)
        tex_assets = _import_textures(man, base_dir, root + "/Textures", log, task)
        mat_assets = _build_materials(man, root + "/Materials", tex_assets, log, task)
        mesh_assets = _import_meshes(man, base_dir, root + "/Meshes", mat_assets, log, task)
        unreal.EditorAssetLibrary.save_directory(root, only_if_is_dirty=True, recursive=True)
        unreal.EditorAssetLibrary.save_directory(masters.MASTER_DIR, only_if_is_dirty=True, recursive=True)
        task.enter_progress_frame(1, "Placing actors")
        if man.get("mode") == "SCENE":
            placed = _place_scene(man, base_dir, root, mesh_assets, log)
        else:
            placed = _place_overview(man, mesh_assets, log)
        task.enter_progress_frame(1, "Saving")
        unreal.EditorAssetLibrary.save_directory(root, only_if_is_dirty=True, recursive=True)
        unreal.get_editor_subsystem(unreal.LevelEditorSubsystem).save_current_level()

    summary = (f"{len(mesh_assets)} meshes, {len(mat_assets)} materials, {len(tex_assets)} textures, "
               f"{placed} actors placed.\nAssets: {root}")
    for w in man.get("warnings", []):
        log.info("Blender warning: " + w)
    for note in log.notes:
        summary += "\n" + note
    if log.warnings:
        shown = "\n".join("- " + w for w in log.warnings[:15])
        more = f"\n... and {len(log.warnings) - 15} more (see Output Log)" if len(log.warnings) > 15 else ""
        summary += f"\n\n{len(log.warnings)} warnings:\n{shown}{more}"
    log.info(summary.replace("\n", " | "))
    _dialog("Blender import finished", summary)


def _dialog(title, message):
    unreal.log("[BlenderBridge] " + message.replace("\n", " | "))
    try:
        unreal.EditorDialog.show_message(title, message, unreal.AppMsgType.OK)
    except Exception:
        pass


# --------------------------------------------------------------------------- textures
HASH_TAG = "BlenderBridgeHash"


HASH_SALT = "3:"   # bump to force every asset to be re-imported once after an importer fix


def _unchanged(asset, sha1):
    try:
        return bool(sha1) and asset is not None and \
            str(unreal.EditorAssetLibrary.get_metadata_tag(asset, HASH_TAG)) == HASH_SALT + sha1
    except Exception:
        return False


def _stamp(asset, sha1):
    try:
        if sha1:
            unreal.EditorAssetLibrary.set_metadata_tag(asset, HASH_TAG, HASH_SALT + sha1)
    except Exception:
        pass


def _texture_class(info):
    usage = set(info.get("usage", []))
    if "normal" in usage:
        return "normal"
    if info.get("is_float"):
        return "hdr"
    if "color" in usage:
        return "color"
    if "data" in usage:
        return "data"
    return "data" if info.get("is_data") or info.get("colorspace") == "Non-Color" else "color"


def _apply_texture_settings(tex, key, info, cls, log):
    """Set everything in ONE call and only if needed: every change re-encodes the texture."""
    want = {}
    if cls == "normal":
        want = {"srgb": False, "compression_settings": unreal.TextureCompressionSettings.TC_NORMALMAP,
                # Blender normals are OpenGL style; a map Blender was already converting is left alone.
                "flip_green_channel": not info.get("normal_directx", False)}
        if set(info.get("usage", [])) - {"normal"}:
            log.warn(f"texture '{key}' is used as a normal map and as something else; only the normal use works")
    elif cls == "data":
        want = {"srgb": False}
    elif cls == "color":
        want = {"srgb": True}
        if info.get("colorspace") not in ("sRGB", None):
            log.warn(f"texture '{key}' is '{info.get('colorspace')}' in Blender but used as colour; treated as sRGB")
    # One property at a time and only when it differs: each change re-encodes the texture.
    # (Setting several at once with set_editor_properties left textures with no mips.)
    for k, v in want.items():
        if get_prop(tex, k) != v:
            set_prop(tex, k, v, log)


def _import_textures(man, base_dir, dest, log, task):
    """Returns {image key: (Texture2D, class)}. Several keys may share one texture."""
    unique = {}
    for key, info in man["textures"].items():
        unique.setdefault(info["asset_name"], (key, info))
    assets, tasks, skipped = {}, [], 0
    for name, (key, info) in unique.items():
        existing = load(f"{dest}/{name}")
        if _unchanged(existing, info.get("sha1")):
            assets[name] = existing
            skipped += 1
            continue
        src = os.path.join(base_dir, info["file"])
        if not os.path.isfile(src):
            log.warn(f"texture file missing: {info['file']}")
            continue
        t = unreal.AssetImportTask()
        t.filename, t.destination_path, t.destination_name = src, dest, name
        t.automated, t.replace_existing, t.save = True, True, False
        tasks.append(t)
    task.enter_progress_frame(len(man["textures"]), f"Importing {len(tasks)} textures ({skipped} unchanged)")
    if tasks:
        asset_tools().import_asset_tasks(tasks)
    for t in tasks:
        tex = load(f"{dest}/{t.destination_name}")
        if tex is None:
            log.warn(f"texture did not import: {t.filename}")
            continue
        assets[t.destination_name] = tex
    out = {}
    for name, (key, info) in unique.items():
        tex = assets.get(name)
        if tex is None:
            continue
        _apply_texture_settings(tex, key, info, _texture_class(info), log)
        _stamp(tex, info.get("sha1"))
    for key, info in man["textures"].items():
        tex = assets.get(info["asset_name"])
        if tex is not None:
            out[key] = (tex, _texture_class(info))
    log.info(f"textures: {len(tasks)} imported, {skipped} unchanged")
    return out


# --------------------------------------------------------------------------- materials
def _color(v, default=(0.8, 0.8, 0.8, 1.0)):
    if not isinstance(v, (list, tuple)) or len(v) < 3:
        v = default
    return unreal.LinearColor(float(v[0]), float(v[1]), float(v[2]), float(v[3]) if len(v) > 3 else 1.0)


def _num(v, default=0.0):
    return float(v) if isinstance(v, (int, float)) else default


def _switch(mi, name, value, log):
    fn = getattr(mel, "set_material_instance_static_switch_parameter_value", None)
    if fn is None:
        log.warn("this engine version cannot set static switches from Python; textures will not show")
        return
    fn(mi, name, bool(value))


def _build_materials(man, dest, tex_assets, log, task):
    out = {}
    for key, entry in man["materials"].items():
        task.enter_progress_frame(1, f"Material {entry['asset_name']}")
        try:
            out[key] = _build_instance(key, entry, dest, tex_assets, log)
        except Exception as exc:
            log.warn(f"material '{key}' could not be built fully ({exc}); a plain grey "
                     f"{entry['asset_name']} is used so the slot still has the right material")
            try:
                out[key] = _plain_instance(entry, dest, log)
            except Exception as exc2:
                log.warn(f"material '{key}' failed completely: {exc2}")
    return out


def _plain_instance(entry, dest, log):
    """Grey instance with the right name: the fallback when a full instance can't be built."""
    mi = load(f"{dest}/{entry['asset_name']}")
    if mi is None:
        mi = asset_tools().create_asset(entry["asset_name"], dest, unreal.MaterialInstanceConstant,
                                        unreal.MaterialInstanceConstantFactoryNew())
    else:
        mel.clear_all_material_instance_parameters(mi)
    mel.set_material_instance_parent(mi, masters.ensure_master("opaque", log))
    mel.set_material_instance_vector_parameter_value(mi, "BaseColor", unreal.LinearColor(0.8, 0.8, 0.8, 1.0))
    mel.update_material_instance(mi)
    return mi


def _build_instance(key, entry, dest, tex_assets, log):
    kind = entry.get("blend_mode", "opaque")
    master = masters.ensure_master(kind, log)
    path = f"{dest}/{entry['asset_name']}"
    mi = load(path)
    if mi is None:
        mi = asset_tools().create_asset(entry["asset_name"], dest, unreal.MaterialInstanceConstant,
                                        unreal.MaterialInstanceConstantFactoryNew())
    else:
        mel.clear_all_material_instance_parameters(mi)
    mel.set_material_instance_parent(mi, master)
    ch = entry.get("channels", {})
    mapping = [None]

    def texture(channel, want):
        """Texture2D for a channel if it has one of the wanted class, else None."""
        c = ch.get(channel) or {}
        t = c.get("texture")
        if not t:
            return None, None
        found = tex_assets.get(t["image"])
        if found is None:
            log.warn(f"material '{key}': texture '{t['image']}' for {channel} was not imported")
            return None, None
        tex, cls = found
        if cls != want:
            log.warn(f"material '{key}': texture '{t['image']}' on {channel} is a {cls} texture, "
                     f"expected {want}; plain value used")
            return None, None
        m = t.get("mapping") or {}
        sig = (tuple(m.get("scale", [1, 1])), tuple(m.get("offset", [0, 0])))
        if mapping[0] is None:
            mapping[0] = sig
        elif mapping[0] != sig:
            log.warn(f"material '{key}': textures use different UV mappings; the first one is used for all")
        return tex, t

    def scalar(name, v):
        mel.set_material_instance_scalar_parameter_value(mi, name, float(v))

    def vector(name, v):
        mel.set_material_instance_vector_parameter_value(mi, name, v)

    def tex_param(name, tex):
        mel.set_material_instance_texture_parameter_value(mi, name, tex)

    # base colour
    base = ch.get("base_color") or {}
    base_tex, _ = texture("base_color", "color")
    if base_tex:
        tex_param("BaseColorTex", base_tex)
        _switch(mi, "UseBaseColorTex", True, log)
        tint = base.get("tint") or [1.0, 1.0, 1.0]
        vector("BaseColor", unreal.LinearColor(tint[0], tint[1], tint[2], 1.0))
        adj = base.get("adjust")
        if adj:
            contrast, bright = _num(adj.get("contrast"), 0.0), _num(adj.get("bright"), 0.0)
            _switch(mi, "UseColorAdjust", True, log)
            scalar("ColorGain", 1.0 + contrast)                 # Blender: max((1+c)*col + (b - c/2), 0)
            scalar("ColorLift", bright - contrast * 0.5)
            scalar("HueShift", _num(adj.get("hue"), 0.5) - 0.5)  # Blender 0.5 = no shift
            scalar("Saturation", _num(adj.get("saturation"), 1.0))
            scalar("ColorValue", _num(adj.get("value"), 1.0))
        ao = base.get("ao")
        if ao:
            found = tex_assets.get(ao["texture"]["image"])
            if found and found[1] == "data":
                tex_param("AOTex", found[0])
                vector("AOChannel", unreal.LinearColor(*CHANNEL_MASK.get(ao["texture"]["channel"], (1, 0, 0, 0))))
                scalar("AOStrength", _num(ao.get("strength"), 1.0))
                _switch(mi, "UseAOTex", True, log)
            else:
                log.warn(f"material '{key}': AO texture '{ao['texture']['image']}' could not be used")
    else:
        vector("BaseColor", _color(base.get("value")))

    # metallic / roughness
    for channel, param, default in (("metallic", "Metallic", 0.0), ("roughness", "Roughness", 0.5)):
        c = ch.get(channel) or {}
        tex, t = texture(channel, "data")
        if tex:
            tex_param(param + "Tex", tex)
            vector(param + "Channel", unreal.LinearColor(*CHANNEL_MASK.get(t["channel"], (1, 0, 0, 0))))
            _switch(mi, "Use" + param + "Tex", True, log)
            scalar(param, _num(c.get("scale"), 1.0))
            scalar(param + "Invert", 1.0 if c.get("invert") else 0.0)
        else:
            scalar(param, _num(c.get("value"), default))
    if "specular" in ch:
        scalar("Specular", _num(ch["specular"].get("value"), 0.5))

    # normal
    n_tex, _ = texture("normal", "normal")
    if n_tex:
        tex_param("NormalTex", n_tex)
        _switch(mi, "UseNormalTex", True, log)
        scalar("NormalStrength", _num((ch.get("normal") or {}).get("strength"), 1.0))

    # emission
    em = ch.get("emission_color")
    if em:
        strength = _num((ch.get("emission_strength") or {}).get("value"), 1.0)
        em_tex, _ = texture("emission_color", "color")
        if em_tex:
            tex_param("EmissiveTex", em_tex)
            _switch(mi, "UseEmissiveTex", True, log)
        else:
            vector("EmissiveColor", _color(em.get("value"), (0, 0, 0, 1)))
        scalar("EmissiveStrength", strength)

    # opacity
    if kind != "opaque":
        alpha = ch.get("alpha") or {}
        a_tex = alpha.get("texture")
        base_t = base.get("texture")
        if a_tex and base_tex and base_t and a_tex["image"] == base_t["image"] and a_tex["channel"] == "a":
            _switch(mi, "AlphaFromBaseColorTex", True, log)
        elif a_tex:
            tex, t = texture("alpha", "data")
            if tex:
                tex_param("AlphaTex", tex)
                vector("AlphaChannel", unreal.LinearColor(*CHANNEL_MASK.get(t["channel"], (1, 0, 0, 0))))
                _switch(mi, "UseAlphaTex", True, log)
                scalar("Alpha", _num(alpha.get("scale"), 1.0))
                scalar("AlphaInvert", 1.0 if alpha.get("invert") else 0.0)
        else:
            scalar("Alpha", _num(alpha.get("value"), 1.0))

    extra = entry.get("extra") or {}
    transmission = _num(extra.get("Transmission Weight"), 0.0)
    coat = _num(extra.get("Coat Weight"), 0.0)
    sss = _num(extra.get("Subsurface Weight"), 0.0)
    shading = None

    # glass: Glass BSDF, or Principled with transmission
    if kind == "translucent" and (entry.get("shader") == "glass" or transmission > 0.5):
        _switch(mi, "GlassFresnel", True, log)
        scalar("IOR", _num(extra.get("IOR"), 1.45))
        scalar("GlassOpacity", max(0.03, 0.1 + 0.6 * _num((ch.get("roughness") or {}).get("value"), 0.0)))
        if entry.get("shader") == "glass":
            scalar("Metallic", 0.0)
            scalar("Specular", 0.5)

    # clear coat
    if coat > 0.01:
        scalar("Coat", coat)
        scalar("CoatRoughness", _num(extra.get("Coat Roughness"), 0.03))
        shading = "MSM_CLEAR_COAT"

    # subsurface (an approximation: Blender scatters by radius, Unreal by colour + opacity)
    if sss > 0.01 and kind != "translucent":
        radius = extra.get("Subsurface Radius")
        if isinstance(radius, (list, tuple)) and len(radius) >= 3 and max(radius[:3]) > 0:
            top = max(radius[:3])
            vector("SubsurfaceColor", unreal.LinearColor(radius[0] / top, radius[1] / top, radius[2] / top, 1.0))
        scalar("SubsurfaceOpacity", max(0.1, 1.0 - 0.7 * sss))
        if shading:
            log.warn(f"material '{key}': has both clear coat and subsurface; Unreal can only do one, clear coat kept")
        else:
            shading = "MSM_SUBSURFACE"

    if shading:
        model = getattr(unreal.MaterialShadingModel, shading, None)
        ov = get_prop(mi, "base_property_overrides")
        if (model is not None and ov is not None and set_prop(ov, "override_shading_model", True)
                and set_prop(ov, "shading_model", model)):
            set_prop(mi, "base_property_overrides", ov)
        else:
            log.warn(f"material '{key}': could not switch the shading model to {shading}")

    # UV mapping (Blender V axis points up, Unreal's points down)
    if mapping[0]:
        (sx, sy), (ox, oy) = mapping[0]
        vector("UVTiling", unreal.LinearColor(sx, sy, 0, 0))
        vector("UVOffset", unreal.LinearColor(ox, 1.0 - sy - oy, 0, 0))

    if entry.get("two_sided"):
        ov = get_prop(mi, "base_property_overrides")
        if ov is not None and set_prop(ov, "override_two_sided", True) and set_prop(ov, "two_sided", True):
            set_prop(mi, "base_property_overrides", ov)
        else:
            log.warn(f"material '{key}': could not enable two-sided on the instance")
    mel.update_material_instance(mi)
    return mi


# --------------------------------------------------------------------------- meshes
def _interchange_options(log, custom_normals=False):
    """Pipeline override: static mesh only, no new materials or textures.

    Each material slot in the FBX carries the name of the Material Instance we already built,
    and the search setting lets Interchange assign that existing asset by name. We assign
    them again ourselves afterwards, so this is only a head start."""
    try:
        pipe = unreal.InterchangeGenericAssetsPipeline()
        set_prop(pipe, "use_source_name_for_asset", True)
        mat = get_prop(pipe, "material_pipeline")
        if mat is not None:
            set_prop(mat, "import_materials", False)
            search = getattr(unreal, "InterchangeMaterialSearchLocation", None)
            for name in ("ALL_ASSETS", "UNDER_ROOT", "UNDER_PARENT"):
                if search is not None and hasattr(search, name):
                    set_prop(mat, "search_location", getattr(search, name))
                    break
            tex = get_prop(mat, "texture_pipeline")
            if tex is not None:
                set_prop(tex, "import_textures", False)
        mesh = get_prop(pipe, "mesh_pipeline")
        if mesh is not None:
            set_prop(mesh, "import_static_meshes", True)
            set_prop(mesh, "combine_static_meshes", True)
            set_prop(mesh, "import_skeletal_meshes", False)
        common = get_prop(pipe, "common_meshes_properties")
        force = getattr(getattr(unreal, "InterchangeForceMeshType", None), "IFMT_STATIC_MESH", None)
        if common is not None and force is not None:
            set_prop(common, "force_all_mesh_as_type", force)
        if common is not None:
            # Custom normals: keep the normals and tangents from the FBX exactly as Blender wrote them.
            set_prop(common, "recompute_normals", not custom_normals)
            set_prop(common, "recompute_tangents", not custom_normals)
            set_prop(common, "use_mikk_t_space", True)
            set_prop(common, "compute_weighted_normals", False)
        anim = get_prop(pipe, "animation_pipeline")
        if anim is not None:
            set_prop(anim, "import_animations", False)
        override = unreal.InterchangePipelineStackOverride()
        override.add_pipeline(pipe)
        return override, pipe
    except Exception as exc:
        log.warn(f"Interchange pipeline override unavailable ({exc}); using the engine's default FBX import")
        return None, None


def _check_build_settings(mesh, name, custom_normals, log):
    """After import: make sure Unreal kept (or recomputes) normals and tangents as the export intends.
    Only touches a mesh whose settings differ, since a change makes Unreal rebuild it."""
    sms = unreal.get_editor_subsystem(unreal.StaticMeshEditorSubsystem)
    try:
        build = sms.get_lod_build_settings(mesh, 0)
    except Exception as exc:
        log.warn(f"could not read the build settings of '{name}': {exc}")
        return
    want = {"recompute_normals": not custom_normals, "recompute_tangents": not custom_normals,
            "use_mikk_t_space": True}
    if custom_normals:
        want["compute_weighted_normals"] = False
    changed = [k for k, v in want.items() if get_prop(build, k) is not None and get_prop(build, k) != v]
    if not changed:
        return
    for k in changed:
        set_prop(build, k, want[k], log)
    try:
        sms.set_lod_build_settings(mesh, 0, build)
        log.info(f"'{name}': corrected build settings ({', '.join(changed)})")
    except Exception as exc:
        log.warn(f"could not correct the build settings of '{name}': {exc}")


def _find_static_mesh(dest, name, task_obj):
    mesh = load(f"{dest}/{name}")
    if isinstance(mesh, unreal.StaticMesh):
        return mesh
    for p in list(getattr(task_obj, "imported_object_paths", []) or []):
        obj = unreal.EditorAssetLibrary.load_asset(str(p).split(".")[0])
        if isinstance(obj, unreal.StaticMesh):
            return obj
    return None


def _norm(name):
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def _assign_materials(mesh, info, man, mat_assets, log):
    """Put each Blender slot's material on the matching Unreal slot.
    The FBX slot names are unique per mesh, so slots are matched by name; only when no names
    match at all and the counts agree is the (unchanged) slot order used."""
    keys = info.get("material_slots", [])
    names = info.get("slot_names") or []
    mesh_name = info["asset_name"]
    statics = list(get_prop(mesh, "static_materials") or [])
    by_name = {}
    for index, sm in enumerate(statics):
        for attr in ("material_slot_name", "imported_material_slot_name"):
            n = _norm(get_prop(sm, attr) or "")
            if n:
                by_name.setdefault(n, index)
    matched = {}
    for i, key in enumerate(keys):
        if i < len(names) and names[i] and _norm(names[i]) in by_name:
            matched[i] = by_name[_norm(names[i])]
    if not matched and len(statics) == len(keys):
        matched = {i: i for i in range(len(keys))}
        log.info(f"mesh '{mesh_name}': slot names not found, slots matched by their order")
    elif len(matched) < len(keys):
        # Unreal never keeps a slot that no face uses; only a missing slot WITH faces is a problem.
        faces = info.get("slot_faces")
        unused = [i for i in range(len(keys)) if i not in matched and faces is not None
                  and i < len(faces) and faces[i] == 0]
        missing = [names[i] if i < len(names) else f"slot {i + 1}" for i in range(len(keys))
                   if i not in matched and i not in unused]
        log.unused_slots = getattr(log, "unused_slots", 0) + len(unused)
        if missing:
            log.warn(f"mesh '{mesh_name}': used material slot(s) missing in Unreal: {', '.join(missing)}")
    for i, index in matched.items():
        mi = mat_assets.get(keys[i]) if keys[i] else None
        if mi is None:
            log.warn(f"mesh '{mesh_name}': slot {i + 1} ({names[i] if i < len(names) else '?'}) has no "
                     f"material to assign")
            continue
        if mesh.get_material(index) != mi:
            mesh.set_material(index, mi)


def _remove_stray_materials(dest, before, keep, log):
    """Delete materials Interchange created next to the meshes (ours live in /Materials)."""
    keep_paths = {m.get_path_name().split(".")[0] for m in keep}
    removed = 0
    for path in unreal.EditorAssetLibrary.list_assets(dest, recursive=True):
        base = str(path).split(".")[0]
        if str(path) in before or base in keep_paths:
            continue
        obj = unreal.EditorAssetLibrary.load_asset(base)
        if isinstance(obj, unreal.MaterialInterface):
            if unreal.EditorAssetLibrary.delete_asset(base):
                removed += 1
    if removed:
        log.info(f"removed {removed} placeholder material(s) created by the FBX import")


def _import_meshes(man, base_dir, dest, mat_assets, log, task):
    out, tasks, skipped = {}, {}, 0
    custom_normals = bool(man.get("custom_normals"))
    override, _keep_alive = _interchange_options(log, custom_normals)
    for key, info in man["meshes"].items():
        name = info["asset_name"]
        existing = load(f"{dest}/{name}")
        if isinstance(existing, unreal.StaticMesh) and _unchanged(existing, info.get("sha1")):
            out[key] = existing
            skipped += 1
            continue
        src = os.path.join(base_dir, info["file"])
        if not os.path.isfile(src):
            log.warn(f"mesh file missing: {info['file']}")
            continue
        t = unreal.AssetImportTask()
        t.filename, t.destination_path, t.destination_name = src, dest, name
        t.automated, t.replace_existing, t.save = True, True, False
        if override is not None:
            t.options = override
        tasks[key] = t
    task.enter_progress_frame(len(man["meshes"]), f"Importing {len(tasks)} meshes ({skipped} unchanged)")
    before = set()
    if unreal.EditorAssetLibrary.does_directory_exist(dest):
        before = {str(p) for p in unreal.EditorAssetLibrary.list_assets(dest, recursive=True)}
    if tasks:
        asset_tools().import_asset_tasks(list(tasks.values()))
    for key, t in tasks.items():
        mesh = _find_static_mesh(dest, t.destination_name, t)
        if mesh is None:
            log.warn(f"mesh '{t.destination_name}' did not import as a static mesh")
            continue
        _check_build_settings(mesh, man["meshes"][key]["asset_name"], custom_normals, log)
        _stamp(mesh, man["meshes"][key].get("sha1"))
        out[key] = mesh
    for key, mesh in out.items():
        _assign_materials(mesh, man["meshes"][key], man, mat_assets, log)
    if getattr(log, "unused_slots", 0):
        log.notes.append(f"{log.unused_slots} unused material slot(s) (no faces) not kept by Unreal - normal")
        log.info(log.notes[-1])
    if tasks:
        try:
            _remove_stray_materials(dest, before, list(mat_assets.values()), log)
        except Exception as exc:
            log.warn(f"could not clean up placeholder materials: {exc}")
    log.info(f"meshes: {len(tasks)} imported, {skipped} unchanged")
    return out


# --------------------------------------------------------------------------- placement
def _open_or_create_map(path, log):
    """Returns True if the map already existed."""
    les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
    if exists(path):
        les.load_level(path)
        return True
    ok = False
    if exists(TEMPLATE_MAP):
        ok = les.new_level_from_template(path, TEMPLATE_MAP)
    if not ok:
        ok = les.new_level(path)
        if ok:
            log.warn("created an empty map (no template found); add a light to see the assets lit")
    if not ok:
        raise RuntimeError(f"could not create map {path}")
    return False


def _tagged_actors():
    eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    return [a for a in eas.get_all_level_actors() if a.actor_has_tag(TAG)]


def _finish_actor(actor, label, folder):
    actor.set_actor_label(label)
    actor.set_folder_path(folder)
    set_prop(actor, "tags", [unreal.Name(TAG)])


def _aim(tr):
    """Location + rotation from the forward / up vectors the exporter wrote."""
    loc = unreal.Vector(*tr["location"])
    rot = unreal.MathLibrary.make_rot_from_xz(unreal.Vector(*tr["forward"]), unreal.Vector(*tr["up"]))
    return loc, rot


def _folder(entry):
    coll = entry.get("collection")
    return "Blender" if coll == "" else "Blender/" + (coll or "Scene Collection")


LIGHT_ACTORS = {"SUN": "DirectionalLight", "POINT": "PointLight", "SPOT": "SpotLight", "AREA": "RectLight"}


def configure_light(comp, light, log, template=False):
    """Light settings on a component: a placed actor's, or a Blueprint component template."""
    set_prop(comp, "mobility", unreal.ComponentMobility.MOVABLE)
    if light["type"] != "SUN":
        set_prop(comp, "intensity_units", unreal.LightUnits.CANDELAS, log)   # before the intensity
    c = light.get("color", [1, 1, 1])
    if template:
        set_prop(comp, "intensity", float(light["intensity"]), log)
        try:
            comp.set_light_color(unreal.LinearColor(c[0], c[1], c[2], 1.0), True)
        except Exception:
            srgb = [int(round(255 * (v * 12.92 if v <= 0.0031308 else 1.055 * v ** (1 / 2.4) - 0.055)))
                    for v in (max(0.0, min(1.0, x)) for x in c[:3])]
            set_prop(comp, "light_color", unreal.Color(srgb[2], srgb[1], srgb[0], 255), log)
        set_prop(comp, "cast_shadows", bool(light.get("cast_shadow", True)), log)
    else:
        comp.set_mobility(unreal.ComponentMobility.MOVABLE)
        comp.set_intensity(float(light["intensity"]))
        comp.set_light_color(unreal.LinearColor(c[0], c[1], c[2], 1.0), True)
        comp.set_cast_shadows(bool(light.get("cast_shadow", True)))
    if light.get("temperature"):
        set_prop(comp, "use_temperature", True)
        set_prop(comp, "temperature", float(light["temperature"]))
    if light["type"] == "SUN":
        set_prop(comp, "light_source_angle", float(light.get("angle", 0.5)))
    else:
        # Blender lights never cut off; keep going until they are too dim to matter.
        reach = math.sqrt(max(light["intensity"], 0.0) / 0.01) * 100.0
        set_prop(comp, "attenuation_radius", min(max(reach, 1000.0), 30000.0))
    if light["type"] in ("POINT", "SPOT"):
        set_prop(comp, "source_radius", float(light.get("radius", 0.0)))
    if light["type"] == "SPOT":
        set_prop(comp, "outer_cone_angle", float(light.get("outer_cone", 44.0)))
        set_prop(comp, "inner_cone_angle", float(light.get("inner_cone", 0.0)))
    if light["type"] == "AREA":
        set_prop(comp, "source_width", float(light.get("width", 64.0)))
        set_prop(comp, "source_height", float(light.get("height", 64.0)))
        if light.get("shape") in ("DISK", "ELLIPSE"):
            log.warn(f"light '{light['name']}': round area lights become rectangular in Unreal")


def configure_camera(comp, cam, log):
    if cam.get("type") != "PERSP":
        log.warn(f"camera '{cam['name']}': only perspective cameras are converted; lens settings left at default")
        return
    fb = get_prop(comp, "filmback")
    if fb is not None:
        set_prop(fb, "sensor_width", float(cam["sensor_width"]))
        set_prop(fb, "sensor_height", float(cam["sensor_height"]))
        set_prop(comp, "filmback", fb, log)
    set_prop(comp, "current_focal_length", float(cam["lens"]), log)
    focus = get_prop(comp, "focus_settings")
    if focus is not None:
        if cam.get("use_dof"):
            set_prop(focus, "focus_method", unreal.CameraFocusMethod.MANUAL)
            set_prop(focus, "manual_focus_distance", float(cam.get("focus_distance", 1000.0)))
            set_prop(comp, "current_aperture", float(cam.get("fstop", 2.8)))
        else:
            set_prop(focus, "focus_method", unreal.CameraFocusMethod.DISABLE)
        set_prop(comp, "focus_settings", focus, log)


def _spawn_light(eas, light, log):
    cls = getattr(unreal, LIGHT_ACTORS.get(light["type"], ""), None)
    if cls is None:
        log.warn(f"light '{light['name']}': type {light['type']} is not supported")
        return None
    loc, rot = _aim(light["unreal"])
    actor = eas.spawn_actor_from_class(cls, loc, rot)
    if actor is None:
        return None
    configure_light(actor.get_component_by_class(unreal.LightComponent), light, log)
    return actor


def _spawn_camera(eas, cam, log):
    loc, rot = _aim(cam["unreal"])
    actor = eas.spawn_actor_from_class(unreal.CineCameraActor, loc, rot)
    if actor is None:
        return None
    configure_camera(actor.get_cine_camera_component(), cam, log)
    return actor


def _spawn_world(eas, man, base_dir, root, log):
    """Sky dome (visible background) + a sky light that captures it."""
    world = man.get("world") or {}
    if world.get("type", "none") == "none":
        return 0
    cubemap = None
    if world.get("type") == "hdri":
        dest, name = root + "/Textures", world["asset_name"]
        cubemap = load(f"{dest}/{name}")
        if not _unchanged(cubemap, world.get("sha1")):
            t = unreal.AssetImportTask()
            t.filename, t.destination_path, t.destination_name = os.path.join(base_dir, world["file"]), dest, name
            t.automated, t.replace_existing, t.save = True, True, False
            asset_tools().import_asset_tasks([t])
            cubemap = load(f"{dest}/{name}")
            if cubemap is not None:
                _stamp(cubemap, world.get("sha1"))
        if not isinstance(cubemap, unreal.TextureCube):
            log.warn("the world HDRI did not import as a cubemap; a flat sky colour is used instead")
            cubemap = None
    # Blender's image centre faces -X, Unreal's is assumed to face +X: half a turn, plus the Mapping rotation.
    rotation = (0.5 - float(world.get("rotation_z", 0.0)) / (2.0 * math.pi)) % 1.0
    mat = masters.build_sky_material(f"{root}/Materials/M_{man['name']}_Sky", cubemap, world.get("color"),
                                     world.get("strength", 1.0), rotation, log)
    count = 0
    sphere = unreal.load_asset("/Engine/BasicShapes/Sphere")
    if sphere is not None:
        dome = eas.spawn_actor_from_object(sphere, unreal.Vector(0, 0, 0), unreal.Rotator(0, 0, 0))
        if dome is not None:
            dome.set_actor_scale3d(unreal.Vector(40000, 40000, 40000))      # 20 km radius
            comp = dome.static_mesh_component
            comp.set_material(0, mat)
            comp.set_cast_shadow(False)
            comp.set_collision_enabled(unreal.CollisionEnabled.NO_COLLISION)
            _finish_actor(dome, "BB_SkyDome", "Blender/World")
            count += 1
    sky = eas.spawn_actor_from_class(unreal.SkyLight, unreal.Vector(0, 0, 0), unreal.Rotator(0, 0, 0))
    if sky is not None:
        comp = sky.get_component_by_class(unreal.SkyLightComponent)
        comp.set_mobility(unreal.ComponentMobility.MOVABLE)
        set_prop(comp, "real_time_capture", True, log)
        _finish_actor(sky, "BB_SkyLight", "Blender/World")
        count += 1
    return count


def _place_scene(man, base_dir, root, mesh_assets, log):
    """Scene mode: one map per export; meshes, lights, cameras and the world rebuilt from Blender."""
    eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    les = unreal.get_editor_subsystem(unreal.LevelEditorSubsystem)
    path = f"{root}/Maps/{man['name']}"
    if exists(path):
        les.load_level(path)
        for actor in _tagged_actors():  # re-import: replace what we placed last time
            eas.destroy_actor(actor)
        leftovers = [a for a in eas.get_all_level_actors()
                     if isinstance(a, (unreal.DirectionalLight, unreal.SkyLight))]
        if leftovers:
            log.warn("this map still has its own sun / sky light (from an older import or added by hand), "
                     "so it is lit twice. Delete them, or delete the map asset and import again.")
    elif not les.new_level(path):
        raise RuntimeError(f"could not create map {path}")
    groups = man.get("hierarchy") or {}
    grouped = groups.get("mode") in ("GROUPED", "BLUEPRINT") and groups.get("units")

    def member(entry):
        return grouped and entry.get("unit") is not None

    count = 0
    placed = {"objects": {}, "lights": {}, "cameras": {}}     # Grouped mode: index -> actor
    for i, obj in enumerate(man["objects"]):
        mesh = mesh_assets.get(obj["mesh"])
        if mesh is None or (member(obj) and groups["mode"] == "BLUEPRINT"):
            continue
        tr = obj["unreal"]
        q = tr["rotation_quat"]
        loc = unreal.Vector(*tr["location"])
        rot = unreal.Quat(q[0], q[1], q[2], q[3]).rotator()
        actor = eas.spawn_actor_from_object(mesh, loc, rot)
        if actor is None:
            log.warn(f"could not place '{obj['name']}'")
            continue
        actor.set_actor_scale3d(unreal.Vector(*tr["scale"]))
        _finish_actor(actor, obj.get("label") or obj["name"], _folder(obj))
        placed["objects"][i] = actor
        count += 1
    for kind, items, spawn in (("light", man.get("lights", []), _spawn_light),
                               ("camera", man.get("cameras", []), _spawn_camera)):
        for i, item in enumerate(items):
            if member(item) and groups["mode"] == "BLUEPRINT":
                continue
            try:
                actor = spawn(eas, item, log)
            except Exception as exc:
                actor = None
                log.warn(f"{kind} '{item.get('name')}' failed: {exc}")
            if actor is not None:
                _finish_actor(actor, item.get("label") or item["name"], _folder(item))
                placed[kind + "s"][i] = actor
                count += 1
    if grouped:
        try:
            if groups["mode"] == "GROUPED":
                count += _attach_groups(eas, man, placed, log)
            else:
                count += blueprints.place_blueprints(eas, man, root, mesh_assets, log)
        except Exception as exc:
            log.warn(f"{groups['mode'].lower()} placement failed: {exc}")
    try:
        count += _spawn_world(eas, man, base_dir, root, log)
    except Exception as exc:
        log.warn(f"world / sky failed: {exc}")
    if not man.get("lights") and (man.get("world") or {}).get("type", "none") == "none":
        log.warn("the scene has no lights and no world, so the map will be dark")
    return count


def spawn_group_root(eas, transform, label, folder):
    """An invisible actor with a movable root, standing in for a Blender empty."""
    q = transform["rotation_quat"]
    actor = eas.spawn_actor_from_class(unreal.StaticMeshActor, unreal.Vector(*transform["location"]),
                                       unreal.Quat(q[0], q[1], q[2], q[3]).rotator())
    if actor is None:
        return None
    actor.set_actor_scale3d(unreal.Vector(*transform["scale"]))
    # Static, like the pieces: Unreal won't attach a Static actor to a Movable one.
    # (Movable lights and cameras can still attach to it.)
    actor.static_mesh_component.set_mobility(unreal.ComponentMobility.STATIC)
    _finish_actor(actor, label, folder)
    return actor


def _attach_groups(eas, man, placed, log):
    """Grouped mode: empties become actors and every piece is attached to its parent, keeping
    its exact world position (Unreal works out the relative transform)."""
    kinds = {"mesh": "objects", "light": "lights", "camera": "cameras"}
    rule = unreal.AttachmentRule.KEEP_WORLD
    made = 0
    for unit in man["hierarchy"]["units"]:
        folder = _folder(unit)
        actors = {}
        for node in unit["nodes"]:
            actor = None
            if node["ref"] is not None and node["kind"] in kinds:
                actor = placed[kinds[node["kind"]]].get(node["ref"])
            if actor is None:   # an empty (or a piece that failed to place): stand-in actor
                actor = spawn_group_root(eas, node["world"], node["label"], folder)
                if actor is None:
                    log.warn(f"group '{unit['label']}': could not create '{node['label']}'")
                    continue
                made += 1
            actor.set_folder_path(folder)
            actors[node["id"]] = actor
            if node["parent"] is not None and node["parent"] in actors:
                if not actor.attach_to_actor(actors[node["parent"]], "", rule, rule, rule, False):
                    log.warn(f"group '{unit['label']}': could not attach '{node['label']}'")
    return made


def _place_overview(man, mesh_assets, log):
    """Asset mode: one shared map, each asset placed once on a grid."""
    eas = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    _open_or_create_map(OVERVIEW_MAP, log)
    gap, row_width = 100.0, 4000.0
    present, start_y = set(), 0.0
    for actor in _tagged_actors():
        present.add(str(actor.get_actor_label()))
        origin, extent = actor.get_actor_bounds(False)
        start_y = max(start_y, origin.y + extent.y + gap)
    x, y, row_depth, count = 0.0, start_y, 0.0, 0
    for key, info in man["meshes"].items():
        mesh = mesh_assets.get(key)
        if mesh is None or info["asset_name"] in present:
            continue  # already in the map; the mesh asset itself was updated in place
        b = mesh.get_bounds()
        o, e = b.origin, b.box_extent
        if x > 0 and x + 2 * e.x > row_width:
            x, y, row_depth = 0.0, y + row_depth + gap, 0.0
        loc = unreal.Vector(x + e.x - o.x, y + e.y - o.y, e.z - o.z)
        actor = eas.spawn_actor_from_object(mesh, loc, unreal.Rotator(0, 0, 0))
        if actor is None:
            log.warn(f"could not place '{info['asset_name']}'")
            continue
        _finish_actor(actor, info["asset_name"], "Blender/" + man["name"])
        x += 2 * e.x + gap
        row_depth = max(row_depth, 2 * e.y)
        count += 1
    return count
