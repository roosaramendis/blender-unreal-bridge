# SPDX-License-Identifier: GPL-3.0-or-later
"""Material export: full node-tree dump + a resolved 'simple PBR' view of it."""
import hashlib
import json
import os

import bpy

_BASE_NODE_PROPS = {p.identifier for p in bpy.types.Node.bl_rna.properties}
_SIMPLE = {'BOOLEAN', 'INT', 'FLOAT', 'STRING', 'ENUM'}


def _plain(v):
    """Convert a Blender value to something JSON can hold."""
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, (set, frozenset)):
        return sorted(v)
    if isinstance(v, bpy.types.ID):
        return {"id": v.name, "id_type": type(v).__name__}
    try:
        return [_plain(x) for x in v]
    except TypeError:
        return str(v)


def _dump_struct(obj, skip=(), depth=0):
    """Generic RNA dump (depth-limited) so ColorRamps, curves etc. survive."""
    out = {}
    for p in obj.bl_rna.properties:
        pid = p.identifier
        if pid in skip or pid in {'rna_type', 'id_data'}:
            continue
        try:
            val = getattr(obj, pid)
        except Exception:
            continue
        if p.type in _SIMPLE:
            out[pid] = _plain(val)
        elif p.type == 'POINTER':
            if val is None:
                out[pid] = None
            elif isinstance(val, bpy.types.ID):
                out[pid] = _plain(val)
            elif depth < 3:
                out[pid] = _dump_struct(val, depth=depth + 1)
        elif p.type == 'COLLECTION' and depth < 3:
            try:
                out[pid] = [_dump_struct(x, depth=depth + 1) for x in val]
            except Exception:
                pass
    return out


