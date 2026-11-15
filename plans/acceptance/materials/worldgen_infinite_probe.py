# coding=utf-8
"""Run through MCDK execute_code on the server of a fresh infinite world.

This loads End chunks without moving any player or manually placing features.
The immediate result is pending; query __builtin__._endernet_worldgen_candidates
after every group's phase becomes complete. Preserve before_generated=False as
proof of natural generation in new chunks. Never reuse a generated test region
as evidence for a changed generation rule.
"""
import mod.server.extraServerApi as worldgen_api
import __builtin__

worldgen_chunks = worldgen_api.GetEngineCompFactory().CreateChunkSource(worldgen_api.GetLevelId())
worldgen_origins = [(0, 0), (12000, 0), (0, 12000), (8000, 8000)]
worldgen_probe = {"dimension": 2, "groups": []}
__builtin__._endernet_worldgen_candidates = worldgen_probe

def worldgen_scan_callback(group):
    def scan(data):
        import mod.server.extraServerApi as local_api
        group["callback_code"] = data.get("code")
        if data.get("code") != 1:
            group["phase"] = "load_failed"
            return
        block = local_api.GetEngineCompFactory().CreateBlock(local_api.GetLevelId())
        for entry in group["chunks"]:
            cx, cz = entry["chunk"]
            palette = block.GetBlockPaletteBetweenPos(2, (cx * 16, 0, cz * 16), (cx * 16 + 15, 127, cz * 16 + 15), False)
            entry["palette_ok"] = palette is not None
            if palette is not None:
                entry["end_stone"] = palette.GetBlockCountInBlockPalette("minecraft:end_stone", -1)
                entry["ore"] = palette.GetBlockCountInBlockPalette("endernet:end_resonance_ore", -1)
                entry["air"] = palette.GetBlockCountInBlockPalette("minecraft:air", -1)
        group["phase"] = "complete"
    return scan

for wx, wz in worldgen_origins:
    group = {"origin": [wx, wz], "phase": "loading", "chunks": []}
    for dx in range(4):
        for dz in range(4):
            cx, cz = wx // 16 + dx, wz // 16 + dz
            group["chunks"].append({"chunk": [cx, cz], "before_generated": worldgen_chunks.IsChunkGenerated(2, (cx, cz))})
    worldgen_probe["groups"].append(group)
    group["accepted"] = worldgen_chunks.DoTaskOnChunkAsync(2, (wx, 0, wz), (wx + 63, 128, wz + 63), worldgen_scan_callback(group))

_result = worldgen_probe
