# coding=utf-8
"""Generate the viewer's rigid quad, white texel and hand-drawn 16px tool icon.

    python -B scripts/gen_network_visualizer.py [--check]
"""
import argparse
import json
from pathlib import Path

from _png import png_bytes

ROOT = Path(__file__).resolve().parent.parent


def assets():
    identity = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0]]
    bounds = [[-0.5, -0.5, -0.01], [0.5, 0.5, 0.01]]
    mesh = {"version": "0.1.0", "mesh": [{
        "material": "textures/endernet/visualizer/white",
        "fields": ["pos", "normal", "uvcoord", "weight"],
        "indices": [0, 1, 2, 0, 2, 3],
        "vertices": [0, 0, 0, 0, 1, 0, 1, 0, 2, 0, 2, 0, 3, 0, 3, 0],
        "poses": [-0.5, -0.5, 0, 0.5, -0.5, 0, 0.5, 0.5, 0, -0.5, 0.5, 0],
        "normals": [0, 0, 1], "uvcoords": [0, 0, 1, 0, 1, 1, 0, 1],
        "weights": ["root", 1.0],
    }]}
    skeleton = {"skeleton": [{
        "name": "root", "parent": "root", "initpos": [0, 0, 0],
        "initquaternion": [0, 0, 0, 1], "initscale": [1, 1, 1],
        "offmtx": identity, "boundingbox": bounds,
    }], "boundingbox": bounds}
    models = {"endernet_network_overlay": {
        "mesh": "mesh/endernet_network_overlay.json",
        "skeleton": "skeleton/endernet_network_overlay.json",
        "material": "endernet_network_overlay", "dy_load": True,
    }}
    out = {}
    for path, data in (("models/mesh/endernet_network_overlay.json", mesh),
                       ("models/skeleton/endernet_network_overlay.json", skeleton),
                       ("models/netease_models.json", models)):
        out[path] = (json.dumps(data, indent=2) + "\n").encode("utf-8")
    palette = {".": (0, 0, 0, 0), "k": (36, 48, 63, 255),
               "g": (77, 96, 112, 255), "s": (173, 194, 205, 255),
               "w": (234, 244, 246, 255), "b": (22, 61, 85, 255),
               "c": (87, 198, 255, 255), "n": (83, 232, 135, 255)}
    rows = ["................", "....kkkkkkkk....", "...kwwwwwwwsk...",
            "...kwbbbbbbsk...", "...kwbnccnbsk...", "...kwbbbcbbsk...",
            "...kwbnccnbsk...", "...kwbbbcbbsk...", "...kwbbbnbbsk...",
            "...kwwssssssk...", "....kgcggngk....", ".....ksssgk.....",
            "......kggk......", "......ksgk......", "......kkkk......",
            "................"]
    out["textures/endernet/items/network_visualizer.png"] = png_bytes(
        [[palette[p] for p in row] for row in rows])
    out["textures/endernet/visualizer/white.png"] = png_bytes([[(255, 255, 255, 255)]])
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    changed = []
    for relative, content in assets().items():
        path = ROOT / "endernet_rp" / relative
        if path.is_file() and path.read_bytes() == content:
            continue
        changed.append(relative)
        if not args.check:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
    print("Viewer assets: %d %s" % (len(changed), "stale" if args.check else "updated"))
    return 1 if args.check and changed else 0


if __name__ == "__main__":
    raise SystemExit(main())
