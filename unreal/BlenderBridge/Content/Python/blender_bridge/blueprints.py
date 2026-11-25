"""Blueprint mode: each Blender parent + children becomes a Blueprint, placed once per copy.

Components are added with the SubobjectDataSubsystem (the same machinery as the Blueprint
editor's "Add Component" button). Identical houses share one Blueprint, so editing it updates
every copy in the level.
"""
import unreal

from .util import Log, asset_tools, exists, load, set_prop

LIGHT_COMPONENTS = {"SUN": "DirectionalLightComponent", "POINT": "PointLightComponent",
                    "SPOT": "SpotLightComponent", "AREA": "RectLightComponent"}


def _xf(t):
    q = t["rotation_quat"]
    return (unreal.Vector(*t["location"]), unreal.Quat(q[0], q[1], q[2], q[3]).rotator(),
            unreal.Vector(*t["scale"]))


def _set_relative(comp, t, log):
    loc, rot, scale = _xf(t)
    ok = (set_prop(comp, "relative_location", loc) and set_prop(comp, "relative_rotation", rot)
          and set_prop(comp, "relative_scale3d", scale))
    if not ok:
        try:
            comp.set_relative_transform(unreal.Transform(loc, rot, scale), False, False)
        except Exception as exc:
            log.warn(f"could not set the transform of component '{comp.get_name()}': {exc}")


class _Builder:
    def __init__(self, bp, log):
        self.bp, self.log = bp, log
        self.sds = unreal.get_engine_subsystem(unreal.SubobjectDataSubsystem)
        self.lib = unreal.SubobjectDataBlueprintFunctionLibrary
        self.names = set()
        handles = self.sds.k2_gather_subobject_data_for_blueprint(bp)
        if not handles:
            raise RuntimeError("the Blueprint has no root to add components to")
        self.root = handles[0]
        self._clear(handles)

    def _obj(self, handle):
        return self.lib.get_object(self.lib.get_data(handle))

    def _clear(self, handles):
        """Re-import: remove every component we added last time (the scene root stays)."""
        doomed = []
        for h in handles[1:]:
            obj = self._obj(h)
            if obj is not None and obj.get_name() != "DefaultSceneRoot":
                doomed.append(h)
        # The gathered list goes parent-before-child. Delete in reverse, one at a time, so a child
        # is never deleted after its parent is already gone (Unreal raises an ensure for that).
        for h in reversed(doomed):
            self.sds.delete_subobjects(self.root, [h], self.bp)

    def add(self, cls, parent, label):
        params = unreal.AddNewSubobjectParams(parent_handle=parent or self.root, new_class=cls,
                                              blueprint_context=self.bp)
        handle, reason = self.sds.add_new_subobject(params)
        if not self.lib.is_handle_valid(handle):
            raise RuntimeError(f"could not add a {cls.__name__}: {reason}")
        name, n = label, 1
        while name.lower() in self.names:
            n += 1
            name = f"{label}_{n}"
        self.names.add(name.lower())
        try:
            self.sds.rename_subobject(handle, unreal.Text(name))
        except Exception:
            pass
        return handle, self._obj(handle)


