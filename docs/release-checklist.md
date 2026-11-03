# Release checklist

Run in OrcaSlicer before tagging a release. The unit tests (`python3 -m unittest discover -s tests`) cover the rules; this list covers what only shows up in Orca.

Setup: copy `plugin/filament_saver/filament_saver.py` into `orca_plugins/filament_saver/`, then in the Plugins window turn off the Orca Cloud copy, turn on the local one (off and on again reloads it). Use a project with one object and one filament, and note its current process values first. Undo everything at the end and switch back to the Orca Cloud copy.

## Suggest and apply

- [ ] The tab opens in Orca's language (English and Portuguese), with no untranslated text.
- [ ] **Suggest changes** lists rows with current → suggested values and reasons; the notes make sense for the purpose.
- [ ] **Apply** on one row, then **Apply** on another: both apply, no "settings changed" refusal.
- [ ] **Apply savings** applies only the "Save filament" section; skipped rows stay skipped.
- [ ] The values in Orca's process tabs match the suggested ones.

## Slice and estimate

- [ ] Slice: the card shows time, filament and cost; Orca shows no new warning.
- [ ] Filament goes down compared with the slice before applying.
- [ ] "With the suggestions" appears after the second slice of the same parts and follows apply/undo; **Reset** clears it.

## Undo and safety

- [ ] **Undo** on a row and **Restore originals** put back the exact previous values; slicing again gives the earlier grams (within 0.1 g).
- [ ] Edit a setting by hand after Apply, then **Restore originals**: the hand edit stays and the message says so.
- [ ] Edit a setting by hand after Suggest, then **Apply**: refused with "Settings changed in Orca…", nothing written.
- [ ] Switch the filament preset after Suggest: the tab says the suggestions are out of date and Apply is refused. Repeat with the printer preset.
- [ ] Switch the process preset: same.

## Special cases

- [ ] **Print as a vase**: the vase rows apply, skip and undo together; with two copies on the plate (print sequence By layer) the vase note appears at the top.
- [ ] **Miniature** with supports on: the notes about On build plate only, ignoring small overhangs and organic branches appear when those are set.
- [ ] A second filament loaded but used by no object: the note "No part prints with …" appears and its material rules are left out.

## Release

- [ ] Version in the file header, `VERSION`, `plugin/README.md` and a `## X.Y.Z (date)` section in `CHANGELOG.md` (the release workflow checks all four).
- [ ] Tag `vX.Y.Z`, push, create the GitHub release with the changelog section as notes; the Orca Cloud workflow ends with a 201 and the new version.
