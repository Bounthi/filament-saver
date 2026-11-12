# /// script
# requires-python = ">=3.12"
# dependencies = []
#
# [tool.orcaslicer.plugin]
# id = "filament-saver"
# name = "Filament Saver"
# description = "Suggests print settings that save filament for what you are printing, and applies the ones you accept."
# author = "bounthi"
# version = "0.1.4"
# ///
# Copyright (c) 2026 bounthi. MIT License, see LICENSE.
# SPDX-License-Identifier: MIT
"""Filament Saver: a main-window tab that suggests filament-saving settings.

Flow:
  1. The user picks what they are printing (purpose) and three sliders:
     strength, finish and reliability.
  2. The rules engine (docs/optimization-rules.md, sections 2-5, 7 and 9)
     turns that into target values and lists every setting that differs
     from the current process preset, with the reason.
  3. The user applies changes one by one or all at once.
     - With the in-place write API (orca.host.preset.process.begin_update),
       changes become unsaved edits of the active process preset.
     - Without it, accepted changes go into a process preset
       "<system parent> - Filament Saver" in the local bundle, which the
       user then selects.
  4. Print time, filament and cost come from the G-code of each slice,
     so the tab shows the estimate before and after the changes.

Text is written in English and shown in Orca's language when TRANSLATIONS has it.
"""

import base64
import hashlib
import json
import math
import os
import queue
import re
import ssl
import tempfile
import threading
import time
import traceback
import urllib.request

import orca


VERSION = "0.1.4"  # keep in sync with the header above
BUNDLE_ID = "filament-saver"
BUNDLE_NAME = "Filament Saver"
PRESET_SUFFIX = " - Filament Saver"
ESTIMATES_FILE = "estimates.json"
MAX_ESTIMATE_MODELS = 20  # parts whose last two slices are kept for comparison
ORIGINALS_FILE = "originals.json"
PRICES_FILE = "prices.json"
FORMS_FILE = "forms.json"
CURRENCY_FILE = "currency.json"
RATES_FILE = "rates.json"
# Exchange rates: Frankfurter, free and keyless, with the European Central Bank's daily reference
# rates. Only currency codes are sent, and only after the user allows it in the tab.
RATES_HOST = "https://api.frankfurter.dev"
RATES_URL = RATES_HOST + "/v1/latest?base=%s"
RATES_MAX_AGE = 12 * 3600   # seconds before cached rates are fetched again
RATES_RETRY = 10 * 60       # seconds before a failed fetch is tried again
# System CA bundles, for Python builds without certificates of their own (python.org on macOS).
SYSTEM_CA_FILES = ("/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt",
                   "/etc/ssl/ca-bundle.pem")
CURRENCIES = ("AUD", "BRL", "CAD", "CHF", "CNY", "CZK", "DKK", "EUR", "GBP", "HKD", "HUF", "IDR", "ILS", "INR",
              "ISK", "JPY", "KRW", "MXN", "MYR", "NOK", "NZD", "PHP", "PLN", "RON", "SEK", "SGD", "THB", "TRY",
              "USD", "ZAR")
# Lifecycle events after which other parts may be in Orca.
PARTS_EVENTS = ("NewProject", "ProjectOpened", "ObjectAdded", "ObjectDeleted", "ObjectTransformed", "ObjectChanged",
                "PlateSelected")
MAX_REMEMBERED_FORMS = 50  # sets of parts whose purpose, sliders and answers are remembered
# Characters Orca cannot keep in a preset name, since it becomes the file name.
PRESET_NAME_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# Names Windows reserves for devices, with or without an extension.
WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {"%s%d" % (p, n) for p in ("COM", "LPT") for n in range(1, 10)}
DEFAULT_SPOOL_GRAMS = 1000

# --- rules (docs/optimization-rules.md) -------------------------------------

PURPOSES = {
    "decoration": {"label": "Decoration", "hint": "Looks matter, load does not",
                   "S": 1, "V": 4, "R": 3,
                   "floors": {"walls": 2, "infill": 0, "top": 3, "bottom": 2, "forbid": ()}},
    "functional": {"label": "Functional", "hint": "Tool, organizer, light-duty bracket",
                   "S": 3, "V": 2, "R": 3,
                   "floors": {"walls": 2, "infill": 10, "top": 4, "bottom": 3, "forbid": ("lightning",)}},
    "prototype": {"label": "Prototype", "hint": "Only the geometry needs to come out right",
                  "S": 0, "V": 0, "R": 2,
                  "floors": {"walls": 1, "infill": 0, "top": 2, "bottom": 2, "forbid": ()}},
    "mechanical": {"label": "Mechanical", "hint": "Gear, hinge, load-bearing bracket",
                   "S": 5, "V": 1, "R": 4,
                   "floors": {"walls": 3, "infill": 20, "top": 5, "bottom": 4,
                              "forbid": ("lightning", "supportcubic")}},
    "container": {"label": "Container", "hint": "Vase, cup, box",
                  "S": 2, "V": 3, "R": 3,
                  "floors": {"walls": 2, "infill": 0, "top": 3, "bottom": 4, "forbid": ()}},
    "miniature": {"label": "Miniature", "hint": "Fine detail; supports are left as they are",
                  "S": 1, "V": 5, "R": 3,
                  "floors": {"walls": 2, "infill": 0, "top": 3, "bottom": 2, "forbid": ()}},
    "large": {"label": "Large part", "hint": "Cosplay, big prints; infill dominates usage",
              "S": 1, "V": 2, "R": 4,
              "floors": {"walls": 2, "infill": 5, "top": 4, "bottom": 3, "forbid": ()}},
}

# Strength (S): walls, sparse infill density, pattern, top layers, bottom layers.
STRENGTH = [
    (2, 5, "lightning", 3, 2),
    (2, 8, "lightning", 4, 3),
    (2, 12, "supportcubic", 4, 3),
    (3, 15, "adaptivecubic", 5, 4),
    (4, 20, "gyroid", 5, 4),
    (5, 30, "gyroid", 6, 5),
]

# Finish (V): top layer delta, one wall on top, support interface layers.
FINISH = [
    (-1, "1", 1),
    (-1, "1", 1),
    (0, "1", 2),
    (0, "0", 2),
    (1, "0", 3),
    (1, "0", 3),
]

# Reliability (R): brim type and skirt loops, both ceilings (they only ever go down). The brim
# column only applies when the user says the part holds on the bed by itself (QUESTIONS), and
# never to materials that warp. Supports are not reduced at any R (see suggest()).
RELIABILITY = [
    ("no_brim", 0),
    ("no_brim", 1),
    ("auto_brim", 1),
    ("auto_brim", 1),
    ("brim_ears", 1),
    ("outer_only", 2),
]
BRIM_TEXT = {"no_brim": "no brim", "auto_brim": "brim only where Orca thinks it is needed",
             "brim_ears": "brim ears on sharp corners only", "outer_only": "full outer brim"}

# Patterns from least to most material for the same density, used to pick a fallback
# when a pattern is forbidden and to tell whether a pattern change saves filament.
PATTERN_ORDER = ["lightning", "supportcubic", "adaptivecubic", "gyroid"]
PATTERN_NOTES = {
    "lightning": "only supports the top surfaces",
    "supportcubic": "dense near the surfaces, sparse in the core",
    "adaptivecubic": "dense near the surfaces, sparse in the core",
    "gyroid": "strong in every direction",
}
BRIM_ORDER = ["no_brim", "auto_brim", "brim_ears", "outer_only", "outer_and_inner"]
# Brim types the user shapes by hand or for a special case; the plugin never replaces them.
OWN_BRIMS = ("painted", "inner_only", "leading_edge_only")

# Strength (S), solid fill Orca adds on its own: extra solid infill along sloped walls
# (ensure_vertical_shell_thickness), which surfaces get gap fill, and the sparse area (mm²)
# below which Orca switches to solid infill.
STRENGTH_SOLID = [
    ("none", "topbottom", 5),
    ("ensure_critical_only", "topbottom", 5),
    ("ensure_critical_only", "topbottom", 10),
    ("ensure_moderate", "everywhere", 10),
    ("ensure_all", "everywhere", 15),
    ("ensure_all", "everywhere", 15),
]
VERTICAL_SHELL_ORDER = ["none", "ensure_critical_only", "ensure_moderate", "ensure_all"]
VERTICAL_SHELL_TEXT = {
    "none": "no extra solid infill along sloped walls",
    "ensure_critical_only": "extra solid infill along sloped walls only where Orca finds it critical",
    "ensure_moderate": "moderate extra solid infill along sloped walls",
    "ensure_all": "extra solid infill wherever sloped walls would get thin",
}
GAP_FILL_ORDER = ["nowhere", "topbottom", "everywhere"]
# Reliability (R): a single wall on the first layer.
RELIABILITY_FIRST_LAYER = ["1", "1", "0", "0", "0", "0"]
# Reliability (R): largest prime tower brim (mm); None keeps the current one. "-1" (auto) is left alone.
RELIABILITY_TOWER_BRIM = [3, 3, 3, 3, None, None]  # Orca's default is 3 mm
# Finish (V): minimum spacing (mm) between top support interface lines; None keeps the current one.
FINISH_INTERFACE_SPACING = [0.8, 0.6, None, None, None, None]

# Vase mode: what Orca requires before it prints in spiral vase mode (ConfigManipulation.cpp),
# and the suggestions that no longer apply once it is on.
VASE_REQUIRES = [("wall_loops", "1"), ("top_shell_layers", "0"), ("sparse_infill_density", "0%"),
                 ("enable_support", "0"), ("enforce_support_layers", "0"), ("detect_thin_wall", "0"),
                 ("overhang_reverse", "0"), ("timelapse_type", "0"), ("enable_wrapping_detection", "0")]
VASE_DROPS = ("sparse_infill_pattern", "top_shell_thickness", "top_surface_density",
              "only_one_wall_top", "minimum_sparse_infill_area", "ensure_vertical_shell_thickness",
              "flush_into_infill", "flush_into_support")

# Part materials that bond poorly to each other: where they meet inside one part, it can split.
# Only a note; which filament prints which part is not known to the plugin.
POOR_BONDS = [({"pla", "silk"}, {"petg"}), ({"pla", "silk"}, {"abs_asa"}), ({"petg"}, {"abs_asa"}),
              ({"pa"}, {"pla", "silk", "petg", "abs_asa", "tpu", "pc"})]

MAX_WALLS = 6
# The tables count layers at this layer height (mm) and walls for this nozzle (mm); other layer
# heights and nozzles get the same thickness.
REFERENCE_LAYER = 0.2
REFERENCE_NOZZLE = 0.4
MAX_INFILL = 40

FAMILY_LABELS = {
    "pla": "PLA", "silk": "Silk PLA", "petg": "PETG", "abs_asa": "ABS/ASA", "tpu": "TPU",
    "pa": "Nylon (PA)", "pc": "PC", "support": "Support material", "unknown": "Unknown material",
}
# Families the user can pick in the material selector, in display order.
MATERIAL_CHOICES = ["pla", "silk", "petg", "abs_asa", "tpu", "pa", "pc"]

# Display order and labels of the settings the engine touches.
SETTINGS = [
    ("spiral_mode", "Spiral vase"),
    ("wall_loops", "Wall loops"),
    ("sparse_infill_density", "Sparse infill density"),
    ("sparse_infill_pattern", "Sparse infill pattern"),
    ("top_shell_layers", "Top shell layers"),
    ("top_shell_thickness", "Top shell thickness"),
    ("bottom_shell_layers", "Bottom shell layers"),
    ("bottom_shell_thickness", "Bottom shell thickness"),
    ("top_surface_density", "Top surface density"),
    ("ensure_vertical_shell_thickness", "Ensure vertical shell thickness"),
    ("minimum_sparse_infill_area", "Minimum sparse infill area"),
    ("gap_fill_target", "Apply gap fill"),
    ("only_one_wall_top", "Only one wall on top surfaces"),
    ("only_one_wall_first_layer", "Only one wall on first layer"),
    ("detect_thin_wall", "Detect thin walls"),
    ("overhang_reverse", "Reverse on even"),
    ("brim_type", "Brim type"),
    ("brim_width", "Brim width"),
    ("skirt_loops", "Skirt loops"),
    ("skirt_height", "Skirt height"),
    ("raft_layers", "Raft layers"),
    ("enable_support", "Enable support"),
    ("enforce_support_layers", "Enforce support layers"),
    ("support_threshold_angle", "Support threshold angle"),
    ("support_remove_small_overhang", "Ignore small overhangs"),
    ("support_base_pattern", "Support base pattern"),
    ("support_base_pattern_spacing", "Support base pattern spacing"),
    ("support_style", "Support style"),
    ("tree_support_wall_count", "Support wall loops"),
    ("support_interface_top_layers", "Support interface top layers"),
    ("support_interface_bottom_layers", "Support interface bottom layers"),
    ("support_interface_spacing", "Support top interface spacing"),
    ("support_top_z_distance", "Support top Z distance"),
    ("prime_tower_brim_width", "Prime tower brim width"),
    ("flush_into_infill", "Purge into objects' infill"),
    ("flush_into_support", "Purge into objects' support"),
    ("timelapse_type", "Timelapse"),
    ("enable_wrapping_detection", "Enable clumping detection"),
]
LABELS = dict(SETTINGS)
UNITS = {"brim_width": "mm", "support_top_z_distance": "mm", "support_threshold_angle": "°",
         "top_shell_thickness": "mm", "bottom_shell_thickness": "mm", "support_base_pattern_spacing": "mm",
         "minimum_sparse_infill_area": "mm²", "support_interface_spacing": "mm", "prime_tower_brim_width": "mm"}
# On/off settings where on uses less filament.
BOOL_KEYS = {"only_one_wall_top", "support_remove_small_overhang", "only_one_wall_first_layer",
             "flush_into_infill", "flush_into_support", "spiral_mode"}
# On/off settings shown as On/Off whose effect on filament depends on the case.
SWITCH_KEYS = BOOL_KEYS | {"enable_support", "detect_thin_wall", "overhang_reverse", "enable_wrapping_detection"}
# Enum values shown with a name instead of Orca's stored value.
VALUE_NAMES = {"timelapse_type": {"0": "Traditional", "1": "Smooth"}}
# Numeric settings where a higher value uses less filament.
HIGHER_SAVES = {"support_top_z_distance", "support_base_pattern_spacing", "support_interface_spacing"}
# Extra process keys the engine reads but never changes.
CONTEXT_KEYS = ["support_type", "layer_height", "enable_prime_tower", "draft_shield", "print_sequence",
                "support_on_build_plate_only"]
# Process keys that give part features their own filament (1-based; 0 means the object's filament).
FEATURE_FILAMENT_KEYS = ("wall_filament", "sparse_infill_filament", "solid_infill_filament")
# Questions only the user can answer (section 6). Unchecked means "not sure": nothing changes.
QUESTIONS = ("top_hidden", "vase", "holds_on_bed", "purge_infill", "purge_support")
# Shell thickness reasons, one per side so each reads naturally once translated.
SHELL_THICKNESS_TOP = ("%g mm forces %d top layers at %g mm layer height, overriding the %d layers above. "
                       "%g mm keeps %d layers.")
# Orca's default top shell thickness: below it, thin layers would leave a weak, pillowed top.
TOP_SHELL_MIN = 0.6
SHELL_THICKNESS_TOP_MIN = ("%g mm forces %d top layers at %g mm layer height, overriding the %d layers above. "
                           "%g mm, Orca's default, still keeps the top thick enough with thin layers.")
SHELL_THICKNESS_BOTTOM = ("%g mm forces %d bottom layers at %g mm layer height, overriding the %d layers above. "
                          "%g mm keeps %d layers.")


def material_family(filament_type, preset_name=""):
    kind = (filament_type or "").strip().upper()
    name = (preset_name or "").upper()
    base = kind.split("-")[0]
    if kind in ("PVA", "BVOH"):
        return "support"
    if base in ("PLA", "PLA+"):
        return "silk" if "SILK" in kind or "SILK" in name else "pla"
    if base in ("PETG", "PET", "PCTG"):
        return "petg"
    if base in ("ABS", "ASA", "HIPS"):  # HIPS warps like ABS when it is the part's material
        return "abs_asa"
    if base in ("TPU", "TPE", "FLEX"):
        return "tpu"
    if base in ("PA", "PA6", "PA11", "PA12", "PAHT", "PPA", "NYLON"):
        return "pa"
    if base == "PC":
        return "pc"
    return "unknown"


def _number(value):
    try:
        number = float(str(value).strip().rstrip("%"))
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _same(a, b):
    na, nb = _number(a), _number(b)
    if na is not None and nb is not None:
        return abs(na - nb) < 1e-6
    return str(a).strip() == str(b).strip()


def _effect(key, current, target):
    """'saves', 'costs' or '' for a change from current to target."""
    if key == "sparse_infill_pattern":
        if current in PATTERN_ORDER and target in PATTERN_ORDER:
            return "saves" if PATTERN_ORDER.index(target) < PATTERN_ORDER.index(current) else "costs"
        return "saves" if target in ("lightning", "supportcubic", "adaptivecubic") else ""
    if key == "support_interface_bottom_layers":
        # -1 means "same as the top interface".
        return "saves" if target == "0" else ""
    if key == "brim_type":
        if current in BRIM_ORDER and target in BRIM_ORDER:
            return "saves" if BRIM_ORDER.index(target) < BRIM_ORDER.index(current) else "costs"
        return ""
    if key in BOOL_KEYS:
        return "saves" if target == "1" else "costs"
    for order_key, order in (("ensure_vertical_shell_thickness", VERTICAL_SHELL_ORDER), ("gap_fill_target", GAP_FILL_ORDER)):
        if key == order_key:
            if current in order and target in order:
                return "saves" if order.index(target) < order.index(current) else "costs"
            return ""
    if key == "timelapse_type" or key in SWITCH_KEYS:
        return ""
    lower_saves = key not in HIGHER_SAVES
    nc, nt = _number(current), _number(target)
    if nc is None or nt is None:
        return ""
    return "saves" if (nt < nc) == lower_saves else "costs"


