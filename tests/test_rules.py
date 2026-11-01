"""Tests for the Filament Saver rules engine, run outside OrcaSlicer.

    python3 -m unittest discover tests            # run
    python3 tests/test_rules.py --update-snapshot # rewrite the suggestions snapshot

The suggestions are pinned by a snapshot so that no change can alter them unnoticed.
"""

import ast
import itertools
import json
import os
import re
import shutil
import sys
import threading
import tempfile
import types
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAPSHOT = os.path.join(ROOT, "tests", "filament_snapshot.json")


def _load_plugin():
    """Import the plugin with a minimal stand-in for Orca's ``orca`` module."""
    orca = types.ModuleType("orca")
    base = type("Base", (), {"__init__": lambda self, *a, **k: None})
    orca.base = base
    orca.plugin = lambda cls: cls
    orca.pages = types.SimpleNamespace(PagesPluginCapabilityBase=base)
    orca.register_capability = lambda cls: None
    orca.request_permissions = lambda **kwargs: None
    sys.modules["orca"] = orca
    sys.path.insert(0, os.path.join(ROOT, "plugin", "filament_saver"))
    import filament_saver
    return filament_saver


fs = _load_plugin()

# Settings of the test machine (Elegoo Centauri Carbon, 0.20mm Standard, PETG), from a sliced G-code.
BASE = {
    "layer_height": "0.2", "wall_loops": "2", "sparse_infill_density": "10%", "sparse_infill_pattern": "lightning",
    "infill_combination": "0", "top_shell_layers": "3", "top_shell_thickness": "1", "bottom_shell_layers": "3",
    "bottom_shell_thickness": "0", "top_surface_pattern": "monotonicline", "only_one_wall_top": "1",
    "ironing_type": "no ironing", "inner_wall_speed": "200", "sparse_infill_speed": "200",
    "internal_solid_infill_speed": "250", "brim_type": "auto_brim", "brim_width": "5", "skirt_loops": "0",
    "support_threshold_angle": "30", "support_style": "default", "support_interface_top_layers": "2",
    "support_top_z_distance": "0.2", "support_on_build_plate_only": "0", "support_remove_small_overhang": "1",
    "support_base_pattern_spacing": "2.5", "enable_support": "0", "support_type": "normal(auto)",
    "line_width": "0.42", "inner_wall_line_width": "0.45", "sparse_infill_line_width": "0.45",
    "internal_solid_infill_line_width": "0.42", "nozzle_diameter": "0.4", "max_layer_height": "0.28",
    "filament_max_volumetric_speed": "11", "ensure_vertical_shell_thickness": "ensure_all",
    "gap_fill_target": "everywhere", "minimum_sparse_infill_area": "15", "only_one_wall_first_layer": "0",
}
# Settings the newer rules read, with values that trigger them; not in BASE so the snapshot stays as it was.
EXTRA = {"raft_layers": "2", "skirt_height": "2", "draft_shield": "disabled", "support_base_pattern": "rectilinear",
         "tree_support_wall_count": "2", "support_interface_bottom_layers": "-1", "support_interface_spacing": "0.5",
         "spiral_mode": "0", "enforce_support_layers": "0", "detect_thin_wall": "1", "overhang_reverse": "1",
         "timelapse_type": "1", "enable_wrapping_detection": "0", "top_surface_density": "100%",
         "flush_into_infill": "0", "flush_into_support": "0"}
SOLID_KEYS = {"ensure_vertical_shell_thickness", "gap_fill_target", "minimum_sparse_infill_area",
              "only_one_wall_first_layer"}
VARIANTS = {
    "petg_no_support": dict(BASE),
    "normal_support": {**BASE, "enable_support": "1"},
    "tree_support": {**BASE, "enable_support": "1", "support_type": "tree(auto)"},
    "slow_printer": {**BASE, "inner_wall_speed": "60,60", "sparse_infill_speed": "100,100",
                     "internal_solid_infill_speed": "100,100", "ironing_type": "top", "infill_combination": "0"},
}
MATERIALS = ["PLA", "PLA Silk", "PETG", "ABS", "ASA", "TPU", "PA-CF", "PC", "PVA", "XYZ"]
# Settings the plugin must never suggest: time-only settings (the time mode was removed) and
# rules removed by the certainty rule.
NEVER_SUGGESTED = {"layer_height", "infill_combination", "ironing_type", "inner_wall_speed",
                   "sparse_infill_speed", "internal_solid_infill_speed", "support_on_build_plate_only"}


def _filament_cases():
    """Every purpose, slider combination, material, override and settings variant."""
    for variant, purpose, material in itertools.product(VARIANTS, fs.PURPOSES, MATERIALS):
        for s, v, r in itertools.product(range(6), repeat=3):
            yield variant, purpose, material, None, s, v, r
    for variant, purpose, override in itertools.product(VARIANTS, fs.PURPOSES, fs.MATERIAL_CHOICES):
        spec = fs.PURPOSES[purpose]
        yield variant, purpose, "PETG", override, spec["S"], spec["V"], spec["R"]


def _run(variant, purpose, material, override, s, v, r):
    return fs.suggest(dict(VARIANTS[variant]), purpose, s, v, r, material, material, family=override)


def _snapshot():
    """Suggestions for each purpose's defaults, every material and every variant."""
    out = {}
    for variant, purpose, material in itertools.product(VARIANTS, fs.PURPOSES, MATERIALS):
        spec = fs.PURPOSES[purpose]
        result = _run(variant, purpose, material, None, spec["S"], spec["V"], spec["R"])
        out["%s|%s|%s" % (variant, purpose, material)] = {
            "changes": [[c["key"], c["current"], c["suggested"], c["effect"], c["reason"]] for c in result["changes"]],
            "notes": result["notes"],
        }
    return out


class SuggestionsTest(unittest.TestCase):
    def test_never_suggests_removed_settings(self):
        for case in _filament_cases():
            keys = {c["key"] for c in _run(*case)["changes"]}
            self.assertFalse(keys & NEVER_SUGGESTED, case)

    def test_matches_snapshot(self):
        with open(SNAPSHOT, encoding="utf-8") as stream:
            expected = json.load(stream)
        actual = _snapshot()
        self.assertEqual(sorted(actual), sorted(expected))
        for key in expected:
            self.assertEqual(actual[key], expected[key], key)


