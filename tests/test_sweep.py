"""Randomized safety sweep of suggest(): random profiles over Orca's full value ranges, every
purpose, slider, material and answer. Each case checks the guarantees the plugin makes, and that
suggesting again after applying everything suggests nothing more.

    python3 -m unittest tests.test_sweep        # or with the rest: discover -s tests
"""

import collections
import math
import random
import unittest

from test_rules import fs

CASES = 2000
rnd = random.Random(7)
ENUMS = {
    "sparse_infill_pattern": ["rectilinear", "alignedrectilinear", "zigzag", "crosszag", "lockedzag", "line", "grid", "triangles", "tri-hexagon", "cubic", "adaptivecubic", "quartercubic", "supportcubic", "lightning", "honeycomb", "3dhoneycomb", "lateral-honeycomb", "lateral-lattice", "crosshatch", "tpmsd", "tpmsfk", "gyroid", "concentric", "hilbertcurve", "archimedeanchords", "octagramspiral"],
    "brim_type": ["no_brim", "auto_brim", "brim_ears", "outer_only", "inner_only", "outer_and_inner", "painted", "leading_edge_only"],
    "ensure_vertical_shell_thickness": ["none", "ensure_critical_only", "ensure_moderate", "ensure_all"],
    "gap_fill_target": ["nowhere", "topbottom", "everywhere"],
    "support_type": ["normal(auto)", "tree(auto)", "normal(manual)", "tree(manual)"],
    "support_style": ["default", "grid", "snug", "organic", "tree_slim", "tree_strong", "tree_hybrid"],
    "support_base_pattern": ["default", "rectilinear", "rectilinear-grid", "honeycomb", "lightning", "hollow", "auto", "concentric", "spiralinset", "rectilinear_interlaced", "grid"],
    "draft_shield": ["disabled", "enabled"],
}
VALID = {k: set(v) for k, v in ENUMS.items()}
VALID["sparse_infill_pattern"] |= {"lightning", "supportcubic", "adaptivecubic", "gyroid"}


def profile():
    lh = rnd.choice([0.08, 0.12, 0.16, 0.2, 0.24, 0.28, 0.32])
    noz = rnd.choice([0.2, 0.4, 0.6, 0.8])
    p = {
        "layer_height": "%g" % lh, "nozzle_diameter": "%g" % noz,
        "wall_loops": str(rnd.randint(1, 8)), "sparse_infill_density": "%d%%" % rnd.choice([0, 5, 10, 15, 20, 25, 40, 60, 100]),
        "sparse_infill_pattern": rnd.choice(ENUMS["sparse_infill_pattern"]),
        "top_shell_layers": str(rnd.randint(1, 10)), "bottom_shell_layers": str(rnd.randint(1, 8)),
        "top_shell_thickness": "%g" % rnd.choice([0, 0.4, 0.6, 0.8, 1, 1.2]), "bottom_shell_thickness": "%g" % rnd.choice([0, 0.4, 0.8]),
        "ensure_vertical_shell_thickness": rnd.choice(ENUMS["ensure_vertical_shell_thickness"]),
        "gap_fill_target": rnd.choice(ENUMS["gap_fill_target"]), "minimum_sparse_infill_area": str(rnd.choice([0, 5, 10, 15, 30])),
        "only_one_wall_top": rnd.choice("01"), "only_one_wall_first_layer": rnd.choice("01"),
        "brim_type": rnd.choice(ENUMS["brim_type"]), "brim_width": "%g" % rnd.choice([0, 2, 3, 5, 8, 10]),
        "skirt_loops": str(rnd.randint(0, 3)), "skirt_height": str(rnd.randint(1, 3)), "draft_shield": rnd.choice(ENUMS["draft_shield"]),
        "raft_layers": str(rnd.choice([0, 0, 2, 3])), "enable_support": rnd.choice("01"), "support_type": rnd.choice(ENUMS["support_type"]),
        "support_threshold_angle": str(rnd.choice([0, 15, 25, 30, 40, 55])), "support_remove_small_overhang": rnd.choice("01"),
        "support_style": rnd.choice(ENUMS["support_style"]), "support_base_pattern": rnd.choice(ENUMS["support_base_pattern"]),
        "support_base_pattern_spacing": "%g" % rnd.choice([1.5, 2, 2.5, 3, 4]), "tree_support_wall_count": str(rnd.randint(0, 2)),
        "support_interface_top_layers": str(rnd.randint(0, 4)), "support_interface_bottom_layers": str(rnd.choice([-1, 0, 1, 2])),
        "support_interface_spacing": "%g" % rnd.choice([0, 0.2, 0.5, 1]), "support_top_z_distance": "%g" % rnd.choice([0, 0.1, 0.2, 0.3]),
        "enable_prime_tower": rnd.choice("01"), "prime_tower_brim_width": "%g" % rnd.choice([-1, 0, 3, 5]),
        "flush_into_infill": rnd.choice("01"), "flush_into_support": rnd.choice("01"), "top_surface_density": "100%",
        "spiral_mode": "0", "enforce_support_layers": "0", "detect_thin_wall": rnd.choice("01"), "overhang_reverse": rnd.choice("01"),
        "timelapse_type": rnd.choice("01"), "enable_wrapping_detection": "0",
    }
    return p

MATS = ["PLA", "PETG", "ABS", "ASA", "TPU", "PA", "PA-CF", "PC", "PPS", "HIPS"]


