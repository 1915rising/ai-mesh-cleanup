#!/usr/bin/env python3
"""
clean_ai_mesh.py -- turn raw AI-generated triangle soup into one clean,
watertight mesh at a target triangle count.

AI image-to-3D / text-to-3D generators typically export a single GLB that
looks fine in a viewer but is unusable downstream: ~1.5 million triangles
split across thousands of disconnected shells, with hundreds of thousands
of boundary edges (the surface is not closed anywhere). You cannot rig it,
3D-print it, bake to it, or decimate it cleanly in that state.

Pipeline (all inside Blender, headless):
  1. Import the .glb / .gltf file.
  2. Audit the raw mesh: shells, boundary edges, non-manifold edges,
     zero-area triangles, watertight or not.
  3. Voxel remesh -- rebuilds the volume so every shell becomes a
     watertight solid and all touching geometry fuses together.
  4. Decimate (collapse) down to the target triangle count.
  5. Audit again, print a before/after report, export the cleaned GLB.
     Materials/textures are stripped on export: the remesh invalidates
     the original UVs, so the exported textures could never map onto the
     cleaned mesh -- the output is geometry only (rebake from the raw
     file if you need the textured look back).

Usage:
  blender --background --python clean_ai_mesh.py -- \\
      --input model.glb --output cleaned.glb \\
      --target-tris 25000 --voxel-size 0.004

Arguments (all after the "--" separator):
  --input PATH        Raw generator export (.glb or .gltf). Required.
  --output PATH       Where to write the cleaned GLB. Required.
  --target-tris N     Triangle budget for the decimate stage (default 25000).
  --voxel-size F      Voxel remesh size, in the model's own units
                      (default 0.004). Rule of thumb from testing: about
                      0.3-0.4% of the model's height.
  --report PATH       Optional: also write the audit report as JSON here.

Copyright (c) 2026 1915rising -- MIT License (see LICENSE).
"""

import argparse
import json
import sys
import time

import bpy
import numpy as np


# --------------------------------------------------------------------------
# Argument parsing (Blender passes script args after "--")
# --------------------------------------------------------------------------

def parse_args():
    argv = sys.argv
    if "--" in argv:
        argv = argv[argv.index("--") + 1:]
    else:
        argv = []
    p = argparse.ArgumentParser(prog="clean_ai_mesh.py",
                                description="Clean raw AI-generated GLB soup "
                                            "into one watertight mesh.")
    p.add_argument("--input", required=True, help="input .glb/.gltf file")
    p.add_argument("--output", required=True, help="output .glb file")
    p.add_argument("--target-tris", type=int, default=25000,
                   help="triangle budget after decimation (default: 25000)")
    p.add_argument("--voxel-size", type=float, default=0.004,
                   help="voxel remesh size in model units (default: 0.004)")
    p.add_argument("--report", default=None,
                   help="optional path to write the JSON audit report")
    return p.parse_args(argv)


# --------------------------------------------------------------------------
# Mesh audit -- the same numpy edge analysis used to verify the manual runs:
# an edge used by exactly 1 triangle is a boundary edge; used by >2 it is
# non-manifold; a mesh with neither is watertight. Shells are counted with a
# union-find over the unique edges.
# --------------------------------------------------------------------------