class SolidFillTest(unittest.TestCase):
    """Solid fill Orca adds on its own follows the Strength and Reliability sliders."""

    def _targets(self, s, r, material="PETG", v=None):
        result = fs.suggest(dict(BASE), "decoration", s, s if v is None else v, r, material)
        return {c["key"]: c["suggested"] for c in result["changes"] if c["key"] in SOLID_KEYS}

    def test_lowest_strength_and_reliability(self):
        self.assertEqual(self._targets(0, 0), {
            "ensure_vertical_shell_thickness": "none", "gap_fill_target": "topbottom",
            "minimum_sparse_infill_area": "5", "only_one_wall_first_layer": "1"})

    def test_highest_strength_keeps_orca_defaults(self):
        self.assertEqual(self._targets(5, 5), {})

    def test_high_finish_keeps_sloped_walls_reinforced(self):
        self.assertNotIn("ensure_vertical_shell_thickness", self._targets(0, 0, v=4))

    def test_gap_fill_only_goes_down(self):
        result = fs.suggest({**BASE, "gap_fill_target": "nowhere"}, "mechanical", 5, 1, 4, "PLA")
        self.assertNotIn("gap_fill_target", {c["key"] for c in result["changes"]})

    def test_tpu_keeps_minimum_sparse_infill_area(self):
        self.assertNotIn("minimum_sparse_infill_area", self._targets(0, 0, "TPU"))


class MaterialFamilyTest(unittest.TestCase):
    """Orca's material types (src/libslic3r/MaterialType.cpp) map to the right rules."""

    def test_orca_material_types(self):
        expected = {
            "pa": ["PA", "PA-CF", "PA6-GF", "PA11", "PA11-CF", "PA12-CF", "PAHT", "PAHT-GF", "PPA-CF", "PPA-GF"],
            "abs_asa": ["ABS", "ABS-GF", "ASA", "ASA-AERO", "HIPS"],
            "support": ["PVA", "BVOH"],
            "petg": ["PETG", "PETG-CF", "PET", "PCTG"],
            "pc": ["PC", "PC-ABS", "PC-CF"],
            "tpu": ["TPU", "FLEX"],
            "pla": ["PLA", "PLA-CF", "PLA-AERO"],
            "unknown": ["PEEK", "PPS", "PP", "PEI-1010"],
        }
        for family, kinds in expected.items():
            for kind in kinds:
                self.assertEqual(fs.material_family(kind), family, kind)
        self.assertEqual(fs.material_family("PLA", "Elegoo PLA Silk"), "silk")

    def _result(self, primary, others, r=2, override=None):
        current = {**BASE, "enable_support": "1", "support_top_z_distance": "0.2", "support_threshold_angle": "45"}
        return fs.suggest(current, "functional", 3, 2, r, primary, primary, family=override,
                          filaments=1 + len(others), other_filaments=[(kind, kind) for kind in others])

    def test_strictest_rule_across_part_filaments(self):
        result = self._result("PLA", ["PETG", "PC"])
        targets = {c["key"]: c["suggested"] for c in result["changes"]}
        self.assertEqual((targets["brim_type"], targets["brim_width"]), ("outer_only", "8"))  # PC
        self.assertEqual(targets["support_top_z_distance"], "0.25")  # PETG
        threshold = next(c for c in result["changes"] if c["key"] == "support_threshold_angle")
        self.assertIn("Reliability 4/5 (raised for PC)", threshold["reason"])
        self.assertEqual(result["families"], ["pc", "petg", "pla"])
        self.assertTrue(any("Several materials" in n for n in result["notes"]))

    def test_single_filament_is_unchanged(self):
        self.assertEqual(self._result("PETG", [])["families"], ["petg"])

    def test_override_replaces_every_material(self):
        result = self._result("PLA", ["PC"], override="petg")
        self.assertEqual(result["families"], ["petg"])

    def test_support_filaments_are_skipped(self):
        cfg = dict(BASE)
        make = lambda name, kind, support="0": types.SimpleNamespace(
            name=name, config_value={"filament_type": kind, "filament_is_support": support}.get)
        preset = types.SimpleNamespace(name="P", is_dirty=False, config_value=cfg.get)
        bundle = types.SimpleNamespace(
            prints=types.SimpleNamespace(edited_preset=lambda: preset, selected_preset=lambda: preset),
            current_filament_presets=lambda: [make("Soluble", "PVA"), make("Breakaway", "PLA", "1"),
                                              make("Elegoo PETG", "PETG"), make("Elegoo ABS", "ABS")])
        fs.orca.host = types.SimpleNamespace(preset_bundle=lambda: bundle)
        try:
            context = fs.FilamentSaverPage()._context()
        finally:
            del fs.orca.host
        self.assertEqual((context["filament_name"], context["other_filaments"], context["skipped_support"]),
                         ("Elegoo PETG", [("ABS", "Elegoo ABS")], True))


class QuestionsTest(unittest.TestCase):
    """Changes that depend on what only the user knows happen only when they confirm it."""

    MULTI = {**BASE, "top_surface_density": "100%", "flush_into_infill": "0", "flush_into_support": "0",
             "enable_prime_tower": "1"}

    def _targets(self, answers, filaments=2, **overrides):
        current = {**self.MULTI, **overrides}
        result = fs.suggest(current, "decoration", 1, 4, 3, "PETG", answers=answers, filaments=filaments)
        return {c["key"]: c["suggested"] for c in result["changes"]}, result["notes"]

    def test_no_answers_change_nothing(self):
        targets, _ = self._targets({}, enable_support="1")
        self.assertFalse({"top_surface_density", "flush_into_infill", "flush_into_support"} & set(targets))

    def test_hidden_top_skips_last_top_layer(self):
        targets, _ = self._targets({"top_hidden": True})
        self.assertEqual(targets["top_surface_density"], "0%")
        self.assertNotIn("top_surface_pattern", targets)

    def test_purge_into_infill_and_support(self):
        targets, _ = self._targets({"purge_infill": True, "purge_support": True}, enable_support="1")
        self.assertEqual((targets["flush_into_infill"], targets["flush_into_support"]), ("1", "1"))

    def test_purge_needs_several_filaments(self):
        targets, _ = self._targets({"purge_infill": True}, filaments=1)
        self.assertNotIn("flush_into_infill", targets)

    def test_purge_into_support_needs_supports(self):
        targets, _ = self._targets({"purge_support": True})
        self.assertNotIn("flush_into_support", targets)

    def test_purge_needs_prime_tower(self):
        targets, notes = self._targets({"purge_infill": True}, enable_prime_tower="0")
        self.assertNotIn("flush_into_infill", targets)
        self.assertTrue(any("prime tower" in n for n in notes))

    def test_purge_into_infill_needs_one_material_family(self):
        current = dict(self.MULTI, enable_support="1")
        result = fs.suggest(current, "decoration", 1, 4, 3, "PETG", filaments=2, same_family=False,
                            answers={"purge_infill": True, "purge_support": True})
        keys = {c["key"] for c in result["changes"]}
        self.assertNotIn("flush_into_infill", keys)
        self.assertIn("flush_into_support", keys)
        self.assertTrue(any("different materials" in n for n in result["notes"]))

    def test_purge_effect_is_saving(self):
        result = fs.suggest(dict(self.MULTI), "decoration", 1, 4, 3, "PETG",
                            answers={"purge_infill": True}, filaments=2)
        change = next(c for c in result["changes"] if c["key"] == "flush_into_infill")
        self.assertEqual(change["effect"], "saves")


