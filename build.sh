#!/usr/bin/env bash
# Builds the release zips into out/.
#
#   ./build.sh            build with the version already in the files
#   ./build.sh 0.16.0     write 0.16.0 into every version field first, then build
#
# Output:
#   out/unreal_bridge.zip                   Blender addon
#   out/BlenderBridge_UnrealPlugin.zip      Unreal plugin + install script
#   out/BlenderUnrealBridge-v<version>.zip  both of the above + README + LICENSE
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

ADDON="blender/unreal_bridge"
PLUGIN="unreal/BlenderBridge"
MANIFEST="$ADDON/blender_manifest.toml"
EXPORTER="$ADDON/exporter.py"
UE_INIT="$PLUGIN/Content/Python/blender_bridge/__init__.py"
UPLUGIN="$PLUGIN/BlenderBridge.uplugin"

if [[ $# -ge 1 ]]; then
    VERSION="${1#v}"
    if [[ ! "$VERSION" =~ ^([0-9]+)\.([0-9]+)\.([0-9]+)$ ]]; then
        echo "error: version must look like 1.2.3 (got '$1')" >&2
        exit 1
    fi
    # Unreal also wants a whole number that goes up with every release.
    NUMBER=$(( BASH_REMATCH[1] * 10000 + BASH_REMATCH[2] * 100 + BASH_REMATCH[3] ))
    sed -i -E "s/^version = \"[^\"]*\"/version = \"$VERSION\"/" "$MANIFEST"
    sed -i -E "s/^ADDON_VERSION = \"[^\"]*\"/ADDON_VERSION = \"$VERSION\"/" "$EXPORTER"
    sed -i -E "s/^__version__ = \"[^\"]*\"/__version__ = \"$VERSION\"/" "$UE_INIT"
    sed -i -E "s/\"VersionName\": \"[^\"]*\"/\"VersionName\": \"$VERSION\"/" "$UPLUGIN"
    sed -i -E "s/\"Version\": [0-9]+/\"Version\": $NUMBER/" "$UPLUGIN"
else
    VERSION=$(sed -n -E 's/^version = "([^"]*)"/\1/p' "$MANIFEST")
fi

# Every version field must agree, or the build stops.
check() {
    if ! grep -q "$2" "$1"; then
        echo "error: $1 does not say version $VERSION" >&2
        exit 1
    fi
}
check "$MANIFEST" "^version = \"$VERSION\""
check "$EXPORTER" "^ADDON_VERSION = \"$VERSION\""
check "$UE_INIT" "^__version__ = \"$VERSION\""
check "$UPLUGIN" "\"VersionName\": \"$VERSION\""

OUT="$ROOT/out"
rm -rf "$OUT"
mkdir -p "$OUT"
EXCLUDE=(-x '*__pycache__*' -x '*.pyc' -x '*/.DS_Store')

# Blender addon: files at the top of the zip, which is what Blender expects.
(cd "$ADDON" && zip -qr "$OUT/unreal_bridge.zip" . "${EXCLUDE[@]}")

# Unreal plugin: the BlenderBridge folder plus the install script.
(cd unreal && zip -qr "$OUT/BlenderBridge_UnrealPlugin.zip" BlenderBridge install_unreal_plugin.sh "${EXCLUDE[@]}")

# One download with everything.
BUNDLE="BlenderUnrealBridge-v$VERSION"
STAGE="$(mktemp -d)"
mkdir "$STAGE/$BUNDLE"
cp "$OUT/unreal_bridge.zip" "$OUT/BlenderBridge_UnrealPlugin.zip" README.md LICENSE "$STAGE/$BUNDLE/"
(cd "$STAGE" && zip -qr "$OUT/$BUNDLE.zip" "$BUNDLE")
rm -rf "$STAGE"

echo "Built version $VERSION:"
ls -1 "$OUT"
