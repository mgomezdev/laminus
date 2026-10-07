# Experiment: subset × quantity → fewest plates, overrides preserved

Goal: given a `.3mf`, a list of `{object, qty}` and a printer, return a `.3mf` with only those objects in those quantities on the fewest plates, with every per-object/part override, modifier, and painted layer intact.

Implemented: `app/subset_3mf.py`, `POST /api/3mf/objects`, `POST /api/arrange/subset`, `tests/test_subset_3mf.py`.

## Design

```
source.3mf ──build_subset──▶ subset.3mf         (byte-exact meshes + model_settings; qty as instances)
                │
                └─expand_instances──▶ oracle.3mf  (one uniquely named object per instance)
                                         │  orcaslicer --arrange 1 [--allow-rotations]
                                         ▼
                                    arranged.3mf   (layout only — never returned)
subset.3mf + layout(arranged) ──transplant_layout──▶ final.3mf
```

1. **Subset surgery** (pure zip/XML): drop unselected objects, their mesh files, rels, per-object side files (`layer_config_ranges.xml`, `cut_information.xml`), stale plate PNGs and `custom_gcode_per_layer.xml`. Mesh files are copied verbatim, so triangle-level paint (`paint_color`, `paint_supports`, `paint_seam`, `paint_fuzzy_skin`) is untouched. `qty` becomes extra build items of the same object (Orca's own "instances").
2. **Printer retarget** (`machine_uuid`): overlay bed/printer keys (`printable_area`, `printable_height`, `bed_exclude_area`, `printer_model`, …) from the resolved machine preset, set `printer_settings_id`, clear `print_compatible_printers`. Without clearing it, Orca aborts with *"process not compatible with printer"* (-17).
3. **Layout oracle**: OrcaSlicer `--arrange 1` does the multi-plate packing.
4. **Transplant**: plate membership + instance transforms copied from the oracle output into the subset.

### Why the oracle indirection (key finding)

Returning Orca's own export of the arranged subset is **lossy**. When instances of one object land on different plates (or get different rotations — seen on Pillar without any flag, and on Teehaus with `--allow-rotations`), Orca 2.4.2 splits them into clone objects that **lose `name`, per-object overrides (`wall_loops`, infill…) and the extruder assignment** (Dach: extruder 2 → 1). Feeding Orca one uniquely named object per instance gives it nothing to clone, names round-trip, and the layout maps back exactly.

## Results (real projects, via the API in a container, Orca 2.4.2)

Fidelity = per-object metadata, part list (incl. `modifier_part` + its overrides), height-range modifiers, painted-attribute counts and instance count equal to the source (`subset_3mf.fingerprint`).

| Project | Selection | Plates | Notes | Fidelity |
|---|---|---|---|---|
| Teehaus (Bambu 2.3) | Dach×3 (paint_color 8046, 4 overrides, extruder 2), Haus×2, Platte×40, text×30 | 7 | `allow_rotations` | PASS |
| Pillar (Bambu **2.6**) | 120×2, 160×2, Body1×1 | 3 | paint_fuzzy_skin + paint_color; Orca rejected the file until the `Application` stamp was dropped | PASS |
| T-Rex (Bambu 1.9) | 6 objects, 19 instances | 2 | paint_supports 341 200 tris, 18/20-part objects, 7 overrides | PASS |
| ZenFlow (Bambu 2.3, 325 MB meshes) | planter×3 + insert×3 on `machine_uuid` = Elegoo Centauri Carbon | 2 | `modifier_part` w/ its own overrides; bed → 256×256 | PASS |

Final file validity: re-loaded in Orca with `--arrange 0` (6 plates, membership honoured) and `--slice 5` succeeded (35 MB gcode).

### How close to "fewest plates"?

Orca's arrange is deterministic and order-insensitive; `--allow-rotations` and `--allow-multicolor-oneplate` never changed the plate count in tests. Capacity probes on Snapmaker U1 bed with the project's prime tower: text_shape 24/plate, Platte 16/plate. 49 text → 3 plates (=⌈49/24⌉); Platte×40 → 3 (=⌈40/16⌉); Platte×40 + text×30 → 4 (fractional bound 2.5+1.25 = 3.75 → 4). So Orca hit the capacity lower bound in every probe, but **minimality is not guaranteed** — it is Orca's heuristic. Not implemented: own bin-packer / retry-with-orderings. Add only if a real case beats the bound.

## Known limitations / follow-ups

- Process & filament settings are **not** remapped to the target printer — only bed/printer keys. A 4-filament project retargeted to a 1-extruder machine keeps its 4 filament slots.
- Plate-scoped data (`custom_gcode_per_layer.xml`, plate PNGs, plate names) is dropped; plates are renumbered.
- Selector names shared by several objects (e.g. 4× "Platte 1.stl") → 422; use `id` (`/api/3mf/objects` lists them).
- Bambu-isms Orca's CLI rejects are fixed up minimally: `Application` stamp dropped (newer-version check); `raft_first_layer_expansion` / `tree_support_wall_count` −1 → 0 (Orca's minimum). Any other out-of-range Bambu value would still fail (400).
- Oracle holds one mesh reference per instance; a very large qty of a huge mesh costs Orca RAM/time (ZenFlow ×3 ≈ 14 s). `ARRANGE_TIMEOUT_SECONDS` applies.
- XML from uploads is parsed with stdlib `ElementTree` after rejecting `DOCTYPE`/`ENTITY` (billion-laughs).

## Reproduce

```bash
python -m pytest tests/test_subset_3mf.py
curl -F file=@project.3mf localhost:5000/api/3mf/objects
curl -F file=@project.3mf -F 'selection=[{"id":6,"qty":3}]' -F machine_uuid=<uuid> \
     localhost:5000/api/arrange/subset -o out.3mf -D -
```