class SupportRaftSkirtTest(unittest.TestCase):
    """Raft, skirt height and support body follow the sliders, and only ever use less filament."""

    def _targets(self, s=1, v=4, r=3, **overrides):
        result = fs.suggest({**BASE, **EXTRA, **overrides}, "decoration", s, v, r, "PLA")
        return {c["key"]: (c["suggested"], c["effect"]) for c in result["changes"]}

    def test_brim_and_skirt_only_go_down(self):
        self.assertNotIn("skirt_loops", self._targets(r=4, skirt_loops="0"))
        self.assertEqual(self._targets(r=0, skirt_loops="3")["skirt_loops"], ("0", "saves"))
        self.assertNotIn("brim_type", self._targets(r=5))  # Orca's auto brim stays for PLA
        self.assertEqual(self._targets(r=0)["brim_type"], ("no_brim", "saves"))

    def test_raft_kept_for_materials_that_warp(self):
        result = fs.suggest({**BASE, **EXTRA}, "functional", 3, 2, 3, "PC")
        self.assertNotIn("raft_layers", {c["key"] for c in result["changes"]})

    def test_top_keeps_orca_minimum_thickness(self):
        targets = self._targets(v=0, layer_height="0.12", top_shell_thickness="1")
        self.assertEqual(targets["top_shell_thickness"][0], "0.6")
        self.assertNotIn("top_shell_thickness", self._targets(v=0, layer_height="0.12", top_shell_thickness="0.6"))

    def test_support_style_only_when_it_saves(self):
        tree = dict(enable_support="1", support_type="tree(auto)")
        self.assertNotIn("support_style", self._targets(r=3, support_style="default", **tree))
        self.assertEqual(self._targets(r=3, support_style="tree_strong", **tree)["support_style"], ("organic", "saves"))
        self.assertEqual(self._targets(r=0, enable_support="1", support_style="default")["support_style"], ("snug", "saves"))
        self.assertNotIn("support_style", self._targets(r=3, enable_support="1", support_style="default"))

    def test_raft_off_below_top_reliability(self):
        self.assertEqual(self._targets(r=4)["raft_layers"], ("0", "saves"))
        self.assertNotIn("raft_layers", self._targets(r=5))

    def test_skirt_height_needs_draft_shield_off(self):
        self.assertEqual(self._targets(skirt_loops="1")["skirt_height"], ("1", "saves"))
        self.assertNotIn("skirt_height", self._targets(skirt_loops="1", draft_shield="enabled"))
        self.assertNotIn("skirt_height", self._targets(r=0))  # no skirt at all

    def test_normal_support_body_and_interfaces(self):
        low = self._targets(v=0, r=0, enable_support="1")
        self.assertEqual(low["support_base_pattern"], ("lightning", "saves"))
        self.assertEqual(low["support_interface_bottom_layers"], ("0", "saves"))
        self.assertEqual(low["support_interface_spacing"], ("0.8", "saves"))
        high = self._targets(v=5, r=5, enable_support="1")
        for key in ("support_base_pattern", "support_interface_bottom_layers", "support_interface_spacing"):
            self.assertNotIn(key, high)

    def test_wider_interface_spacing_is_kept(self):
        self.assertNotIn("support_interface_spacing", self._targets(v=0, enable_support="1", support_interface_spacing="1.2"))

    def test_tree_support_body(self):
        tree = self._targets(r=3, enable_support="1", support_type="tree(auto)")
        self.assertEqual(tree["support_base_pattern"], ("hollow", "saves"))
        self.assertEqual(tree["tree_support_wall_count"], ("1", "saves"))
        self.assertNotIn("support_base_pattern", self._targets(r=3, enable_support="1", support_type="tree(auto)",
                                                               support_base_pattern="default"))

    def test_support_rules_need_supports_on(self):
        targets = self._targets(v=0, r=0)
        for key in ("support_base_pattern", "support_interface_bottom_layers", "support_interface_spacing",
                    "tree_support_wall_count"):
            self.assertNotIn(key, targets)


class VaseTest(unittest.TestCase):
    """Vase mode comes only from the user's answer, with everything Orca requires for it, as one group."""

    def _result(self, answers, purpose="mechanical", **overrides):
        current = {**BASE, **EXTRA, "enable_support": "1", **overrides}
        return fs.suggest(current, purpose, *(fs.PURPOSES[purpose][k] for k in "SVR"), "PETG", answers=answers)

    def test_not_without_answer(self):
        keys = {c["key"] for c in self._result({})["changes"]}
        self.assertNotIn("spiral_mode", keys)
        self.assertNotIn("enable_support", keys)

    def test_vase_group(self):
        changes = {c["key"]: c for c in self._result({"vase": True, "top_hidden": True})["changes"]}
        expected = {"spiral_mode": "1", "wall_loops": "1", "top_shell_layers": "0", "sparse_infill_density": "0%",
                    "enable_support": "0", "detect_thin_wall": "0", "overhang_reverse": "0", "timelapse_type": "0"}
        for key, value in expected.items():
            self.assertEqual((changes[key]["suggested"], changes[key]["group"]), (value, "vase"), key)
        # Already as Orca needs them: not listed.
        self.assertNotIn("enforce_support_layers", changes)
        self.assertNotIn("enable_wrapping_detection", changes)
        # No floor brings walls back, and settings that no longer apply are gone.
        for key in fs.VASE_DROPS + ("support_threshold_angle", "support_base_pattern"):
            self.assertNotIn(key, changes)
        self.assertEqual(changes["spiral_mode"]["effect"], "saves")
        self.assertEqual(changes["timelapse_type"]["values"], {"0": "Traditional", "1": "Smooth"})

    def test_other_changes_have_no_group(self):
        changes = self._result({"vase": True})["changes"]
        self.assertEqual({c["group"] for c in changes if c["key"] in ("brim_type", "bottom_shell_layers")}, {""})