def _build(bp, unit, man, mesh_assets, instanced, log):
    from .importer import configure_camera, configure_light
    b = _Builder(bp, log)
    nodes = unit["nodes"]
    handles = {}

    # Pieces that use the same mesh more than once (and have no children) -> one instanced component.
    pooled = {}
    if instanced:
        groups = {}
        for n in nodes:
            if n["kind"] == "mesh" and n.get("leaf") and n.get("in_root") is not None:
                groups.setdefault(man["objects"][n["ref"]]["mesh"], []).append(n)
        pooled = {}
        for k, v in groups.items():
            # Unreal decides "mirrored or not" once per component, so mirrored copies get their own
            # instanced component that carries the mirror (instances inside it are unmirrored).
            normal = [n for n in v if not n.get("mirrored")]
            mirrored = [n for n in v if n.get("mirrored") and n.get("in_root_unmirrored")]
            if len(normal) > 1:
                pooled[(k, False)] = normal
            if len(mirrored) > 1:
                pooled[(k, True)] = mirrored
    pooled_ids = {n["id"] for v in pooled.values() for n in v}

    for n in nodes:
        if n["id"] in pooled_ids:
            continue
        parent = handles.get(n["parent"]) if n["parent"] is not None else None
        kind = n["kind"]
        if kind == "mesh":
            mesh = mesh_assets.get(man["objects"][n["ref"]]["mesh"])
            if mesh is None:
                kind = "empty"
        if kind == "mesh":
            handle, comp = b.add(unreal.StaticMeshComponent, parent, n["label"])
            comp.set_static_mesh(mesh)
        elif kind == "light":
            light = man["lights"][n["ref"]]
            cls = getattr(unreal, LIGHT_COMPONENTS.get(light["type"], ""), None)
            if cls is None:
                log.warn(f"light '{light['name']}': type {light['type']} is not supported")
                kind = "empty"
            else:
                handle, comp = b.add(cls, parent, n["label"])
                configure_light(comp, light, log, template=True)
        elif kind == "camera":
            handle, comp = b.add(unreal.CineCameraComponent, parent, n["label"])
            configure_camera(comp, man["cameras"][n["ref"]], log)
        if kind == "empty":
            if n["id"] == 0:
                continue            # the Blueprint's own scene root stands in for the root empty
            handle, comp = b.add(unreal.SceneComponent, parent, n["label"])
        _set_relative(comp, n["local"], log)
        handles[n["id"]] = handle

    for (key, mirrored), members in pooled.items():
        mesh = mesh_assets.get(key)
        if mesh is None:
            continue
        asset = man["meshes"].get(key, {}).get("asset_name", "Mesh")
        handle, comp = b.add(unreal.InstancedStaticMeshComponent, None,
                             "ISM_" + (asset[3:] if asset.startswith("SM_") else asset)
                             + ("_Mirrored" if mirrored else ""))
        comp.set_static_mesh(mesh)
        if mirrored:
            _set_relative(comp, {"location": [0, 0, 0], "rotation_quat": [0, 0, 0, 1],
                                 "scale": [-1.0, 1.0, 1.0]}, log)
        added = 0
        for n in members:
            loc, rot, scale = _xf(n["in_root_unmirrored"] if mirrored else n["in_root"])
            try:
                comp.add_instance(unreal.Transform(loc, rot, scale), False)
                added += 1
            except Exception as exc:
                log.warn(f"Blueprint '{bp.get_name()}': could not add an instance of {key}: {exc}")
                break
        try:
            if comp.get_instance_count() != added:
                added = -1
        except Exception:
            pass
        if added != len(members):
            # fall back to one component per piece
            b.sds.delete_subobjects(b.root, [handle], bp)
            for n in members:
                h, c = b.add(unreal.StaticMeshComponent, handles.get(n["parent"]), n["label"])
                c.set_static_mesh(mesh)
                _set_relative(c, n["local"], log)
    try:
        unreal.BlueprintEditorLibrary.compile_blueprint(bp)
    except Exception as exc:
        log.warn(f"Blueprint '{bp.get_name()}' could not be compiled from Python ({exc}); open and compile it once")
    unreal.EditorAssetLibrary.save_loaded_asset(bp)


def _get_blueprint(folder, name):
    path = f"{folder}/{name}"
    bp = load(path)
    if bp is None:
        factory = unreal.BlueprintFactory()
        factory.set_editor_property("parent_class", unreal.Actor)
        bp = asset_tools().create_asset(name, folder, unreal.Blueprint, factory)
    if bp is None:
        raise RuntimeError(f"could not create Blueprint {path}")
    return bp, path


def place_blueprints(eas, man, root, mesh_assets, log):
    from .importer import _finish_actor, _folder
    hierarchy = man["hierarchy"]
    folder = root + "/Blueprints"
    classes = {}
    for name, info in hierarchy["blueprints"].items():
        unit = hierarchy["units"][info["source_unit"]]
        try:
            bp, path = _get_blueprint(folder, name)
            _build(bp, unit, man, mesh_assets, hierarchy.get("instanced", True), log)
            classes[name] = unreal.EditorAssetLibrary.load_blueprint_class(path)
        except Exception as exc:
            log.warn(f"Blueprint '{name}' failed: {exc}")
    count = 0
    for unit in hierarchy["units"]:
        cls = classes.get(unit["blueprint"])
        if cls is None:
            continue
        loc, rot, scale = _xf(unit["world"])
        actor = eas.spawn_actor_from_class(cls, loc, rot)
        if actor is None:
            log.warn(f"could not place Blueprint '{unit['blueprint']}' for '{unit['label']}'")
            continue
        actor.set_actor_scale3d(scale)
        _finish_actor(actor, unit["label"], _folder(unit))
        count += 1
    log.info(f"Blueprints: {len(classes)} built, {count} placed")
    return count
