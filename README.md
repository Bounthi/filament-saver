<p align="center"><img src="assets/logo.png" alt="Filament Saver logo" width="128"></p>

# Filament Saver

A plugin for [OrcaSlicer](https://github.com/OrcaSlicer/OrcaSlicer) 2.5 that adds a **Filament Saver** tab. Tell it what you are printing, and it suggests process settings that use less filament, shows why, and applies only the ones you accept.

- **Purpose and three sliders.** Pick what the part is for (decoration, functional, prototype, mechanical, container, miniature, large part) and set Strength, Finish and Reliability from 0 to 5. Walls, infill, shells, solid fill, brim, skirt, raft and supports follow them.
- **Material aware.** Rules adapt to the filament (PLA, Silk PLA, PETG, ABS/ASA, TPU, Nylon, PC), detected from the profile or picked by hand.
- **Only certain changes.** Every suggestion has a known effect, and Orca still decides where it applies. The plugin never guesses what the part needs (such as whether it needs supports) and never changes the part itself. Things only you know, like hidden faces or printing as a vase, are optional questions.
- **Apply, skip or undo.** One by one or all at once, with **Restore originals** for everything the plugin changed.
- **See the savings.** Print time, filament and cost of each slice, compared with the previous slice of the same parts. Optional spool price and currency conversion.
- **Save as a preset**, and the tab remembers your choices for each set of parts.
- English and Brazilian Portuguese, following Orca's language.

## Install

1. Download `filament_saver.py` from the [latest release](../../releases/latest).
2. In OrcaSlicer, open the **Plugins** window, click **Install local plugin** and pick the file.
3. Enable **Filament Saver** and accept the thread permission. The tab appears next to Prepare / Preview.

Other ways to install, permissions and details: [`plugin/README.md`](plugin/README.md). The rules behind every suggestion: [`docs/optimization-rules.md`](docs/optimization-rules.md). Changes per version: [`CHANGELOG.md`](CHANGELOG.md).

Requires OrcaSlicer 2.5 or later (Python plugins). Tested on macOS with OrcaSlicer 2.5.0-beta.

## Development

```bash
python3 -m unittest discover -s tests
```

## License

[MIT](LICENSE) © 2026 bounthi
