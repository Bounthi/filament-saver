# Changelog

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