class TranslationTest(unittest.TestCase):
    """Every text the user sees has a pt_BR entry with the same placeholders."""

    PT = fs.TRANSLATIONS["pt_BR"]

    def _rule_texts(self):
        """Texts suggest() looks up, over every case, answer and multicolor setup."""
        requested = set()
        original = fs.translator

        def recording(lang):
            def lookup(text):
                requested.add(text)
                return text
            return lookup

        fs.translator = recording
        try:
            answers = {"top_hidden": True, "vase": True, "purge_infill": True, "purge_support": True}
            for variant, purpose, material, override, s, v, r in _filament_cases():
                fs.suggest({**VARIANTS[variant], **EXTRA}, purpose, s, v, r, material, material, family=override)
                spec = fs.PURPOSES[purpose]
                if (s, v, r) != (spec["S"], spec["V"], spec["R"]):
                    continue  # answers are checked at each purpose's default sliders
                for same_family, tower in itertools.product((True, False), ("1", "0")):
                    current = {**VARIANTS[variant], **EXTRA, "enable_prime_tower": tower}
                    fs.suggest(current, purpose, s, v, r, material, material, family=override, answers=answers,
                               filaments=2, same_family=same_family, lang="pt_BR")
            # Thickness scaling only shows at other layer heights and nozzles.
            fs.suggest({**BASE, "layer_height": "0.12", "nozzle_diameter": "0.2"}, "miniature", 1, 5, 3, "PLA", lang="pt_BR")
        finally:
            fs.translator = original
        return requested

    def _page_texts(self):
        with open(os.path.join(ROOT, "plugin", "filament_saver", "filament_saver.py"), encoding="utf-8") as stream:
            source = stream.read()
        texts = {ast.literal_eval(m) for m in re.findall(r'self\._t\(("(?:[^"\\]|\\.)*")\)', source)}
        page = source[source.index('PAGE = r"""'):]
        texts |= {m.replace("\\'", "'") for m in re.findall(r"\bT\('((?:[^'\\]|\\.)*)'", page)}
        texts |= set(re.findall(r"data-t>([^<]+)<", page))
        texts |= {m.replace("\\'", "'") for m in re.findall(r"(?:label|hint): '((?:[^'\\]|\\.)*)'", page)}
        return texts

    def test_every_text_is_translated(self):
        texts = self._rule_texts() | self._page_texts()
        texts |= {label for _, label in fs.SETTINGS} | set(fs.FAMILY_LABELS.values())
        texts |= {text for spec in fs.PURPOSES.values() for text in (spec["label"], spec["hint"])}
        texts |= set(fs.BRIM_TEXT.values()) | set(fs.PATTERN_NOTES.values()) | set(fs.VERTICAL_SHELL_TEXT.values())
        texts |= {name for names in fs.VALUE_NAMES.values() for name in names.values()}
        self.assertEqual(sorted(texts - set(self.PT)), [])

    def test_placeholders_match(self):
        for english, translated in self.PT.items():
            for pattern in (r"%[-#0 +]*\d*(?:\.\d+)?[sdgfr%]", r"\{\w+\}"):
                self.assertEqual(re.findall(pattern, english), re.findall(pattern, translated), english)

    def test_language_for_orca_codes(self):
        self.assertEqual([fs.language_for(c) for c in ("pt_BR", "pt", "en_US", "de_DE", "", None)],
                         ["pt_BR", "pt_BR", "en", "en", "en", "en"])

    def test_suggestions_in_portuguese(self):
        result = fs.suggest({**BASE, "wall_loops": "5"}, "functional", 3, 2, 3, "PETG", lang="pt_BR")
        walls = next(c for c in result["changes"] if c["key"] == "wall_loops")
        self.assertEqual(walls["label"], "Voltas da parede")
        self.assertTrue(walls["reason"].startswith("Resistência 3/5"))


class OriginalsStorageTest(unittest.TestCase):
    """Values to restore survive a reload of Orca or the plugin."""

    def setUp(self):
        self.storage = tempfile.mkdtemp()
        fs.orca.host = types.SimpleNamespace(plugin=types.SimpleNamespace(storage=lambda: self.storage))

    def tearDown(self):
        del fs.orca.host
        shutil.rmtree(self.storage)

    def _page(self):
        page = fs.FilamentSaverPage()
        page._load_originals()
        return page

    def test_empty_without_file(self):
        page = self._page()
        self.assertEqual((page.originals, page.originals_source, page.bundle_values), ({}, None, {}))

    def test_round_trip(self):
        page = self._page()
        page.originals = {"wall_loops": "3", "sparse_infill_density": "15%"}
        page.originals_source = "0.20mm Standard @Elegoo CC"
        page.bundle_values = {"wall_loops": "2"}
        page._save_originals()
        reloaded = self._page()
        self.assertEqual(reloaded.originals, page.originals)
        self.assertEqual(reloaded.originals_source, page.originals_source)
        self.assertEqual(reloaded.bundle_values, page.bundle_values)

    def test_source_cleared_when_nothing_to_restore(self):
        fs._write_originals("Some preset", {}, {})
        self.assertIsNone(self._page().originals_source)

    def test_undo_respects_edits_made_after_the_plugin(self):
        """Undo goes back to the value before the plugin's change; if the user edited the setting
        after an earlier plugin change, their edit is that value."""
        page = self._page()
        page.post_message = lambda message: None
        page._write = lambda pairs, done, failed, message: done("P")
        page.mode = lambda: "bundle"
        page.source = "P"

        def apply(current, suggested):
            page.result = {"changes": [{"id": "sparse_infill_density", "key": "sparse_infill_density", "current": current,
                                        "suggested": suggested, "status": "pending"}]}
            page._apply(["sparse_infill_density"])

        apply("15%", "10%")  # the plugin writes 10%
        apply("10%", "5%")   # re-suggest on the plugin's own value: the original stays 15%
        self.assertEqual(page.originals["sparse_infill_density"], "15%")
        apply("12%", "5%")   # the user set 12% by hand in between
        self.assertEqual(page.originals["sparse_infill_density"], "12%")
        reloaded = self._page()  # survives a reload
        self.assertEqual((reloaded.originals, reloaded.applied), ({"sparse_infill_density": "12%"}, {"sparse_infill_density": "5%"}))

    def test_only_known_settings_are_restored(self):
        """A tampered file must not turn Restore into a way to set post_process (runs scripts)."""
        with open(os.path.join(self.storage, fs.ORIGINALS_FILE), "w") as stream:
            json.dump({"source": "P", "originals": {"post_process": "/tmp/evil.sh", "wall_loops": "3"},
                       "bundle_values": {"process_change_extrusion_role_gcode": "M999", "wall_loops": "2"}}, stream)
        page = self._page()
        self.assertEqual((page.originals, page.bundle_values), ({"wall_loops": "3"}, {"wall_loops": "2"}))

    def test_corrupt_file_is_ignored(self):
        with open(os.path.join(self.storage, fs.ORIGINALS_FILE), "w") as stream:
            stream.write("{not json")
        self.assertEqual(self._page().originals, {})