class SweepTest(unittest.TestCase):
    def test_guarantees_hold_on_random_profiles(self):
        problems = collections.Counter()
        examples = {}

        def bad(kind, info):
            problems[kind] += 1
            examples.setdefault(kind, info)

        for _ in range(CASES):
            cur = profile()
            purpose = rnd.choice(list(fs.PURPOSES)); S, V, R = (rnd.randint(0, 5) for _ in range(3))
            mat = rnd.choice(MATS); others = rnd.sample(MATS, rnd.choice([0, 0, 1]))
            answers = {q: rnd.random() < 0.2 for q in fs.QUESTIONS}
            res = fs.suggest(dict(cur), purpose, S, V, R, mat, mat, answers=answers, filaments=1 + len(others),
                             same_family=not others, other_filaments=[(o, o) for o in others])
            ch = {c["key"]: c for c in res["changes"]}
            final = {**cur, **{k: c["suggested"] for k, c in ch.items()}}
            fams = set(res["families"]); vase = answers["vase"]
            info = (purpose, S, V, R, mat, others, {k: v for k, v in answers.items() if v})
            for k, c in ch.items():
                if k not in cur: bad("key not in profile", (k, info))
                if k in VALID and c["suggested"] not in VALID[k]: bad("invalid enum " + k, (c["suggested"], info))
                if c["suggested"] == c["current"]: bad("no-op change", (k, info))
                if c["effect"] == "" and not c.get("group"): bad("change with unknown effect " + k, (c["current"], c["suggested"], info))
            lh = float(cur["layer_height"]); noz = float(cur["nozzle_diameter"])
            if not vase:
                if int(final["wall_loops"]) < min(2, int(cur["wall_loops"])): bad("walls lowered below 2", (cur["wall_loops"], final["wall_loops"], info))
                for side in ("top", "bottom"):
                    if int(final[side + "_shell_layers"]) < min(2, int(cur[side + "_shell_layers"])):
                        bad(side + " layers lowered below 2", (cur[side + "_shell_layers"], final[side + "_shell_layers"], info))
                # effective top thickness lowered below 0.6 by the plugin
                def top_mm(p): return max(int(p["top_shell_layers"]) * lh, float(p["top_shell_thickness"]))
                if top_mm(final) < min(0.6, top_mm(cur)) - 1e-6: bad("top thinner than 0.6 mm", (top_mm(cur), top_mm(final), info))
                floors = fs.PURPOSES[purpose]["floors"]
                wall_mm = int(final["wall_loops"]) * noz
                if wall_mm < floors["walls"] * 0.4 - 1e-6 and wall_mm < int(cur["wall_loops"]) * noz: bad("walls below purpose floor", (cur["wall_loops"], final["wall_loops"], noz, info))
            if final.get("top_surface_density") == "0%" and cur.get("top_surface_density") != "0%":
                eff_top = max(int(final["top_shell_layers"]), math.ceil(float(final["top_shell_thickness"]) / lh - 1e-9))
                if eff_top < 3: bad("hidden top with too few layers", (eff_top, info))
            if fams & {"abs_asa", "pa", "pc"}:
                order = fs.BRIM_ORDER
                if cur["brim_type"] not in order and final["brim_type"] != cur["brim_type"]:
                    bad("warping brim of another kind replaced", (cur["brim_type"], final["brim_type"], info))
                if cur["brim_type"] in order and final["brim_type"] in order and order.index(final["brim_type"]) < order.index(cur["brim_type"]):
                    bad("warping brim reduced", (cur["brim_type"], final["brim_type"], info))
                if float(final["brim_width"]) < float(cur["brim_width"]): bad("warping brim width reduced", (cur["brim_width"], final["brim_width"], info))
                if int(final["raft_layers"]) < int(cur["raft_layers"]): bad("raft removed for warping", info)
            if "petg" in fams and final["enable_support"] == "1" and not vase:
                if float(final["support_top_z_distance"]) < min(0.25, float(cur["support_top_z_distance"])): bad("petg z gap lowered", info)
                if int(final["support_interface_top_layers"]) < min(2, int(cur["support_interface_top_layers"])): bad("petg interface lowered", info)
            if "tpu" in fams:
                for k in ("sparse_infill_density", "sparse_infill_pattern", "minimum_sparse_infill_area"):
                    if k in ch and not vase: bad("tpu infill touched", (k, info))
            for k, c in ch.items():
                if c["effect"] == "costs" and k not in ("wall_loops", "top_shell_layers", "bottom_shell_layers", "sparse_infill_density",
                                                          "sparse_infill_pattern", "support_interface_top_layers", "brim_type", "brim_width"):
                    bad("unexpected increase " + k, (c["current"], c["suggested"], info))
            if "support_interface_top_layers" in ch and int(ch["support_interface_top_layers"]["suggested"]) < 1 and int(cur["support_interface_top_layers"]) >= 1:
                bad("interface removed", info)
            # stability: apply everything, suggest again
            res2 = fs.suggest(dict(final), purpose, S, V, R, mat, mat, answers=answers, filaments=1 + len(others),
                              same_family=not others, other_filaments=[(o, o) for o in others])
            again = [(c["key"], c["current"], c["suggested"]) for c in res2["changes"]]
            if again: bad("not stable after applying", (again[:3], info))

        self.assertEqual({kind: (count, str(examples[kind])[:300]) for kind, count in problems.items()}, {})
