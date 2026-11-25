# SPDX-License-Identifier: GPL-3.0-or-later
"""Unreal Bridge - one-click export of assets and scenes to Unreal Engine."""
import time
import traceback

import bpy
from bpy.props import BoolProperty, EnumProperty, PointerProperty, StringProperty

from . import exporter


class UnrealBridgeSettings(bpy.types.PropertyGroup):
    mode: EnumProperty(
        name="Mode",
        items=[('ASSETS', "Assets", "Export the selected objects as standalone assets"),
               ('SCENE', "Scene", "Export the scene with its layout; Unreal builds a map from it")],
        default='ASSETS')
    selected_only: BoolProperty(
        name="Selected Only", default=False,
        description="Scene mode: export only the selected objects instead of everything renderable")
    export_dir: StringProperty(
        name="Export Folder", subtype='DIR_PATH', default="//unreal_export",
        description="Folder that receives the meshes, textures and manifest")
    unreal_root: StringProperty(
        name="Content Path", default="/Game/Blender",
        description="Content Browser folder inside the Unreal project, e.g. /Game/Blender "
                    "(a folder inside the project's Content directory also works)")
    export_name: StringProperty(
        name="Name", default="",
        description="Name of this export (sub-folder and map name). Empty = name of the .blend file")
    hierarchy: EnumProperty(
        name="Hierarchy",
        items=[('AS_IS', "As Is", "Every piece is its own actor at its world position"),
               ('GROUPED', "Grouped", "Parents (e.g. an empty holding a house) become actors in the level "
                                      "and their children are attached, so the house moves as one"),
               ('BLUEPRINT', "Blueprint", "Each parent with its children becomes a Blueprint; identical "
                                          "copies and collection instances share one Blueprint")],
        default='AS_IS')
    group_by: EnumProperty(
        name="Group By",
        items=[('PARENT', "Parent", "Each top-level object with children (e.g. an empty holding a house) "
                                    "becomes one group / Blueprint"),
               ('COLLECTION', "Collection", "Each collection that holds objects (each Outliner folder in "
                                            "Unreal) becomes one group / Blueprint. Origin: the collection's "
                                            "Instance Offset if set, else the bottom-centre of its contents")],
        default='PARENT')
    instance_repeats: BoolProperty(
        name="Instance Repeated Pieces", default=True,
        description="Inside a Blueprint, pieces that use the same mesh more than once become one instanced "
                    "component (faster to render). Off: one component per piece, each editable on its own")
    custom_normals: BoolProperty(
        name="Export Custom Normals", default=True,
        description="Triangulate each mesh in Blender first, keeping every corner normal exactly, then "
                    "export with smoothing groups and Blender's tangents; Unreal uses them as they are. "
                    "Off: Unreal computes normals and tangents itself")
    bake: BoolProperty(
        name="Bake Procedural Materials", default=True,
        description="Materials that can't be converted node by node (Noise, Color Ramp, mixed shaders, ...) "
                    "are baked to textures. A mesh that needs it gets a new UV map and all its materials baked")
    bake_resolution: EnumProperty(
        name="Bake Size",
        items=[('512', "512", ""), ('1024', "1024", ""), ('2048', "2048", ""), ('4096', "4096", "")],
        default='2048', description="Texture size for baked materials")
    last_report: StringProperty(default="")
    last_manifest: StringProperty(default="")


# Shown in the panel while an export runs.
STATUS = {"running": False, "fraction": 0.0, "text": ""}
# Events that only move the view; everything else is held back during an export so the
# scene can't change under it.
_PASS_THROUGH = {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE', 'MIDDLEMOUSE', 'WHEELUPMOUSE', 'WHEELDOWNMOUSE',
                 'TRACKPADPAN', 'TRACKPADZOOM', 'WINDOW_DEACTIVATE', 'TIMER', 'TIMER_REPORT'}


def _redraw(context):
    for window in context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()