def suggest(current, purpose, strength, finish, reliability, filament_type="", filament_name="", family=None,
            answers=None, filaments=1, same_family=True, lang="en", other_filaments=(), support_filament=False):
    """Suggested changes for the current settings.

    ``current`` maps setting keys to Orca's string values. ``family`` overrides
    the material family detected from the filament profile. ``answers`` holds the
    questions the user confirmed (QUESTIONS); ``filaments`` is the number of
    filaments in the project and ``same_family`` whether they all share one family.
    ``other_filaments`` lists (filament_type, preset_name) of the project's other part filaments
    (not support materials): the strictest rule across all of them applies.
    ``lang`` picks the language of labels, reasons and notes (LANGUAGES). ``support_filament`` says
    whether a support material (PVA, BVOH, or a filament flagged as support) is loaded.
    Returns a list of dicts with key, label, current, suggested, reason and
    effect, in display order, containing only settings that would change.
    """
    t = translator(lang)
    spec = PURPOSES[purpose]
    floors = spec["floors"]
    detected = material_family(filament_type, filament_name)
    overridden = family in FAMILY_LABELS
    family = family if overridden else detected
    # Every part material of the project; the user's pick in the material selector replaces them.
    families = {family} if overridden else \
        {family} | {material_family(t_, n_) for t_, n_ in other_filaments} - {"support"}
    targets, reasons, effects, groups = {}, {}, {}, {}
    notes = []
    answers = answers or {}

    def put(key, value, why):
        targets[key] = str(value)
        reasons[key] = why

    # Material limits that act on the sliders themselves.
    raised_by, min_r = family, 0
    for each in sorted(families):
        need = min(reliability + 1, 5) if each == "unknown" else {"abs_asa": 3, "pa": 3, "pc": 4}.get(each, 0)
        if need > min_r:
            raised_by, min_r = each, need
    eff_r = max(reliability, min_r)

    # Strength (S).
    walls, density, pattern, top, bottom = STRENGTH[strength]
    put("wall_loops", walls, t("Strength %d/5: %d walls. Walls add more strength per gram than infill.") % (strength, walls))
    put("sparse_infill_density", "%d%%" % density, t("Strength %d/5: %d%% sparse infill.") % (strength, density))
    put("sparse_infill_pattern", pattern, t("Strength %d/5: %s, %s.") % (strength, pattern, t(PATTERN_NOTES[pattern])))
    put("bottom_shell_layers", bottom, t("Strength %d/5: %d bottom layers.") % (strength, bottom))
    _, gap_fill, min_sparse_area = STRENGTH_SOLID[strength]
    # Sloped surfaces are visible, so a high finish keeps them reinforced even at low strength.
    vertical_shell = STRENGTH_SOLID[max(strength, finish)][0]
    put("ensure_vertical_shell_thickness", vertical_shell,
        t("Strength %d/5 and finish %d/5: %s.") % (strength, finish, t(VERTICAL_SHELL_TEXT[vertical_shell])))
    # Gap fill only goes down: Orca's default is none, and more of it adds filament for little strength.
    current_gap = current.get("gap_fill_target")
    if current_gap in GAP_FILL_ORDER and GAP_FILL_ORDER.index(gap_fill) < GAP_FILL_ORDER.index(current_gap):
        put("gap_fill_target", gap_fill,
            t("Strength %d/5: %s.") % (strength, t("gap fill only on top and bottom surfaces, not on internal solid infill")))
    if purpose == "miniature":
        # Thin limbs and weapons are small areas: Orca's default keeps them solid.
        min_sparse_area = max(min_sparse_area, 15)
    put("minimum_sparse_infill_area", min_sparse_area,
        t("Strength %d/5: only sparse areas under %d mm² become solid infill.") % (strength, min_sparse_area))

    # Finish (V).
    top_delta, one_wall, interface = FINISH[finish]
    top_layers = max(3, top + top_delta) if top_delta < 0 else top + top_delta
    put("top_shell_layers", top_layers, t("Strength %d/5 and finish %d/5: %d top layers.") % (strength, finish, top_layers))
    put("only_one_wall_top", one_wall,
        t("Finish %d/5: %s.") % (finish, t("one wall on flat tops saves filament" if one_wall == "1" else "full walls on top for a cleaner surface")))

    # Reliability (R).
    brim, skirt = RELIABILITY[eff_r]
    r_text = t("Reliability %d/5") % eff_r + (t(" (raised for %s)") % t(FAMILY_LABELS[raised_by]) if eff_r != reliability else "")
    warping = families & {"abs_asa", "pa", "pc"}
    # Brim and raft hold the part on the bed: they only go down when the user says the part holds
    # by itself, and never for materials that warp (section 7). Skirt only goes down: the start
    # G-code primes the nozzle.
    holds = answers.get("holds_on_bed") and not warping
    current_brim = current.get("brim_type")
    if holds and current_brim in BRIM_ORDER and BRIM_ORDER.index(brim) < BRIM_ORDER.index(current_brim):
        put("brim_type", brim, t("You said the part holds on the bed by itself. %s: %s.") % (r_text, t(BRIM_TEXT[brim])))
    current_skirt = _number(current.get("skirt_loops"))
    if current_skirt is not None and skirt < current_skirt:
        put("skirt_loops", skirt, t("%s: %d skirt loop(s).") % (r_text, skirt))
    skirt = min(skirt, current_skirt) if current_skirt is not None else skirt
    first_layer = RELIABILITY_FIRST_LAYER[eff_r]
    put("only_one_wall_first_layer", first_layer,
        t("%s: %s.") % (r_text, t("one wall on the first layer; its outline is slightly weaker" if first_layer == "1"
                       else "normal walls on the first layer for a firm base outline")))
    if skirt > 0 and current.get("draft_shield") == "disabled" and (_number(current.get("skirt_height")) or 1) > 1:
        put("skirt_height", 1, t("%s: one skirt layer is enough without a draft shield.") % r_text)
    tower_brim = RELIABILITY_TOWER_BRIM[eff_r]
    current_tower_brim = _number(current.get("prime_tower_brim_width"))
    if filaments > 1 and current.get("enable_prime_tower") == "1" and tower_brim is not None \
            and current_tower_brim is not None and current_tower_brim > tower_brim:
        put("prime_tower_brim_width", tower_brim,
            t("%s: %d mm brim around the prime tower; a wider one only matters for tall, narrow towers.") % (r_text, tower_brim))
    if holds and (_number(current.get("raft_layers")) or 0) > 0 and eff_r <= 4:
        put("raft_layers", 0, t("You said the part holds on the bed by itself. %s: no raft, which prints several layers under the whole part.") % r_text)

    supports_on = current.get("enable_support") == "1"
    if supports_on:
        # Where supports go and how they are built (threshold, small overhangs, style, base pattern
        # and spacing, tree walls) is never reduced: whether an overhang needs that support depends
        # on the part, which the plugin does not guess. Only the interface, which sets how the
        # supported surface looks, follows the Finish slider.
        put("support_interface_top_layers", interface, t("Finish %d/5: %d support interface layers.") % (finish, interface))
        bottom_interface = current.get("support_interface_bottom_layers")
        if bottom_interface not in (None, "0") and finish <= 3:
            put("support_interface_bottom_layers", 0, t("Finish %d/5: no interface where supports rest on the part.") % finish)
        spacing = FINISH_INTERFACE_SPACING[finish]
        current_spacing = _number(current.get("support_interface_spacing"))
        if spacing is not None and current_spacing is not None and current_spacing < spacing:
            put("support_interface_spacing", "%g" % spacing,
                t("Finish %d/5: %g mm between support interface lines; the underside of overhangs is rougher.") % (finish, spacing))

    # Material (section 7).
    forbidden = set(floors["forbid"])
    label = t(FAMILY_LABELS[family])
    purpose_label = t(spec["label"])
    if "petg" in families and supports_on:
        # A support material is printed with no gap on purpose (soluble or breakaway interface).
        if not support_filament and (_number(current.get("support_top_z_distance")) or 0) < 0.25:
            put("support_top_z_distance", "0.25", t("PETG sticks to supports: at least a 0.25 mm gap."))
        if int(targets["support_interface_top_layers"]) < 2:
            put("support_interface_top_layers", 2, t("PETG sticks to supports: at least 2 interface layers."))
    warping = [(each, {"abs_asa": 5, "pa": 5, "pc": 8}[each]) for each in sorted(families) if each in ("abs_asa", "pa", "pc")]
    brim_minimums = {}
    if warping:
        warp_family, min_brim = max(warping, key=lambda item: item[1])
        warp_label = t(FAMILY_LABELS[warp_family])
        # Only raised to the minimum: a wider or fuller brim the user set against warping is kept.
        current_brim = current.get("brim_type")
        needed = "outer_only" if "pc" in families or eff_r == 5 else "brim_ears"
        brim_minimums = {
            "brim_type": (needed, t("%s warps: a full outer brim is required.") % warp_label if needed == "outer_only" else
                          t("%s warps at the corners: brim ears hold them down with far less filament than a full brim.") % warp_label),
            "brim_width": (str(min_brim), t("%s warps: brim of at least %d mm.") % (warp_label, min_brim)),
        }
        if current_brim in OWN_BRIMS:
            notes.append(t("Your %s brim is kept. %s warps: make sure it holds the corners down.") % (current_brim, warp_label))
        elif current_brim == "auto_brim":
            # Orca sizes the auto brim itself, from the material and the part: brim ears or a fixed
            # width could hold less than what it would print.
            brim_minimums = {}
            notes.append(t("Orca's auto brim is kept: it sizes the brim for %s and the part itself.") % warp_label)
        elif current_brim not in BRIM_ORDER or BRIM_ORDER.index(current_brim) < BRIM_ORDER.index(needed):
            if needed == "outer_only":
                put("brim_type", "outer_only", t("%s warps: a full outer brim is required.") % warp_label)
            else:
                put("brim_type", "brim_ears",
                    t("%s warps at the corners: brim ears hold them down with far less filament than a full brim.") % warp_label)
        current_width = _number(current.get("brim_width"))
        if current_brim != "auto_brim" and (current_width is None or current_width < min_brim):
            put("brim_width", min_brim, t("%s warps: brim of at least %d mm.") % (warp_label, min_brim))
    if "silk" in families and purpose in ("functional", "mechanical") and int(targets["wall_loops"]) < 3:
        put("wall_loops", 3, t("Silk PLA has weak layer adhesion: at least 3 walls for a functional part."))
    if "tpu" in families:
        forbidden.add("lightning")
        for key in ("sparse_infill_density", "sparse_infill_pattern", "minimum_sparse_infill_area"):
            targets.pop(key, None)
            reasons.pop(key, None)

    # Floors and ceilings of the purpose (section 9).
    if int(targets["wall_loops"]) < floors["walls"]:
        put("wall_loops", floors["walls"], t("%s needs at least %d walls.") % (purpose_label, floors["walls"]))
    if int(targets["wall_loops"]) > MAX_WALLS:
        put("wall_loops", MAX_WALLS, t("Capped at %d walls.") % MAX_WALLS)
    if "sparse_infill_density" in targets:
        infill = _number(targets["sparse_infill_density"])
        if infill < floors["infill"]:
            put("sparse_infill_density", "%d%%" % floors["infill"], t("%s needs at least %d%% infill.") % (purpose_label, floors["infill"]))
        elif infill > MAX_INFILL:
            put("sparse_infill_density", "%d%%" % MAX_INFILL, t("Capped at %d%% infill.") % MAX_INFILL)
        chosen = targets["sparse_infill_pattern"]
        if chosen in forbidden:
            fallback = next(p for p in PATTERN_ORDER if p not in forbidden)
            put("sparse_infill_pattern", fallback,
                t("%s is not allowed for %s; using %s, %s.") % (chosen, purpose_label if chosen in floors["forbid"] else label,
                                                             fallback, t(PATTERN_NOTES[fallback])))
    if int(targets["top_shell_layers"]) < floors["top"]:
        put("top_shell_layers", floors["top"], t("%s needs at least %d top layers.") % (purpose_label, floors["top"]))
    if int(targets["bottom_shell_layers"]) < floors["bottom"]:
        put("bottom_shell_layers", floors["bottom"], t("%s needs at least %d bottom layers.") % (purpose_label, floors["bottom"]))

    # Same thickness at other layer heights and nozzles: fine layers keep a solid shell, coarse
    # ones do not waste filament.
    layer_height = _number(current.get("layer_height"))
    nozzle = _number(str(current.get("nozzle_diameter") or "").split(",")[0])
    scale_layers = layer_height and abs(layer_height - REFERENCE_LAYER) > 1e-6
    scale_walls = nozzle and abs(nozzle - REFERENCE_NOZZLE) > 1e-6

    def shell_layers(reference):
        # At least 2 layers: a single layer would be the only solid layer over the infill.
        return max(2, math.ceil(reference * REFERENCE_LAYER / layer_height - 1e-9)) if scale_layers else reference

    def wall_count(reference):
        return max(min(reference, 2), math.ceil(reference * REFERENCE_NOZZLE / nozzle - 1e-9)) if scale_walls else reference

    for side in ("top", "bottom"):
        key = "%s_shell_layers" % side
        reference = int(targets[key])
        if shell_layers(reference) != reference:
            put(key, shell_layers(reference), reasons[key] + " " + t("That is %g mm: %d layers at %g mm layer height.")
                % (round(reference * REFERENCE_LAYER, 2), shell_layers(reference), layer_height))
    if "wall_loops" in targets and wall_count(int(targets["wall_loops"])) != int(targets["wall_loops"]):
        reference = int(targets["wall_loops"])
        put("wall_loops", wall_count(reference), reasons["wall_loops"] + " " + t("That is %g mm of wall: %d walls with a %g mm nozzle.")
            % (round(reference * REFERENCE_NOZZLE, 2), wall_count(reference), nozzle))

    # Minimums of the purpose and the material. Slider values above them only ever save filament:
    # a setting already leaner than the slider, but at or above its minimum, is left as it is.
    def layers_reason(text, reference):
        # The floors count layers at 0.2 mm: say what that is at this layer height.
        if shell_layers(reference) == reference:
            return text
        return text + " " + t("That is %g mm: %d layers at %g mm layer height.") % (
            round(reference * REFERENCE_LAYER, 2), shell_layers(reference), layer_height)

    def walls_reason(text, reference):
        if wall_count(reference) == reference:
            return text
        return text + " " + t("That is %g mm of wall: %d walls with a %g mm nozzle.") % (
            round(reference * REFERENCE_NOZZLE, 2), wall_count(reference), nozzle)

    minimums = {
        **brim_minimums,
        "wall_loops": (wall_count(floors["walls"]),
                       walls_reason(t("%s needs at least %d walls.") % (purpose_label, floors["walls"]), floors["walls"])),
        "top_shell_layers": (shell_layers(floors["top"]), layers_reason(
            t("%s needs at least %d top layers.") % (purpose_label, floors["top"]), floors["top"])),
        "bottom_shell_layers": (shell_layers(floors["bottom"]), layers_reason(
            t("%s needs at least %d bottom layers.") % (purpose_label, floors["bottom"]), floors["bottom"])),
    }
    if "silk" in families and purpose in ("functional", "mechanical") and floors["walls"] < 3:
        minimums["wall_loops"] = (wall_count(3), walls_reason(
            t("Silk PLA has weak layer adhesion: at least 3 walls for a functional part."), 3))
    if "sparse_infill_density" in targets:
        minimums["sparse_infill_density"] = ("%d%%" % floors["infill"],
                                             t("%s needs at least %d%% infill.") % (purpose_label, floors["infill"]))
        if current.get("sparse_infill_pattern") in forbidden:
            allowed = next(p for p in PATTERN_ORDER if p not in forbidden)
            chosen = current["sparse_infill_pattern"]
            minimums["sparse_infill_pattern"] = (allowed, t("%s is not allowed for %s; using %s, %s.") % (
                chosen, purpose_label if chosen in floors["forbid"] else label, allowed, t(PATTERN_NOTES[allowed])))
    if "petg" in families and supports_on:
        minimums["support_interface_top_layers"] = ("2", t("PETG sticks to supports: at least 2 interface layers."))

    # Answers the user confirmed (section 6). Nothing here is inferred from the model.
    if filaments > 1 and (answers.get("purge_infill") or answers.get("purge_support")):
        if current.get("enable_prime_tower") == "0":
            notes.append(t("Orca only purges into infill or supports when the prime tower is on, so the purge answers changed nothing."))
        else:
            if answers.get("purge_infill") and not same_family:
                notes.append(t("The filaments are different materials and may not bond inside the part, so purge into the infill is not suggested."))
            elif answers.get("purge_infill") and _number(targets.get("sparse_infill_density", current.get("sparse_infill_density"))):
                put("flush_into_infill", "1",
                    t("You said the walls hide the infill: color-change purge goes into the infill instead of the prime tower."))
            if answers.get("purge_support") and supports_on:
                put("flush_into_support", "1",
                    t("You said mixed colors in the supports are fine: color-change purge goes into them instead of the prime tower."))

    # Orca prints max(layers, thickness / layer height) solid layers, so a shell thickness above
    # the suggested layer count would silently override it. Lower it to match the layer count.
    layer_height = _number(current.get("layer_height"))
    if layer_height:
        for side in ("top", "bottom"):
            layers = int(targets["%s_shell_layers" % side])
            thickness = _number(current.get("%s_shell_thickness" % side))
            matching = round(layers * layer_height, 2)
            # The top keeps Orca's minimum thickness, which protects thin layer heights.
            target = max(matching, TOP_SHELL_MIN) if side == "top" else matching
            if thickness is not None and thickness > target + 1e-6:
                forced = math.ceil(thickness / layer_height - 1e-9)
                if target > matching:
                    put("top_shell_thickness", "%g" % target,
                        t(SHELL_THICKNESS_TOP_MIN) % (thickness, forced, layer_height, layers, target))
                else:
                    template = (SHELL_THICKNESS_TOP if side == "top" else SHELL_THICKNESS_BOTTOM)
                    put("%s_shell_thickness" % side, "%g" % matching,
                        t(template) % (thickness, forced, layer_height, layers, matching, layers))

    # Vase mode (section 6), last so that no floor brings back walls, infill or top layers.
    if answers.get("vase"):
        for key in VASE_DROPS + tuple(k for k in targets if k.startswith("support_")):
            targets.pop(key, None)
            reasons.pop(key, None)
        put("spiral_mode", "1", t("You said this is an open container that can print as one continuous outer wall: "
                                  "vase mode uses the least filament possible."))
        groups["spiral_mode"] = "vase"
        for key, value in VASE_REQUIRES:
            put(key, value, t("Required by vase mode; Orca does not print a vase without it."))
            groups[key] = "vase"
        effects["enable_support"] = "saves"
        notes.append(t("Vase mode prints only the outside of the part, one wall thick, with an open top. "
                       "A model with thick walls or an inner cavity comes out as a thin shell."))

    # Only changes that save filament, plus increases up to a minimum: a change that uses more
    # filament, or whose effect is not known (e.g. from a pattern the tables do not rank), is kept
    # only when the current value is below what the purpose or the material needs, and then only
    # up to that minimum.
    for key in list(targets):
        if key not in current or key in effects or key in groups:
            continue
        if _effect(key, current[key], targets[key]) == "saves":
            continue
        minimum = minimums.get(key)
        below = minimum is not None and (key == "sparse_infill_pattern" or _effect(key, current[key], minimum[0]) == "costs")
        if not below:
            targets.pop(key)
            reasons.pop(key)
        elif key == "sparse_infill_pattern" or _effect(key, minimum[0], targets[key]) == "costs":
            put(key, minimum[0], minimum[1])

    if answers.get("top_hidden") and not answers.get("vase"):
        # Orca applies the density to every top surface, but only to its last layer; the solid
        # layers below it still close the part. Counted on the final settings, after the filter
        # above, including the layers a shell thickness forces.
        top_layers = int(_number(targets.get("top_shell_layers", current.get("top_shell_layers"))) or 0)
        thickness = _number(targets.get("top_shell_thickness", current.get("top_shell_thickness"))) or 0
        if layer_height:
            top_layers = max(top_layers, math.ceil(thickness / layer_height - 1e-9))
        if top_layers >= 3:
            put("top_surface_density", "0%",
                t("You said the faces pointing up while printing will be hidden: skip their last layer. The %d solid layers below it still close the part.") % (top_layers - 1))

    changes = []
    for key, label_text in SETTINGS:
        if key not in targets or key not in current:
            continue
        if _same(current[key], targets[key]):
            continue
        changes.append({
            "id": key,
            "key": key,
            "label": t(label_text),
            "unit": UNITS.get(key, ""),
            "current": current[key],
            "suggested": targets[key],
            "reason": reasons[key],
            "effect": effects.get(key, _effect(key, current[key], targets[key])),
            "bool": key in SWITCH_KEYS,
            "values": {value: t(name) for value, name in VALUE_NAMES.get(key, {}).items()},
            "group": groups.get(key, ""),
            "status": "pending",
        })
    if family != detected:
        notes.append(t("Using %s instead of the detected %s.") % (label, t(FAMILY_LABELS[detected])))
    if len(families) > 1:
        notes.append(t("Several materials (%s): the strictest rule of each applies.")
                     % ", ".join(t(FAMILY_LABELS[each]) for each in sorted(families)))
        for one, other in POOR_BONDS:
            if families & one and families & other:
                first, second = sorted(families & one)[0], sorted(families & other)[0]
                notes.append(t("%s and %s bond poorly to each other: where they meet inside one part, it can split there.")
                             % (t(FAMILY_LABELS[first]), t(FAMILY_LABELS[second])))
    if "tpu" in families:
        notes.append(t("TPU: infill sets the hardness of the part, so infill is left unchanged."))
    if "unknown" in families:
        unknown = [kind for kind, name in [(filament_type, filament_name)] + list(other_filaments)
                   if material_family(kind, name) == "unknown"] if not overridden else [filament_type]
        notes.append(t("Unknown material '%s': PLA rules with extra reliability.") % (", ".join(k or "?" for k in unknown) or "?"))
    if "silk" in families and purpose in ("functional", "mechanical"):
        notes.append(t("Silk PLA is not recommended for functional parts."))
    if purpose == "container":
        notes.append(t("These suggestions were not tested on containers that must hold liquid; whether one leaks is not checked."))
    if purpose == "miniature" and (_number(current.get("layer_height")) or 0) > 0.16 + 1e-9:
        # A note only: layer height sets detail and time, not filament.
        notes.append(t("%g mm layers are coarse for a miniature: faces and fine detail such as chainmail fade above 0.16 mm. Miniature guides use 0.08–0.12 mm.")
                     % _number(current.get("layer_height")))
    if purpose == "miniature" and supports_on:
        # Notes only: whether the figure needs that support depends on the model.
        if current.get("support_on_build_plate_only") == "1":
            notes.append(t("On build plate only is on: parts of the figure above itself (arms, capes, weapons over the base) get no support."))
        if current.get("support_remove_small_overhang") == "1":
            notes.append(t("Ignore small overhangs is on: small overhangs such as fingers, chins and weapon tips get no support."))
        if str(current.get("support_type", "")).startswith("tree") and current.get("support_style") in ("organic", "default"):
            notes.append(t("Organic supports: Orca's tree support brim only applies to Tree Slim, Strong and Hybrid, so tall organic branches have no brim of their own."))
    if "pa" in families:
        notes.append(t("Nylon absorbs moisture: dry the filament before printing."))
    return {"family": family, "family_label": label, "detected": detected, "families": sorted(families),
            "changes": changes, "notes": notes}


