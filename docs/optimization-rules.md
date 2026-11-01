# Filament Saver — Optimization Rules

Draft v0.1 of the rules engine: how the preset, sliders, answers and geometry become OrcaSlicer settings.

> Every key used here was checked against `src/libslic3r/PrintConfig.cpp` (OrcaSlicer `main` branch, October 2026). What the plugin API allows is covered in section 13.

---

## 1. Order of application

Each stage overrides the previous one. Floors always win, except against an explicit manual override by the user (with a warning).

```
1. Preset          → base values + default strength slider
2. Sliders         → Strength (S), Finish (V), Reliability (R)
3. Answers         → modifiers from the questions
4. Material        → modifiers read from the filament profile (not asked)
5. Geometry        → modifiers inferred from the mesh
6. Floors/ceilings → safety limits from preset ∪ material (always last)
7. Manual override → the user may break a floor, with a warning
```

**Conflict resolution:** when two rules set the same key:
- **safety** keys (walls, top/bottom layers, brim, support enabled): the **safest** value wins;
- **economy** keys (density, infill pattern, support spacing): the **most economical** value that respects the floors wins.

---

## 2. Presets

| Preset | Default S | Default V | Default R | Notes |
|---|---|---|---|---|
| `decoration` | 1 | 4 | 3 | Looks matter, load does not |
| `functional` | 3 | 2 | 3 | Tool, organizer, light-duty bracket |
| `prototype` | 0 | 0 | 2 | Only the geometry needs to come out right |
| `mechanical` | 5 | 1 | 4 | Gear, hinge, load-bearing bracket |
| `container` | 2 | 3 | 3 | Vase, cup, box; vase mode candidate |
| `miniature` | 1 | 5 | 3 | Fine detail, delicate supports |
| `large` | 1 | 2 | 4 | Cosplay, large part; infill dominates usage |

---

## 3. Strength slider (S) — global base

A single table for all presets. The preset applies later, through floors and overrides.

**Counts are thicknesses.** Layer counts (here and in section 4) are calibrated at 0.2 mm layers, and wall counts for a 0.4 mm nozzle. At other layer heights and nozzles the plugin keeps the same thickness: `layers = ceil(count × 0.2 / layer_height)`, at least 2, and `walls = ceil(count × 0.4 / nozzle)`, never fewer than 2 (or the count, if it is lower). So 3 top layers at 0.2 mm become 8 layers at 0.08 mm, and 2 walls become 4 walls with a 0.2 mm nozzle; a 0.8 mm nozzle needs fewer walls for the same strength. The nozzle comes from the printer preset.

| S | `wall_loops` | `sparse_infill_density` | `sparse_infill_pattern` | `top_shell_layers` | `bottom_shell_layers` |
|---|---|---|---|---|---|
| 0 | 2 | 5% | `lightning` | 3 | 2 |
| 1 | 2 | 8% | `lightning` | 4 | 3 |
| 2 | 2 | 12% | `supportcubic` | 4 | 3 |
| 3 | 3 | 15% | `adaptivecubic` | 5 | 4 |
| 4 | 4 | 20% | `gyroid` | 5 | 4 |
| 5 | 5 | 30% | `gyroid` | 6 | 5 |

Solid fill that Orca adds on its own also follows S:

| S | `ensure_vertical_shell_thickness` | `gap_fill_target` | `minimum_sparse_infill_area` |
|---|---|---|---|
| 0 | `none` | `topbottom` | 5 mm² |
| 1 | `ensure_critical_only` | `topbottom` | 5 mm² |
| 2 | `ensure_critical_only` | `topbottom` | 10 mm² |
| 3 | `ensure_moderate` | `everywhere` | 10 mm² |
| 4 | `ensure_all` | `everywhere` | 15 mm² |
| 5 | `ensure_all` | `everywhere` | 15 mm² |

- `ensure_vertical_shell_thickness` uses the row of **max(S, V)**: sloped surfaces are visible, so a high finish keeps them reinforced even at low strength (a decoration at S1/V4 keeps `ensure_all`).
- `gap_fill_target` **only goes down**: Orca's default is `nowhere`, and more gap fill adds filament for little strength. It controls gap fill on top, bottom and internal **solid** surfaces, not between walls. At S ≤ 2 it becomes `topbottom` (no gap fill on internal solid infill).
- `minimum_sparse_infill_area` follows S in both directions.

Each change has a known cost: sloped walls a bit thinner, small gaps in internal solid infill, small sparse areas no longer turned solid. Orca still decides where any of it applies. TPU keeps `minimum_sparse_infill_area` (infill sets its hardness).

Rationale:
- **Walls give more strength per gram than infill.** From S=3 up, walls increase before density does.
- `lightning` only supports the top surfaces and adds no structural strength. Good for S ≤ 1.
- `supportcubic` and `adaptivecubic` concentrate material near the surfaces and save in the core.
- `gyroid` is strong in every direction. Reserved for high S.