class MaterialExporter:
    def __init__(self, report):
        self.report = report
        self.images = {}        # image name -> {"image": Image, "usage": set()}
        self.node_groups = {}   # group name -> dumped tree

    # ------------------------------------------------------------------ dump
    def dump_tree(self, tree):
        nodes = []
        for n in tree.nodes:
            d = {
                "name": n.name, "type": n.bl_idname, "label": n.label,
                "location": [n.location.x, n.location.y], "mute": n.mute,
                "parent": n.parent.name if n.parent else None,
                "props": _dump_struct(n, skip=_BASE_NODE_PROPS),
                "inputs": [self._dump_socket(s) for s in n.inputs],
                "outputs": [self._dump_socket(s) for s in n.outputs],
            }
            img = getattr(n, "image", None)
            if isinstance(img, bpy.types.Image):
                self._use_image(img, None)
            grp = getattr(n, "node_tree", None)
            if isinstance(grp, bpy.types.NodeTree):
                d["group"] = grp.name
                if grp.name not in self.node_groups:
                    self.node_groups[grp.name] = None  # guard against recursion
                    self.node_groups[grp.name] = self.dump_tree(grp)
            nodes.append(d)
        links = [{
            "from_node": l.from_node.name, "from_socket": l.from_socket.identifier,
            "to_node": l.to_node.name, "to_socket": l.to_socket.identifier,
            "muted": l.is_muted, "valid": l.is_valid,
        } for l in tree.links]
        out = {"nodes": nodes, "links": links}
        iface = getattr(tree, "interface", None)
        if iface is not None:
            out["interface"] = [{
                "name": i.name, "in_out": getattr(i, "in_out", None),
                "socket_type": getattr(i, "socket_type", None),
                "identifier": getattr(i, "identifier", None),
            } for i in iface.items_tree if i.item_type == 'SOCKET']
        return out

    @staticmethod
    def _dump_socket(s):
        d = {"identifier": s.identifier, "name": s.name, "type": s.type,
             "idname": s.bl_idname, "enabled": s.enabled, "linked": s.is_linked}
        if hasattr(s, "default_value"):
            d["default"] = _plain(s.default_value)
        return d

    # ------------------------------------------------------------- signature
    def signature(self, mat, entry):
        """Fingerprint of everything that affects how a material looks. Two materials with the same
        fingerprint (e.g. Stone and Stone.001 left untouched) are true duplicates. Node positions in
        the editor are ignored; images are compared by file, node groups by their contents."""
        group_sigs = {}

        def norm(value):
            if isinstance(value, dict):
                if value.get("id_type") == "Image":
                    img = bpy.data.images.get(value.get("id"))
                    if img is not None and img.source == 'FILE' and not img.packed_file and img.filepath_raw:
                        path = os.path.normpath(bpy.path.abspath(img.filepath_raw, library=img.library))
                        return {"image_file": path, "colorspace": img.colorspace_settings.name}
                    return {"image": value.get("id")}
                if value.get("id_type") in ("ShaderNodeTree", "NodeTree"):
                    return {"group": group_sig(value.get("id"))}
                return {k: norm(v) for k, v in value.items() if k not in ("location", "location_absolute",
                                                                         "width", "height", "dimensions",
                                                                         "select", "show_options",
                                                                         "show_preview", "hide")}
            if isinstance(value, list):
                return [norm(v) for v in value]
            if isinstance(value, float):
                return round(value, 6)
            return value

        def group_sig(name):
            if name not in group_sigs:
                group_sigs[name] = "..."            # recursion guard
                tree = self.node_groups.get(name)
                group_sigs[name] = hashlib.sha1(
                    json.dumps(norm(tree), sort_keys=True, default=str).encode()).hexdigest()
            return group_sigs[name]

        tree = entry.get("node_tree")
        if tree is not None:
            tree = dict(tree, nodes=[dict(n, group=group_sig(n["group"])) if n.get("group") else n
                                     for n in tree["nodes"]])
        settings = {"backface": mat.use_backface_culling,
                    "render_method": getattr(mat, "surface_render_method", None),
                    "diffuse": list(mat.diffuse_color), "metallic": mat.metallic, "roughness": mat.roughness}
        return hashlib.sha1(json.dumps(norm({"tree": tree, "settings": settings}), sort_keys=True,
                                       default=str).encode()).hexdigest()

    # --------------------------------------------------------------- resolve
    def _use_image(self, img, usage):
        entry = self.images.setdefault(img.name, {"image": img, "usage": set(), "directx": False})
        if usage:
            entry["usage"].add(usage)
        return entry

    @staticmethod
    def _by_id(sockets, identifier):
        for s in sockets:
            if s.identifier == identifier:
                return s
        return None

    def _src(self, sock, ctx):
        """Follow a link backwards through reroutes and INTO / OUT OF node groups.
        ctx is the stack of group nodes we are inside.
        Returns (node, out_socket, ctx, terminal): node is None when the chain ends
        on an unlinked socket, which is then returned as `terminal`."""
        for _ in range(256):
            if sock is None:
                return None, None, ctx, None
            if not sock.is_linked:
                return None, None, ctx, sock
            link = sock.links[0]
            node, out = link.from_node, link.from_socket
            if node.type == 'REROUTE':
                sock = node.inputs[0]
            elif node.type == 'GROUP' and node.node_tree:
                outs = [n for n in node.node_tree.nodes if n.type == 'GROUP_OUTPUT']
                inner = next((n for n in outs if n.is_active_output), outs[0] if outs else None)
                target = self._by_id(inner.inputs, out.identifier) if inner else None
                if target is None:
                    return node, out, ctx, None
                sock, ctx = target, ctx + (node,)
            elif node.type == 'GROUP_INPUT' and ctx:
                sock, ctx = self._by_id(ctx[-1].inputs, out.identifier), ctx[:-1]
            else:
                return node, out, ctx, None
        return None, None, ctx, None

    def _const(self, sock, ctx):
        """Plain value of a socket if nothing but reroutes / group plumbing feeds it, else None."""
        node, _, _, term = self._src(sock, ctx)
        if node is None and term is not None and hasattr(term, "default_value"):
            return _plain(term.default_value)
        return None

    def _mapping(self, tex_node, ctx, warn):
        m = {"scale": [1.0, 1.0], "offset": [0.0, 0.0], "rotation": 0.0, "uv_map": None}
        node, _, c, _ = self._src(tex_node.inputs["Vector"], ctx)
        if node is None:
            return m
        if node.type == 'MAPPING':
            s, l, r = (self._const(node.inputs[k], c) for k in ("Scale", "Location", "Rotation"))
            if s is None or l is None or r is None:
                warn(f"Mapping node on '{tex_node.name}' is driven by other nodes; plain UVs used")
                return m
            m.update(scale=[s[0], s[1]], offset=[l[0], l[1]], rotation=r[2])
            if abs(r[2]) > 1e-5:
                warn(f"UV rotation on '{tex_node.name}' is not carried over yet")
            node, _, c, _ = self._src(node.inputs["Vector"], c)
        if node is None or node.type == 'TEX_COORD':
            return m
        if node.type == 'UVMAP':
            m["uv_map"] = node.uv_map or None
            return m
        warn(f"Texture coordinates of '{tex_node.name}' come from '{node.bl_idname}', plain UVs used")
        return m

    def _tex(self, node, channel, usage, ctx, warn):
        self._use_image(node.image, usage)
        return {"image": node.image.name, "channel": channel, "mapping": self._mapping(node, ctx, warn),
                "extension": node.extension, "interpolation": node.interpolation}

    # ---- scalar chains: texture channel, optionally inverted / scaled ----------
    def _scalar(self, sock, ctx, warn, usage='data'):
        res = {"value": None, "texture": None, "unsupported": None, "invert": False, "scale": 1.0}
        for _ in range(32):
            node, out, ctx, term = self._src(sock, ctx)
            if node is None:
                v = _plain(term.default_value) if term is not None and hasattr(term, "default_value") else None
                if isinstance(v, list):
                    v = sum(v[:3]) / 3.0 if len(v) >= 3 else (v[0] if v else None)
                if isinstance(v, (int, float)):
                    v = res["scale"] * ((1.0 - v) if res["invert"] else v)
                res.update(value=v, invert=False, scale=1.0)
                return res
            t = node.type
            if t == 'TEX_IMAGE' and node.image:
                if out.identifier == 'Alpha':
                    res["texture"] = self._tex(node, 'a', None, ctx, warn)
                else:
                    res["texture"] = self._tex(node, 'r', usage, ctx, warn)
                return res
            if t in {'SEPARATE_COLOR', 'SEPRGB', 'SEPXYZ'}:
                idx = list(node.outputs).index(out)
                n2, o2, c2, _ = self._src(node.inputs[0], ctx)
                if (n2 is not None and n2.type == 'TEX_IMAGE' and n2.image and idx < 3
                        and o2.identifier == 'Color' and getattr(node, "mode", 'RGB') == 'RGB'):
                    res["texture"] = self._tex(n2, 'rgb'[idx], usage, c2, warn)
                    return res
                res["unsupported"] = (n2 or node).bl_idname
                return res
            if t == 'INVERT' and self._const(node.inputs["Fac"], ctx) == 1.0:
                res["invert"] = not res["invert"]
                sock = node.inputs["Color"]
                continue
            if t == 'MATH':
                a, b = node.inputs[0], node.inputs[1]
                ca, cb = self._const(a, ctx), self._const(b, ctx)
                if node.operation == 'MULTIPLY' and not res["invert"] and (ca is None) != (cb is None):
                    res["scale"] *= cb if ca is None else ca
                    sock = a if ca is None else b
                    continue
                if node.operation == 'SUBTRACT' and ca is not None and abs(ca - 1.0) < 1e-6 and cb is None:
                    res["invert"] = not res["invert"]
                    sock = b
                    continue
            if t in {'RGBTOBW', 'CLAMP'}:
                sock = node.inputs[0]
                continue
            res["unsupported"] = node.bl_idname
            return res
        res["unsupported"] = "chain too long"
        return res

    # ---- colour chains: texture + simple adjustments ---------------------------
    def _peek_ao_like(self, sock, ctx):
        """Does this input look like an AO / mask map rather than a colour map?"""
        via_sep = False
        for _ in range(32):
            node, out, ctx, _ = self._src(sock, ctx)
            if node is None:
                return False
            if node.type == 'TEX_IMAGE' and node.image:
                img = node.image
                name = img.name.lower()
                return (via_sep or img.colorspace_settings.is_data
                        or any(k in name for k in ("_ao", "ao_", "occlusion", "ambient", "_arm", "_orm")))
            if node.type in {'SEPARATE_COLOR', 'SEPRGB', 'SEPXYZ'}:
                via_sep = True
            if node.type in {'SEPARATE_COLOR', 'SEPRGB', 'SEPXYZ', 'INVERT', 'RGBTOBW', 'CLAMP', 'MATH',
                             'HUE_SAT', 'BRIGHTCONTRAST'}:
                linked = [s for s in node.inputs if s.is_linked and s.name != 'Fac']
                if len(linked) != 1:
                    return False
                sock = linked[0]
                continue
            return False
        return False

    def _mix(self, node, ctx, res, warn):
        """Handle a Mix node on a colour chain. Returns the socket to continue with, or None."""
        if node.type == 'MIX':
            if node.data_type != 'RGBA':
                return None
            fac, a, b = (self._by_id(node.inputs, k) for k in ("Factor_Float", "A_Color", "B_Color"))
        else:
            fac, a, b = node.inputs[0], node.inputs[1], node.inputs[2]
        f = self._const(fac, ctx)
        if not isinstance(f, (int, float)):
            return None
        if getattr(node, "clamp_factor", True):
            f = min(max(f, 0.0), 1.0)
        if f <= 0.001:
            return a
        if node.blend_type == 'MIX':
            return b if f >= 0.999 else None
        if node.blend_type != 'MULTIPLY':
            return None
        ca, cb = self._const(a, ctx), self._const(b, ctx)
        if cb is not None or ca is not None:                 # texture * plain colour
            if ca is not None and cb is not None:
                return None
            if cb is None and f < 0.999:
                return None
            c, main = (cb, a) if cb is not None else (ca, b)
            tint = [1.0 + f * (c[i] - 1.0) for i in range(3)]
            res["tint"] = [res["tint"][i] * tint[i] for i in range(3)] if res.get("tint") else tint
            return main
        if self._peek_ao_like(b, ctx):                       # texture * AO map
            main, other = a, b
        elif self._peek_ao_like(a, ctx) and f >= 0.999:
            main, other = b, a
        else:
            return None
        ao = self._scalar(other, ctx, warn)
        if res.get("ao") or not ao["texture"] or ao["invert"] or ao["scale"] != 1.0:
            return None
        res["ao"] = {"texture": ao["texture"], "strength": f}
        return main

    def _color(self, sock, ctx, warn):
        res = {"value": None, "texture": None, "unsupported": None, "adjust": None, "tint": None, "ao": None}
        for _ in range(32):
            node, out, ctx, term = self._src(sock, ctx)
            if node is None:
                if term is not None and hasattr(term, "default_value"):
                    res["value"] = _plain(term.default_value)
                return res
            t = node.type
            nxt = None
            if t == 'TEX_IMAGE' and node.image and out.identifier == 'Color':
                res["texture"] = self._tex(node, 'rgb', 'color', ctx, warn)
                return res
            adj = res["adjust"] or {}
            if t == 'HUE_SAT' and "hue" not in adj:
                h, s, v, f = (self._const(node.inputs[k], ctx) for k in ("Hue", "Saturation", "Value", "Fac"))
                if None not in (h, s, v) and f is not None and abs(f - 1.0) < 1e-4:
                    adj.update(hue=h, saturation=s, value=v)
                    res["adjust"], nxt = adj, node.inputs["Color"]
            elif t == 'BRIGHTCONTRAST' and "bright" not in adj:
                br, co = self._const(node.inputs["Bright"], ctx), self._const(node.inputs["Contrast"], ctx)
                if br is not None and co is not None:
                    adj.update(bright=br, contrast=co)
                    res["adjust"], nxt = adj, node.inputs["Color"]
            elif t in {'MIX', 'MIX_RGB'}:
                nxt = self._mix(node, ctx, res, warn)
            if nxt is None:
                res["unsupported"] = node.bl_idname
                return res
            sock = nxt
        res["unsupported"] = "chain too long"
        return res

    # ---- normals ----------------------------------------------------------------
    def _normal(self, sock, ctx, warn):
        res = {"value": None, "texture": None, "unsupported": None, "strength": 1.0}
        node, out, ctx, _ = self._src(sock, ctx)
        if node is None:
            return res
        if node.type != 'NORMAL_MAP':
            res["unsupported"] = node.bl_idname
            return res
        s = self._const(node.inputs["Strength"], ctx)
        res["strength"] = s if isinstance(s, (int, float)) else 1.0
        if node.space != 'TANGENT':
            warn(f"Normal map space '{node.space}' treated as tangent space")
        n2, o2, c2, _ = self._src(node.inputs["Color"], ctx)
        if n2 is None:
            return res
        if n2.type == 'TEX_IMAGE' and n2.image and o2.identifier == 'Color':
            res["texture"] = self._tex(n2, 'rgb', 'normal', c2, warn)
            return res
        if n2.type in {'COMBINE_COLOR', 'COMBRGB', 'COMBXYZ'} and getattr(n2, "mode", 'RGB') == 'RGB':
            # Separate -> (invert green) -> Combine: a DirectX-style normal map being converted.
            tex, flip, ok = None, False, True
            for i in range(3):
                if i == 2 and self._const(n2.inputs[2], c2) is not None:
                    continue  # blue left as a constant
                p = self._scalar(n2.inputs[i], c2, warn, usage='normal')
                pt = p["texture"]
                if (not pt or pt["channel"] != 'rgb'[i] or p["scale"] != 1.0 or (p["invert"] and i != 1)
                        or (tex and pt["image"] != tex["image"])):
                    ok = False
                    break
                tex = tex or pt
                if i == 1:
                    flip = p["invert"]
            if ok and tex:
                res["texture"] = dict(tex, channel='rgb')
                if flip:
                    self.images[tex["image"]]["directx"] = True
                return res
        res["unsupported"] = n2.bl_idname
        return res

    # ---- material ---------------------------------------------------------------
    def export_material(self, mat):
        warnings = []
        warn = warnings.append
        entry = {"name": mat.name, "shader": "none", "blend_mode": "opaque",
                 "two_sided": not mat.use_backface_culling, "channels": {},
                 "extra": {}, "warnings": warnings, "bake_warnings": [], "node_tree": None}
        tree = mat.node_tree
        ch = entry["channels"]
        plain = lambda v: {"value": v, "texture": None, "unsupported": None}
        if tree is None:
            ch["base_color"] = plain(list(mat.diffuse_color))
            ch["metallic"] = plain(mat.metallic)
            ch["roughness"] = plain(mat.roughness)
            return entry
        entry["node_tree"] = self.dump_tree(tree)

        out = None
        for target in ('ALL', 'EEVEE', 'CYCLES'):
            out = tree.get_output_node(target)
            if out:
                break
        shader, _, ctx, _ = self._src(out.inputs["Surface"], ()) if out else (None, None, (), None)
        if shader is None:
            warn("No surface shader connected; exported as plain grey")
            return entry

        entry["_shader"], entry["_top"] = shader.name, ctx == ()
        I = shader.inputs
        if shader.type == 'BSDF_PRINCIPLED':
            entry["shader"] = "principled"
            ch["base_color"] = self._color(I["Base Color"], ctx, warn)
            ch["metallic"] = self._scalar(I["Metallic"], ctx, warn)
            ch["roughness"] = self._scalar(I["Roughness"], ctx, warn)
            ch["specular"] = self._scalar(I["Specular IOR Level"], ctx, warn)
            ch["alpha"] = self._scalar(I["Alpha"], ctx, warn)
            ch["normal"] = self._normal(I["Normal"], ctx, warn)
            ch["emission_color"] = self._color(I["Emission Color"], ctx, warn)
            ch["emission_strength"] = self._scalar(I["Emission Strength"], ctx, warn)
            for key in ("IOR", "Transmission Weight", "Coat Weight", "Coat Roughness", "Sheen Weight",
                        "Subsurface Weight", "Subsurface Radius", "Subsurface Scale", "Anisotropic"):
                if key in I:
                    v = self._const(I[key], ctx)
                    if v is None:
                        v = _plain(I[key].default_value)
                        warn(f"'{key}' is driven by nodes; only its plain value is exported")
                    entry["extra"][key] = v
        elif shader.type == 'BSDF_GLASS':
            entry["shader"] = "glass"
            ch["base_color"] = self._color(I["Color"], ctx, warn)
            ch["roughness"] = self._scalar(I["Roughness"], ctx, warn)
            ch["normal"] = self._normal(I["Normal"], ctx, warn)
            entry["extra"]["IOR"] = self._const(I["IOR"], ctx)
        elif shader.type == 'EMISSION':
            entry["shader"] = "emission"
            ch["base_color"] = plain([0, 0, 0, 1])
            ch["emission_color"] = self._color(I["Color"], ctx, warn)
            ch["emission_strength"] = self._scalar(I["Strength"], ctx, warn)
        elif shader.type == 'BSDF_DIFFUSE':
            entry["shader"] = "diffuse"
            ch["base_color"] = self._color(I["Color"], ctx, warn)
            ch["roughness"] = plain(1.0)
            ch["normal"] = self._normal(I["Normal"], ctx, warn)
        else:
            entry["shader"] = "unsupported"
            entry["bake_warnings"].append(
                f"Surface shader '{shader.bl_idname}' can't be converted directly; plain grey without baking")
            return entry

        for name, c in ch.items():
            if c.get("unsupported"):
                # fall back to the socket's own value on the shader
                entry["bake_warnings"].append(
                    f"'{name}' uses '{c['unsupported']}', which can't be converted directly; "
                    f"plain value used without baking")
        if ch.get("emission_strength", {}).get("texture"):
            warn("Textured emission strength is not supported; plain value used")

        alpha = ch.get("alpha")
        transmission = entry["extra"].get("Transmission Weight") or 0.0
        alpha_used = bool(alpha) and (alpha["texture"] is not None
                                      or (isinstance(alpha["value"], (int, float)) and alpha["value"] < 0.999))
        if entry["shader"] == "glass" or transmission > 0.5:
            entry["blend_mode"] = "translucent"
        elif alpha_used:
            blended = getattr(mat, "surface_render_method", 'DITHERED') == 'BLENDED'
            entry["blend_mode"] = "translucent" if blended else "masked"
        return entry