# --- print estimate from exported G-code -------------------------------------

_DHMS = re.compile(r"(\d+)\s*([dhms])")
_TIME_PATTERNS = (
    re.compile(r"^; estimated printing time \(normal mode\) = (.+)$"),
    re.compile(r"total estimated time: ([^;]+)"),
)
_WEIGHT_TOTAL = re.compile(r"^; total filament used \[g\] = ([\d.]+)")
_WEIGHT_LIST = re.compile(r"^; filament used \[g\] = ([\d.,\s]+)")
_COST_TOTAL = re.compile(r"^; total filament cost = ([\d.]+)")
_COST_LIST = re.compile(r"^; filament cost = ([\d.,\s]+)")


def parse_duration(text):
    """Seconds in an Orca duration such as '1d 2h 3m 4s', or None."""
    parts = _DHMS.findall(text or "")
    if not parts:
        return None
    scale = {"d": 86400, "h": 3600, "m": 60, "s": 1}
    return sum(int(amount) * scale[unit] for amount, unit in parts)


def _number_list(text):
    return [_number(v) for v in text.split(",") if v.strip()]


def _sum_list(text):
    values = [_number(v) for v in text.split(",")]
    values = [v for v in values if v is not None]
    return sum(values) if values else None


GCODE_TAIL_BYTES = 256 * 1024  # Orca writes the statistics in the last lines of the file


def parse_gcode_estimate(path):
    """Print time, filament grams and cost from an Orca G-code file.

    Reads the end of the file first: a big print's G-code is hundreds of MB, and this runs on
    Orca's UI thread after every slice. The whole file is read only when the end lacks them."""
    with open(path, "rb") as stream:
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        stream.seek(max(0, size - GCODE_TAIL_BYTES))
        tail = stream.read().decode("utf-8", errors="replace").splitlines()
    found = _parse_estimate_lines(tail[1:] if size > GCODE_TAIL_BYTES else tail)  # the first line may be cut
    if found["seconds"] is not None and found["grams"] is not None:
        return found
    with open(path, "r", encoding="utf-8", errors="replace") as stream:
        return _parse_estimate_lines(stream)


def _parse_estimate_lines(lines):
    found = {"seconds": None, "grams": None, "cost": None}
    grams_list = cost_list = None
    grams_each = cost_each = None
    in_config = False
    for line in lines:
        if not line.startswith(";"):
            continue
        line = line.rstrip("\n")
        # The statistics come after the config block, whose keys (e.g. filament_cost)
        # must not be read as results.
        if line.startswith("; CONFIG_BLOCK_START"):
            in_config = True
        elif line.startswith("; CONFIG_BLOCK_END"):
            if not in_config:
                # The end of a file was read from inside the config block: drop what came before.
                found = {"seconds": None, "grams": None, "cost": None}
                grams_list = cost_list = grams_each = cost_each = None
            in_config = False
        if in_config:
            continue
        if found["seconds"] is None:
            for pattern in _TIME_PATTERNS:
                match = pattern.search(line)
                if match:
                    found["seconds"] = parse_duration(match.group(1))
                    break
        match = _WEIGHT_TOTAL.match(line)
        if match:
            found["grams"] = float(match.group(1))
        match = _WEIGHT_LIST.match(line)
        if match:
            grams_list = _sum_list(match.group(1))
            grams_each = _number_list(match.group(1))
        match = _COST_TOTAL.match(line)
        if match:
            found["cost"] = float(match.group(1))
        match = _COST_LIST.match(line)
        if match:
            cost_list = _sum_list(match.group(1))
            cost_each = _number_list(match.group(1))
    if found["grams"] is None:
        found["grams"] = grams_list
    if found["cost"] is None:
        found["cost"] = cost_list
    # Per filament, in the order of Orca's filament list; used to price each spool.
    found["grams_each"] = grams_each
    found["cost_each"] = cost_each
    return found


# --- host helpers ------------------------------------------------------------

def _data_dir():
    parts = os.path.abspath(__file__).replace("\\", "/").split("/")
    if "orca_plugins" not in parts:
        raise RuntimeError("Install this file under <data_dir>/orca_plugins/<folder>/")
    return "/".join(parts[: parts.index("orca_plugins")])


def _preset_folder(bundle):
    for collection in (bundle.prints, bundle.filaments, bundle.printers):
        for index in range(collection.size()):
            parts = os.path.normpath(collection.preset(index).file or "").replace("\\", "/").split("/")
            if "user" in parts and parts.index("user") + 1 < len(parts) - 1:
                return parts[parts.index("user") + 1]
    return "default"