def _volume(facets=12, size=1.0, scale=(1.0, 1.0, 1.0), kind="MODEL_PART"):
    return types.SimpleNamespace(type=lambda: "ModelVolumeType." + kind, facets_count=lambda: facets,
                                 volume=lambda: size, scaling_factor=lambda: scale,
                                 is_model_part=lambda: kind == "MODEL_PART",
                                 is_negative_volume=lambda: kind == "NEGATIVE_VOLUME")


def _object(volumes, scales=((1.0, 1.0, 1.0),), name="part"):
    instances = [types.SimpleNamespace(printable=True, scaling_factor=lambda s=s: s) for s in scales]
    return types.SimpleNamespace(name=name, volumes=lambda: volumes, instances=lambda: instances)


def _model(objects, plate=0):
    return types.SimpleNamespace(objects=lambda: objects, current_plate_index=lambda: plate)


class FingerprintTest(unittest.TestCase):
    """Slices are compared only for the same parts, at the same size, on the same plate."""

    def test_same_parts_match(self):
        a = _model([_object([_volume()], name="a"), _object([_volume(30)])])
        b = _model([_object([_volume(30)]), _object([_volume()], name="renamed")])
        self.assertEqual(fs.model_fingerprint(a), fs.model_fingerprint(b))

    def test_changes_that_alter_the_print(self):
        base = fs.model_fingerprint(_model([_object([_volume()])]))
        for other in (_model([_object([_volume()])], plate=1),
                      _model([_object([_volume(14)])]),
                      _model([_object([_volume(scale=(2.0, 2.0, 2.0))])]),
                      _model([_object([_volume()], scales=((1.0, 1.0, 1.0), (1.0, 1.0, 1.0)))]),
                      _model([_object([_volume()]), _object([_volume()])])):
            self.assertNotEqual(fs.model_fingerprint(other), base)

    def test_settings_volumes_keep_the_comparison(self):
        base = fs.model_fingerprint(_model([_object([_volume()])]))
        for kind in ("PARAMETER_MODIFIER", "SUPPORT_ENFORCER", "SUPPORT_BLOCKER"):
            self.assertEqual(fs.model_fingerprint(_model([_object([_volume(), _volume(8, kind=kind)])])), base, kind)
        negative = _model([_object([_volume(), _volume(8, kind="NEGATIVE_VOLUME")])])
        self.assertNotEqual(fs.model_fingerprint(negative), base)

    def test_settings_never_reach_the_fingerprint(self):
        """The plugin only writes process settings; reading any settings here would let its own
        suggestions reset the comparison."""
        def forbidden(*args):
            raise AssertionError("the fingerprint must not read settings")

        volume = _volume()
        volume.config_value = volume.config_keys = forbidden
        obj = _object([volume])
        obj.config_value = obj.config_keys = forbidden
        model = _model([obj])
        before = fs.model_fingerprint(model)
        # Every suggestion, vase mode included, goes to the process preset, not to the model.
        fs.suggest({**BASE, **EXTRA, "enable_support": "1"}, "container", 0, 0, 0, "PLA",
                   answers={"vase": True, "top_hidden": True})
        self.assertEqual(fs.model_fingerprint(model), before)

    def test_older_orca_without_some_calls(self):
        bare = types.SimpleNamespace(volumes=lambda: [types.SimpleNamespace()], instances=lambda: [types.SimpleNamespace()])
        self.assertEqual(len(fs.model_fingerprint(types.SimpleNamespace(objects=lambda: [bare]))), 16)


class EstimateHistoryTest(unittest.TestCase):
    TEXT = ("; CONFIG_BLOCK_START\n; CONFIG_BLOCK_END\n; total filament used [g] = %s\n"
            "; total filament cost = 1\n; estimated printing time (normal mode) = 1h\n")

    def setUp(self):
        self.storage = tempfile.mkdtemp()
        self.model = _model([_object([_volume()])])
        fs.orca.host = types.SimpleNamespace(plugin=types.SimpleNamespace(storage=lambda: self.storage),
                                             model=lambda: self.model,
                                             preset_bundle=lambda: types.SimpleNamespace(current_filament_presets=list))
        self.page = fs.FilamentSaverPage()

    def tearDown(self):
        del fs.orca.host
        shutil.rmtree(self.storage)

    def _slice(self, grams):
        path = os.path.join(self.storage, ".slice.gcode")
        with open(path, "w") as stream:
            stream.write(self.TEXT % grams)
        self.page._record_export(path)
        current = fs._current_estimates()
        return [(current.get(k) or {}).get("grams") for k in ("previous", "latest")]

    def test_compares_only_the_same_parts(self):
        self.assertEqual(self._slice(10), [None, 10])
        self.assertEqual(self._slice(8), [10, 8])
        self.model = _model([_object([_volume(99)])])  # another project
        self.assertEqual(self._slice(50), [None, 50])
        self.model = _model([_object([_volume()])])  # back to the first one
        self.assertEqual(self._slice(7), [8, 7])

    def test_old_global_history_is_dropped(self):
        with open(os.path.join(self.storage, fs.ESTIMATES_FILE), "w") as stream:
            json.dump({"previous": {"grams": 158}, "latest": {"grams": 97}}, stream)
        self.assertEqual(fs._current_estimates(), {})
        self.assertEqual(self._slice(10), [None, 10])


