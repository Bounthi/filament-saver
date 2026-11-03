# Changelog

## Unreleased

**Projects saved with 0.1.0, 0.1.1 or the development version before them may still hold reduced supports**: a 30° threshold, organic style, a hollow or lightning support base, a single tree support wall, or (development version) On build plate only. Those versions changed supports and 0.1.2 stopped, but a saved project keeps what was applied, and Undo may not reach them. Check the Support tab of old projects, or set those values back to your preset's.

- Container: a note says the suggestions were not tested on containers that must hold liquid.
- Miniature: with supports on, notes warn when On build plate only is on, when small overhangs are ignored, and that organic branches get no brim of their own. Nothing is changed: whether the figure needs that support depends on the model.
- Miniature: a note when the layer height is above 0.16 mm, where faces and fine detail start to fade (miniature guides use 0.08–0.12 mm). The plugin does not change the layer height.
- Miniature never lowers the minimum sparse infill area below Orca's default of 15 mm², so thin limbs and weapons stay solid.
- Switching a preset while Apply is still running keeps the message that the suggestions are out of date.
- The tests now check that the tab's JavaScript parses, so a typo there can no longer ship as a blank tab.
- Portuguese texts about brims use Orca's own word, "borda", instead of "brim".
- The Miniature hint now reads "Fine detail; supports are left as they are" (it said "delicate supports", which the plugin never does).

## 0.1.3 (2026-11-01)

Rules follow the filaments the parts really print with, and Apply never writes suggestions worked out from settings that changed since.

- **Apply** refuses suggestions worked out from settings or filaments that changed in Orca since **Suggest changes**, instead of overwriting a hand edit or missing a material rule (for example, switching from PLA to ABS after the brim was reduced).
- Switching the filament or printer preset marks the suggestions out of date, like switching the process preset already did.
- PETG with a support material loaded (PVA, BVOH, or a filament flagged as support) keeps its support gap: those are printed with no gap on purpose.
- For materials that warp, Orca's auto brim is kept: Orca sizes it from the material and the part, so brim ears or a fixed width could hold less.
- Material rules only follow the filaments the parts print with: a filament that is loaded but used by no object (for example a PC kept in slot 2) no longer forces its brim and reliability on every part. Objects, parts, modifiers and the per-feature wall and infill filaments are read; with multi-material painting every loaded filament still counts. **Apply** also refuses suggestions if an object was moved to another filament since.
- With **Print as a vase**, a note says up front when Orca will refuse to slice it: more than one copy on a plate without the By object print sequence, or an object that prints with more than one filament.
- New running total under the estimate: the filament, time and cost saved between slices of the same parts before and after applying (or undoing) suggestions, with a **Reset** button.
- A note when the project mixes part materials that bond poorly (PLA or Silk PLA with PETG or ABS/ASA, PETG with ABS/ASA, nylon with any other), since a part can split where they meet.
- A damaged estimate file no longer leaves the tab blank.
- Releases check that `plugin/README.md` and `CHANGELOG.md` match the version, and the workflow uses `actions/checkout@v5`.

## 0.1.2 (2026-11-01)

Nothing that holds the print up is reduced on a guess, and Undo or Restore never overwrite your own edits.

- Brim and raft are no longer reduced unless you check the new question **The part holds on the bed by itself**, and never for materials that warp. Whether a part needs them depends on its geometry and the bed, which the plugin does not guess.
- Supports are no longer reduced: the threshold angle, ignoring small overhangs, the support style, the base pattern and spacing, and the tree support walls stay as you set them, since whether an overhang needs that support depends on the part. Only the support interface still follows the Finish slider.
- The prime tower brim is never lowered below Orca's default of 3 mm.
- Undo and Restore originals now keep the values of each process preset apart: restoring in one preset no longer writes values that belonged to another.
- **Apply all** and **Apply savings** no longer apply rows you skipped.
- Undo and Restore originals leave a setting alone if you changed it by hand after the plugin applied it, instead of overwriting your edit with the older value.
- A note says when an object has its own layer height, since top and bottom layer counts are worked out for the process layer height.
- The estimate card shows a time difference under a minute in seconds (it showed "+0m"), and a difference too small to show reads as no change.

## 0.1.1 (2026-11-01)

Safer suggestions: nothing the plugin suggests may make a print more likely to fail.

### Profiles you already tuned are kept

- A change that uses more filament is only suggested when the current value is below what the purpose or the material needs, and only up to that minimum. Those changes have their own section, **Needed for the purpose or the material**, and a new **Apply savings** button applies only the savings (**Apply all** still applies both).
- Undo and Restore originals go back to the value the setting had before the plugin's last change: an edit you made by hand after an earlier plugin change is no longer overwritten.
- Rows say when objects have their own value for the setting, which the process preset does not reach.

