# Runs automatically when the editor starts (Python plugin picks up Content/Python/init_unreal.py).
import unreal

try:
    import blender_bridge
    blender_bridge.register_menu()
except Exception as exc:  # never block editor start-up
    unreal.log_error(f"[BlenderBridge] failed to start: {exc}")
