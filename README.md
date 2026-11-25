# Blender → Unreal Bridge

Move your Blender scene or models into Unreal Engine in two clicks: one in Blender to export, one in Unreal to import.

Free and open source (GPL-3.0).

## What you need

- Blender 5.x (tested on 5.3)
- Unreal Engine 5.8 or newer

## Install

**Blender:** install `unreal_bridge.zip` like any other extension (Edit > Preferences > Get Extensions > Install from Disk), or run:
```bash
blender --command extension install-file -r user_default -e unreal_bridge.zip
```

**Unreal:** close Unreal, unzip `BlenderBridge_UnrealPlugin.zip`, then run:
```bash
bash install_unreal_plugin.sh "/path/to/YourUnrealProject"
```
This copies the plugin into your project and switches it on.

## How to use it

1. In Blender, press **N** to open the side panel and go to the **Unreal** tab.
2. Pick your settings and press **Export to Unreal**. A progress bar shows how far along it is. Press **Esc** to cancel.
3. In Unreal, open the **Blender Bridge** menu at the top and click **Import Latest Blender Export**.

Unreal always imports the most recent export.

## Settings

### Mode

- **Assets**: exports only the objects you selected and lays them out in a row in Unreal so you can look at them. Use this when you just want models in your project.
- **Scene**: exports the whole scene and builds a new level in Unreal with the same objects, lights, cameras and sky.

### Hierarchy (Scene mode only)

This decides how your objects arrive in Unreal.

- **As Is**: every object comes in separately. Simple, but a big scene means a long list of loose objects.
- **Grouped**: objects that belong together stay together. Move the group and everything in it moves too.
- **Blueprint**: each group is saved as a reusable Blueprint. If the same group appears many times, it's stored once and reused, which keeps big scenes light.

### Group By

This decides what counts as a group:

- **Parent**: an object and everything parented to it.
- **Collection**: everything inside the same collection.

Objects sitting directly in the Scene Collection aren't grouped; they get their own folder.

### Other options

- **Export Folder**: where the exported files are saved on disk.
- **Content Path**: where the files go inside your Unreal project.
- **Name**: the name of this export (and of the level, in Scene mode).
- **Selected Only**: export only what you have selected.
- **Instance Repeated Pieces**: if the same object appears many times inside a group, Unreal draws it as one instanced object. This is faster and uses less memory
- **Export Custom Normals**: keeps your shading exactly as it looks in Blender. Leave it on unless you want Unreal to calculate the normals itself.
- **Bake Procedural Materials**: turns materials built from nodes (noise, color ramps and so on) into textures. Turn it off if you'd rather rebuild those materials in Unreal; you'll get plain grey materials with the right names instead.

## What gets carried over

- Meshes, with their position, rotation and scale
- Materials and textures (color, roughness, metal, normal map, emission, transparency, glass)
- Material slots, in the same order as in Blender
- Lights, cameras and the world/HDRI (Scene mode)
- Mirrored objects (negative scale) display correctly

When you export again, Unreal skips anything that hasn't changed, so re-imports are quick.

## Not supported yet

- Characters with bones (rigs and animation)
- Converting node materials into Unreal node materials
- UDIM textures
- Blender's Sky Texture
- Geometry Nodes that create new geometry

## Linux + KDE tip

If your desktop freezes while Unreal is open, start Unreal like this:
```bash
SDL_WINDOW_ACTIVATE_WHEN_RAISED=0 UnrealEditor
```

## License

GPL-3.0-or-later