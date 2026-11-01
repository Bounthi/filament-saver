<p align="center"><img src="../assets/logo.png" alt="Filament Saver logo" width="128"></p>

# Filament Saver plugin (v0.1.1)

OrcaSlicer 2.5 plugin that adds a **Filament Saver** tab to the main window. It implements the rules from [`docs/optimization-rules.md`](../docs/optimization-rules.md): presets, the three sliders, material rules and the per-purpose floors (sections 2–5, 7 and 9), plus three of the questions from section 6 (hidden top surfaces and multicolor purge). The other questions and the geometry rules are not implemented yet. Changes per version are in [`CHANGELOG.md`](../CHANGELOG.md).

## How it works

1. **Current estimate**: print time, filament (g) and cost of the last slice. The plugin API does not expose Orca's preview estimate, so the plugin reads the G-code Orca writes after each slice (no export needed). After the next slice it shows the difference against the previous slice **of the same parts on the same plate**: each slice is tagged with a fingerprint of the parts (mesh, size, number of copies) and the plate, so slices of another project, another plate, or parts that were added, removed or rescaled are never compared. Names, positions, rotations, modifiers and support enforcers or blockers do not count: they are settings or do not change the part, so their effect shows up in the comparison. The last two slices of up to 20 sets of parts are kept, so switching projects and back keeps each comparison.

   **Spool price (optional)**: under the estimate, each filament in the project has a spool price and a spool weight (1000 g when empty). With a price, that filament's cost is grams × price / spool weight; without one, the card keeps the cost Orca computes from the filament profile (`filament_cost` per kg). The label under Cost says which one is shown. Prices are saved per filament preset in the plugin storage (`prices.json`) and applied when the card is shown, so changing a price updates every slice alike and never appears as a difference between slices.

   **Currency**: pick the currency of your spool prices and of Orca's filament cost (Orca does not store one; BRL by default in Portuguese, USD otherwise) and the currency to show the cost in. Costs are formatted with the currency symbol. When the two differ, the plugin can convert with the European Central Bank's daily reference rates from [Frankfurter](https://frankfurter.dev) (free, no key, 30 currencies), but only after you check **Allow fetching exchange rates**: until then nothing is sent and the cost stays in the prices' currency. Once allowed, only the currency code is sent, and rates are cached for 12 hours in `rates.json`. Unchecking the box stops both the requests and the conversion, even with cached rates. Offline or without Orca's network permission, the cost stays in the prices' currency and the card offers **Try again**.
2. **What are you printing?**: pick a purpose (Decoration, Functional, Prototype, Mechanical, Container, Miniature, Large part). The sliders jump to that purpose's defaults; adjust Strength, Finish and Reliability if needed, then click **Suggest changes**. The **Material** selector shows the family detected from the filament profile; pick another one if the profile is generic or wrong. Under the sliders, check only what you are sure of: **Faces pointing up while printing will be hidden** skips the last top layer (for example the back of a wall sign printed face down; not for boxes, figures or parts with visible ledges), and with more than one filament **Purge into the infill** / **Purge into the supports** send color-change purge there instead of the prime tower. Unchecked boxes change nothing. **Print as a vase** turns on spiral vase mode for an open container, together with the settings Orca requires for it; those rows are applied, skipped and undone as one group.

   Besides walls, infill and shells, the suggestions cover shell thickness (which otherwise overrides the layer count), brim ears for materials that warp, no raft below top reliability, a single skirt layer when there is no draft shield, and, when supports are on, sparser support, a lighter support body and interface, and ignoring small overhangs. Solid fill Orca adds on its own (sloped-wall reinforcement, gap fill between walls, small solid areas) and the first-layer walls follow the sliders too. The plugin never guesses whether the part needs something, such as supports; Orca keeps deciding where every setting applies.
3. **Suggested changes**: one row per setting, showing current → suggested value, the reason, and whether it saves filament or uses more. Changes are split into **Save filament** and **Needed for the purpose or the material**: an increase only appears when the current value is below what the purpose or the material needs, and then only up to that minimum, so a profile you already made leaner is kept. Use **Apply** / **Skip** per row, **Apply savings** for the first section, or **Apply all** / **Skip all**.
4. **Restore**: **Undo** on an applied row, or **Restore originals** for all of them, puts back the value each setting had before Filament Saver first changed it. Only settings changed by Filament Saver are restored. The original value is the one before Filament Saver's change; it is kept if you click **Suggest changes** again, but if you edit a setting by hand after the plugin changed it, your edit becomes the value to go back to. They are saved in the plugin's storage folder (`originals.json`), so they survive a restart of Orca or a reload of the plugin. In place, restoring only works on the preset the changes were made to.
5. **Save as a new preset**: below the suggestions, type a name (or keep the suggested one) and click **Save preset**. The process settings as they are now, unsaved edits and applied suggestions included, are saved as a process preset in the Filament Saver group of the process list, on top of the same system preset. Saving again under a name the plugin created updates it; names of other presets are refused. The OrcaSlicer plugin API cannot save presets itself, so this uses the local bundle (see below).

Applying uses one of two paths, shown under the sliders:

- **In place** (when Orca has `orca.host.preset.process.begin_update`): changes become unsaved edits of the active process preset. Save or discard them in Orca as usual.
- **Preset bundle** (fallback): accepted changes are written to a process preset `<system preset> - Filament Saver` in the local bundle `filament-saver`, together with your existing edits. Select that preset in the process list.

If you switch process presets, the suggestions are marked out of date until you click **Suggest changes** again.

**Object settings.** When an object (or one of its parts) has its own value for a suggested setting, for example its own wall loops, the process preset does not reach it. The row says which objects keep their own value, and a note explains it.

**Choices per set of parts.** Each time you click **Suggest changes**, the purpose, material, sliders and answers are saved for the parts in Orca (the same fingerprint the estimate uses). When you open the project again, or come back to those parts, the tab brings those choices back. Up to 50 sets of parts are remembered (`forms.json` in the plugin storage).

## Language

The tab follows the language set in Orca's preferences: Brazilian Portuguese for any Portuguese setting, English otherwise. Setting names use Orca's own translation, so they match what you see in the process settings. Add a language by adding an entry to `TRANSLATIONS` in `filament_saver.py`, keyed by the English text, and a case to `language_for`; the tests list any text left untranslated.

## Permissions

- **Network**: not requested at load. After you allow exchange rates in the tab, the first request to `https://api.frankfurter.dev` makes Orca ask for network access to that host; allow it and click **Try again**.
- **Threads**: requested once at load. In-place changes run on a worker thread because the write API must not run on the UI thread.
- **Reading the sliced G-code**: the file lives in the system temp folder, outside Orca's data folder, so Orca asks once per session (the temp path changes on every launch). The read runs on the UI thread so the prompt can appear.

## Install

The plugin is a single file, `plugin/filament_saver/filament_saver.py`, and runs the same on macOS, Windows and Linux. Use one of the two options below, not both, or Orca lists the plugin twice.

### Option 1: from inside Orca (any system)

1. Open the **Plugins** window.
2. Click **Install local plugin** and pick `filament_saver.py`.

### Option 2: copy the folder

Orca loads plugins from the `orca_plugins` folder inside its data folder:

| System | Data folder |
|---|---|
| macOS | `~/Library/Application Support/OrcaSlicer` |
| Windows | `%APPDATA%\OrcaSlicer` |
| Linux | `~/.config/OrcaSlicer` (or `$XDG_CONFIG_HOME/OrcaSlicer`) |
| Linux, Flatpak | `~/.var/app/com.orcaslicer.OrcaSlicer/config/OrcaSlicer` |
| Portable install | the `data_dir` folder next to the OrcaSlicer executable (next to `OrcaSlicer.app` on macOS), when it exists |

Run from the repository folder:

macOS:

```bash
mkdir -p "$HOME/Library/Application Support/OrcaSlicer/orca_plugins"
cp -R plugin/filament_saver "$HOME/Library/Application Support/OrcaSlicer/orca_plugins/"
```

Windows (PowerShell):

```powershell
New-Item -ItemType Directory -Force "$env:APPDATA\OrcaSlicer\orca_plugins" | Out-Null
Copy-Item -Recurse -Force plugin\filament_saver "$env:APPDATA\OrcaSlicer\orca_plugins\"
```

Linux:

```bash
mkdir -p "${XDG_CONFIG_HOME:-$HOME/.config}/OrcaSlicer/orca_plugins"
cp -R plugin/filament_saver "${XDG_CONFIG_HOME:-$HOME/.config}/OrcaSlicer/orca_plugins/"
```

Linux, Flatpak:

```bash
mkdir -p ~/.var/app/com.orcaslicer.OrcaSlicer/config/OrcaSlicer/orca_plugins
cp -R plugin/filament_saver ~/.var/app/com.orcaslicer.OrcaSlicer/config/OrcaSlicer/orca_plugins/
```

### Enable it

In Orca: open the Plugins window, enable **Filament Saver** and accept the thread permission. The **Filament Saver** tab appears next to Prepare / Preview. After editing the file, install or copy it again and choose **Reload** in the plugin's right-click menu.

Only tested on macOS so far (OrcaSlicer 2.5.0-beta). The folders above come from Orca's source (`default_data_dir()` on Linux, `wxStandardPaths::GetUserDataDir()` on macOS and Windows, `local_plugin_root()` for `orca_plugins`).

## Tests

The rules engine and the G-code reader run outside Orca:

```bash
python3 -m unittest discover -s tests
```

The suggestions are pinned, in English, by `tests/filament_snapshot.json` (every purpose × material × settings variant). The tests also check that every text has a pt_BR translation with the same placeholders, and that settings removed on purpose (layer height, speeds, ironing, infill combination, supports on build plate only) are never suggested. `tests/test_sweep.py` runs 2000 random profiles over Orca's full value ranges and checks the plugin's guarantees (minimums kept, brims of materials that warp never reduced, TPU infill untouched, a stable result after applying everything). Regenerate the snapshot with `python3 tests/test_rules.py --update-snapshot` only when a filament rule changes on purpose.

## Try it

1. Load a model and slice it: the estimate card fills in (allow the read prompt).
2. Pick a purpose, click **Suggest changes**, and apply some changes.
3. Slice again: the card shows the new time, filament and cost, with the difference.

The tab sits in the main tab bar next to Prepare / Preview / Device. If the bar is full, plugin tabs collapse into a dropdown on the last plugin tab, or press Space in Prepare to open the Speed Dial and search for "Filament Saver".
