"""Blender Bridge - Unreal side. Reads manifest.json written by the Blender addon."""
import unreal

from .importer import import_latest, import_manifest  # noqa: F401
from .masters import rebuild_masters  # noqa: F401

__version__ = "0.15.0"


def register_menu():
    menus = unreal.ToolMenus.get()
    main = menus.extend_menu("LevelEditor.MainMenu")
    sub = main.add_sub_menu(main.get_name(), "BlenderBridge", "BlenderBridge", "Blender Bridge")

    def entry(name, label, tip, command):
        e = unreal.ToolMenuEntry(name=name, type=unreal.MultiBlockType.MENU_ENTRY)
        e.set_label(label)
        e.set_tool_tip(tip)
        e.set_string_command(unreal.ToolMenuStringCommandType.PYTHON, "", command)
        sub.add_menu_entry("BlenderBridge", e)

    entry("ImportLatest", "Import Latest Blender Export",
          "Import whatever the Blender addon exported last",
          "import blender_bridge; blender_bridge.import_latest()")
    entry("RebuildMasters", "Rebuild Master Materials",
          "Delete and re-create the BlenderBridge master materials",
          "import blender_bridge; blender_bridge.rebuild_masters()")
    menus.refresh_all_widgets()
    unreal.log("[BlenderBridge] ready - menu: Blender Bridge > Import Latest Blender Export")