def _count_shells(ukeys, tris, nv):
    parent = np.arange(nv, dtype=np.int64)

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    eu = np.stack([ukeys // nv, ukeys % nv], axis=1)
    for i in range(len(eu)):
        ra, rb = find(int(eu[i, 0])), find(int(eu[i, 1]))
        if ra != rb:
            parent[ra] = rb
    used = np.unique(tris)
    roots = np.array([find(int(v)) for v in used])
    return int(len(np.unique(roots)))


def audit_mesh(me):
    """Return a dict of health metrics for a Blender mesh datablock."""
    me.calc_loop_triangles()
    nv = len(me.vertices)
    ntris = len(me.loop_triangles)
    info = {"verts": nv, "tris": ntris}
    if nv == 0 or ntris == 0:
        info.update({"unique_edges": 0, "boundary_edges": 0,
                     "nonmanifold_edges": 0, "zero_area_tris": 0,
                     "shells": 0, "watertight": False})
        return info

    tris = np.empty(ntris * 3, dtype=np.int64)
    me.loop_triangles.foreach_get("vertices", tris)
    tris = tris.reshape(-1, 3)

    e = np.concatenate([tris[:, [0, 1]], tris[:, [1, 2]], tris[:, [2, 0]]])
    e.sort(axis=1)
    keys = e[:, 0] * nv + e[:, 1]
    ukeys, counts = np.unique(keys, return_counts=True)
    boundary = int((counts == 1).sum())
    nonmanifold = int((counts > 2).sum())

    verts = np.empty(nv * 3, dtype=np.float64)
    me.vertices.foreach_get("co", verts)
    verts = verts.reshape(-1, 3)
    a = verts[tris[:, 1]] - verts[tris[:, 0]]
    b = verts[tris[:, 2]] - verts[tris[:, 0]]
    cr = np.cross(a, b)
    area2 = np.sqrt((cr ** 2).sum(axis=1))
    zero_area = int((area2 < 1e-12).sum())

    info.update({
        "unique_edges": int(len(ukeys)),
        "boundary_edges": boundary,
        "nonmanifold_edges": nonmanifold,
        "zero_area_tris": zero_area,
        "shells": _count_shells(ukeys, tris, nv),
        "watertight": bool(boundary == 0 and nonmanifold == 0),
    })
    return info


def fmt(info):
    return ("{verts:,} verts | {tris:,} tris | {shells:,} shells | "
            "{boundary_edges:,} boundary edges | {nonmanifold_edges:,} "
            "non-manifold edges | {zero_area_tris:,} zero-area tris | "
            "watertight: {wt}").format(wt="YES" if info["watertight"] else "NO",
                                       **info)


# --------------------------------------------------------------------------
# Pipeline
# --------------------------------------------------------------------------

def main():
    args = parse_args()
    report = {"input": args.input, "output": args.output,
              "target_tris": args.target_tris, "voxel_size": args.voxel_size}
    t0 = time.time()

    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.gltf(filepath=args.input)

    meshes = [o for o in bpy.context.scene.objects if o.type == 'MESH']
    if not meshes:
        print("ERROR: no mesh objects found in input file", flush=True)
        sys.exit(1)
    report["input_objects"] = len(meshes)

    # Fuse multi-object exports into a single object so the voxel stage
    # sees (and fuses) the whole model at once.
    if len(meshes) > 1:
        bpy.ops.object.select_all(action='DESELECT')
        for o in meshes:
            o.select_set(True)
        bpy.context.view_layer.objects.active = meshes[0]
        bpy.ops.object.join()
    ob = [o for o in bpy.context.scene.objects if o.type == 'MESH'][0]
    bpy.context.view_layer.objects.active = ob
    ob.select_set(True)
    # Bake any object transform into the mesh so voxel size means the same
    # thing regardless of how the exporter set up the scene.
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)

    me = ob.data
    report["before"] = audit_mesh(me)
    print("BEFORE:  " + fmt(report["before"]), flush=True)

    # Stage 1: voxel remesh -> one fused manifold solid.
    t = time.time()
    me.remesh_voxel_size = args.voxel_size
    me.remesh_voxel_adaptivity = 0.0
    me.use_remesh_preserve_volume = True
    bpy.ops.object.voxel_remesh()
    report["voxel_secs"] = round(time.time() - t, 1)
    me = ob.data
    report["voxel"] = audit_mesh(me)
    print("VOXEL:   " + fmt(report["voxel"]) +
          f"  ({report['voxel_secs']}s)", flush=True)

    # Stage 2: decimate down to the target triangle count.
    # (QuadriFlow remesh is not used: it refuses to run headless on
    # non-manifold input and is unreliable in background mode. Decimate
    # collapse is the reducer that works in this pipeline.)
    if report["voxel"]["tris"] > args.target_tris:
        t = time.time()
        ratio = args.target_tris / report["voxel"]["tris"]
        mod = ob.modifiers.new("dec", 'DECIMATE')
        mod.ratio = max(0.005, min(0.99, ratio))
        bpy.ops.object.modifier_apply(modifier=mod.name)
        report["decimate_secs"] = round(time.time() - t, 1)
        print(f"decimated with ratio {mod.ratio:.4f} "
              f"({report['decimate_secs']}s)", flush=True)
    else:
        report["decimate_secs"] = 0.0
        print("voxel output already at/under target; decimate skipped",
              flush=True)

    me = ob.data
    report["after"] = audit_mesh(me)
    print("AFTER:   " + fmt(report["after"]), flush=True)

    report["method"] = "voxel+decimate"
    report["total_secs"] = round(time.time() - t0, 1)

    # Strip materials before export: voxel remeshing invalidates the
    # original UV layout, so the source textures cannot map onto the
    # cleaned mesh. Exporting them anyway would bloat the file by tens of
    # MB of dead texture data (and, in testing, made the exporter run
    # long / fail on a memory-constrained machine). Geometry only.
    me.materials.clear()
    bpy.ops.export_scene.gltf(filepath=args.output, export_format='GLB',
                              export_materials='NONE')

    if args.report:
        with open(args.report, "w") as f:
            json.dump(report, f, indent=1)

    print(json.dumps(report, indent=1))
    print(f"DONE in {report['total_secs']}s -> {args.output}", flush=True)


if __name__ == "__main__":
    main()