### No trade of something important for a little filament

- Layer and wall counts keep the same thickness at any layer height and nozzle (calibrated at 0.2 mm layers and a 0.4 mm nozzle): fine layers no longer get thin shells, and big nozzles no longer get extra walls. At least 2 shell layers and 2 walls. Reasons explain the count ("That is 1 mm: 4 layers at 0.28 mm layer height").
- The top shell keeps Orca's 0.6 mm minimum thickness.
- Sloped-wall reinforcement follows the higher of Strength and Finish, so visible parts keep it.
- At least 1 support interface layer at the lowest finish.
- "Faces pointing up will be hidden" counts the top layers that will actually print, so it never leaves fewer than 2 solid layers under the skipped one.
- Skirt and gap fill only go down; for materials that do not warp, the brim only goes down too (Orca's auto brim decides where it is needed).

### Materials

- For materials that warp (ABS/ASA, PA, PC), the brim is only raised to the minimum: a fuller or wider brim is kept, and a painted, inner-only, leading-edge or outer-and-inner brim is never replaced. The raft is kept for them too.
- PA11, PAHT and PPA (with CF/GF) follow the nylon rules; HIPS follows the ABS/ASA rules instead of being treated as a support material.
- Several filaments: support filaments (PVA, BVOH, or flagged as support) are skipped, and the strictest rule across all part filaments applies, instead of only the first filament's.

### Removed or fixed

- Suggestions that do not save filament: top surface pattern, and tree support style changes that did nothing (Orca's default is organic). Support style now only changes to a style that uses less material.
- Infill patterns the rules do not rank (triangles, cubic, crosshatch, …) are no longer swapped for another pattern of unknown filament use; only a pattern the purpose forbids is replaced, by the cheapest allowed one.
- The gap fill reason: it covers solid surfaces, not the gaps between walls.

### Tests

- A randomized test (`tests/test_sweep.py`) checks the plugin's guarantees on random profiles over Orca's full value ranges, and that suggesting again after applying everything suggests nothing more.

## 0.1.0 (2026-11-01)

First release.

### Suggestions

- Filament Saver tab in OrcaSlicer 2.5: pick what you are printing (decoration, functional, prototype, mechanical, container, miniature, large part) and set the Strength, Finish and Reliability sliders from 0 to 5.
- Strength: walls, sparse infill density and pattern, bottom layers, and the solid fill Orca adds on its own (sloped-wall reinforcement, gap fill, small solid areas).
- Finish: top layers, top surface pattern, one wall on top surfaces, support interface layers; no bottom support interface at V ≤ 3 and wider top interface spacing at V ≤ 1.
- Reliability:
  - brim, skirt, first-layer walls;
  - no raft below R5, one skirt layer when the draft shield is off;
  - with supports on: threshold angle, ignoring small overhangs, sparser normal supports, lighter support body (lightning base at R ≤ 1, hollow tree supports and one tree support wall at R ≤ 3);
  - narrower prime tower brim at R ≤ 3 in multicolor prints.
- Shell thickness is lowered when it would override the suggested layer count.
- Material rules (PLA, Silk PLA, PETG, ABS/ASA, TPU, Nylon, PC), with the family detected from the filament profile or picked by hand.
- Questions only the user can answer, off until checked: faces pointing up while printing will be hidden, print as a vase (applied, skipped and undone as one group with every setting Orca requires), and in multicolor prints purge into the infill or the supports.
- Only changes with a known effect: the plugin never guesses what the part needs and never changes the part itself.

### Applying

- Apply or skip each suggestion, or all at once; Undo per row and Restore originals, kept in plugin storage across reloads.
- Applies in place through the preset editor API, with a local preset bundle as fallback.
- Save the current process settings as a new preset in the Filament Saver group.
- Remembers the purpose, material, sliders and answers per set of parts.

### Estimate and cost

- Print time, filament and cost read from the G-code of each slice.
- Slices are only compared for the same parts on the same plate (a fingerprint of mesh, size and copies; modifiers and support enforcers or blockers are left out, so their effect is compared), with one comparison kept per set of parts.
- Optional spool price and weight per filament; empty keeps Orca's filament cost.
- Costs show their currency; conversion with daily ECB rates from Frankfurter only after you allow it with a checkbox (cached 12 h; only the currency code is sent).

### Other

- English and Brazilian Portuguese, following Orca's language; setting names match Orca's own translation.
- Install guide for macOS, Windows, Linux and Flatpak, plus Orca's **Install local plugin** button.
- Logo in the tab header and as the tab icon in Orca.
- Restore and the local-bundle fallback only write settings the plugin manages; numbers must be finite; unsafe preset names are refused.
