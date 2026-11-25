#!/usr/bin/env bash
# Installs the BlenderBridge plugin into an Unreal project and enables it.
# Usage: bash install_unreal_plugin.sh "/path/to/UnrealProject"      (close the editor first)
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT="${1:-}"
if [[ -z "$PROJECT" || ! -d "$PROJECT" ]]; then
    echo "Usage: bash install_unreal_plugin.sh \"/path/to/UnrealProject\""; exit 1
fi
UPROJECT="$(find "$PROJECT" -maxdepth 1 -name '*.uproject' | head -n 1)"
if [[ -z "$UPROJECT" ]]; then
    echo "ERROR: no .uproject file in: $PROJECT"; exit 1
fi
if [[ ! -f "$HERE/BlenderBridge/BlenderBridge.uplugin" ]]; then
    echo "ERROR: BlenderBridge folder not found next to this script"; exit 1
fi
mkdir -p "$PROJECT/Plugins"
rm -rf "$PROJECT/Plugins/BlenderBridge"
cp -r "$HERE/BlenderBridge" "$PROJECT/Plugins/BlenderBridge"
[[ -f "$UPROJECT.bak" ]] || cp "$UPROJECT" "$UPROJECT.bak"
python3 - "$UPROJECT" <<'PY'
import json, sys
path = sys.argv[1]
with open(path, encoding="utf-8-sig") as f:
    data = json.load(f)
plugins = data.setdefault("Plugins", [])
for name in ("PythonScriptPlugin", "BlenderBridge"):
    for p in plugins:
        if p.get("Name") == name:
            p["Enabled"] = True
            break
    else:
        plugins.append({"Name": name, "Enabled": True})
with open(path, "w", encoding="utf-8") as f:
    json.dump(data, f, indent="\t")
    f.write("\n")
PY
echo "Installed: $PROJECT/Plugins/BlenderBridge"
echo "Enabled in: $UPROJECT   (backup: $(basename "$UPROJECT").bak)"
echo "Now start the editor. Menu bar: Blender Bridge > Import Latest Blender Export"
