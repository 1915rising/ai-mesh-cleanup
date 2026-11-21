# ai-mesh-cleanup

Turn raw AI-generated 3D soup into a clean, watertight mesh at a triangle
count you choose — headless, in Blender, in about a minute.

## The problem

AI image-to-3D / text-to-3D generators export GLBs that *look* fine in a
viewer but are unusable for real work. Measured on actual generator output:

| Raw export | Verts | Tris | Shells | Boundary edges | Watertight |
|---|---|---|---|---|---|
| Generated head model | 929,059 | 1,499,426 | 8,643 | 345,056 | No |
| Generated full-body model | 861,433 | 1,499,968 | 7,680 | 218,610 | No |

Thousands of disconnected shells ("shells" = separate floating pieces),
hundreds of thousands of boundary edges (the surface is open everywhere),
and ~1.5 million triangles. You can't rig that, 3D-print it, retopo it, or
decimate it cleanly — decimation alone just gives you a smaller pile of
soup, because nothing is connected to anything else.

## What it does

One script, three stages:

1. **Audit** — counts shells, boundary edges, non-manifold edges and
   zero-area triangles, and reports whether the mesh is watertight.
2. **Voxel remesh** — rebuilds the volume so every shell becomes a
   watertight solid and everything that touches fuses together. This is
   the step that fixes the soup; it works *because* it rebuilds the
   volume instead of trying to stitch the broken surface.
3. **Decimate** — collapses the fused solid down to your target triangle
   count, then audits again so you can see exactly what you got.

Same two models, after the pipeline:

| Cleaned | Verts | Tris | Shells | Boundary edges | Watertight |
|---|---|---|---|---|---|
| Head model (voxel 0.0035, target 20,000) | 10,062 | 20,000 | 78 | 0 | Yes |
| Full-body model (voxel 0.004, target 25,000) | 12,504 | 25,000 | 3 | 0 | Yes |

Every remaining shell is itself a closed, watertight solid — voxel
remeshing seals everything, but pieces that never touch the main body
(floating strand shells are common in generator output) stay separate
islands instead of merging. See Limitations.

Total runtime of the packaged script, import and both audits included:
~41 s (full body) and ~73 s (head) on an ordinary CPU-only machine.

## Usage

```bash
blender --background --python clean_ai_mesh.py -- \
    --input model.glb --output cleaned.glb \
    --target-tris 25000 --voxel-size 0.004
```

| Argument | Default | Meaning |
|---|---|---|
| `--input` | *(required)* | Raw generator export (`.glb` / `.gltf`) |
| `--output` | *(required)* | Where to write the cleaned GLB |
| `--target-tris` | `25000` | Triangle budget for the decimate stage |
| `--voxel-size` | `0.004` | Voxel remesh size, in the model's own units |
| `--report` | — | Optional path to also save the audit report as JSON |

**Picking a voxel size:** it is an absolute size in the model's units, so
scale matters. In testing, a voxel size of roughly **0.3–0.4% of the
model's height** preserved the silhouette and features well (0.0035 for a
0.93-unit-tall head, 0.004 for a 1.16-unit-tall full body). Smaller voxels
keep more detail but produce a heavier intermediate mesh; larger voxels
start to melt fine features.

The script prints the before / voxel / after audit as it runs:

```
BEFORE:  861,433 verts | 1,499,968 tris | 7,680 shells | 218,610 boundary edges | 0 non-manifold edges | 0 zero-area tris | watertight: NO
VOXEL:   78,872 verts | 157,736 tris | 3 shells | 0 boundary edges | 0 non-manifold edges | 0 zero-area tris | watertight: YES  (17.9s)
AFTER:   12,504 verts | 25,000 tris | 3 shells | 0 boundary edges | 0 non-manifold edges | 0 zero-area tris | watertight: YES
DONE in 41.1s -> cleaned.glb
```

## Requirements

- **Blender 4.x** — developed and tested on **4.2.3 LTS** (uses Blender's
  bundled Python and NumPy — no extra packages to install). Blender
  4.0.2 also runs the full pipeline with identical audit results, but
  its glTF exporter writes split vertices (the same 25,000-triangle mesh
  reimports with 20,111 vertices instead of 12,504 — same geometry, just
  duplicated verts), so 4.2+ is recommended for the cleanest files.
- A `.glb` / `.gltf` file from a 3D generator

## Limitations — read before you rely on it

- **UVs are destroyed.** Voxel remeshing rebuilds the surface, so the
  original UV layout and textures do not carry over. The script strips
  materials on export, so the cleaned file is geometry only (~0.5 MB
  instead of ~28 MB of textures that could never map onto it). Plan on
  re-UVing and baking colour/normal maps from the raw export if you need
  the textured look back. Shape survives cleanup well; surface detail
  (eyes, skin read) comes back via the bake.
- **Detached floaters stay detached.** Voxel remeshing seals every
  shell but cannot merge solids that never touch. In testing, shell
  counts dropped 7,680 → 3 and 8,643 → 78; the survivors are small
  watertight islands (typically floating hair-strand shells from the
  generator). If you need exactly one shell, delete the islands
  afterwards (separate by loose parts and keep the largest).
- **This is not retopology.** The output is decimated triangles — no clean
  edge loops, no quad flow, no facial loops for animation. It is a solid
  base mesh for sculpting, printing, or further processing, not a
  production-ready game rig.
- **Tested on exactly 2 models**, both Hunyuan3D GLB exports (one head,
  one full body). Other generators (Meshy, Tripo, Rodin, TRELLIS, …) are
  **untested** — the method is generator-agnostic in principle (it only
  assumes triangle soup in a GLB), but nobody has run it on their output
  yet. Issues and test reports welcome.
- **QuadriFlow is deliberately not used.** It refuses to run headless on
  non-manifold input and is unreliable in background mode; Decimate
  collapse is the reducer that actually works in this pipeline.
- **Very fine or thin structures** (individual hair strands, jewellery,
  lace) can merge or vanish at practical voxel sizes — that is inherent
  to voxel remeshing, not a bug.

## License

MIT — see [LICENSE](LICENSE). Copyright (c) 2026 1915rising.