class SpoolPriceTest(unittest.TestCase):
    """The spool price, when set, replaces Orca's filament cost for that filament only."""

    ONE = {"grams": 100.0, "cost": 2.5, "grams_each": [100.0], "cost_each": [2.5], "filaments": ["PETG A"]}
    TWO = {"grams": 150.0, "cost": 4.0, "grams_each": [100.0, 50.0], "cost_each": [2.5, 1.5],
           "filaments": ["PETG A", "PLA B"]}

    def test_numbers_must_be_finite(self):
        self.assertEqual([fs._number(v) for v in ("nan", "inf", "-inf", "1e999", "12.5", "10%")],
                         [None, None, None, None, 12.5, 10.0])

    def test_no_price_keeps_orca_cost(self):
        self.assertEqual(fs.priced_cost(self.ONE, {}), (2.5, False))

    def test_spool_price(self):
        self.assertEqual(fs.priced_cost(self.ONE, {"PETG A": {"price": 120, "grams": 1000}}), (12.0, True))
        self.assertEqual(fs.priced_cost(self.ONE, {"PETG A": {"price": 60, "grams": 500}}), (12.0, True))

    def test_only_priced_filaments_change(self):
        self.assertEqual(fs.priced_cost(self.TWO, {"PLA B": {"price": 100, "grams": 1000}}), (7.5, True))

    def test_older_slices_without_filament_names(self):
        self.assertEqual(fs.priced_cost({"grams": 100.0, "cost": 2.5}, {"PETG A": {"price": 120, "grams": 1000}}),
                         (2.5, False))

    def test_set_price_and_card(self):
        storage = tempfile.mkdtemp()
        model = _model([_object([_volume()])])
        fil = types.SimpleNamespace(name="PETG A", config_value=lambda key: None)
        fs.orca.host = types.SimpleNamespace(plugin=types.SimpleNamespace(storage=lambda: storage), model=lambda: model,
                                             preset_bundle=lambda: types.SimpleNamespace(current_filament_presets=lambda: [fil]))
        try:
            page = fs.FilamentSaverPage()
            path = os.path.join(storage, ".slice.gcode")
            for grams in (100, 80):
                with open(path, "w") as stream:
                    stream.write(EstimateHistoryTest.TEXT.replace("; total filament cost = 1", "; filament used [g] = %s\n"
                                                                  "; filament cost = 1") % (grams, grams))
                page._record_export(path)
            self.assertEqual([fs._current_estimates()[k]["cost"] for k in ("previous", "latest")], [1, 1])
            page._set_price({"name": "PETG A", "price": "120", "grams": ""})
            shown = fs._current_estimates()
            self.assertEqual([(shown[k]["cost"], shown[k]["priced"]) for k in ("previous", "latest")],
                             [(12.0, True), (9.6, True)])
            page._set_price({"name": "PETG A", "price": ""})
            self.assertEqual(fs._read_prices(), {})
        finally:
            del fs.orca.host
            shutil.rmtree(storage)


class ThicknessScalingTest(unittest.TestCase):
    """Layer and wall counts keep the same thickness at other layer heights and nozzles."""

    def _targets(self, purpose="prototype", **overrides):
        spec = fs.PURPOSES[purpose]
        current = {**BASE, "top_shell_thickness": "0", **overrides}
        result = fs.suggest(current, purpose, spec["S"], spec["V"], spec["R"], "PLA")
        return {c["key"]: c["suggested"] for c in result["changes"]}

    def test_fine_layers_get_more_layers(self):
        # Raised only to the prototype's minimum (2 layers = 0.4 mm), not to the slider's 3.
        targets = self._targets(layer_height="0.08")
        self.assertEqual((targets["top_shell_layers"], targets["bottom_shell_layers"]), ("5", "5"))
        # From above, the slider value is kept as the ceiling: 3 layers = 0.6 mm.
        targets = self._targets(layer_height="0.08", top_shell_layers="12", bottom_shell_layers="12")
        self.assertEqual((targets["top_shell_layers"], targets["bottom_shell_layers"]), ("8", "5"))

    def test_coarse_layers_get_fewer_layers(self):
        targets = self._targets(layer_height="0.4", top_shell_layers="5", bottom_shell_layers="4")
        self.assertEqual((targets["top_shell_layers"], targets["bottom_shell_layers"]), ("2", "2"))  # never 1

    def test_small_nozzle_gets_more_walls(self):
        # Mechanical needs at least 3 walls of 0.4 mm: 6 walls with a 0.2 mm nozzle.
        self.assertEqual(self._targets("mechanical", nozzle_diameter="0.2", wall_loops="2")["wall_loops"], "6")

    def test_big_nozzle_keeps_at_least_two_walls(self):
        self.assertNotIn("wall_loops", self._targets(nozzle_diameter="0.8", wall_loops="2"))
        self.assertEqual(self._targets("mechanical", nozzle_diameter="0.8", wall_loops="6")["wall_loops"], "3")

    def test_minimum_reason_explains_the_scaled_count(self):
        result = fs.suggest({**BASE, "layer_height": "0.25", "top_shell_layers": "3", "top_shell_thickness": "0"},
                            "mechanical", 5, 1, 4, "PLA")
        top = next(c for c in result["changes"] if c["key"] == "top_shell_layers")
        self.assertEqual(top["suggested"], "4")
        self.assertIn("That is 1 mm: 4 layers at 0.25 mm layer height.", top["reason"])

    def test_reference_printer_is_unchanged(self):
        self.assertEqual(self._targets(), self._targets(layer_height="0.2", nozzle_diameter="0.4"))


class WarpingBrimTest(unittest.TestCase):
    def test_wider_or_fuller_brim_is_kept(self):
        for material in ("ABS", "PA", "PC"):
            result = fs.suggest({**BASE, "brim_type": "outer_only", "brim_width": "10"}, "functional", 3, 2, 3, material)
            self.assertFalse({"brim_type", "brim_width"} & {c["key"] for c in result["changes"]}, material)

    def test_raised_to_the_minimum(self):
        result = fs.suggest({**BASE, "brim_type": "no_brim", "brim_width": "2"}, "functional", 3, 2, 3, "PC")
        targets = {c["key"]: c["suggested"] for c in result["changes"]}
        self.assertEqual((targets["brim_type"], targets["brim_width"]), ("outer_only", "8"))


class SafetyTest(unittest.TestCase):
    """Cases found by a randomized sweep over Orca's full value ranges."""

    def test_hand_made_brims_are_kept_for_materials_that_warp(self):
        for brim in ("painted", "inner_only", "leading_edge_only", "outer_and_inner"):
            result = fs.suggest({**BASE, "brim_type": brim}, "functional", 3, 2, 3, "ABS")
            self.assertNotIn("brim_type", {c["key"] for c in result["changes"]}, brim)

    def test_unranked_pattern_is_not_swapped(self):
        # gyroid instead of triangles: the effect on filament is not known, so nothing is suggested.
        result = fs.suggest({**BASE, "sparse_infill_pattern": "triangles"}, "mechanical", 5, 1, 4, "PLA")
        self.assertNotIn("sparse_infill_pattern", {c["key"] for c in result["changes"]})

    def test_forbidden_pattern_is_replaced_by_the_cheapest_allowed(self):
        result = fs.suggest(dict(BASE), "mechanical", 5, 1, 4, "PLA")  # BASE uses lightning
        pattern = next(c for c in result["changes"] if c["key"] == "sparse_infill_pattern")
        self.assertEqual(pattern["suggested"], "adaptivecubic")

    def test_hidden_top_counts_the_final_layers(self):
        # Two top layers stay (no increase is suggested): too few to skip the last one.
        result = fs.suggest({**BASE, "top_shell_layers": "2", "top_shell_thickness": "0", "top_surface_density": "100%"},
                            "prototype", 0, 0, 2, "PLA", answers={"top_hidden": True})
        self.assertNotIn("top_surface_density", {c["key"] for c in result["changes"]})


