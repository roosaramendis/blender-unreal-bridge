import unreal

PREFIX = "[BlenderBridge] "


class Log:
    """Collects warnings so they can be shown in one dialog at the end."""

    def __init__(self):
        self.warnings = []
        self.notes = []           # short informational lines for the summary dialog

    def info(self, msg):
        unreal.log(PREFIX + msg)

    def warn(self, msg):
        if msg not in self.warnings:
            self.warnings.append(msg)
        unreal.log_warning(PREFIX + msg)


def set_prop(obj, name, value, log=None):
    """set_editor_property that never raises (engine property names move between versions)."""
    try:
        obj.set_editor_property(name, value)
        return True
    except Exception as exc:
        if log:
            log.warn(f"could not set {type(obj).__name__}.{name}: {exc}")
        return False


def get_prop(obj, name):
    try:
        return obj.get_editor_property(name)
    except Exception:
        return None


def asset_tools():
    return unreal.AssetToolsHelpers.get_asset_tools()


def exists(path):
    return unreal.EditorAssetLibrary.does_asset_exist(path)


def load(path):
    return unreal.EditorAssetLibrary.load_asset(path) if exists(path) else None