---

## 4. Finish slider (V)

| V | `top_shell_layers` | `only_one_wall_top` | `support_interface_top_layers` |
|---|---|---|---|
| 0 | −1 (min. 3) | `true` | 1 |
| 1 | −1 (min. 3) | `true` | 1 |
| 2 | +0 | `true` | 2 |
| 3 | +0 | `false` | 2 |
| 4 | +1 | `false` | 3 |
| 5 | +1 | `false` | 3 |

`top_surface_pattern` and ironing are not suggested: they change the look or the time, not the filament.

- Signed values (+1, −1) are **deltas** on top of the S table.
- **Shell thickness overrides the layer count.** Orca prints `max(top_shell_layers, ceil(top_shell_thickness / layer_height))` top layers (same for bottom). With `top_shell_thickness = 1` mm at 0.2 mm layers, any suggestion below 5 top layers has no effect. When the current thickness is larger than `layers × layer_height`, the engine also suggests lowering `top_shell_thickness` / `bottom_shell_thickness` to that value. The top never goes below **0.6 mm** (Orca's default), which keeps thin layer heights from leaving a weak, pillowed top: at 0.12 mm layers that still means 5 top layers.
- Support interface improves the supported face but costs material. Reduce it at low V.
- With supports on, two more interface settings follow V, and only ever go down:
  - `support_interface_bottom_layers` (interface where supports rest on the part; `-1` means "same as top"): 0 at V ≤ 3.
  - `support_interface_spacing` (gap between top interface lines, Orca's default 0.5 mm; 0 = solid): at least 0.8 mm at V = 0 and 0.6 mm at V = 1. A wider current value is kept.

---

## 5. Reliability slider (R)

| R | `brim_type` | `skirt_loops` | `support_threshold_angle` | `support_base_pattern_spacing` (normal) | `support_remove_small_overhang` |
|---|---|---|---|---|---|
| 0 | `no_brim` | 0 | 20° | 3.5 mm | on |
| 1 | `no_brim` | 1 | 25° | 3.0 mm | on |
| 2 | `auto_brim` | 1 | 30° | 2.5 mm | on |
| 3 | `auto_brim` | 1 | 30° | 2.5 mm | on |
| 4 | `brim_ears` | 1 | 35° | 2.0 mm | on |
| 5 | `outer_only` | 2 | 40° | 2.0 mm | (unchanged) |

**Brim and skirt only go down.** For materials that do not warp, the brim column is only applied when it uses less than the current brim (`no_brim` < `auto_brim` < `brim_ears` < `outer_only`): Orca's auto brim already decides where a part needs one. The skirt column is likewise a ceiling, since the start G-code primes the nozzle. Materials that warp (ABS/ASA, PA, PC) get their brim from section 7.

`only_one_wall_first_layer` is on at R ≤ 1 (one wall on the first layer, slightly weaker base outline) and off from R = 2.

Support settings are only suggested when supports are enabled. `support_base_pattern_spacing` applies to normal supports (Orca's default is 2.5 mm). `support_style` is only changed when the new style uses less material, as Orca describes them: tree `tree_strong`/`tree_hybrid` → `organic` at R ≤ 3 (Orca's `default` tree style is already organic), and normal `default`/`grid` → `snug` at R ≤ 1 (less stable). Brim ears (`brim_ears`) put brim only on sharp corners, which is where parts lift.

**Certainty rule.** The plugin only suggests changes whose effect is known: they use less filament (or time) with a known cost, and Orca still decides where they apply. It never guesses whether the part needs something. For that reason `support_on_build_plate_only` was removed: it left overhangs above the part unsupported without knowing whether they needed support. Disabling supports from mesh analysis is out for the same reason.

`support_threshold_angle` measures the overhang slope **from the horizontal**: Orca generates support when the slope is below the threshold. Lower value = less support. Orca's default is 30°. To convert an angle measured "from the vertical" (as in question Q13): `threshold = 90 − angle`.

More settings that follow R and never use more filament than the current value:

| Setting | Rule |
|---|---|
| `prime_tower_brim_width` | At most 0 mm at R = 0, 1 mm at R = 1, 2 mm at R = 2–3; kept from R = 4. Only with more than one filament and the prime tower on; `-1` (auto) is left alone |
| `raft_layers` | 0 at R ≤ 4. A raft prints several layers under the whole part; at R = 5 the current raft is kept, and it is never removed for materials that warp (ABS/ASA, PA, PC) |
| `skirt_height` | 1 when there is a skirt and `draft_shield` is `disabled` (more skirt layers only make sense as a draft shield) |
| `support_base_pattern` (normal supports) | `lightning` at R ≤ 1, the lightest support body, less stable |
| `support_base_pattern` (tree supports) | `hollow` at R ≤ 3 when another pattern is set. Orca's `default` for tree supports is already hollow, so it is left alone |
| `tree_support_wall_count` | 2 → 1 at R ≤ 3 (0 means auto and is left alone) |

Low R saves on brim and supports but raises the risk of failure. **A failed print wipes out all the savings.**

---

## 6. Answer modifiers

| # | Question | Answer | Effect |
|---|---|---|---|
| Q1 | Load direction | Pulling layers apart (Z axis) | Suggest reorientation; `wall_loops` +1 |
| Q2 | Load type | Impact or torsion | Infill pattern → `gyroid`; `wall_loops` +1 |
| Q2 | Load type | Compression only | Allows `adaptivecubic` even at high S |
| Q3 | Screw holes / heat-set inserts | Yes | `wall_loops` +1 globally. Reinforcing only around the holes is not possible: the API cannot create modifiers (see section 13) |
| Q4 | Flexible (TPU) | Yes | Infill ≤ 15%; walls drive stiffness; no `lightning` |
| Q5 | Visible faces | Top/front only | Support and bottom allowed at effective V −2 |
| Q5b | Faces pointing up while printing will be hidden | Yes (checkbox) | `top_surface_density = 0%`. **Implemented** |
| Q6 | Post-processing | Sanding | `wall_loops` +1, `top_shell_layers` +1 |
| Q6 | Post-processing | Painting | Effective V −1 |
| Q7 | Watertight | Yes | `wall_loops` ≥ 3; `bottom_shell_layers` ≥ 4 |
| Q7b | Vase mode acceptable | Yes, and geometry is compatible | `spiral_mode = true` (largest possible saving). **Implemented** |
| Q8 | Fits with another part | Yes | `wall_loops` ≥ 2; do not use `only_one_wall_top` |
| Q9 | Weight matters | Yes | Effective S −1 (respecting floors); prefer `lightning`/`supportcubic` |
| Q10 | Environment | Heat, UV or outdoors | Floor: `wall_loops` ≥ 3, `top_shell_layers` ≥ 5 |
| Q10 | Environment | Food contact | Warning: material/certification out of scope; do not reduce walls |
| Q11 | Reorientation acceptable | Yes | Compute the orientation with the smallest overhang area and **suggest** it. The API cannot move objects |
| Q12 | Splitting the part acceptable | Yes | Suggestion only (v2) |
| Q13 | Printer's overhang capability | Angle from the vertical | `support_threshold_angle = 90 − angle`, replaces the R table |
| Q14 | Batch (N copies) | N > 5 | Enable a wider variant search (more slices) |
| Q15 | Material | — | **Not asked.** Read from the filament profile, see section 7 |
| Q16 | Reliable printer | No | Effective R +1 |
| Q17 | Multicolor | Purging into the infill is OK | `flush_into_infill = true`. **Implemented** |
| Q17b | Multicolor | Purging into the supports is OK | `flush_into_support = true`. **Implemented** |

### Implemented questions (plugin)

The tab shows these as checkboxes under the sliders, titled "Only check what you are sure of". Unchecked means "not sure": the setting is not touched. They follow the certainty rule: the effect is known, and only the user knows whether it is acceptable, so the plugin never infers the answer from the model.

- **Q5b, faces pointing up while printing will be hidden** (always shown). "Top" is the orientation on the bed, not in use, and it covers every upward-facing area: the top of the part, ledges, and the floor of any pocket. Use it for, e.g., the back of a wall sign printed face down, a spacer glued between parts, or a part closed inside an enclosure. Not for boxes (inner floor and rim are top surfaces), figures, parts with visible ledges, or faces that slide, press or fit tightly against something. Orca applies `top_surface_density` to every top surface, but only to its last layer (`Fill.cpp`: external top surfaces use it, internal solid infill stays at 100%), and 0% prints only the walls on that layer. The suggestion keeps at least 2 solid layers underneath (the top layer count is always ≥ 3), so the part stays closed. The bottom surface is not offered: `bottom_surface_density` also applies to the first layer, and Orca warns it may hurt bed adhesion, so its effect is not certain.
- **Q17, purge into the infill** (shown with more than one filament). The walls are opaque, so mixed colors inside will not show (Orca's tooltip warns about transparent walls). It only uses infill Orca already prints, so it never adds material. Not suggested when the sparse infill density is 0%, or when the project's filaments belong to different families (they may not bond inside the part; a note explains it).
- **Q17b, purge into the supports** (shown with more than one filament and supports on). Orca's default is already on, so this only changes presets that turned it off.
- Both purge options only work with the prime tower on (`enable_prime_tower`). When it is off, nothing changes and a note says why.

- **Q7b, print as a vase** (always shown). An open container (vase, cup, pot) printed as one continuous outer wall. When checked, the plugin suggests `spiral_mode = 1` together with everything Orca requires before it prints a vase (`ConfigManipulation.cpp`): `wall_loops = 1`, `top_shell_layers = 0`, `sparse_infill_density = 0%`, `enable_support = 0`, `enforce_support_layers = 0`, `detect_thin_wall = 0`, `overhang_reverse = 0`, `timelapse_type = 0` (traditional) and `enable_wrapping_detection = 0`. These rows form one group: Apply, Skip or Undo on any of them acts on all, so the preset is never left half in vase mode. The vase rules run last, so no purpose floor brings walls, infill or top layers back, and suggestions that no longer apply (infill pattern, top surface, support settings, purge into infill or supports) are dropped. A note warns that vase mode prints only the outside of the part, so a model with thick walls or an inner cavity comes out as a thin shell. Orca itself refuses to slice a vase with several objects (unless printing by object) or several materials in one object.

`flush_into_objects` (use a whole object as a purge target) is a per-object setting and is not offered. Settings that change the part's geometry, such as `make_overhang_printable`, are never suggested.

---

## 7. Material layer (not asked)

### Reading

| Key | Use |
|---|---|
| `filament_type` | Material family (list of strings, one per filament) |
| `filament_density` | Convert volume to grams in the estimate |
| `filament_cost` | Show savings as money |
| `filament_is_support` / `filament_soluble` | Identify support filament |

Read through `orca.host.preset_bundle().current_filament_presets()` → `config_value(key)`.

**Material selector:** the tab shows the detected family ("Auto: PETG") and lets the user pick another family (PLA, Silk PLA, PETG, ABS/ASA, TPU, Nylon, PC) when the filament profile is generic or wrong, e.g. a silk filament registered as `PLA`. The override only changes the rules; the filament profile is not touched.

### Family classification

`filament_type` is an open list, but Orca offers a fixed set of types (`src/libslic3r/MaterialType.cpp`). The plugin uppercases the type, takes the part before the first `-` and maps it:

| Family | Matches | Notes |
|---|---|---|
| `pla` | `PLA`, `PLA+`, `PLA-CF`, `PLA-AERO` | |
| `silk` | `pla` family with "SILK" in the type **or in the preset name** | Silk is often registered as `PLA` |
| `petg` | `PETG`, `PET`, `PCTG`, with `-CF`/`-GF` | |
| `abs_asa` | `ABS`, `ASA` (with `-CF`, `-GF`, `-AERO`), `HIPS` | HIPS warps like ABS when it is the part's material |
| `tpu` | `TPU`, `TPE`, `FLEX` | |
| `pa` | `PA`, `PA6`, `PA11`, `PA12`, `PAHT`, `PPA`, `NYLON`, with `-CF`/`-GF` | |
| `pc` | `PC`, `PC-ABS`, `PC-CF`, `PC-PBT` | `PCTG` is `petg` |
| `support` | `PVA`, `BVOH` | Never used for the rules (see Multi-material) |
| `unknown` | anything else (PEEK, PEI, PPS, PP, PE, …) | `pla` rules + effective R +1 + warning |

Fiber-filled types (`-CF`, `-GF`) follow their base family. The material selector in the tab replaces the detected families with the one picked.

### Rules per family

| Family | Floors | Adjustments | Forbidden |
|---|---|---|---|
| `pla` | — | None (it is the reference for the tables) | — |
| `silk` | In `functional`/`mechanical`: `wall_loops` ≥ 3 | Warning: weak layer adhesion, not recommended for functional parts | — |
| `petg` | With supports: `support_top_z_distance` ≥ 0.25 mm | `support_interface_top_layers` ≥ 2 (PETG sticks to supports) | — |
| `abs_asa` | `brim_type` at least `brim_ears` (or `outer_only` at R = 5), `brim_width` ≥ 5 mm; effective R ≥ 3 | — | `no_brim` |
| `tpu` | Infill **left unchanged** (density, pattern and minimum sparse area): it sets the hardness of the part | Note in the tab | `lightning` |
| `pa` | `brim_type` at least `brim_ears` (or `outer_only` at R = 5), `brim_width` ≥ 5 mm; effective R ≥ 3 | Warning: dry the filament before printing | `no_brim` |
| `pc` | `brim_type = outer_only`, `brim_width` ≥ 8 mm; effective R ≥ 4 | — | `no_brim` |

For materials that warp, the brim is only ever **raised** to these minimums: a fuller brim type or a wider brim that the user set is kept. Painted, inner-only and leading-edge brims are never replaced; a note asks to check they hold the corners.
| `unknown` | Effective R = R + 1 | Warning with the type | — |

Not implemented, on purpose: turning supports off for TPU (it guesses what the part needs, see the certainty rule), a question to unlock TPU infill, and sorting fiber-filled materials by money saved. Per-material strength-per-gram adjustment (e.g. nylon holds up with fewer walls) is deferred until it can be calibrated with real parts.

### Multi-material

- **Support filaments are skipped.** Filaments of the `support` family or with `filament_is_support` set are not used for the rules. When the first filament is one, a note says which filament the rules follow.
- **Strictest rule across part filaments.** The API does not say which object uses which filament, so every part filament counts: the highest reliability floor (and the family that raised it), the widest brim (and `outer_only` with any PC), the PETG support gap, the silk wall floor and the TPU infill lock all apply when any part filament needs them. A note lists the materials.
- **Purging between different families:** do not reduce flush volumes. Purge into the infill is only suggested when all filaments belong to the same family (different materials may not bond).
- **Pairs that do not bond** (e.g. PLA + PETG) are not warned about yet.

### Money saved

```
savings = grams_saved × filament_cost / 1000
```

`filament_cost` has no currency in Orca: show only the number, labeled "cost". With several filaments, sum per filament using `statistics.json`, which breaks grams down per filament.

---

## 8. Geometry modifiers (not asked)

| Mesh signal | Condition | Effect |
|---|---|---|
| Volume / surface area | Low (thin part) | Infill is irrelevant → focus on walls and support; hide infill changes saving < 1 g from the diff |
| Volume / surface area | High (solid part) | Infill is the biggest lever → prioritize pattern and density |
| Height / shortest base side | > 3 | Floor: brim `outer_only` ≥ 3 mm (tipping risk) |
| Base area | Very small | Floor: brim on |
| Overhang area | > 15% of the surface | Suggest reorientation; prefer tree/organic |
| Thin walls | Below 2× the line width | Enable `detect_thin_wall` |
| Shape | Shell open at the top, single wall | `spiral_mode` candidate (ask Q7b) |

---

## 9. Floors and ceilings per preset and material

Limits no slider can break. Only a manual override can, with a warning.

| Preset | Min. `wall_loops` | Min. infill | Min. `top_shell_layers` | Min. `bottom_shell_layers` | Forbidden patterns |
|---|---|---|---|---|---|
| `decoration` | 2 | 0% | 3 | 2 | — |
| `functional` | 2 | 10% | 4 | 3 | `lightning` |
| `prototype` | 1 | 0% | 2 | 2 | — |
| `mechanical` | 3 | 20% | 5 | 4 | `lightning`, `supportcubic` |
| `container` | 2 | 0% | 3 | 4 | — |
| `miniature` | 2 | 0% | 3 | 2 | — |
| `large` | 2 | 5% | 4 | 3 | — |

Ceilings (keep the tool from making usage worse): `wall_loops` ≤ 6, infill ≤ 40%. Above that, the user sets values manually.

**The slider tables are ceilings; these floors are the only minimums.** A change that would use more filament is only suggested when the current value is below a minimum of the purpose (this table, scaled to the layer height and nozzle) or of the material (section 7: PETG interface layers, the brim of materials that warp, Silk PLA walls, a forbidden infill pattern), and then only up to that minimum: mechanical with 2 walls is raised to 3, not to the 5 of Strength 5. A profile already leaner than the sliders but at or above the minimums is left as it is. Every other increase the sliders would ask for (more top layers at a high finish, a steeper support angle at a high reliability, and so on) is not suggested.

In the tab, those increases have their own section, **Needed for the purpose or the material**, below **Save filament**. **Apply savings** applies only the savings; **Apply all** applies both sections.

**Combining with material:** the final floor for each key is the **safest** of the preset floor and the material family floor (section 7). Forbidden patterns add up.

Examples: `functional` + `abs_asa` → walls ≥ 2 (preset) and brim ≥ 5 mm (material). `mechanical` + `tpu` → infill locked at its current value (material) and ≥ 20% (preset), with neither `lightning` nor `supportcubic`.

---

## 10. Rule format (to become code)

Each rule has an id, a condition, an effect and a text shown in the diff.

```yaml
- id: watertight-walls
  stage: answers
  when: { answer: Q7, equals: true }
  set:
    wall_loops: { min: 3 }
    bottom_shell_layers: { min: 4 }
  kind: safety            # safety → safest wins; economy → most economical wins
  explain: "Watertight part: at least 3 walls and 4 bottom layers to prevent leaks."

- id: weight-matters
  stage: answers
  when: { answer: Q9, equals: true }
  adjust:
    strength: -1
  kind: economy
  explain: "Weight is a priority: strength lowered by 1 level, within the preset limits."

- id: material-abs-brim
  stage: material
  when: { family: abs_asa }
  set:
    brim_type: { forbid: [no_brim], default: outer_only }
    brim_width: { min: 5 }
  kind: safety
  explain: "ABS/ASA warp: a brim of at least 5 mm is required."
```

The final diff lists each change with the id of the rule that caused it, in grams and cost. The numbers below are illustrative:

```
sparse_infill_density  20% → 8%    (−34 g · cost −4.10)  [slider-S=1]
sparse_infill_pattern  grid → lightning                  [slider-S=1]
wall_loops             2 → 3                              [watertight-walls]
brim_type              no_brim → outer_only (+3 g)        [material-abs-brim]
```

---

## 11. Examples

**Decorative vase** — preset `container`, Q7 = yes, Q7b = yes, compatible geometry.
→ `spiral_mode = true`. Infill, top layers and extra walls disappear. Largest possible saving.

**Shelf bracket** — preset `mechanical`, S = 5, Q3 = holes, Q1 = side load.
→ 5 walls +1 for the holes (Q3) = 6 (at the ceiling), `gyroid` 30%, suggestion to reorient so the layers are not loaded in tension. Savings come only from brim and support.

**Cosplay helmet** — preset `large`, S = 1, V = 2 (will be painted → effective V 1), Q9 = weight matters, PLA.
→ Effective S 0, but the `large` floor holds infill at 5% `lightning`. 2 walls, minimal top layers, sparse tree support. Largest absolute saving potential.

**Same helmet in ASA** — everything the same, ASA material.
→ Same infill and wall savings. The `large` preset already uses R = 4 (3 mm brim), but the material layer raises the brim to ≥ 5 mm and forbids `no_brim`. Part of the saving goes back into the brim, in exchange for no warping.

**TPU rubber foot** — preset `functional`, S = 2.
→ Infill **does not change** (it sets the hardness) unless the user unlocks it. Savings come from brim (`no_brim`), skirt and top/bottom layers. No `lightning`.

---

## 12. Open questions

- ~~Local bundle format and location~~: solved and confirmed by the probe, see section 13 → "Probe results". Still to check: whether the preset survives an Orca restart, and whether writing to the data folder triggered a permission prompt.
- **Unsaved edits on the source preset:** the user's process preset is often a system preset with unsaved edits (`is_dirty = true`). Inheriting from the system preset and writing only Filament Saver's keys drops those edits. The plugin must also write every key where the edited preset differs from the saved one (see "Probe results").
- **Selecting the generated preset:** no documented API. For now, the user picks it from the list.
- **Running the CLI from the plugin:** test whether the CLI accepts the mesh and settings extracted through the Host API, and how long each variant takes.
- **Which filament each object uses:** not documented in the Host API. Until that is known, v1 applies the most restrictive rule across all filaments.
- **Orca's type list:** `filament_type` comes from Orca's `MaterialType::all()`. Extract the full list to complete the family mapping in section 7.
- Calibrate every number in the S table and section 7 with real test parts.
- The API is in alpha and may change before 2.5 stable.

---

## 13. Integration with the OrcaSlicer 2.5 plugin API (checked 2026-10-29)

### What the API allows

| Need | Available? | How |
|---|---|---|
| Read current settings | ✅ | `orca.host.preset_bundle().full_config_value(key)` and `current_print_preset().config_value(key)` |
| Read material, density and cost | ✅ | `preset_bundle().current_filament_presets()` → `config_value("filament_type")`, `filament_density`, `filament_cost` |
| Read mesh geometry | ✅ | `orca.host.model().objects()` → `volumes()` → `mesh()`: `volume()`, `bounding_box()`, `face_normals()`, `vertices()` (numpy) |
| Questionnaire UI | ✅ | `pages` capability (main-window tab) or `orca.host.ui.create_dock_panel(...)`. HTML + `postMessage` |
| React to events | ✅ | `on_lifecycle_event`: `PresetSelected`, `ObjectAdded`, `ObjectChanged`, `GCodeExportFinished` (carries the file path)… |
| **Change settings** | ✅ (undocumented) | `orca.host.preset.process.begin_update()` → `set(key, value)` → `commit()` edits the active preset in place, off the UI thread (confirmed, see "Plugin v0.1 results"). Fallback: write a process preset into a **local bundle** and call `orca.host.preset.bundles.reload_local(bundle_id)`, see "Local bundle" |
| Store the intent (preset/sliders/answers) | ✅ | The plugin's own configuration (`save_config`). Process presets can override plugin configuration, so the intent is stored with the preset |
| Read grams and time | ⚠️ needs a setting | Enable *Preferences > Developer > Export statistics with G-code*. Each export writes a `<name>.statistics.json` with grams per filament, split into model/support/tower/flush. Read it after `GCodeExportFinished` |
| Read time, grams and cost after a slice | ✅ | Parse the temporary G-code from `GCodeExportFinished` (fires on every slice), see "Plugin v0.1 results". Each slice is tagged with a fingerprint of `orca.host.model()` (per part or negative volume: type, facet count, mesh volume, scale; modifiers and support enforcers or blockers are left out; per printable instance: scale; plus `current_plate_index()`), and only slices with the same fingerprint are compared |
| Trigger slicing | ❌ | Not documented. Alternative: run the `orca-slicer` CLI as a subprocess with `--export-statistics` (requires process permission) |
| Create local modifiers (holes) | ❌ | Not documented |
| Move/reorient objects | ❌ | Read-only. Suggestion only |
| Change sliced layers | ✅ experimental | The `slicing-pipeline` capability can edit layers and G-code. Not Filament Saver's focus |

### Local bundle (how the plugin applies settings)

Checked against the Orca source code (`PresetBundle.cpp`, `Preset.cpp`, `PluginHostPresets.cpp`, `main` branch), [PR #15424](https://github.com/OrcaSlicer/OrcaSlicer/pull/15424) and the demo plugin `sandboxes/orca_reload_local_bundle_demo.py`.

**Folder layout**

```
{data_dir}/user/{preset_folder}/_local/{bundle_id}/
├── bundle_metadata.json
├── process/     ← process presets (what Filament Saver uses)
├── filament/
└── machine/
```

- `data_dir` on macOS: `~/Library/Application Support/OrcaSlicer`.
- `preset_folder`: the id of the user signed in to Orca Cloud, or `default` when signed out. The plugin cannot read the app configuration, so it finds the folder from the path of an already-loaded user preset (`preset.file` contains `user/<folder>/…`), as the demo does.
- `bundle_id`: a single path component, without `/` or `\`. Must not collide with a subscribed bundle (`_subscribed`). Proposed: `filament-saver`.

**`bundle_metadata.json`**

```json
{ "id": "filament-saver", "name": "Filament Saver", "version": "0.1.0",
  "description": "Presets optimized to save filament", "author": "…" }
```

The `id` field must **match the folder name**. The other fields are optional.

**Process preset** (`process/<name>.json`)

```json
{
  "type": "process",
  "name": "0.20mm Standard @Elegoo CC 0.4 nozzle · Filament Saver",
  "from": "User",
  "instantiation": "true",
  "version": "2.5.0.0",
  "inherits": "0.20mm Standard @Elegoo CC 0.4 nozzle",
  "print_settings_id": "0.20mm Standard @Elegoo CC 0.4 nozzle · Filament Saver",
  "wall_loops": "2",
  "sparse_infill_density": "8%",
  "sparse_infill_pattern": "lightning"
}
```

- All values are **strings**. Per-extruder/variant keys are **lists of strings** (e.g. `"inner_wall_speed": ["60"]`). Copy the value shape from the parent preset.
- `version` must be a valid semver, or the preset is rejected.
- Inside Orca, the canonical name becomes `_local/filament-saver/<name>`. That is how the plugin finds it with `find_preset()`.

**Reloading**

```python
orca.host.preset.bundles.reload_local("filament-saver")   # raises RuntimeError with the reason on failure
orca.host.preset.bundles.list_ids()                       # valid local bundles
```

- Validates everything in a staging copy before applying. If any preset is invalid, **nothing changes**.
- Adds, updates (same name), removes and renames presets in the bundle. Leaves every other preset alone.
- Keeps the current selection. If the user edited the Filament Saver preset without saving, those edits are kept on top of the new version.
- Runs on the UI thread and returns only after the tabs and selectors are refreshed.

**Rules the plugin must follow**

1. **Inherit from the system preset, not from a user preset.** Local bundles are loaded **before** user presets at startup. Inheriting from a user preset will probably fail with "can not find parent" when Orca is reopened. *(Inferred from the load order in `load_user_presets`, not yet tested.)* If the current preset is a user preset, use its parent as `inherits` and write the user's overrides plus Filament Saver's changes into the file.
2. **Stable names.** One preset per source preset, always with the same name. Reload is **rejected** if it removes or renames a selected preset with unsaved edits.
3. **Nothing may inherit from the Filament Saver preset.** If the user saves their own preset based on the Filament Saver one, the reload is rejected and the error names the dependent preset. The plugin must explain this and offer to write under another name.
4. **Atomic writes.** Write to a temporary file and use `os.replace()`, as the demo does. Orca never reads a half-written JSON.
5. **Fallback.** If `reload_local` does not exist (older Orca), tell the user to restart Orca.

**Still unsolved:** there is no API to **select** the generated preset. After the reload, the plugin shows a notification (`orca.host.ui.push_notification`) asking the user to pick "… · Filament Saver" in the process selector.

### Probe results (2026-10-29, OrcaSlicer 2.5.0-beta build 345efec6, macOS)

Run with a diagnostic probe plugin written to check the API (not part of this repository), Elegoo CC printer, PETG, one object on the plate.

| Check | Result |
|---|---|
| Plugin load + `numpy` dependency | ✅ Loaded; `uv` installed numpy 2.5.4 in about 4 s |
| `preset_folder` discovery from preset paths | ✅ Found the Orca Cloud user folder |
| Value types from `config_value` / `full_config_value` | ✅ **Always `str`, in Orca's on-disk form**: `"10%"`, `"lightning"`, `"1"`/`"0"` for bools, `"no ironing"`. They can be written back to preset JSON unchanged |
| Missing key | `Preset.config_value` returns `""`, `full_config_value` returns `None` (seen with `inherits` on a system preset) |
| Filament values | ✅ One `str` per filament preset (`filament_type = "PETG"`, `filament_cost = "30"`, `filament_density = "1.25"`) |
| Mesh geometry with numpy | ✅ Volume, bounding box, surface area, overhang area and ratios all plausible |
| Write local bundle + `reload_local` | ✅ Preset `_local/filament-saver/<name>` loaded with no error (log: `commit_user_preset … load config successful`) |
| Inherit from system preset | ✅ Works at runtime. Surviving a restart is not checked yet |

**Finding: the source preset had unsaved edits.** The selected process preset was the system preset with `is_dirty = true`. `current_print_preset()` returns the **edited** values, so the probe saw the user's edits (e.g. `sparse_infill_density = 10%`, `lightning`). The test preset inherits the saved system preset and only sets 3 keys, so all other unsaved edits were lost. The real plugin must:

1. Compare `bundle.prints.edited_preset()` with `bundle.prints.selected_preset()` key by key (`config_keys()` + `config_value()`).
2. Write every differing key into the generated preset, then apply Filament Saver's changes on top.
3. Do the same for the overrides of a saved user preset relative to its system parent.

Since values are already strings in on-disk form, copying them needs no conversion. Still to check: how per-extruder list keys (e.g. `inner_wall_speed`) come back from `config_value`.

### Plugin v0.1 results (2026-10-30, same build)

Run with `plugin/filament_saver` (Pages tab).

| Check | Result |
|---|---|
| **In-place write API** `orca.host.preset.process.begin_update()` → `set()` → `commit()` | ✅ **Present in 2.5.0-beta** although undocumented. Called from a plugin-created Python thread (not the UI thread), it changed the active process preset as unsaved edits. This is now the main path; the local bundle is the fallback |
| Restore through the same API | ✅ Writing the original values back returned the preset to its previous state |
| Print estimate without export | ✅ `GCodeExportFinished` also fires for the temporary G-code of every slice (`<temp>/orcaslicer_model/…/Metadata/.<pid>.<n>.gcode`). Time, grams and cost are in the statistics lines **after** `CONFIG_BLOCK_END` |
| Reading that G-code | ⚠️ It is outside the data folder: the read needs a permission prompt, which only works on the UI thread (an event on a background thread is denied). Grants are exact paths, so the prompt returns once per Orca session |

### Revised architecture

```
["Filament Saver" tab (pages)]
   questionnaire + sliders + geometry summary
        │ postMessage
        ▼
[Plugin Python]
   1. read current settings      → preset_bundle().full_config_value()
   2. analyze the mesh           → model().objects()[i].volumes()[j].mesh()
   3. rules engine (sections 1–9)
   4. build an explained diff    → show it in the tab
   5. "Apply": write preset "<current preset> · Filament Saver"
      into the local bundle + reload_local(bundle_id)
   6. the user selects the preset and slices
        │ GCodeExportFinished
        ▼
   7. read <name>.statistics.json → show actual grams before/after
```

**Optional (v2):** compare variants before applying by running the CLI in the background with `--export-statistics`. Requires process and thread permissions.

### Required permissions

- **Files:** write to the local bundle folder (inside the Orca data folder, where there should be no permission prompt; to be confirmed) and read `statistics.json` next to the exported G-code (user-chosen folder, triggers a prompt).
- **Process + thread:** v2 only (CLI).
- **Network:** none.

### Impact on the rules

- **Q3 (holes):** no local modifier. The reinforcement becomes +1 global wall.
- **Q11 (reorient):** becomes a suggestion with the computed angle.
- **Measuring savings:** before slicing, savings are an **estimate** computed from geometry and settings. The actual value comes from `statistics.json` after export.

Sources: [plugin development](https://www.orcaslicer.com/wiki/developer_reference/plugin_development/plugin_development), [Host API](https://www.orcaslicer.com/wiki/developer_reference/plugin_development/api_reference/host.html), [Slicing Pipeline](https://www.orcaslicer.com/wiki/developer_reference/plugin_development/api_reference/slicing.html), [lifecycle events](https://www.orcaslicer.com/wiki/developer_reference/plugin_development/api_reference/lifecycle_events.html), [Pages](https://www.orcaslicer.com/wiki/developer_reference/plugin_development/api_reference/pages.html), [Registry](https://www.orcaslicer.com/wiki/developer_reference/plugin_development/api_reference/registry.html), [2.5.0 Alpha release notes](https://www.orcaslicer.com/wiki/releases/release_2_5_0_alpha).