class ObjectOverrideTest(unittest.TestCase):
    def test_objects_with_their_own_settings_are_flagged(self):
        volume = types.SimpleNamespace(config_keys=lambda: ["sparse_infill_density", "extruder"])
        obj = types.SimpleNamespace(name="Bracket", config_keys=lambda: ["wall_loops"], volumes=lambda: [volume])
        plain = types.SimpleNamespace(name="Lid", config_keys=lambda: [], volumes=lambda: [])
        fs.orca.host = types.SimpleNamespace(model=lambda: types.SimpleNamespace(objects=lambda: [obj, plain]))
        try:
            self.assertEqual(fs._object_overrides(), {"wall_loops": ["Bracket"], "sparse_infill_density": ["Bracket"]})
        finally:
            del fs.orca.host


class PrimeTowerBrimTest(unittest.TestCase):
    def _targets(self, r, filaments=2, **overrides):
        current = {**BASE, "enable_prime_tower": "1", "prime_tower_brim_width": "3", **overrides}
        result = fs.suggest(current, "decoration", 1, 4, r, "PLA", filaments=filaments)
        return {c["key"]: (c["suggested"], c["effect"]) for c in result["changes"]}

    def test_follows_reliability_and_only_goes_down(self):
        self.assertEqual(self._targets(0)["prime_tower_brim_width"], ("0", "saves"))
        self.assertEqual(self._targets(3)["prime_tower_brim_width"], ("2", "saves"))
        self.assertNotIn("prime_tower_brim_width", self._targets(4))
        self.assertNotIn("prime_tower_brim_width", self._targets(3, prime_tower_brim_width="1.5"))  # already narrower

    def test_needs_a_prime_tower_in_use(self):
        self.assertNotIn("prime_tower_brim_width", self._targets(0, filaments=1))
        self.assertNotIn("prime_tower_brim_width", self._targets(0, enable_prime_tower="0"))
        self.assertNotIn("prime_tower_brim_width", self._targets(0, prime_tower_brim_width="-1"))  # auto


class RememberedFormTest(unittest.TestCase):
    """The purpose, sliders and answers come back with the same parts."""

    def setUp(self):
        self.storage = tempfile.mkdtemp()
        self.model = _model([_object([_volume()])])
        cfg = dict(BASE)
        preset = types.SimpleNamespace(name="P", is_dirty=False, config_value=cfg.get)
        bundle = types.SimpleNamespace(prints=types.SimpleNamespace(edited_preset=lambda: preset, selected_preset=lambda: preset),
                                       current_filament_presets=list)
        fs.orca.host = types.SimpleNamespace(plugin=types.SimpleNamespace(storage=lambda: self.storage),
                                             model=lambda: self.model, preset_bundle=lambda: bundle)

    def tearDown(self):
        del fs.orca.host
        shutil.rmtree(self.storage)

    def test_clean_form(self):
        self.assertEqual(fs.clean_form({"purpose": "bogus", "S": "9", "V": "x", "material": "pla", "answers": {"vase": 1}}),
                         {"purpose": "decoration", "material": "pla", "S": 5, "V": 4, "R": 3,
                          "answers": {"top_hidden": False, "vase": True, "purge_infill": False, "purge_support": False}})

    def test_choices_come_back_with_the_same_parts(self):
        choices = {"purpose": "mechanical", "S": 5, "V": 1, "R": 4, "material": "petg", "answers": {"top_hidden": True}}
        fs.FilamentSaverPage()._suggest(choices)
        page = fs.FilamentSaverPage()  # Orca restarted
        page._sync_parts()
        self.assertEqual((page.form["purpose"], page.form["S"], page.form["answers"]["top_hidden"]), ("mechanical", 5, True))
        self.assertEqual(page.form_version, 1)
        page._sync_parts()  # same parts again: nothing to bring back
        self.assertEqual(page.form_version, 1)

    def test_other_parts_keep_the_current_choices(self):
        fs.FilamentSaverPage()._suggest({"purpose": "mechanical"})
        self.model = _model([_object([_volume(99)])])
        page = fs.FilamentSaverPage()
        page._sync_parts()
        self.assertEqual((page.form["purpose"], page.form_version), ("decoration", 0))

    def test_empty_plate_is_not_remembered(self):
        self.model = _model([])
        fs.FilamentSaverPage()._suggest({"purpose": "mechanical"})
        self.assertEqual(fs._read_forms(), {})


class SavePresetTest(unittest.TestCase):
    """Save as new preset writes the edited process settings into the local bundle."""

    def setUp(self):
        self.data_dir = tempfile.mkdtemp()
        self.saved_data_dir = fs._data_dir
        fs._data_dir = lambda: self.data_dir
        system = {"inherits": "", "wall_loops": "2", "sparse_infill_density": "15%", "layer_height": "0.2"}
        edited = {**system, "sparse_infill_density": "10%"}  # an unsaved edit
        self.parent = types.SimpleNamespace(name="0.20mm Standard", is_system=True, config_value=system.get,
                                            config_keys=lambda: list(system))
        self.edited = types.SimpleNamespace(name="0.20mm Standard", is_system=True, config_value=edited.get,
                                            config_keys=lambda: list(edited))
        self.existing = {"0.20mm Standard"}
        empty = types.SimpleNamespace(size=lambda: 0)
        prints = types.SimpleNamespace(selected_preset=lambda: self.parent, edited_preset=lambda: self.edited,
                                       find_preset=lambda name: object() if name in self.existing else None, size=lambda: 0)
        bundle = types.SimpleNamespace(prints=prints, filaments=empty, printers=empty, current_filament_presets=list)
        self.reloads = []
        fs.orca.host = types.SimpleNamespace(preset_bundle=lambda: bundle, preset=types.SimpleNamespace(
            bundles=types.SimpleNamespace(reload_local=self.reloads.append)))
        self.page = fs.FilamentSaverPage()

    def tearDown(self):
        fs._data_dir = self.saved_data_dir
        del fs.orca.host
        shutil.rmtree(self.data_dir)

    def _file(self, name):
        return os.path.join(self.data_dir, "user", "default", "_local", fs.BUNDLE_ID, "process", name + ".json")

    def test_saves_edits_on_top_of_the_system_preset(self):
        self.page._save_preset({"name": "PETG economy"})
        with open(self._file("PETG economy"), encoding="utf-8") as stream:
            preset = json.load(stream)
        self.assertEqual((preset["name"], preset["inherits"], preset["sparse_infill_density"]),
                         ("PETG economy", "0.20mm Standard", "10%"))
        self.assertNotIn("wall_loops", preset)  # same as the system preset
        self.assertEqual(self.reloads, [fs.BUNDLE_ID])

    def test_refuses_bad_or_taken_names(self):
        for name in ("", "  ", "a/b", "a\\b", "..", ".hidden", "0.20mm Standard", "CON", "nul.txt", "com1",
                     "ends with dot."):
            with self.assertRaises(RuntimeError, msg=name):
                self.page._save_preset({"name": name})

    def test_other_capitals_do_not_overwrite(self):
        self.page._save_preset({"name": "Mine"})
        with self.assertRaises(RuntimeError):
            self.page._save_preset({"name": "mine"})

    def test_updates_its_own_preset(self):
        self.page._save_preset({"name": "Mine"})
        self.existing.add("Mine")
        self.page._save_preset({"name": "Mine"})
        self.assertIn("Updated", self.page.message)