class UNREALBRIDGE_OT_export(bpy.types.Operator):
    bl_idname = "unreal_bridge.export"
    bl_label = "Export to Unreal"
    bl_description = ("Export meshes, textures and material descriptions for the Unreal BlenderBridge plugin. "
                      "Esc cancels")
    bl_options = {'REGISTER'}

    _timer = None
    _steps = None
    _result = None

    @classmethod
    def poll(cls, context):
        return not STATUS["running"]

    def _finish(self, context, report):
        settings = context.scene.unreal_bridge
        settings.last_manifest = self._result["manifest"]
        settings.last_report = "\n".join([report.summary] + report.warnings)
        for w in report.warnings:
            print("[Unreal Bridge] WARNING:", w)
        level = 'WARNING' if report.warnings else 'INFO'
        extra = f" ({len(report.warnings)} warnings, see panel)" if report.warnings else ""
        self.report({level}, f"Exported {report.summary}{extra}")

    # Blocking version: scripts, background mode.
    def execute(self, context):
        self._result = {}
        try:
            for _ in exporter.export_steps(context, context.scene.unreal_bridge, self._result):
                pass
        except exporter.ExportError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        self._finish(context, self._result["report"])
        return {'FINISHED'}

    # Interactive version: small steps on a timer, progress bar in the panel, Esc cancels.
    def invoke(self, context, event):
        if bpy.app.background:
            return self.execute(context)
        self._result = {}
        self._steps = exporter.export_steps(bpy.context, context.scene.unreal_bridge, self._result)
        STATUS.update(running=True, fraction=0.0, text="Starting")
        wm = context.window_manager
        self._timer = wm.event_timer_add(0.01, window=context.window)
        wm.modal_handler_add(self)
        wm.progress_begin(0, 1000)
        _redraw(context)
        return {'RUNNING_MODAL'}

    def _stop(self, context):
        wm = context.window_manager
        if self._timer is not None:
            wm.event_timer_remove(self._timer)
            self._timer = None
        wm.progress_end()
        STATUS.update(running=False, fraction=0.0, text="")
        _redraw(context)

    def modal(self, context, event):
        if event.type == 'ESC' and event.value == 'PRESS':
            self._steps.close()          # runs the exporter's clean-up
            self._stop(context)
            self.report({'WARNING'}, "Export cancelled")
            return {'CANCELLED'}
        if event.type != 'TIMER':
            return {'PASS_THROUGH'} if event.type in _PASS_THROUGH else {'RUNNING_MODAL'}
        start = time.perf_counter()
        try:
            while time.perf_counter() - start < 0.2:   # work for a moment, then let the UI redraw
                fraction, text = next(self._steps)
                STATUS.update(fraction=fraction, text=text)
        except StopIteration:
            self._stop(context)
            self._finish(context, self._result["report"])
            return {'FINISHED'}
        except exporter.ExportError as exc:
            self._stop(context)
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        except Exception as exc:
            self._stop(context)
            traceback.print_exc()
            self.report({'ERROR'}, f"Export failed: {exc} (details in the system console)")
            return {'CANCELLED'}
        context.window_manager.progress_update(int(STATUS["fraction"] * 1000))
        _redraw(context)
        return {'RUNNING_MODAL'}


class UNREALBRIDGE_PT_panel(bpy.types.Panel):
    bl_idname = "UNREALBRIDGE_PT_panel"
    bl_label = "Unreal Bridge"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Unreal"

    def draw(self, context):
        layout = self.layout
        s = context.scene.unreal_bridge
        layout.prop(s, "mode", expand=True)
        col = layout.column()
        if s.mode == 'SCENE':
            col.prop(s, "selected_only")
            col.prop(s, "hierarchy")
            if s.hierarchy != 'AS_IS':
                col.prop(s, "group_by")
            if s.hierarchy == 'BLUEPRINT':
                col.prop(s, "instance_repeats")
        col.prop(s, "export_dir")
        col.prop(s, "unreal_root")
        col.label(text="Imports into: " + exporter.content_path(s.unreal_root), icon='FILE_FOLDER')
        col.prop(s, "export_name")
        col.prop(s, "custom_normals")
        col.prop(s, "bake")
        sub = col.row()
        sub.enabled = s.bake
        sub.prop(s, "bake_resolution")
        if STATUS["running"]:
            box = layout.box()
            box.progress(factor=STATUS["fraction"], type='BAR',
                         text=f"{STATUS['fraction'] * 100:.0f}%  {STATUS['text']}"[:90])
            box.label(text="Esc to cancel", icon='CANCEL')
        else:
            row = layout.row()
            row.scale_y = 1.6
            row.operator(UNREALBRIDGE_OT_export.bl_idname, icon='EXPORT')
        if s.last_report:
            box = layout.box()
            lines = s.last_report.split("\n")
            box.label(text=lines[0], icon='CHECKMARK')
            for line in lines[1:13]:
                box.label(text=line[:110], icon='ERROR')
            if len(lines) > 13:
                box.label(text=f"... {len(lines) - 13} more in the manifest")


classes = (UnrealBridgeSettings, UNREALBRIDGE_OT_export, UNREALBRIDGE_PT_panel)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.unreal_bridge = PointerProperty(type=UnrealBridgeSettings)


def unregister():
    del bpy.types.Scene.unreal_bridge
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