def _write_json_atomic(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix="." + os.path.basename(path) + ".", suffix=".tmp", dir=os.path.dirname(path)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _system_parent(bundle, preset):
    if preset.is_system:
        return preset
    inherits = preset.config_value("inherits")
    if isinstance(inherits, str) and inherits:
        parent = bundle.prints.find_preset(inherits)
        if parent is not None and parent.is_system:
            return parent
    return None


def _write_editor():
    editor = getattr(getattr(orca.host, "preset", None), "process", None)
    if editor is None or not hasattr(editor, "begin_update"):
        return None
    return editor


def _storage_path(name):
    return os.path.join(str(orca.host.plugin.storage()), name)


def _read_estimates():
    """{"current": fingerprint, "models": {fingerprint: {"latest": ..., "previous": ...}}}."""
    try:
        with open(_storage_path(ESTIMATES_FILE), "r", encoding="utf-8") as stream:
            data = json.load(stream)
    except (FileNotFoundError, ValueError, OSError):
        data = {}
    # Older files kept one global pair of slices, from any project: dropped.
    if not isinstance(data, dict) or not isinstance(data.get("models"), dict):
        return {"current": None, "models": {}}
    return data


def _saved_total(data):
    """Validated running total of what slices after Filament Saver's changes saved:
    {"grams", "seconds", "cost", "currency", "count", "since"}, or None."""
    saved = data.get("saved") if isinstance(data, dict) else None
    if not isinstance(saved, dict):
        return None
    grams, seconds = _number(saved.get("grams")), _number(saved.get("seconds"))
    count = _number(saved.get("count"))
    if grams is None or seconds is None or not count or count < 1:
        return None
    cost = _number(saved.get("cost"))
    currency = saved.get("currency") if saved.get("currency") in CURRENCIES else None
    return {"grams": grams, "seconds": seconds, "cost": cost if currency else None, "currency": currency,
            "count": int(count), "since": _number(saved.get("since")) or 0}


def add_saved(data, previous, latest, prices, currency):
    """Add what changed from previous to latest (same parts) to the running total in data.

    Only called when Filament Saver applied or undid changes between the two slices, so the
    total follows its suggestions; an undo that brings filament back subtracts it again."""
    total = _saved_total(data) or {"grams": 0.0, "seconds": 0.0, "cost": 0.0, "currency": currency,
                                   "count": 0, "since": latest.get("at", time.time())}
    if None in (previous.get("grams"), latest.get("grams"), previous.get("seconds"), latest.get("seconds")):
        return
    total["grams"] = round(total["grams"] + previous["grams"] - latest["grams"], 2) + 0.0
    total["seconds"] = total["seconds"] + previous["seconds"] - latest["seconds"]
    before, after = priced_cost(previous, prices)[0], priced_cost(latest, prices)[0]
    # Costs in another currency than the total's cannot be added up.
    if total["cost"] is not None and total["currency"] == currency and None not in (before, after):
        total["cost"] = round(total["cost"] + before - after, 2) + 0.0  # + 0.0 turns -0.0 into 0.0
    else:
        total["cost"] = None
    total["count"] += 1
    data["saved"] = total


def _read_prices():
    """{filament preset name: {"price": spool price, "grams": spool weight}}."""
    try:
        with open(_storage_path(PRICES_FILE), "r", encoding="utf-8") as stream:
            data = json.load(stream)
    except (FileNotFoundError, ValueError, OSError):
        return {}
    return {name: entry for name, entry in data.items()
            if isinstance(entry, dict) and (_number(entry.get("price")) or 0) > 0
            and (_number(entry.get("grams")) or 0) > 0} if isinstance(data, dict) else {}


def _read_storage_json(name):
    try:
        with open(_storage_path(name), "r", encoding="utf-8") as stream:
            data = json.load(stream)
    except (FileNotFoundError, ValueError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def read_currency(lang):
    """{"price": currency of spool prices and Orca's filament cost, "display": currency to show,
    "rates_allowed": whether the user allowed fetching exchange rates (off until they check it)}."""
    data = _read_storage_json(CURRENCY_FILE)
    price = data.get("price") if data.get("price") in CURRENCIES else ("BRL" if lang == "pt_BR" else "USD")
    display = data.get("display") if data.get("display") in CURRENCIES else price
    return {"price": price, "display": display, "rates_allowed": data.get("rates_allowed") is True}


def read_rates():
    """Cached rates: {"base", "date", "fetched_at", "rates": {code: units per 1 base}}, validated."""
    data = _read_storage_json(RATES_FILE)
    rates = data.get("rates") if isinstance(data.get("rates"), dict) else {}
    rates = {code: _number(value) for code, value in rates.items() if code in CURRENCIES}
    rates = {code: value for code, value in rates.items() if value and value > 0}
    if data.get("base") not in CURRENCIES:
        return {}
    rates[data["base"]] = 1.0
    return {"base": data["base"], "date": str(data.get("date") or ""), "rates": rates,
            "fetched_at": _number(data.get("fetched_at")) or 0}


def conversion(source, target, rates):
    """Multiplier from source to target currency with the cached rates, or None when unknown."""
    if source == target:
        return 1.0
    table = rates.get("rates") or {}
    if source in table and target in table:
        return table[target] / table[source]
    return None


def _ssl_context():
    """Verifying TLS context; adds the system's CA bundle when Python has no certificates."""
    context = ssl.create_default_context()
    if not context.cert_store_stats().get("x509_ca"):
        for path in SYSTEM_CA_FILES:
            try:
                context.load_verify_locations(cafile=path)
                break
            except (OSError, ssl.SSLError):
                continue
    return context


def fetch_rates(base):
    """Download today's rates for base and cache them. Runs on a worker thread."""
    if base not in CURRENCIES:
        raise ValueError("Unknown currency %r" % base)
    with urllib.request.urlopen(RATES_URL % base, timeout=10, context=_ssl_context()) as response:
        data = json.loads(response.read(1_000_000).decode("utf-8"))
    rates = {code: value for code, value in (data.get("rates") or {}).items()
             if code in CURRENCIES and isinstance(value, (int, float)) and math.isfinite(value) and value > 0}
    if data.get("base") != base or not rates:
        raise ValueError("Unexpected answer from the exchange-rate service")
    _write_json_atomic(_storage_path(RATES_FILE), {"base": base, "date": str(data.get("date") or ""),
                                                   "rates": rates, "fetched_at": time.time()})


def priced_cost(estimate, prices):
    """(cost, uses_spool_price) for a slice.

    Each filament with a spool price set by the user costs grams × price / spool weight;
    the others keep the cost Orca wrote from the filament profile (filament_cost per kg).
    Prices are applied when the card is shown, so changing a price changes every slice
    alike and never shows up as a difference between slices.
    """
    names = estimate.get("filaments") or []
    grams = estimate.get("grams_each") or ([estimate.get("grams")] if len(names) == 1 else [])
    costs = estimate.get("cost_each") or ([estimate.get("cost")] if len(names) == 1 else [])
    if not any(name in prices for name in names) or len(grams) != len(names):
        return estimate.get("cost"), False
    total = 0.0
    for index, name in enumerate(names):
        if grams[index] is None:
            return estimate.get("cost"), False
        if name in prices:
            total += grams[index] * float(prices[name]["price"]) / float(prices[name]["grams"])
        elif index < len(costs) and costs[index] is not None:
            total += costs[index]
        else:
            return estimate.get("cost"), False
    return round(total, 2), True


def _current_estimates(rate=1.0):
    """Last two slices of the parts sliced last, priced and converted with rate; empty when there
    is nothing to show."""
    data = _read_estimates()
    entry = data["models"].get(data.get("current"))
    if not isinstance(entry, dict):
        return {}
    prices = _read_prices()
    shown = {}
    for which in ("latest", "previous"):
        if isinstance(entry.get(which), dict):
            try:
                cost, priced = priced_cost(entry[which], prices)
                shown[which] = {**entry[which], "cost": round(cost * rate, 2) if cost is not None else None, "priced": priced}
            except (TypeError, ValueError):  # a damaged file must not take the tab down
                traceback.print_exc()
    return shown


def _call(target, name):
    """target.name(), or None when this Orca version does not have it."""
    method = getattr(target, name, None)
    return method() if callable(method) else None


def _rounded(value):
    if isinstance(value, (list, tuple)):
        return [_rounded(v) for v in value]
    return round(value, 3) if isinstance(value, float) else value


def _is_geometry(volume):
    """Parts and negative volumes shape what is printed; modifiers and support enforcers or
    blockers only change settings, so they stay comparable."""
    checks = [_call(volume, name) for name in ("is_model_part", "is_negative_volume")]
    if any(check is not None for check in checks):
        return any(checks)
    kind = str(_call(volume, "type") or "").upper()
    return not kind or "MODEL_PART" in kind or "NEGATIVE" in kind


def model_fingerprint(model):
    """Same value for the same parts at the same size on the same plate.

    Slices are only compared when their fingerprints match, so a difference always comes
    from settings, never from another project, another plate, or parts that were added,
    removed or rescaled. Names, positions, rotations, modifiers and support enforcers or
    blockers are left out: they are settings or do not change the part.
    """
    parts = []
    for obj in model.objects():
        volumes = sorted(json.dumps([str(_call(v, "type")), _call(v, "facets_count"), _rounded(_call(v, "volume")),
                                     _rounded(_call(v, "scaling_factor"))])
                         for v in obj.volumes() if _is_geometry(v))
        instances = sorted(json.dumps(_rounded(_call(i, "scaling_factor"))) for i in obj.instances()
                           if getattr(i, "printable", True))
        parts.append(json.dumps([volumes, instances]))
    payload = json.dumps([_call(model, "current_plate_index"), sorted(parts)])
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def _parts_fingerprint():
    """Fingerprint of the parts in Orca now, or None for an empty plate. UI thread only."""
    try:
        model = orca.host.model()
        if not list(model.objects()):
            return None
        return model_fingerprint(model)
    except Exception:
        traceback.print_exc()
        return None


def _object_overrides():
    """{setting: [object names]} for settings that objects or their parts set themselves, which the
    process preset then does not reach. UI thread only."""
    found = {}
    try:
        for obj in orca.host.model().objects():
            keys = set(_call(obj, "config_keys") or [])
            for volume in obj.volumes():
                keys |= set(_call(volume, "config_keys") or [])
            for key in keys & (set(LABELS) | {"layer_height"}):
                found.setdefault(key, []).append(str(getattr(obj, "name", "") or "?"))
    except Exception:
        traceback.print_exc()
    return found


def _filament_number(value):
    number = _number(value)
    return int(number) if number is not None and number >= 1 and number == int(number) else None


def _printable(item):
    check = getattr(item, "is_printable", None)
    return bool(check()) if callable(check) else bool(getattr(item, "printable", True))


def model_filament_use(model, current):
    """Which filaments the parts print with, read from the objects in Orca.

    Returns {"used": set of 1-based filament numbers, or None when it cannot be told (painted
    multi-material parts), "mixed": names of objects that print with more than one filament,
    "copies": printable copies of all objects}. Modifiers only count when they set a filament;
    support enforcers and blockers never do."""
    used, mixed, copies, painted = set(), [], 0, False
    for obj in model.objects():
        if not _printable(obj):
            continue
        copies += sum(1 for instance in obj.instances() if _printable(instance))
        own = set()
        obj_filament = _filament_number(obj.config_value("extruder")) or 1
        for volume in obj.volumes():
            keys = set(_call(volume, "config_keys") or [])
            if _call(volume, "is_mm_painted"):
                painted = True
                own.add(0)  # some other filament, painted on
            if _call(volume, "is_model_part"):
                own.add(_filament_number(_call(volume, "extruder_id")) or obj_filament)
            elif "extruder" in keys:
                own.add(_filament_number(volume.config_value("extruder")) or obj_filament)
            else:
                continue
            for key in FEATURE_FILAMENT_KEYS:
                value = volume.config_value(key) if key in keys else \
                    obj.config_value(key) if key in set(_call(obj, "config_keys") or []) else current.get(key)
                if _filament_number(value):
                    own.add(_filament_number(value))
        if len(own) > 1:
            mixed.append(str(getattr(obj, "name", "") or "?"))
        used |= own - {0}
    return {"used": None if painted or not used else used, "mixed": mixed, "copies": copies}


def clean_form(data):
    """Purpose, material, sliders and answers from the page, with anything invalid replaced."""
    data = data if isinstance(data, dict) else {}
    purpose = data.get("purpose") if data.get("purpose") in PURPOSES else "decoration"
    values = {}
    for name in ("S", "V", "R"):
        try:
            values[name] = max(0, min(5, int(data.get(name, PURPOSES[purpose][name]))))
        except (TypeError, ValueError):
            values[name] = PURPOSES[purpose][name]
    material = data.get("material") if data.get("material") in MATERIAL_CHOICES else "auto"
    given = data.get("answers") if isinstance(data.get("answers"), dict) else {}
    return {"purpose": purpose, "material": material, **values,
            "answers": {name: bool(given.get(name)) for name in QUESTIONS}}


def _read_forms():
    """{parts fingerprint: {"form": ..., "at": ...}}: the choices last used with each set of parts."""
    try:
        with open(_storage_path(FORMS_FILE), "r", encoding="utf-8") as stream:
            data = json.load(stream)
    except (FileNotFoundError, ValueError, OSError):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}


def _remember_form(fingerprint, form):
    forms = _read_forms()
    forms[fingerprint] = {"form": form, "at": time.time()}
    for old in sorted(forms, key=lambda k: forms[k].get("at", 0))[:-MAX_REMEMBERED_FORMS]:
        del forms[old]
    _write_json_atomic(_storage_path(FORMS_FILE), forms)


def _read_originals():
    """Values to restore, per process preset, kept across Orca and plugin reloads:
    {"presets": {preset: {"originals": {key: value}, "applied": {key: value}}}, "bundle_values": {...}}."""
    try:
        with open(_storage_path(ORIGINALS_FILE), "r", encoding="utf-8") as stream:
            data = json.load(stream)
    except (FileNotFoundError, ValueError, OSError):
        data = {}
    data = data if isinstance(data, dict) else {}

    # Only settings the plugin manages: whatever else is in the file (e.g. post_process, which
    # runs scripts after export) must never reach a preset through Restore.
    def pick(values):
        return {str(k): str(v) for k, v in values.items() if k in LABELS} if isinstance(values, dict) else {}

    presets = {}
    for name, store in (data.get("presets") or {}).items() if isinstance(data.get("presets"), dict) else ():
        if isinstance(name, str) and isinstance(store, dict) and pick(store.get("originals")):
            presets[name] = {"originals": pick(store.get("originals")), "applied": pick(store.get("applied"))}
    # Files written before per-preset storage kept one preset's values at the top level.
    if isinstance(data.get("source"), str) and pick(data.get("originals")) and data["source"] not in presets:
        presets[data["source"]] = {"originals": pick(data.get("originals")), "applied": pick(data.get("applied"))}
    return {"presets": presets, "bundle_values": pick(data.get("bundle_values"))}


def _write_originals(presets, bundle_values):
    kept = {name: {"originals": store["originals"],
                   "applied": {k: v for k, v in store["applied"].items() if k in store["originals"]}}
            for name, store in presets.items() if store["originals"]}
    _write_json_atomic(_storage_path(ORIGINALS_FILE), {"presets": kept, "bundle_values": bundle_values})


# --- translations --------------------------------------------------------------

# Text is written in English and looked up in TRANSLATIONS; a missing entry falls back
# to English. Setting labels follow Orca's own translation (localization/i18n).
LANGUAGES = ("en", "pt_BR")
TRANSLATIONS = {
    "pt_BR": {
        # Setting labels, as in Orca's pt_BR translation (support settings get "(suporte)",
        # since Orca shows them under the Support tab).
        "Wall loops": "Voltas da parede",
        "Sparse infill density": "Densidade do preenchimento esparso",
        "Sparse infill pattern": "Padrão de preenchimento esparso",
        "Top shell layers": "Camadas de topo da casca",
        "Top shell thickness": "Espessura da casca do topo",
        "Bottom shell layers": "Camadas da casca de base",
        "Bottom shell thickness": "Espessura da casca de base",
        "Top surface density": "Densidade da superfície superior",
        "Ensure vertical shell thickness": "Garantir a espessura vertical da casca",
        "Minimum sparse infill area": "Limiar mínimo de preenchimento esparso",
        "Apply gap fill": "Aplicar preenchimento de vão",
        "Only one wall on top surfaces": "Parede única em superfícies superiores",
        "Only one wall on first layer": "Parede única na primeira camada",
        "Brim type": "Tipo de borda",
        "Brim width": "Largura da borda",
        "Skirt loops": "Voltas da saia",
        "Support threshold angle": "Ângulo limiar (suporte)",
        "Ignore small overhangs": "Ignorar pequenas saliências (suporte)",
        "Support base pattern spacing": "Espaçamento do padrão de base (suporte)",
        "Support style": "Estilo (suporte)",
        "Support interface top layers": "Camadas de interface superior (suporte)",
        "Support top Z distance": "Distância Z superior (suporte)",
        "Purge into objects' infill": "Purgar no preenchimento dos objetos",
        "Prime tower brim width": "Largura da borda (torre de purga)",
        "%s: %d mm brim around the prime tower; a wider one only matters for tall, narrow towers.":
            "%s: borda de %d mm em volta da torre de purga; uma mais larga só faz diferença em torres altas e estreitas.",
        "Purge into objects' support": "Purgar nos suportes dos objetos",
        "Spiral vase": "Vaso espiral",
        "Detect thin walls": "Detectar paredes finas",
        "Reverse on even": "Reversão em par",
        "Skirt height": "Altura da saia",
        "Raft layers": "Camadas do raft",
        "Enable support": "Ativar suporte",
        "Enforce support layers": "Camadas de suporte forçado",
        "Support base pattern": "Padrão da base (suporte)",
        "Support wall loops": "Voltas de parede de suporte",
        "Support interface bottom layers": "Camadas de interface inferior (suporte)",
        "Support top interface spacing": "Espaçamento da interface superior (suporte)",
        "Timelapse": "Timelapse",
        "Enable clumping detection": "Habilitar detecção de aglomeração",
        "Traditional": "Tradicional",
        "Smooth": "Suave",

        # Purposes.
        "Decoration": "Decoração",
        "Looks matter, load does not": "A aparência importa, a carga não",
        "Functional": "Funcional",
        "Tool, organizer, light-duty bracket": "Ferramenta, organizador, suporte leve",
        "Prototype": "Protótipo",
        "Only the geometry needs to come out right": "Só a geometria precisa sair certa",
        "Mechanical": "Mecânica",
        "Gear, hinge, load-bearing bracket": "Engrenagem, dobradiça, suporte com carga",
        "Container": "Recipiente",
        "Vase, cup, box": "Vaso, copo, caixa",
        "Miniature": "Miniatura",
        "Fine detail; supports are left as they are": "Detalhes finos; o suporte fica como está",
        "Large part": "Peça grande",
        "Cosplay, big prints; infill dominates usage": "Cosplay, impressões grandes; o preenchimento domina o consumo",

        # Material families.
        "PLA": "PLA",
        "Silk PLA": "PLA Silk",
        "PETG": "PETG",
        "ABS/ASA": "ABS/ASA",
        "TPU": "TPU",
        "Nylon (PA)": "Nylon (PA)",
        "PC": "PC",
        "Support material": "Material de suporte",
        "Unknown material": "Material desconhecido",

        # Pieces of the reasons.
        "only supports the top surfaces": "só sustenta as superfícies superiores",
        "dense near the surfaces, sparse in the core": "denso perto das superfícies, esparso no miolo",
        "strong in every direction": "resistente em todas as direções",
        "no extra solid infill along sloped walls": "sem preenchimento sólido extra junto às paredes inclinadas",
        "extra solid infill along sloped walls only where Orca finds it critical":
            "preenchimento sólido extra junto às paredes inclinadas só onde o Orca acha crítico",
        "moderate extra solid infill along sloped walls": "preenchimento sólido extra moderado junto às paredes inclinadas",
        "extra solid infill wherever sloped walls would get thin":
            "preenchimento sólido extra onde as paredes inclinadas ficariam finas",
        "gap fill only on top and bottom surfaces, not on internal solid infill":
            "preenchimento de vão só nas superfícies de topo e base, não no preenchimento sólido interno",
        "Strength %d/5 and finish %d/5: %s.": "Resistência %d/5 e acabamento %d/5: %s.",
        SHELL_THICKNESS_TOP_MIN:
            "%g mm forçam %d camadas de topo com altura de camada de %g mm, anulando as %d camadas acima. "
            "%g mm, o padrão do Orca, ainda mantém o topo grosso o bastante com camadas finas.",
        "one wall on flat tops saves filament": "uma parede só nos topos planos economiza filamento",
        "full walls on top for a cleaner surface": "paredes completas no topo para uma superfície mais limpa",
        "no brim": "sem borda",
        "brim only where Orca thinks it is needed": "borda só onde o Orca acha necessário",
        "brim ears on sharp corners only": "orelhas de borda só nos cantos vivos",
        "full outer brim": "borda externa completa",
        "one wall on the first layer; its outline is slightly weaker":
            "uma parede na primeira camada; o contorno fica um pouco mais fraco",
        "normal walls on the first layer for a firm base outline":
            "paredes normais na primeira camada para um contorno de base firme",
        "Reliability %d/5": "Confiabilidade %d/5",
        " (raised for %s)": " (elevada para %s)",

        # Reasons.
        "Strength %d/5: %d walls. Walls add more strength per gram than infill.":
            "Resistência %d/5: %d paredes. Paredes dão mais resistência por grama que o preenchimento.",
        "Strength %d/5: %d%% sparse infill.": "Resistência %d/5: %d%% de preenchimento esparso.",
        "Strength %d/5: %s, %s.": "Resistência %d/5: %s, %s.",
        "Strength %d/5: %d bottom layers.": "Resistência %d/5: %d camadas de base.",
        "Strength %d/5: %s.": "Resistência %d/5: %s.",
        "Strength %d/5: only sparse areas under %d mm² become solid infill.":
            "Resistência %d/5: só áreas esparsas abaixo de %d mm² viram preenchimento sólido.",
        "Strength %d/5 and finish %d/5: %d top layers.": "Resistência %d/5 e acabamento %d/5: %d camadas de topo.",
        "Finish %d/5: %s.": "Acabamento %d/5: %s.",
        "Finish %d/5: %d support interface layers.": "Acabamento %d/5: %d camadas de interface do suporte.",
        "%s: %s.": "%s: %s.",
        "%s: %d mm brim.": "%s: borda de %d mm.",
        "%s: %d skirt loop(s).": "%s: %d volta(s) de saia.",
        "You said the part holds on the bed by itself. %s: %s.": "Você disse que a peça fica firme na mesa sem ajuda. %s: %s.",
        "You said the part holds on the bed by itself. %s: no raft, which prints several layers under the whole part.":
            "Você disse que a peça fica firme na mesa sem ajuda. %s: sem raft, que imprime várias camadas embaixo da peça inteira.",
        "%s has its own layer height: top and bottom layer counts were worked out for the process layer height.":
            "%s tem altura de camada própria: as camadas de topo e de base foram calculadas para a altura do perfil de processo.",
        "%s: one skirt layer is enough without a draft shield.":
            "%s: uma camada de saia basta sem a proteção contra correntes de ar.",
        "Finish %d/5: no interface where supports rest on the part.":
            "Acabamento %d/5: sem interface onde o suporte apoia na peça.",
        "Finish %d/5: %g mm between support interface lines; the underside of overhangs is rougher.":
            "Acabamento %d/5: %g mm entre as linhas da interface do suporte; a parte de baixo das saliências fica mais áspera.",
        "You said this is an open container that can print as one continuous outer wall: "
        "vase mode uses the least filament possible.":
            "Você disse que é um recipiente aberto que pode ser impresso como uma parede externa contínua: "
            "o modo vaso é o que menos gasta filamento.",
        "Required by vase mode; Orca does not print a vase without it.":
            "Exigido pelo modo vaso; o Orca não imprime um vaso sem isso.",
        "PETG sticks to supports: at least a 0.25 mm gap.": "PETG gruda no suporte: pelo menos 0.25 mm de folga.",
        "PETG sticks to supports: at least 2 interface layers.": "PETG gruda no suporte: pelo menos 2 camadas de interface.",
        "%s warps: a full outer brim is required.": "%s empena: é necessária uma borda externa completa.",
        "%s warps at the corners: brim ears hold them down with far less filament than a full brim.":
            "%s empena nos cantos: orelhas de borda seguram os cantos com bem menos filamento que uma borda completa.",
        "%s warps: brim of at least %d mm.": "%s empena: borda de pelo menos %d mm.",
        "Your %s brim is kept. %s warps: make sure it holds the corners down.":
            "Sua borda %s foi mantida. %s empena: confira se ela segura os cantos.",
        "Silk PLA has weak layer adhesion: at least 3 walls for a functional part.":
            "PLA Silk tem adesão fraca entre camadas: pelo menos 3 paredes numa peça funcional.",
        "%s needs at least %d walls.": "%s precisa de pelo menos %d paredes.",
        "Capped at %d walls.": "Limitado a %d paredes.",
        "%s needs at least %d%% infill.": "%s precisa de pelo menos %d%% de preenchimento.",
        "Capped at %d%% infill.": "Limitado a %d%% de preenchimento.",
        "%s is not allowed for %s; using %s, %s.": "%s não é permitido para %s; usando %s, %s.",
        "%s needs at least %d top layers.": "%s precisa de pelo menos %d camadas de topo.",
        "%s needs at least %d bottom layers.": "%s precisa de pelo menos %d camadas de base.",
        "You said the faces pointing up while printing will be hidden: skip their last layer. "
        "The %d solid layers below it still close the part.":
            "Você disse que as faces viradas para cima na impressão vão ficar escondidas: a última camada delas "
            "não é impressa. As %d camadas sólidas abaixo continuam fechando a peça.",
        "You said the walls hide the infill: color-change purge goes into the infill instead of the prime tower.":
            "Você disse que as paredes escondem o preenchimento: a purga da troca de cor vai para o preenchimento "
            "em vez da torre de purga.",
        "You said mixed colors in the supports are fine: color-change purge goes into them instead of the prime tower.":
            "Você disse que cores misturadas no suporte não são problema: a purga da troca de cor vai para ele "
            "em vez da torre de purga.",
        SHELL_THICKNESS_TOP:
            "%g mm forçam %d camadas de topo com altura de camada de %g mm, anulando as %d camadas acima. "
            "%g mm mantêm %d camadas.",
        SHELL_THICKNESS_BOTTOM:
            "%g mm forçam %d camadas de base com altura de camada de %g mm, anulando as %d camadas acima. "
            "%g mm mantêm %d camadas.",

        # Notes.
        "Using %s instead of the detected %s.": "Usando %s em vez do %s detectado.",
        "TPU: infill sets the hardness of the part, so infill is left unchanged.":
            "TPU: o preenchimento define a dureza da peça, então ele não é alterado.",
        "Unknown material '%s': PLA rules with extra reliability.":
            "Material desconhecido '%s': regras de PLA com confiabilidade extra.",
        "Silk PLA is not recommended for functional parts.": "PLA Silk não é recomendado para peças funcionais.",
        "These suggestions were not tested on containers that must hold liquid; whether one leaks is not checked.":
            "Estas sugestões não foram testadas em recipientes que precisam segurar líquido; não se verifica se ele vaza.",
        "%g mm layers are coarse for a miniature: faces and fine detail such as chainmail fade above 0.16 mm. Miniature guides use 0.08–0.12 mm.":
            "Camadas de %g mm são grossas para uma miniatura: rosto e detalhes finos como cota de malha somem acima de 0,16 mm. Guias de miniatura usam 0,08–0,12 mm.",
        "On build plate only is on: parts of the figure above itself (arms, capes, weapons over the base) get no support.":
            "Apenas na placa de impressão está ligado: partes da figura acima dela mesma (braços, capas, armas sobre a base) ficam sem suporte.",
        "Ignore small overhangs is on: small overhangs such as fingers, chins and weapon tips get no support.":
            "Ignorar pequenas saliências está ligado: saliências pequenas como dedos, queixo e pontas de arma ficam sem suporte.",
        "Organic supports: Orca's tree support brim only applies to Tree Slim, Strong and Hybrid, so tall organic branches have no brim of their own.":
            "Suporte orgânico: a borda de suporte de árvore do Orca só vale para Árvore Estreita, Árvore Forte e Árvore Híbrida, então galhos orgânicos altos ficam sem borda própria.",
        "Nylon absorbs moisture: dry the filament before printing.":
            "Nylon absorve umidade: seque o filamento antes de imprimir.",
        "Orca only purges into infill or supports when the prime tower is on, so the purge answers changed nothing.":
            "O Orca só purga no preenchimento ou no suporte com a torre de purga ligada, então as respostas "
            "sobre purga não mudaram nada.",
        "The filaments are different materials and may not bond inside the part, so purge into the infill is not suggested.":
            "Os filamentos são de materiais diferentes e podem não colar dentro da peça, então a purga no "
            "preenchimento não é sugerida.",
        "Vase mode prints only the outside of the part, one wall thick, with an open top. "
        "A model with thick walls or an inner cavity comes out as a thin shell.":
            "O modo vaso imprime só o lado de fora da peça, com uma parede e o topo aberto. "
            "Um modelo com paredes grossas ou cavidade interna sai como uma casca fina.",
        "No part prints with %s, so its material rules are left out.":
            "Nenhuma peça é impressa com %s, então as regras desse material ficam de fora.",
        "Vase mode: there are %d copies of parts. On one plate, Orca only slices vase mode with more than one copy when the print sequence is By object.":
            "Modo vaso: há %d cópias de peças. Numa mesma placa, o Orca só fatia modo vaso com mais de uma cópia quando a sequência de impressão é Por objeto.",
        "Vase mode: Orca does not slice an object that prints with more than one filament in vase mode (%s).":
            "Modo vaso: o Orca não fatia em modo vaso um objeto impresso com mais de um filamento (%s).",
        "%s and %s bond poorly to each other: where they meet inside one part, it can split there.":
            "%s e %s aderem mal entre si: onde se encontram dentro de uma peça, ela pode se separar ali.",
        "Several materials (%s): the strictest rule of each applies.":
            "Vários materiais (%s): vale a regra mais rígida de cada um.",
        "%s is a support material; the rules follow %s.": "%s é material de suporte; as regras seguem %s.",

        # Messages.
        "Orca's auto brim is kept: it sizes the brim for %s and the part itself.":
            "A borda automática do Orca foi mantida: ela dimensiona a borda para %s e para a peça.",
        "A preset or the filaments changed. Click Suggest changes again.":
            "Um preset ou os filamentos mudaram. Clique em Sugerir mudanças de novo.",
        "Settings changed in Orca since the suggestions. Click Suggest changes again.":
            "Configurações mudaram no Orca depois das sugestões. Clique em Sugerir mudanças de novo.",
        "%d suggested change(s).": "%d mudança(s) sugerida(s).",
        "Your settings already match this profile.": "Suas configurações já batem com este perfil.",
        "Still applying the previous changes.": "Ainda aplicando as mudanças anteriores.",
        "Applied %d change(s) to '%s'. Slice again to see the new estimate.":
            "%d mudança(s) aplicada(s) em '%s'. Fatie de novo para ver a nova estimativa.",
        "Saved to the process preset '%s'. Select it in the process list, then slice again.":
            "Salvo no perfil de processo '%s'. Selecione-o na lista de processos e fatie de novo.",
        "Could not apply: %s": "Não foi possível aplicar: %s",
        "Applying %d change(s)…": "Aplicando %d mudança(s)…",
        "Restored %d setting(s) to their values before the suggestions.":
            "%d configuração(ões) voltaram aos valores de antes das sugestões.",
        "%d setting(s) were left as they are: they changed after Filament Saver applied them, or what it applied is not on record.":
            "%d configuração(ões) ficaram como estão: mudaram depois que o Filament Saver as aplicou, ou o valor que ele aplicou não está registrado.",
        "Could not restore: %s": "Não foi possível restaurar: %s",
        "Restoring %d setting(s)…": "Restaurando %d configuração(ões)…",
        "Could not save the original values: %s": "Não foi possível salvar os valores originais: %s",
        "'%s' has no system preset to build on.": "'%s' não tem um perfil de sistema como base.",
        "Saved '%s', but this Orca version needs a restart to show it.":
            "'%s' foi salvo, mas esta versão do Orca precisa ser reiniciada para mostrá-lo.",
        "Could not read the exported G-code: %s": "Não foi possível ler o G-code exportado: %s",
        "Estimate updated from %s.": "Estimativa atualizada a partir de %s.",
        "Estimate updated from the last slice.": "Estimativa atualizada a partir do último fatiamento.",

        # Page.
        "Current estimate": "Estimativa atual",
        "Print time": "Tempo de impressão",
        "Filament": "Filamento",
        "Cost": "Custo",
        "no change": "sem mudança",
        "{diff} vs previous slice": "{diff} em relação ao fatiamento anterior",
        "From the last slice, {date}.": "Do último fatiamento, {date}.",
        "From {file}, {date}.": "De {file}, {date}.",
        "Slice again after applying changes to compare.": "Fatie de novo depois de aplicar as mudanças para comparar.",
        "With the suggestions:": "Com as sugestões:",
        "{n} comparison(s) since {date}, between slices of the same parts before and after applying or undoing suggestions; other edits made in between count too.":
            "{n} comparação(ões) desde {date}, entre fatiamentos das mesmas peças antes e depois de aplicar ou desfazer sugestões; outras edições feitas no meio também contam.",
        "Reset": "Zerar",
        "The difference is against the previous slice of the same parts on the same plate.":
            "A diferença é em relação ao fatiamento anterior das mesmas peças, na mesma placa.",
        "No estimate yet. Slice the plate; the time and filament show up here.":
            "Ainda sem estimativa. Fatie a mesa; o tempo e o filamento aparecem aqui.",
        "What are you printing?": "O que você está imprimindo?",
        "Material": "Material",
        "Auto: {family}": "Auto: {family}",
        "Auto": "Auto",
        "Detected from {filament}": "Detectado em {filament}",
        "Overrides the filament profile for these suggestions.": "Substitui o perfil do filamento nestas sugestões.",
        "Strength": "Resistência",
        "Walls, infill and solid layers": "Paredes, preenchimento e camadas sólidas",
        "Finish": "Acabamento",
        "Top surfaces and support interfaces": "Superfícies superiores e interfaces do suporte",
        "Reliability": "Confiabilidade",
        "Brim, skirt and support coverage": "Borda, saia e cobertura do suporte",
        "Only check what you are sure of": "Marque só o que você tem certeza",
        "Faces pointing up while printing will be hidden": "As faces viradas para cima na impressão vão ficar escondidas",
        "E.g. the back of a wall sign printed face down. Not for boxes, figures or parts with visible ledges.":
            "Ex.: o verso de uma placa de parede impressa com a frente para baixo. Não serve para caixas, "
            "figuras ou peças com degraus visíveis.",
        "Print as a vase": "Imprimir como vaso",
        "Open container (vase, cup, pot) printed as one continuous outer wall: no top, no infill, no supports.":
            "Recipiente aberto (vaso, copo, pote) impresso como uma parede externa contínua: sem topo, sem "
            "preenchimento, sem suporte.",
        "vase mode, applied together": "modo vaso, aplicado em conjunto",
        "Purge into the infill": "Purgar no preenchimento",
        "The part holds on the bed by itself": "A peça fica firme na mesa sem ajuda",
        "Wide, low base with no thin corners that lift, or you have printed this part without raft or brim and it stayed down.":
            "Base larga e baixa, sem cantos finos que levantam, ou você já imprimiu esta peça sem raft nem borda e ela não soltou.",
        "The walls are opaque, so mixed colors inside the part will not show.":
            "As paredes são opacas, então as cores misturadas dentro da peça não aparecem.",
        "Purge into the supports": "Purgar nos suportes",
        "Mixed colors in the supports are fine; they are removed anyway.":
            "Cores misturadas no suporte não são problema; ele é removido de qualquer jeito.",
        "Suggest changes": "Sugerir mudanças",
        "Changes are applied as unsaved edits of the current process preset.":
            "As mudanças entram como edições não salvas do perfil de processo atual.",
        "Changes are saved to a \"… - Filament Saver\" process preset.":
            "As mudanças são salvas num perfil de processo \"… - Filament Saver\".",
        "Suggested changes": "Mudanças sugeridas",
        "Apply all": "Aplicar todas",
        "Apply savings": "Aplicar economias",
        "Save filament": "Economizam filamento",
        "Needed for the purpose or the material": "Necessárias para o propósito ou o material",
        "Your current value is below what the purpose or the material needs. Apply all also applies these.":
            "Seu valor atual está abaixo do que o propósito ou o material precisa. Aplicar todas também aplica estas.",
        "Skip all": "Pular todas",
        "Restore originals": "Restaurar originais",
        "Restore originals ({n})": "Restaurar originais ({n})",
        "Pick what you are printing and click Suggest changes.":
            "Escolha o que você está imprimindo e clique em Sugerir mudanças.",
        "Nothing to change for this profile.": "Nada a mudar para este perfil.",
        "saves filament": "economiza filamento",
        "uses more": "usa mais",
        "Applied": "Aplicada",
        "Undo": "Desfazer",
        "Undo skip": "Desfazer pulo",
        "Apply": "Aplicar",
        "Skip": "Pular",
        "Failed": "Falhou",
        "On": "Ligado",
        "Off": "Desligado",
        "(edited)": "(editado)",
        "That is %g mm: %d layers at %g mm layer height.": "Isso dá %g mm: %d camadas com altura de camada de %g mm.",
        "That is %g mm of wall: %d walls with a %g mm nozzle.": "Isso dá %g mm de parede: %d paredes com bico de %g mm.",
        "Some objects set their own value for a suggested setting; changing the process preset does not reach them.":
            "Alguns objetos têm valor próprio para uma configuração sugerida; mudar o perfil de processo não os afeta.",
        "Not used by objects with their own value: {names}": "Não vale para objetos com valor próprio: {names}",
        "Brought back the choices you last used with these parts.":
            "As escolhas que você usou da última vez com estas peças foram recuperadas.",
        "Save the current process settings as a new preset": "Salvar as configurações de processo atuais como um preset novo",
        "Save preset": "Salvar preset",
        "Includes your unsaved edits and the applied suggestions. It goes into the Filament Saver group of the process list.":
            "Inclui suas edições não salvas e as sugestões aplicadas. Ele vai para o grupo Filament Saver da lista de processos.",
        "Type a name for the new preset.": "Digite um nome para o preset novo.",
        "'%s' cannot be used as a file name on every system. Pick another name.":
            "'%s' não pode ser usado como nome de arquivo em todos os sistemas. Escolha outro nome.",
        "A preset named '%s' already exists. Use the same capitals to update it.":
            "Já existe um preset chamado '%s'. Use as mesmas maiúsculas para atualizá-lo.",
        "A preset name cannot start with a dot or contain < > : \" / \\ | ? *":
            "O nome do preset não pode começar com ponto nem conter < > : \" / \\ | ? *",
        "A process preset named '%s' already exists. Pick another name.":
            "Já existe um perfil de processo chamado '%s'. Escolha outro nome.",
        "Saved the preset '%s'. Select it in the process list to use it.":
            "Preset '%s' salvo. Selecione-o na lista de processos para usar.",
        "Updated the preset '%s'. Select it in the process list to use it.":
            "Preset '%s' atualizado. Selecione-o na lista de processos para usar.",
        "your spool price": "seu preço do rolo",
        "Orca's filament cost": "custo do filamento no Orca",
        "Spool price (optional), in {code}": "Preço do rolo (opcional), em {code}",
        "Currency of your prices and of Orca's filament cost": "Moeda dos seus preços e do custo do filamento no Orca",
        "Show the cost in": "Mostrar o custo em",
        "Getting exchange rates…": "Buscando a cotação…",
        "No exchange rate yet. Showing the cost in {code}.": "Ainda sem cotação. Mostrando o custo em {code}.",
        "Check the box above to convert. Until then the cost stays in {code}.":
            "Marque a caixa acima para converter. Até lá o custo fica em {code}.",
        "If Orca asked for network access, allow it and click Try again. Showing the cost in {code}.":
            "Se o Orca pediu acesso à rede, permita e clique em Tentar de novo. Mostrando o custo em {code}.",
        "Allow fetching exchange rates from {service}. Only the currency code is sent.":
            "Permitir buscar a cotação em {service}. Só o código da moeda é enviado.",
        "Try again": "Tentar de novo",
        "converted from {code} at the {date} rate": "convertido de {code} pela cotação de {date}",
        "Could not get exchange rates: %s": "Não foi possível obter a cotação: %s",
        "Spool price": "Preço do rolo",
        "Spool weight (g)": "Peso do rolo (g)",
        "{grams} g": "{grams} g",
        "Empty: Orca's filament cost, {cost} per kg.": "Vazio: custo do filamento no Orca, {cost} por kg.",
        "Empty: Orca's filament cost.": "Vazio: custo do filamento no Orca.",
        "{cost} per kg with this spool.": "{cost} por kg com este rolo.",
        "The spool weight must be more than 0 g.": "O peso do rolo precisa ser maior que 0 g.",
    },
}


def language_for(code):
    """One of LANGUAGES for Orca's language code, e.g. "pt_BR" or "en_US"."""
    return "pt_BR" if str(code or "").lower().startswith("pt") else "en"


def translator(lang):
    catalog = TRANSLATIONS.get(lang, {})
    return lambda text: catalog.get(text, text)


# --- logo --------------------------------------------------------------------

# 128 px spool in Orca's accent color on a transparent background (assets/logo.png at 512 px).
# Embedded so the plugin stays a single file; written to the plugin storage for the tab icon.
LOGO_PNG = (
    "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"
)
LOGO_FILE = "logo.png"


def _logo_path():
    """Path of the logo PNG for Orca's tab icon, or "" when it cannot be written."""
    try:
        path = _storage_path(LOGO_FILE)
        data = base64.b64decode(LOGO_PNG)
        if not os.path.exists(path) or os.path.getsize(path) != len(data):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as stream:
                stream.write(data)
        return path
    except Exception:
        traceback.print_exc()
        return ""


# --- the tab -----------------------------------------------------------------

class FilamentSaverPage(orca.pages.PagesPluginCapabilityBase):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.form = {"purpose": "decoration", "material": "auto", "S": 1, "V": 4, "R": 3,
                     "answers": {name: False for name in QUESTIONS}}
        self.result = None       # last suggest() result plus context
        self.source = None       # preset the suggestions were made for
        self.stale = False
        self.basis = None        # settings and filaments the suggestions were worked out from
        self.busy = False
        self.message = ""
        self.error = ""
        self.jobs = None
        self.worker = None
        self.pending_gcode = None
        # Per process preset: values before Filament Saver changed them, and the values it wrote.
        self.stores = {}
        self.originals = {}          # the store of originals_source: key -> value to go back to
        self.originals_source = None  # preset those values belong to
        self.bundle_values = {}      # settings managed in the local-bundle preset (fallback mode)
        self.loaded = False          # originals read from plugin storage
        self.applied = {}            # the store of originals_source: key -> value the plugin wrote
        self.changed_since_slice = False  # the plugin applied or undid changes since the last slice
        self.lang = "en"             # follows Orca's language, see _detect_language
        self.parts = None            # fingerprint of the parts last seen, see _sync_parts
        self.form_version = 0        # bumped when the form changes on this side, so the page reloads it
        self.rates_fetching = False
        self.rates_failed_at = 0
        self.rates_error = ""
        self.lock = threading.Lock()

    def get_name(self):
        return "Filament Saver"

    def get_icon(self):
        return _logo_path()

    def get_ui(self):
        return PAGE.replace("__LOGO_PNG__", LOGO_PNG)

    # Messages from the page run on the UI thread.
    def on_message(self, data):
        # Held until this message's state is posted. Workers build their state under the lock too,
        # but post it after releasing it (see _post_unlocked).
        with self.lock:
            self._handle(data)

    def _t(self, text):
        return translator(self.lang)(text)

    def _detect_language(self):
        # app_info() must run on the UI thread.
        if threading.current_thread() is not threading.main_thread():
            return
        try:
            self.lang = language_for(orca.host.app_info().get("language", ""))
        except Exception:
            traceback.print_exc()

    def _handle(self, data):
        command = (data or {}).get("cmd")
        try:
            self.error = ""
            self._detect_language()
            self._load_originals()
            if command in ("ready", "read_estimate"):
                # "ready" also catches a slice that finished before the tab was opened.
                if self.pending_gcode:
                    self._record_export(self.pending_gcode)
            elif command == "suggest":
                self._suggest(data)
            elif command in ("apply", "skip"):
                ids = data.get("ids") or []
                if command == "skip":
                    self._set_status(ids, "skipped")
                else:
                    self._apply(ids)
            elif command == "undo_skip":
                self._set_status(data.get("ids") or [], "pending")
            elif command == "restore":
                self._restore(data.get("ids"))
            elif command == "set_price":
                self._set_price(data)
            elif command == "save_preset":
                self._save_preset(data)
            elif command == "set_currency":
                self._set_currency(data)
            elif command == "reset_saved":
                data = _read_estimates()
                data.pop("saved", None)
                _write_json_atomic(_storage_path(ESTIMATES_FILE), data)
            else:
                raise RuntimeError("Unknown command %r" % command)
        except Exception as error:
            self.error = str(error)
            traceback.print_exc()
        self._push()

    def on_lifecycle_event(self, event, ctx):
        events = orca.LifecycleEvent
        if event == getattr(events, "GCodeExportFinished", None):
            # Fires for the temporary G-code of every slice, not only for File > Export.
            if getattr(ctx, "code", None) == getattr(orca.LifecycleEvtCode, "Error", object()):
                return
            self.pending_gcode = str(ctx.msg)
            if threading.current_thread() is not threading.main_thread():
                # Off the UI thread Orca cannot show the read-permission prompt, so the read
                # would be denied. Ask the page to call back; its messages run on the UI thread.
                self.post_message({"type": "read_request"})
                return
            self._record_export(self.pending_gcode)
        elif event == getattr(events, "PresetSelected", None):
            # The rules read the process, the filaments (material rules) and the printer (nozzle).
            if self.result is not None:
                self.stale = True
                self.message = self._t("A preset or the filaments changed. Click Suggest changes again.")
        elif event in [getattr(events, name, None) for name in PARTS_EVENTS]:
            # Other parts may be in Orca now. Orca waits for this handler and sends one event per
            # object when arranging or undoing (10 for 7 objects), so only ask the page, which
            # refreshes once after the last one.
            self.post_message({"type": "refresh_request"})
            return
        else:
            return
        self._push()

    # --- actions ---

    def _context(self):
        bundle = orca.host.preset_bundle()
        edited = bundle.prints.edited_preset()
        selected = bundle.prints.selected_preset()
        current = {}
        for key in [k for k, _ in SETTINGS] + CONTEXT_KEYS:
            value = edited.config_value(key)
            if value not in (None, ""):
                current[key] = value
        if "nozzle_diameter" not in current:  # a printer setting, needed to scale wall counts
            try:
                nozzle = bundle.printers.edited_preset().config_value("nozzle_diameter")
                if nozzle:
                    current["nozzle_diameter"] = nozzle
            except Exception:
                traceback.print_exc()
        filaments = [p for p in bundle.current_filament_presets() if p is not None]
        # Rules follow the part's materials: support filaments (PVA, BVOH, or flagged as support) are skipped.
        loaded_parts = [p for p in filaments if material_family(p.config_value("filament_type"), p.name) != "support"
                        and not str(p.config_value("filament_is_support") or "0").startswith("1")]
        # Only the filaments the objects print with; all of them when that cannot be told.
        try:
            use = model_filament_use(orca.host.model(), current)
        except Exception:
            traceback.print_exc()
            use = {"used": None, "mixed": [], "copies": 0}
        used = use["used"]
        parts = [p for p in loaded_parts if used is None or filaments.index(p) + 1 in used] or loaded_parts
        filament = (parts or filaments or [None])[0]
        return {
            "bundle": bundle,
            "edited": edited,
            "selected": selected,
            "current": current,
            "filament_type": filament.config_value("filament_type") if filament else "",
            "filament_name": filament.name if filament else "",
            "filament_count": len(filaments),
            "other_filaments": [(p.config_value("filament_type"), p.name) for p in parts if p is not filament],
            "skipped_support": filament is not None and bool(filaments) and filaments[0] not in loaded_parts,
            "support_filament": len(loaded_parts) < len(filaments),
            "unused_filaments": [p.name for p in loaded_parts if p not in parts],
            "used_filaments": sorted(used) if used is not None else None,
            "filament_use": use,
            "first_filament_name": filaments[0].name if filaments else "",
            "filament_names": [p.name for p in filaments],
            "filament_costs": [p.config_value("filament_cost") for p in filaments],
            "same_family": len({material_family(p.config_value("filament_type"), p.name) for p in filaments}) <= 1,
        }

    def _suggest(self, data):
        self.form = clean_form(data)
        purpose, material, answers = self.form["purpose"], self.form["material"], self.form["answers"]
        values = {name: self.form[name] for name in ("S", "V", "R")}
        self.parts = _parts_fingerprint()
        if self.parts:
            try:
                _remember_form(self.parts, self.form)
            except Exception:
                traceback.print_exc()
        context = self._context()
        result = suggest(context["current"], purpose, values["S"], values["V"], values["R"],
                         context["filament_type"], context["filament_name"],
                         family=None if material == "auto" else material,
                         answers=answers, filaments=context["filament_count"],
                         same_family=context["same_family"], lang=self.lang,
                         other_filaments=context["other_filaments"],
                         support_filament=context["support_filament"])
        overrides = _object_overrides()
        for change in result["changes"]:
            if overrides.get(change["key"]):
                change["overridden_by"] = overrides[change["key"]]
        if overrides.get("layer_height"):
            result["notes"].append(self._t("%s has its own layer height: top and bottom layer counts were worked out "
                                           "for the process layer height.") % ", ".join(overrides["layer_height"]))
        if any(change.get("overridden_by") for change in result["changes"]):
            result["notes"].append(self._t("Some objects set their own value for a suggested setting; "
                                           "changing the process preset does not reach them."))
        if context["skipped_support"]:
            result["notes"].append(self._t("%s is a support material; the rules follow %s.")
                                   % (context["first_filament_name"], context["filament_name"]))
        if context["unused_filaments"]:
            result["notes"].append(self._t("No part prints with %s, so its material rules are left out.")
                                   % ", ".join(context["unused_filaments"]))
        use = context["filament_use"]
        if answers.get("vase"):
            # Orca refuses to slice these (Print::validate); nothing would print, but say so before applying.
            if use["copies"] > 1 and context["current"].get("print_sequence") != "by object":
                result["notes"].insert(0, self._t("Vase mode: there are %d copies of parts. On one plate, Orca only slices "
                                                  "vase mode with more than one copy when the print sequence is By object.")
                                       % use["copies"])
            if use["mixed"]:
                result["notes"].insert(0, self._t("Vase mode: Orca does not slice an object that prints with more than one "
                                                  "filament in vase mode (%s).") % ", ".join(use["mixed"]))
        self.result = result
        self.basis = {"current": dict(context["current"]), "filaments": list(context["filament_names"]),
                      "used": context["used_filaments"]}
        self.source = context["selected"].name
        self.stale = False
        self.message = (self._t("%d suggested change(s).") % len(result["changes"])) if result["changes"] \
            else self._t("Your settings already match this profile.")

    def _set_price(self, data):
        """Spool price for one filament preset; an empty price goes back to Orca's filament cost."""
        name = str(data.get("name") or "")
        if not name:
            raise RuntimeError("No filament name.")
        prices = _read_prices()
        price = _number(data.get("price"))
        grams = _number(data.get("grams")) or DEFAULT_SPOOL_GRAMS
        if price is None or price <= 0:
            prices.pop(name, None)
        elif grams <= 0:
            raise RuntimeError(self._t("The spool weight must be more than 0 g."))
        else:
            prices[name] = {"price": price, "grams": grams}
        _write_json_atomic(_storage_path(PRICES_FILE), prices)
        self.message = ""

    def _set_currency(self, data):
        currency = read_currency(self.lang)
        for key in ("price", "display"):
            if data.get(key) in CURRENCIES:
                currency[key] = data[key]
        if isinstance(data.get("rates_allowed"), bool):
            currency["rates_allowed"] = data["rates_allowed"]
        _write_json_atomic(_storage_path(CURRENCY_FILE), currency)
        self.rates_failed_at = 0
        self.rates_error = ""

    def _ensure_rates(self, currency):
        """Fetch rates in the background when the cost is shown in another currency and the cache
        has no rate for it or is older than RATES_MAX_AGE."""
        if not currency["rates_allowed"] or currency["price"] == currency["display"] or self.rates_fetching:
            return
        rates = read_rates()
        fresh = time.time() - rates.get("fetched_at", 0) < RATES_MAX_AGE
        # A fresh fetch for this base that lacks the target is not retried until it gets old.
        if fresh and (conversion(currency["price"], currency["display"], rates) is not None
                      or rates.get("base") == currency["price"]):
            return
        if time.time() - self.rates_failed_at < RATES_RETRY:
            return
        self.rates_fetching = True
        try:
            threading.Thread(target=self._fetch_rates, args=(currency["price"],), name="filament-saver-rates",
                             daemon=True).start()
        except Exception as error:  # e.g. the thread permission was denied: no fetch will ever finish
            traceback.print_exc()
            self.rates_fetching = False
            self.rates_failed_at = time.time()
            self.rates_error = self._t("Could not get exchange rates: %s") % error

    def _fetch_rates(self, base):
        error = ""
        try:
            fetch_rates(base)
        except Exception as caught:
            traceback.print_exc()
            error = str(caught)
        with self.lock:
            self.rates_fetching = False
            if error:
                self.rates_failed_at = time.time()
                self.rates_error = self._t("Could not get exchange rates: %s") % error
            else:
                self.rates_error = ""
            state = self._worker_state()
        self._post_unlocked(state)

    def _currency_state(self):
        currency = read_currency(self.lang)
        self._ensure_rates(currency)
        rates = read_rates()
        # Without the user's permission nothing is converted, not even with rates cached earlier.
        rate = conversion(currency["price"], currency["display"], rates if currency["rates_allowed"] else {})
        shown = currency["display"] if rate is not None else currency["price"]
        return {**currency, "shown": shown, "rate": rate if rate is not None else 1.0,
                "converted": rate is not None and currency["price"] != currency["display"],
                "rates_date": rates.get("date", ""), "fetching": self.rates_fetching,
                "error": self.rates_error, "available": list(CURRENCIES), "service": RATES_HOST}

    def _set_status(self, ids, status):
        for change in self._changes(ids):
            if change["status"] in ("pending", "skipped", "error"):
                change["status"] = status

    def _changes(self, ids):
        """Changes with these ids, plus the rest of any group they belong to (vase mode is one switch)."""
        if self.result is None:
            return []
        wanted = set(ids)
        groups = {c["group"] for c in self.result["changes"] if c["id"] in wanted and c.get("group")}
        return [c for c in self.result["changes"] if c["id"] in wanted or c.get("group") in groups]

    def _apply(self, ids):
        if self.busy:
            raise RuntimeError(self._t("Still applying the previous changes."))
        if self.stale:
            raise RuntimeError(self._t("A preset or the filaments changed. Click Suggest changes again."))
        if self.basis is not None and self.mode() == "in_place":
            # The suggestions were worked out from the settings and filaments at that time: if they
            # changed in Orca since, writing them now could undo the user's edit or miss a material rule.
            context = self._context()
            settings_changed = any(not _same(context["current"].get(k, ""), v) for k, v in self.basis["current"].items()) or \
                set(context["current"]) != set(self.basis["current"])
            if settings_changed or context["filament_names"] != self.basis["filaments"] or \
                    context.get("used_filaments") != self.basis.get("used"):
                self.stale = True
                raise RuntimeError(self._t("Settings changed in Orca since the suggestions. Click Suggest changes again.")
                                   if settings_changed else
                                   self._t("A preset or the filaments changed. Click Suggest changes again."))
        changes = [c for c in self._changes(ids) if c["status"] in ("pending", "skipped", "error")]
        if not changes:
            return

        def done(target):
            self._use_preset(self.source)
            for change in changes:
                change["status"] = "applied"
                change.pop("error", None)
                # Keep the value from before the first suggestion across re-suggests, but only while
                # the setting still holds what the plugin wrote: if the user changed it since,
                # their value is the one to go back to.
                key = change["key"]
                if key not in self.originals or self.applied.get(key) != change["current"]:
                    self.originals[key] = change["current"]
                self.applied[key] = change["suggested"]
                if self.basis is not None:
                    self.basis["current"][key] = change["suggested"]
            self.changed_since_slice = True
            self._save_originals()
            if self.stale:  # a preset changed while applying: keep the message that says so
                return
            self.message = (self._t("Applied %d change(s) to '%s'. Slice again to see the new estimate.") % (len(changes), target)
                            if self.mode() == "in_place" else
                            self._t("Saved to the process preset '%s'. Select it in the process list, then slice again.") % target)

        def failed(error):
            for change in changes:
                change["status"] = "error"
                change["error"] = str(error)
            self.error = self._t("Could not apply: %s") % error

        self._write([(c["key"], c["suggested"]) for c in changes], done, failed,
                    self._t("Applying %d change(s)…") % len(changes))

    def _restore(self, keys=None):
        """Put the settings Filament Saver changed back to their values before the suggestions."""
        if self.busy:
            raise RuntimeError(self._t("Still applying the previous changes."))
        # Each process preset has its own values to go back to: restore those of the one in use.
        self._use_preset(self._selected_preset())
        store = self.originals
        if keys is not None:
            keys = list(keys) + [c["key"] for c in self._changes(keys)]
        keys = list(dict.fromkeys(k for k in (keys if keys is not None else list(store)) if k in store))
        # A setting edited by hand after the plugin changed it keeps the user's value: only settings
        # that still hold what the plugin wrote are put back. Files from 0.1.0 did not record what
        # the plugin wrote, so those settings cannot be checked and are left alone too.
        edited = []
        if self.mode() == "in_place":
            try:
                current = self._context()["current"]
            except Exception:
                traceback.print_exc()
                current = None
            if current is not None:
                edited = [k for k in keys if k not in self.applied or not _same(current.get(k, ""), self.applied[k])]
        if edited:
            for key in edited:
                store.pop(key, None)
                self.applied.pop(key, None)
                for change in (self.result or {}).get("changes", []):
                    if change["key"] == key:
                        change["current"] = current.get(key, "")
                        change["status"] = "pending"
            self._save_originals()
            keys = [k for k in keys if k not in edited]
            kept = self._t("%d setting(s) were left as they are: they changed after Filament Saver applied them, or what it applied is not on record.") % len(edited)
            if not keys:
                self.message = kept
                return
        pairs = [(key, store[key]) for key in keys if key in LABELS]

        def done(target):
            for key in keys:
                original = store.pop(key)
                self.applied.pop(key, None)
                if self.basis is not None and key in self.basis["current"]:
                    self.basis["current"][key] = original
                for change in (self.result or {}).get("changes", []):
                    if change["key"] == key:
                        change["current"] = original
                        change["status"] = "pending"
            self.changed_since_slice = True
            self._save_originals()
            self.message = self._t("Restored %d setting(s) to their values before the suggestions.") % len(keys)
            if edited:
                self.message += " " + kept

        def failed(error):
            self.error = self._t("Could not restore: %s") % error

        self._write(pairs, done, failed, self._t("Restoring %d setting(s)…") % len(keys))

    def _load_originals(self):
        if self.loaded:
            return
        saved = _read_originals()
        self.stores = saved["presets"]
        self.bundle_values = saved["bundle_values"]
        self.loaded = True
        self._use_preset(self.originals_source)

    def _use_preset(self, name):
        """Point originals/applied at the store of one process preset."""
        store = self.stores.setdefault(name, {"originals": {}, "applied": {}}) if name else {"originals": {}, "applied": {}}
        self.originals_source = name
        self.originals = store["originals"]
        self.applied = store["applied"]

    def _selected_preset(self):
        try:
            return orca.host.preset_bundle().prints.selected_preset().name
        except Exception:
            traceback.print_exc()
            return self.source

    def _save_originals(self):
        try:
            _write_originals(self.stores, self.bundle_values)
        except Exception as error:
            traceback.print_exc()
            self.error = self._t("Could not save the original values: %s") % error

    def mode(self):
        return "in_place" if _write_editor() is not None else "bundle"

    def _write(self, pairs, done, failed, busy_message):
        """Write (key, value) pairs with the in-place API or the local bundle."""
        if self.mode() == "bundle":
            try:
                done(self._write_bundle(pairs))
            except Exception as error:
                failed(error)
            return
        # The in-place write API must run off the UI thread.
        if self.worker is None:
            self.jobs = queue.Queue()
            worker = threading.Thread(target=self._work, name="filament-saver", daemon=True)
            worker.start()  # if this raises, the next Apply tries again instead of queueing for nobody
            self.worker = worker
        self.busy = True
        self.message = busy_message
        self.jobs.put((pairs, done, failed))

    def _work(self):
        while True:
            pairs, done, failed = self.jobs.get()
            # The transaction needs the UI thread, so it runs without the lock.
            try:
                transaction = _write_editor().begin_update()
                for key, value in pairs:
                    transaction.set(key, value)
                target, error = transaction.commit().target_name, None
            except Exception as caught:
                target, error = None, caught
            with self.lock:
                try:
                    if error is None:
                        done(target)
                    else:
                        failed(error)
                except Exception as caught:  # the worker must outlive any error, or Apply waits forever
                    traceback.print_exc()
                    self.error = self._t("Could not apply: %s") % caught
                self.busy = False
                # Off the UI thread: reuse the last preset info instead of reading the host here.
                state = self._worker_state()
            self._post_unlocked(state)

    def _write_bundle(self, pairs):
        """Fallback: write the managed settings into a local-bundle process preset."""
        self.bundle_values.update((key, value) for key, value in pairs if key in LABELS)
        return self._write_bundle_preset(None, self.bundle_values)

    def _save_preset(self, data):
        """Save the process settings as they are now, unsaved edits included, as a new preset."""
        name = str(data.get("name") or "").strip()
        if not name:
            raise RuntimeError(self._t("Type a name for the new preset."))
        if PRESET_NAME_FORBIDDEN.search(name) or name.startswith("."):
            raise RuntimeError(self._t("A preset name cannot start with a dot or contain < > : \" / \\ | ? *"))
        if name.endswith((".", " ")) or name.split(".")[0].strip().upper() in WINDOWS_RESERVED:
            raise RuntimeError(self._t("'%s' cannot be used as a file name on every system. Pick another name.") % name)
        bundle = orca.host.preset_bundle()
        # File names ignore case on macOS and Windows: "mine" would overwrite "Mine".
        folder = os.path.dirname(self._bundle_preset_path(bundle, name))
        saved = [f[:-5] for f in (os.listdir(folder) if os.path.isdir(folder) else []) if f.endswith(".json")]
        clash = next((other for other in saved if other.lower() == name.lower() and other != name), None)
        if clash:
            raise RuntimeError(self._t("A preset named '%s' already exists. Use the same capitals to update it.") % clash)
        ours = name in saved
        if bundle.prints.find_preset(name) is not None and not ours:
            raise RuntimeError(self._t("A process preset named '%s' already exists. Pick another name.") % name)
        self._write_bundle_preset(name, {})
        self.message = (self._t("Updated the preset '%s'. Select it in the process list to use it.") if ours else
                        self._t("Saved the preset '%s'. Select it in the process list to use it.")) % name

    def _bundle_preset_path(self, bundle, name):
        return os.path.join(_data_dir(), "user", _preset_folder(bundle), "_local", BUNDLE_ID, "process", name + ".json")

    def _write_bundle_preset(self, name, values):
        """Write the edited process preset plus values into the local bundle as a preset named name
        ("<system preset> - Filament Saver" when name is None), and make Orca load it."""
        context = self._context()
        bundle = context["bundle"]
        parent = _system_parent(bundle, context["selected"])
        if parent is None:
            raise RuntimeError(self._t("'%s' has no system preset to build on.") % context["selected"].name)
        name = name or parent.name + PRESET_SUFFIX
        preset = {"type": "process", "name": name, "from": "User", "instantiation": "true",
                  "version": "1.0.0", "inherits": parent.name, "print_settings_id": name}
        edited = context["edited"]
        for key in edited.config_keys():
            if key in preset:
                continue
            value = edited.config_value(key)
            if value is not None and value != parent.config_value(key):
                preset[key] = value
        preset.update(values)
        bundle_dir = os.path.join(_data_dir(), "user", _preset_folder(bundle), "_local", BUNDLE_ID)
        _write_json_atomic(self._bundle_preset_path(bundle, name), preset)
        _write_json_atomic(os.path.join(bundle_dir, "bundle_metadata.json"),
                           {"id": BUNDLE_ID, "name": BUNDLE_NAME, "version": VERSION,
                            "description": "Presets optimized to save filament"})
        bundles = getattr(getattr(orca.host, "preset", None), "bundles", None)
        if bundles is None or not hasattr(bundles, "reload_local"):
            raise RuntimeError(self._t("Saved '%s', but this Orca version needs a restart to show it.") % name)
        bundles.reload_local(BUNDLE_ID)
        return name

    def _record_export(self, gcode_path):
        try:
            estimate = parse_gcode_estimate(gcode_path)
        except Exception as error:
            self.error = self._t("Could not read the exported G-code: %s") % error
            return
        exported = not os.path.basename(gcode_path).startswith(".")
        # An empty file name means the temporary G-code of a slice; the page names it.
        estimate.update({"file": os.path.basename(gcode_path) if exported else "", "at": time.time()})
        try:
            filaments = orca.host.preset_bundle().current_filament_presets()
            estimate["filaments"] = [p.name if p is not None else "" for p in filaments]
        except Exception:
            traceback.print_exc()
        try:
            # Runs on the UI thread (see on_lifecycle_event), where the model can be read.
            fingerprint = model_fingerprint(orca.host.model())
        except Exception:
            traceback.print_exc()
            fingerprint = "unknown-%d" % int(estimate["at"])  # never matches another slice
        data = _read_estimates()
        models = data["models"]
        entry = models.get(fingerprint) or {}
        latest = entry.get("latest") or {}
        if all(latest.get(k) == estimate[k] for k in ("seconds", "grams", "cost")) or \
                all(latest.get(k) is None for k in ("seconds", "grams", "cost")):
            # Same numbers again (e.g. exporting what was just sliced): keep the comparison.
            entry["latest"] = estimate
        else:
            if self.changed_since_slice and entry.get("latest"):
                try:
                    add_saved(data, entry["latest"], estimate, _read_prices(), read_currency(self.lang)["price"])
                except Exception:
                    traceback.print_exc()
            entry = {"previous": entry.get("latest"), "latest": estimate}
        self.changed_since_slice = False
        models[fingerprint] = entry
        for old in sorted(models, key=lambda k: (models[k].get("latest") or {}).get("at", 0))[:-MAX_ESTIMATE_MODELS]:
            del models[old]
        data["current"] = fingerprint
        try:
            _write_json_atomic(_storage_path(ESTIMATES_FILE), data)
        except Exception:
            traceback.print_exc()
        self.pending_gcode = None
        self.message = (self._t("Estimate updated from %s.") % estimate["file"]) if exported \
            else self._t("Estimate updated from the last slice.")

    # --- state to the page ---

    def _sync_parts(self):
        """When other parts are in Orca, bring back the choices last used with them."""
        fingerprint = _parts_fingerprint()
        if fingerprint == self.parts:
            return
        self.parts = fingerprint
        saved = _read_forms().get(fingerprint) if fingerprint else None
        if saved:
            self.form = clean_form(saved.get("form"))
            self.form_version += 1
            self.message = self._t("Brought back the choices you last used with these parts.")

    def _worker_state(self):
        """The page state, built by a worker thread while it holds self.lock."""
        try:
            self._load_originals()
            return self._state()
        except Exception:
            traceback.print_exc()
            return None

    def _post_unlocked(self, state):
        # Called without self.lock: posting can wait for the UI thread, and the UI thread may be
        # waiting for the lock in on_message (on Linux, closing a dropdown sends "ready").
        if state is not None:
            try:
                self.post_message(state)
            except Exception:
                traceback.print_exc()

    def _push(self, refresh_info=True):
        try:
            self._load_originals()
            if refresh_info and threading.current_thread() is threading.main_thread():
                self._detect_language()
                self._sync_parts()
                self.info = self._read_info()
                # Show what can be restored in the process preset in use.
                if self.info.get("process"):
                    self._use_preset(self.info["process"])
            self.post_message(self._state())
        except Exception:
            traceback.print_exc()

    def _saved_state(self, currency):
        saved = _saved_total(_read_estimates())
        if saved is None:
            return None
        # The total was added up in the prices' currency of that time: shown converted, or without cost.
        same = saved["currency"] == currency["price"] and saved["cost"] is not None
        return {**saved, "cost": round(saved["cost"] * currency["rate"], 2) if same else None}

    def _read_info(self):
        try:
            context = self._context()
            return {
                "process": context["selected"].name,
                "dirty": bool(context["edited"].is_dirty),
                "filament": context["filament_name"],
                "family": self._t(FAMILY_LABELS[material_family(context["filament_type"], context["filament_name"])]),
                "filament_type": context["filament_type"],
                "filaments": context["filament_count"],
                "filament_list": [{"name": name, "orca_cost": cost}
                                  for name, cost in zip(context["filament_names"], context["filament_costs"])],
                "supports": context["current"].get("enable_support") == "1",
            }
        except Exception as error:
            return {"error": str(error)}

    def _state(self):
        currency = self._currency_state()
        return {
            "type": "state",
            "info": getattr(self, "info", {}),
            "mode": self.mode(),
            "restorable": [{"key": k, "label": self._t(LABELS.get(k, k)), "original": v} for k, v in self.originals.items()],
            "restore_source": self.originals_source,
            "purposes": [{"id": k, "label": self._t(v["label"]), "hint": self._t(v["hint"]), "S": v["S"], "V": v["V"], "R": v["R"]}
                         for k, v in PURPOSES.items()],
            "materials": [{"id": k, "label": self._t(FAMILY_LABELS[k])} for k in MATERIAL_CHOICES],
            "lang": self.lang,
            "strings": TRANSLATIONS.get(self.lang, {}),
            "form": self.form,
            "form_version": self.form_version,
            "result": self.result,
            "source": self.source,
            "stale": self.stale,
            "busy": self.busy,
            "currency": currency,
            "estimates": _current_estimates(currency["rate"]),
            "saved": self._saved_state(currency),
            "prices": _read_prices(),
            "spool_grams": DEFAULT_SPOOL_GRAMS,
            "message": self.message,
            "error": self.error,
        }


PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root {
  color-scheme: light dark;
  --bg: var(--orca-bg, #f6f7f8); --fg: var(--orca-fg, #1d2125); --accent: var(--orca-accent, #009688);
  --card: rgba(127,127,127,.08); --line: rgba(127,127,127,.25); --muted: rgba(127,127,127,.95);
  --save: #1f9d55; --cost: #c2410c; --error: #d13c32;
  font: 13px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif;
}
* { box-sizing: border-box; }
body { margin: 0; padding: 16px; background: var(--bg); color: var(--fg); }
.wrap { max-width: 980px; margin: 0 auto; display: grid; gap: 12px; }
header { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
h1 { font-size: 18px; margin: 0; display: flex; align-items: center; gap: 10px; }
.logo { width: 34px; height: 34px; flex: none; background: url(data:image/png;base64,__LOGO_PNG__) center / contain no-repeat; }
.meta { color: var(--muted); font-size: 12px; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 14px; }
.card h2 { font-size: 13px; margin: 0 0 10px; text-transform: uppercase; letter-spacing: .04em; color: var(--muted); }
.stats { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }
.stat .value { font-size: 22px; font-weight: 650; font-variant-numeric: tabular-nums; }
.stat .label { color: var(--muted); font-size: 12px; }
.delta { font-size: 12px; font-weight: 600; }
.delta.saves { color: var(--save); } .delta.costs { color: var(--cost); }
.hint { color: var(--muted); font-size: 12px; margin-top: 8px; }
.saved { font-size: 12px; margin-top: 8px; display: flex; gap: 8px; align-items: baseline; flex-wrap: wrap; }
.saved:empty { display: none; } .saved .total { font-weight: 600; } .saved small { color: var(--muted); }
.saved .total.saves { color: var(--save); } .saved .total.costs { color: var(--cost); }
.form { display: grid; grid-template-columns: 1.2fr 2fr; gap: 14px; align-items: start; }
.purposes { display: grid; gap: 6px; }
.purpose { display: flex; gap: 8px; align-items: flex-start; padding: 7px 9px; border: 1px solid var(--line); border-radius: 8px; cursor: pointer; }
.purpose.on { border-color: var(--accent); box-shadow: inset 0 0 0 1px var(--accent); }
.purpose input { margin-top: 3px; accent-color: var(--accent); }
.purpose small { display: block; color: var(--muted); }
.field { display: grid; grid-template-columns: 110px 1fr; gap: 4px 10px; align-items: center; margin-bottom: 14px; }
.field select { font: inherit; padding: 5px 8px; border-radius: 7px; border: 1px solid var(--line); background: var(--bg); color: var(--fg); }
.field small { grid-column: 2; color: var(--muted); }
.slider { display: grid; grid-template-columns: 110px 1fr 28px; gap: 10px; align-items: center; margin-bottom: 10px; }
.slider input { width: 100%; accent-color: var(--accent); }
.slider output { font-weight: 650; text-align: right; font-variant-numeric: tabular-nums; }
.slider small { grid-column: 2 / 4; color: var(--muted); margin-top: -6px; }
button { border: 0; border-radius: 7px; padding: 7px 12px; font: inherit; font-weight: 600; cursor: pointer; background: var(--accent); color: #fff; }
button.ghost { background: transparent; color: var(--fg); border: 1px solid var(--line); }
button:disabled { opacity: .45; cursor: default; }
.row-actions { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
.toolbar { display: flex; justify-content: space-between; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 10px; }
.changes { display: grid; gap: 14px; }
.section { display: grid; gap: 8px; }
.section-title { font-weight: 650; }
.section .hint { margin-top: -4px; }
.change { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 10px; align-items: center;
          padding: 10px 12px; border: 1px solid var(--line); border-radius: 8px; background: var(--bg); }
.change.applied { opacity: .7; } .change.skipped { opacity: .5; }
.change .name { font-weight: 650; }
.change .values { font-variant-numeric: tabular-nums; margin: 2px 0; }
.change .values .from { color: var(--muted); text-decoration: line-through; }
.change .values .to { font-weight: 650; }
.change .why { color: var(--muted); font-size: 12px; }
.badge { display: inline-block; font-size: 11px; font-weight: 650; padding: 1px 7px; border-radius: 999px; margin-left: 6px; }
.badge.saves { background: rgba(31,157,85,.15); color: var(--save); }
.badge.costs { background: rgba(194,65,12,.15); color: var(--cost); }
.badge.state { background: rgba(127,127,127,.2); }
.status { min-height: 18px; font-size: 12px; }
.status.error { color: var(--error); }
.notes { margin: 8px 0 0; padding-left: 18px; color: var(--muted); font-size: 12px; }
.empty { color: var(--muted); padding: 6px 0; }
.save-preset { border-top: 1px solid var(--line); margin-top: 12px; padding-top: 10px; display: grid; gap: 6px; }
.save-preset .title { font-weight: 650; }
.save-preset input { font: inherit; padding: 6px 8px; border-radius: 7px; border: 1px solid var(--line); background: var(--bg); color: var(--fg); flex: 1; min-width: 180px; }
.save-preset small { color: var(--muted); }
.prices { border-top: 1px solid var(--line); margin-top: 12px; padding-top: 10px; display: grid; gap: 8px; }
.prices .title { font-weight: 650; }
.currency { display: flex; flex-wrap: wrap; gap: 6px 16px; align-items: center; }
.currency select { font: inherit; padding: 4px 6px; border-radius: 7px; border: 1px solid var(--line); background: var(--bg); color: var(--fg); }
.currency small { flex-basis: 100%; color: var(--muted); }
.currency .consent { flex-basis: 100%; display: flex; gap: 6px; align-items: flex-start; }
.currency .consent input { margin-top: 3px; accent-color: var(--accent); }
.price { display: grid; grid-template-columns: minmax(0, 1fr) 120px 110px; gap: 4px 10px; align-items: center; }
.price input { font: inherit; padding: 5px 8px; border-radius: 7px; border: 1px solid var(--line); background: var(--bg); color: var(--fg); width: 100%; }
.price small { grid-column: 1 / -1; color: var(--muted); }
@media (max-width: 720px) { .price { grid-template-columns: 1fr 1fr; } .price .name { grid-column: 1 / -1; } }
.questions { border-top: 1px solid var(--line); margin: 4px 0 14px; padding-top: 10px; display: grid; gap: 8px; }
.questions .title { font-weight: 650; }
.question { display: flex; gap: 8px; align-items: flex-start; cursor: pointer; }
.question input { margin-top: 3px; accent-color: var(--accent); }
.question small { display: block; color: var(--muted); }
@media (max-width: 720px) { .form { grid-template-columns: 1fr; } .stats { grid-template-columns: 1fr 1fr; } }
</style></head><body>
<div class="wrap">
  <header>
    <h1><span class="logo" aria-hidden="true"></span>Filament Saver</h1>
    <div class="meta" id="info"></div>
  </header>

  <section class="card">
    <h2 data-t>Current estimate</h2>
    <div class="stats">
      <div class="stat"><div class="value" id="time">—</div><div class="label" data-t>Print time</div><div class="delta" id="time-delta"></div></div>
      <div class="stat"><div class="value" id="grams">—</div><div class="label" data-t>Filament</div><div class="delta" id="grams-delta"></div></div>
      <div class="stat"><div class="value" id="cost">—</div><div class="label"><span data-t>Cost</span> <span id="cost-source"></span></div><div class="delta" id="cost-delta"></div></div>
    </div>
    <div class="hint" id="estimate-hint"></div>
    <div class="saved" id="saved"></div>
    <div class="prices" id="prices"></div>
  </section>

  <section class="card">
    <h2 data-t>What are you printing?</h2>
    <div class="form">
      <div class="purposes" id="purposes"></div>
      <div>
        <div class="field"><label for="material" data-t>Material</label><select id="material"></select>
          <small id="material-hint"></small></div>
        <div class="slider"><label for="S" data-t>Strength</label><input type="range" id="S" min="0" max="5" step="1"><output id="S-out"></output>
          <small data-t>Walls, infill and solid layers</small></div>
        <div class="slider"><label for="V" data-t>Finish</label><input type="range" id="V" min="0" max="5" step="1"><output id="V-out"></output>
          <small data-t>Top surfaces and support interfaces</small></div>
        <div class="slider"><label for="R" data-t>Reliability</label><input type="range" id="R" min="0" max="5" step="1"><output id="R-out"></output>
          <small data-t>Brim, skirt and support coverage</small></div>
        <div class="questions" id="questions"></div>
        <div class="row-actions"><button id="suggest" data-t>Suggest changes</button><span class="meta" id="mode"></span></div>
      </div>
    </div>
  </section>

  <section class="card">
    <div class="toolbar">
      <h2 style="margin:0" data-t>Suggested changes</h2>
      <div class="row-actions">
        <button id="apply-savings" disabled data-t>Apply savings</button>
        <button id="apply-all" class="ghost" disabled data-t>Apply all</button>
        <button id="skip-all" class="ghost" disabled data-t>Skip all</button>
        <button id="restore-all" class="ghost" disabled>Restore originals</button>
      </div>
    </div>
    <div class="status" id="status"></div>
    <div class="changes" id="changes"><div class="empty" data-t>Pick what you are printing and click Suggest changes.</div></div>
    <ul class="notes" id="notes"></ul>
    <div class="save-preset">
      <label for="preset-name" class="title" data-t>Save the current process settings as a new preset</label>
      <div class="row-actions"><input id="preset-name" type="text" maxlength="120"><button id="save-preset" class="ghost" data-t>Save preset</button></div>
      <small data-t>Includes your unsaved edits and the applied suggestions. It goes into the Filament Saver group of the process list.</small>
    </div>
  </section>
</div>
<script>
'use strict';
const $ = id => document.getElementById(id);
let state = null;
// Page text is written in English; state.strings maps it to Orca's language.
function T(text, vars) {
  let out = (state && state.strings && state.strings[text]) || text;
  for (const k in vars || {}) out = out.split('{' + k + '}').join(vars[k]);
  return out;
}
let form = {purpose: 'decoration', material: 'auto', S: 1, V: 4, R: 3, answers: {}};
const QUESTIONS = [
  {id: 'top_hidden', label: 'Faces pointing up while printing will be hidden',
   hint: 'E.g. the back of a wall sign printed face down. Not for boxes, figures or parts with visible ledges.'},
  {id: 'vase', label: 'Print as a vase',
   hint: 'Open container (vase, cup, pot) printed as one continuous outer wall: no top, no infill, no supports.'},
  {id: 'holds_on_bed', label: 'The part holds on the bed by itself',
   hint: 'Wide, low base with no thin corners that lift, or you have printed this part without raft or brim and it stayed down.'},
  {id: 'purge_infill', label: 'Purge into the infill', multi: true,
   hint: 'The walls are opaque, so mixed colors inside the part will not show.'},
  {id: 'purge_support', label: 'Purge into the supports', multi: true, supports: true,
   hint: 'Mixed colors in the supports are fine; they are removed anyway.'},
];

const esc = v => String(v == null ? '' : v).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const send = msg => orca.postMessage(msg);

function fmtTime(s) {
  if (s == null) return '—';
  if (s < 60) return Math.round(s) + 's';  // a difference of under a minute is not "0m"
  const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
  return (d ? d + 'd ' : '') + (d || h ? h + 'h ' : '') + m + 'm';
}
const fmtGrams = g => g == null ? '—' : g.toFixed(g < 10 ? 2 : 1) + ' g';
// Money in a currency, with the symbol and decimals of the page's language.
function fmtMoney(value, currency) {
  if (value == null) return '—';
  try {
    return new Intl.NumberFormat(state.lang === 'pt_BR' ? 'pt-BR' : 'en-US', {style: 'currency', currency}).format(value);
  } catch (e) {
    return value.toFixed(2) + ' ' + currency;
  }
}
const fmtCost = c => fmtMoney(c, state.currency.shown);
function fmtValue(change, v) {
  if (change.bool) return v === '1' ? T('On') : T('Off');
  if (change.values && change.values[v]) return change.values[v];
  return v + (change.unit ? ' ' + change.unit : '');
}
function delta(el, latest, previous, fmt) {
  el.textContent = ''; el.className = 'delta';
  if (latest == null || previous == null) return;
  const diff = latest - previous;
  // A difference too small to show (e.g. R$ 0,004) reads as no change, not as "+R$ 0,00".
  if (Math.abs(diff) < 1e-9 || fmt(Math.abs(diff)) === fmt(0)) { el.textContent = T('no change'); return; }
  el.textContent = T('{diff} vs previous slice', {diff: (diff < 0 ? '−' : '+') + fmt(Math.abs(diff))});
  el.classList.add(diff < 0 ? 'saves' : 'costs');
}

function renderSaved() {
  const box = $('saved'), saved = state.saved;
  if (!saved) { box.innerHTML = ''; return; }
  const signed = (v, fmt) => (v > 0 ? '−' : v < 0 ? '+' : '') + fmt(Math.abs(v));  // positive totals are savings
  const parts = [signed(saved.grams, fmtGrams), signed(saved.seconds, fmtTime)];
  if (saved.cost != null) parts.push(signed(saved.cost, fmtCost));
  const since = new Date(saved.since * 1000).toLocaleDateString(state.lang === 'pt_BR' ? 'pt-BR' : undefined);
  box.innerHTML = '<span>' + esc(T('With the suggestions:')) + '</span><span class="total ' + (saved.grams < 0 ? 'costs' : 'saves') +
    '">' + esc(parts.join(' · ')) + '</span><small>' +
    esc(T('{n} comparison(s) since {date}, between slices of the same parts before and after applying or undoing suggestions; other edits made in between count too.', {n: saved.count, date: since})) +
    '</small><button class="ghost" data-reset-saved>' + esc(T('Reset')) + '</button>';
}

function renderEstimate() {
  renderSaved();
  const est = state.estimates || {}, latest = est.latest, previous = est.previous;
  $('time').textContent = fmtTime(latest && latest.seconds);
  $('grams').textContent = fmtGrams(latest && latest.grams);
  $('cost').textContent = fmtCost(latest && latest.cost);
  const cur = state.currency;
  const rateDate = cur.rates_date ? new Date(cur.rates_date + 'T00:00:00Z').toLocaleDateString(state.lang === 'pt_BR' ? 'pt-BR' : 'en-US', {timeZone: 'UTC'}) : '';
  const converted = cur.converted ? ', ' + T('converted from {code} at the {date} rate', {code: cur.price, date: rateDate}) : '';
  $('cost-source').textContent = latest ? '(' + T(latest.priced ? 'your spool price' : "Orca's filament cost") + converted + ')' : '';
  delta($('time-delta'), latest && latest.seconds, previous && previous.seconds, fmtTime);
  delta($('grams-delta'), latest && latest.grams, previous && previous.grams, fmtGrams);
  delta($('cost-delta'), latest && latest.cost, previous && previous.cost, fmtCost);
  if (!latest) {
    $('estimate-hint').textContent = T('No estimate yet. Slice the plate; the time and filament show up here.');
    return;
  }
  const date = new Date(latest.at * 1000).toLocaleString(state.lang === 'pt_BR' ? 'pt-BR' : undefined);
  // An empty name is the temporary G-code of a slice.
  const source = latest.file ? T('From {file}, {date}.', {file: latest.file, date}) : T('From the last slice, {date}.', {date});
  // The stored previous slice always has the same parts, on the same plate, at the same size.
  $('estimate-hint').textContent = source + ' ' + (previous
    ? T('The difference is against the previous slice of the same parts on the same plate.')
    : T('Slice again after applying changes to compare.'));
}

function renderPrices() {
  const box = $('prices');
  // Leave the fields alone while one is being edited.
  if (box.contains(document.activeElement)) return;
  const list = (state.info && state.info.filament_list) || [];
  const prices = state.prices || {};
  const cur = state.currency;
  const options = selected => cur.available.map(code =>
    '<option value="' + esc(code) + '"' + (code === selected ? ' selected' : '') + '>' + esc(code) + '</option>').join('');
  let status = '', retry = false;
  if (cur.price === cur.display) status = '';
  else if (!cur.rates_allowed) status = T('Check the box above to convert. Until then the cost stays in {code}.', {code: cur.shown});
  else if (cur.fetching) status = T('Getting exchange rates…');
  else if (cur.error) {
    status = cur.error + ' ' + T('If Orca asked for network access, allow it and click Try again. Showing the cost in {code}.', {code: cur.shown});
    retry = true;
  }
  else if (!cur.converted) status = T('No exchange rate yet. Showing the cost in {code}.', {code: cur.shown});
  const currencyRow = '<div class="currency">' +
    '<label>' + esc(T('Currency of your prices and of Orca\'s filament cost')) + ' <select data-currency="price">' + options(cur.price) + '</select></label>' +
    '<label>' + esc(T('Show the cost in')) + ' <select data-currency="display">' + options(cur.display) + '</select></label>' +
    '<label class="consent"><input type="checkbox" data-consent' + (cur.rates_allowed ? ' checked' : '') + '> ' +
    esc(T('Allow fetching exchange rates from {service}. Only the currency code is sent.', {service: cur.service.replace('https://', '')})) +
    '</label>' +
    (status ? '<small>' + esc(status) + (retry ? ' <button class="ghost" data-retry>' + esc(T('Try again')) + '</button>' : '') + '</small>' : '') + '</div>';
  box.innerHTML = currencyRow + (!list.length ? '' : '<div class="title">' + esc(T('Spool price (optional), in {code}', {code: cur.price})) + '</div>' + list.map((f, i) => {
    const p = prices[f.name];
    const orcaCost = _numberOrNull(f.orca_cost);
    const fallback = orcaCost != null ? T("Empty: Orca's filament cost, {cost} per kg.", {cost: fmtMoney(orcaCost, cur.price)}) : T("Empty: Orca's filament cost.");
    const hint = p ? T('{cost} per kg with this spool.', {cost: fmtMoney(p.price * 1000 / p.grams, cur.price)}) : fallback;
    return '<div class="price" data-name="' + esc(f.name) + '"><span class="name">' + esc(f.name) + '</span>' +
      '<input type="number" min="0" step="0.01" data-field="price" aria-label="' + esc(T('Spool price')) + '" placeholder="' +
      esc(T('Spool price')) + '" value="' + esc(p ? p.price : '') + '">' +
      '<input type="number" min="1" step="1" data-field="grams" aria-label="' + esc(T('Spool weight (g)')) + '" placeholder="' +
      esc(T('{grams} g', {grams: state.spool_grams})) + '" value="' + esc(p && p.grams !== state.spool_grams ? p.grams : '') + '">' +
      '<small>' + esc(hint) + '</small></div>';
  }).join(''));
}
function _numberOrNull(v) { const n = parseFloat(v); return Number.isFinite(n) ? n : null; }

function renderForm() {
  $('purposes').innerHTML = state.purposes.map(p =>
    '<label class="purpose' + (p.id === form.purpose ? ' on' : '') + '"><input type="radio" name="purpose" value="' + esc(p.id) + '"' +
    (p.id === form.purpose ? ' checked' : '') + '><span>' + esc(p.label) + '<small>' + esc(p.hint) + '</small></span></label>').join('');
  const info = state.info || {};
  const detected = info.family ? T('Auto: {family}', {family: info.family}) : T('Auto');
  $('material').innerHTML = '<option value="auto">' + esc(detected) + '</option>' +
    state.materials.map(m => '<option value="' + esc(m.id) + '">' + esc(m.label) + '</option>').join('');
  $('material').value = form.material || 'auto';
  $('material-hint').textContent = form.material === 'auto'
    ? (info.filament ? T('Detected from {filament}', {filament: info.filament}) + (info.filament_type ? ' (' + info.filament_type + ')' : '') : '')
    : T('Overrides the filament profile for these suggestions.');
  for (const k of ['S', 'V', 'R']) { $(k).value = form[k]; $(k + '-out').textContent = form[k]; }
  const purpose = state.purposes.find(p => p.id === form.purpose);
  $('preset-name').placeholder = (info.process || '') + (purpose ? ' - ' + purpose.label : '');
  const shown = QUESTIONS.filter(q => (!q.multi || info.filaments > 1) && (!q.supports || info.supports));
  const answers = form.answers || {};
  $('questions').innerHTML = '<div class="title">' + esc(T('Only check what you are sure of')) + '</div>' + shown.map(q =>
    '<label class="question"><input type="checkbox" data-q="' + esc(q.id) + '"' + (answers[q.id] ? ' checked' : '') + '><span>' +
    esc(T(q.label)) + '<small>' + esc(T(q.hint)) + '</small></span></label>').join('');
  $('mode').textContent = state.mode === 'in_place'
    ? T('Changes are applied as unsaved edits of the current process preset.')
    : T('Changes are saved to a "… - Filament Saver" process preset.');
}

function renderChanges() {
  const result = state.result, list = $('changes');
  $('notes').innerHTML = result ? result.notes.map(n => '<li>' + esc(n) + '</li>').join('') : '';
  const blocked = state.busy || state.stale;
  // Skipped rows stay out of both buttons until "Undo skip".
  const toApply = result ? result.changes.filter(c => c.status === 'pending' || c.status === 'error') : [];
  $('apply-all').disabled = blocked || !toApply.length;
  $('apply-savings').disabled = blocked || !toApply.some(c => c.effect !== 'costs');
  $('skip-all').disabled = blocked || !result || !result.changes.some(c => c.status === 'pending');
  const restorable = state.restorable || [];
  $('restore-all').disabled = state.busy || !restorable.length;
  $('restore-all').textContent = restorable.length ? T('Restore originals ({n})', {n: restorable.length}) : T('Restore originals');
  $('restore-all').title = restorable.map(r => r.label + ': ' + r.original).join('\n');
  if (!result) return;
  const canRestore = new Set(restorable.map(r => r.key));
  if (!result.changes.length) { list.innerHTML = '<div class="empty">' + esc(T('Nothing to change for this profile.')) + '</div>'; return; }
  const row = c => {
    const badge = (c.effect ? '<span class="badge ' + c.effect + '">' + esc(T(c.effect === 'saves' ? 'saves filament' : 'uses more')) + '</span>' : '') +
      (c.group === 'vase' ? '<span class="badge state">' + esc(T('vase mode, applied together')) + '</span>' : '');
    let actions;
    if (c.status === 'applied') actions = '<span class="badge state">' + esc(T('Applied')) + '</span>' + (canRestore.has(c.key)
      ? '<button class="ghost" data-act="restore" data-id="' + esc(c.id) + '"' + (state.busy ? ' disabled' : '') + '>' + esc(T('Undo')) + '</button>' : '');
    else if (c.status === 'skipped') actions = '<button class="ghost" data-act="undo_skip" data-id="' + esc(c.id) + '"' + (blocked ? ' disabled' : '') + '>' + esc(T('Undo skip')) + '</button>';
    else actions = '<button data-act="apply" data-id="' + esc(c.id) + '"' + (blocked ? ' disabled' : '') + '>' + esc(T('Apply')) + '</button>' +
                   '<button class="ghost" data-act="skip" data-id="' + esc(c.id) + '"' + (blocked ? ' disabled' : '') + '>' + esc(T('Skip')) + '</button>';
    const err = c.status === 'error' ? '<div class="why" style="color:var(--error)">' + esc(c.error || T('Failed')) + '</div>' : '';
    const overridden = (c.overridden_by || []).length
      ? '<div class="why" style="color:var(--cost)">' + esc(T('Not used by objects with their own value: {names}', {names: c.overridden_by.join(', ')})) + '</div>' : '';
    return '<div class="change ' + esc(c.status) + '"><div><div class="name">' + esc(c.label) + badge + '</div>' +
      '<div class="values"><span class="from">' + esc(fmtValue(c, c.current)) + '</span> → <span class="to">' + esc(fmtValue(c, c.suggested)) + '</span></div>' +
      '<div class="why">' + esc(c.reason) + '</div>' + overridden + err + '</div><div class="row-actions">' + actions + '</div></div>';
  };
  // Increases only show up when the current value is below what the purpose or the material
  // needs; they get their own section so "Apply savings" never raises filament use.
  const savings = result.changes.filter(c => c.effect !== 'costs');
  const needed = result.changes.filter(c => c.effect === 'costs');
  const section = (title, hint, rows) => !rows.length ? '' : '<div class="section"><div class="section-title">' + esc(title) +
    '</div>' + (hint ? '<div class="hint">' + esc(hint) + '</div>' : '') + rows.map(row).join('') + '</div>';
  list.innerHTML = section(T('Save filament'), '', savings) +
    section(T('Needed for the purpose or the material'),
            T('Your current value is below what the purpose or the material needs. Apply all also applies these.'), needed);
}

function render() {
  const i = state.info || {};
  $('info').textContent = i.error ? i.error : (i.process || '') + (i.dirty ? ' ' + T('(edited)') : '') + (i.filament ? ' · ' + i.filament + ' (' + i.family + ')' : '');
  // The form comes from the plugin on load and whenever it brings back the choices for other parts.
  if (state.form && state.form_version !== render.formVersion) {
    form = Object.assign({}, state.form);
    render.formVersion = state.form_version;
  }
  document.documentElement.lang = state.lang === 'pt_BR' ? 'pt-BR' : 'en';
  for (const el of document.querySelectorAll('[data-t]')) {
    if (!el.dataset.t) el.dataset.t = el.textContent;
    el.textContent = T(el.dataset.t);
  }
  renderEstimate(); renderPrices(); renderForm(); renderChanges();
  const status = $('status');
  status.textContent = state.error || state.message || '';
  status.className = 'status' + (state.error ? ' error' : '');
  $('suggest').disabled = state.busy;
  $('save-preset').disabled = state.busy;
}

$('purposes').addEventListener('change', e => {
  const p = state.purposes.find(x => x.id === e.target.value);
  if (!p) return;
  form = {purpose: p.id, material: form.material, S: p.S, V: p.V, R: p.R, answers: form.answers};
  renderForm();
});
$('material').addEventListener('change', e => { form.material = e.target.value; renderForm(); });
$('saved').addEventListener('click', e => {
  if (e.target.dataset.resetSaved !== undefined) send({cmd: 'reset_saved'});
});
$('prices').addEventListener('click', e => {
  if (e.target.dataset.retry !== undefined) send({cmd: 'set_currency'});
});
$('prices').addEventListener('change', e => {
  if (e.target.dataset.consent !== undefined) {
    send({cmd: 'set_currency', rates_allowed: e.target.checked});
    e.target.blur();
    return;
  }
  if (e.target.dataset.currency) {
    send({cmd: 'set_currency', [e.target.dataset.currency]: e.target.value});
    e.target.blur();
    return;
  }
  const row = e.target.closest('.price');
  if (!row) return;
  const field = name => row.querySelector('[data-field="' + name + '"]').value;
  e.target.blur();
  send({cmd: 'set_price', name: row.dataset.name, price: field('price'), grams: field('grams')});
});
$('questions').addEventListener('change', e => {
  const q = e.target.dataset.q;
  if (q) form.answers = Object.assign({}, form.answers, {[q]: e.target.checked});
});
for (const k of ['S', 'V', 'R']) $(k).addEventListener('input', e => { form[k] = Number(e.target.value); $(k + '-out').textContent = form[k]; });
$('suggest').addEventListener('click', () => send(Object.assign({cmd: 'suggest'}, form)));
$('apply-savings').addEventListener('click', () => {
  const ids = state.result.changes.filter(c => (c.status === 'pending' || c.status === 'error') && c.effect !== 'costs').map(c => c.id);
  if (ids.length) send({cmd: 'apply', ids});
});
$('apply-all').addEventListener('click', () => {
  const ids = state.result.changes.filter(c => c.status === 'pending' || c.status === 'error').map(c => c.id);
  if (ids.length) send({cmd: 'apply', ids});
});
$('skip-all').addEventListener('click', () => send({cmd: 'skip', ids: state.result.changes.filter(c => c.status === 'pending').map(c => c.id)}));
$('restore-all').addEventListener('click', () => send({cmd: 'restore'}));
$('save-preset').addEventListener('click', () => {
  const name = $('preset-name').value.trim() || $('preset-name').placeholder;
  send({cmd: 'save_preset', name});
});
$('preset-name').addEventListener('keydown', e => { if (e.key === 'Enter') $('save-preset').click(); });
// Another project may have been opened while the tab was in the background.
window.addEventListener('focus', () => send({cmd: 'ready'}));
$('changes').addEventListener('click', e => {
  const b = e.target.closest('[data-act]');
  if (b && !b.disabled) send({cmd: b.dataset.act, ids: [b.dataset.id]});
});
let refreshTimer = null;
orca.onMessage(msg => {
  if (!msg) return;
  if (msg.type === 'read_request') send({cmd: 'read_estimate'});
  else if (msg.type === 'refresh_request') {
    // Orca sends one per object when arranging or undoing: refresh once, after the last.
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(() => send({cmd: 'ready'}), 300);
  }
  else if (msg.type === 'state') { state = msg; render(); }
});
send({cmd: 'ready'});
</script></body></html>"""


@orca.plugin
class FilamentSaverPackage(orca.base):
    def register_capabilities(self):
        # One worker thread applies changes: the in-place write API must not run on the UI thread.
        # Network access is not declared here: the exchange-rate service is only contacted after
        # the user allows it in the tab, and Orca then asks for that host itself.
        orca.request_permissions(threading=True)
        orca.register_capability(FilamentSaverPage)