class CurrencyTest(unittest.TestCase):
    """Costs are shown in the chosen currency, converted with cached rates fetched in the background."""

    def setUp(self):
        self.storage = tempfile.mkdtemp()
        fs.orca.host = types.SimpleNamespace(plugin=types.SimpleNamespace(storage=lambda: self.storage))
        self.saved_urlopen = fs.urllib.request.urlopen
        self.requests = []

    def tearDown(self):
        fs.urllib.request.urlopen = self.saved_urlopen
        del fs.orca.host
        shutil.rmtree(self.storage)

    def _serve(self, payload):
        test = self

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, limit):
                return json.dumps(payload).encode("utf-8")

        def urlopen(url, timeout, context=None):
            test.requests.append(url)
            return Response()
        fs.urllib.request.urlopen = urlopen

    def test_default_currency_follows_language(self):
        self.assertEqual(fs.read_currency("pt_BR"), {"price": "BRL", "display": "BRL", "rates_allowed": False})
        self.assertEqual(fs.read_currency("en"), {"price": "USD", "display": "USD", "rates_allowed": False})

    def test_conversion_through_any_base(self):
        rates = {"rates": {"EUR": 1.0, "BRL": 6.0, "USD": 1.2}}
        self.assertEqual(fs.conversion("BRL", "BRL", {}), 1.0)
        self.assertAlmostEqual(fs.conversion("USD", "BRL", rates), 5.0)
        self.assertIsNone(fs.conversion("USD", "JPY", rates))

    def test_fetch_validates_and_caches(self):
        self._serve({"base": "BRL", "date": "2026-10-30", "rates": {"USD": 0.18, "EUR": "x", "XXX": 3, "JPY": -1}})
        fs.fetch_rates("BRL")
        self.assertEqual(self.requests, ["https://api.frankfurter.dev/v1/latest?base=BRL"])
        rates = fs.read_rates()
        self.assertEqual((rates["base"], rates["rates"]), ("BRL", {"USD": 0.18, "BRL": 1.0}))
        with self.assertRaises(ValueError):
            fs.fetch_rates("XYZ")  # only known currency codes ever reach the URL

    def test_page_fetches_once_and_converts(self):
        self._serve({"base": "BRL", "date": "2026-10-30", "rates": {"USD": 0.2}})
        page = fs.FilamentSaverPage()
        page.post_message = lambda message: None
        page._set_currency({"price": "BRL", "display": "USD", "rates_allowed": True})
        page._currency_state()
        for thread in threading.enumerate():
            if thread.name == "filament-saver-rates":
                thread.join(5)
        state = page._currency_state()
        page._currency_state()
        self.assertEqual(len(self.requests), 1)
        self.assertEqual((state["shown"], state["rate"], state["converted"]), ("USD", 0.2, True))

    def test_failure_keeps_price_currency(self):
        def urlopen(url, timeout, context=None):
            raise OSError("offline")
        fs.urllib.request.urlopen = urlopen
        page = fs.FilamentSaverPage()
        page.post_message = lambda message: None
        page._set_currency({"price": "BRL", "display": "EUR", "rates_allowed": True})
        page._fetch_rates("BRL")
        state = page._currency_state()
        self.assertEqual((state["shown"], state["converted"], state["fetching"]), ("BRL", False, False))
        self.assertIn("offline", state["error"])

    def test_nothing_is_sent_without_permission(self):
        self._serve({"base": "BRL", "date": "2026-10-30", "rates": {"USD": 0.2}})
        page = fs.FilamentSaverPage()
        page.post_message = lambda message: None
        page._set_currency({"price": "BRL", "display": "USD"})
        state = page._currency_state()
        self.assertEqual((self.requests, state["shown"], state["converted"]), ([], "BRL", False))
        # Rates cached while it was allowed are not used after the permission is taken back.
        fs.fetch_rates("BRL")
        page._set_currency({"rates_allowed": False})
        self.assertEqual(page._currency_state()["shown"], "BRL")

    def test_estimates_are_converted(self):
        self.assertEqual(fs._current_estimates(0.5), {})  # nothing sliced yet


class LogoTest(unittest.TestCase):
    def test_tab_icon_and_header(self):
        storage = tempfile.mkdtemp()
        fs.orca.host = types.SimpleNamespace(plugin=types.SimpleNamespace(storage=lambda: storage))
        try:
            page = fs.FilamentSaverPage()
            with open(page.get_icon(), "rb") as stream:
                self.assertEqual(stream.read(8), b"\x89PNG\r\n\x1a\n")
            self.assertNotIn("__LOGO_PNG__", page.get_ui())
            self.assertIn("data:image/png;base64," + fs.LOGO_PNG[:40], page.get_ui())
        finally:
            del fs.orca.host
            shutil.rmtree(storage)


class GcodeEstimateTest(unittest.TestCase):
    def test_statistics_after_config_block(self):
        text = ("; HEADER_BLOCK_START\n; filament_density: 1.25\n; HEADER_BLOCK_END\nG1 X1\n"
                "; CONFIG_BLOCK_START\n; filament_cost = 30\n; CONFIG_BLOCK_END\n\n"
                "; filament used [g] = 15.52\n; filament cost = 0.47\n; total filament used [g] = 15.52\n"
                "; total filament cost = 0.47\n; estimated printing time (normal mode) = 36m 38s\n")
        with tempfile.NamedTemporaryFile("w", suffix=".gcode", delete=False) as stream:
            stream.write(text)
        try:
            self.assertEqual(fs.parse_gcode_estimate(stream.name), {"seconds": 2198, "grams": 15.52, "cost": 0.47,
                                                                     "grams_each": [15.52], "cost_each": [0.47]})
        finally:
            os.unlink(stream.name)


if __name__ == "__main__":
    if "--update-snapshot" in sys.argv:
        with open(SNAPSHOT, "w", encoding="utf-8") as stream:
            json.dump(_snapshot(), stream, ensure_ascii=False, indent=1, sort_keys=True)
            stream.write("\n")
        print("Wrote", SNAPSHOT)
    else:
        unittest.main()
