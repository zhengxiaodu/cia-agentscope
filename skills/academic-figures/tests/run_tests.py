#!/usr/bin/env python3
"""Regression test suite for academic-figures scripts.

Runs on stdlib `unittest` only (no pytest dependency).
Coverage:
  1. Smoke test — all 14 chart types generate via the real CLI (exit 0 + file exists)
  2. validate_data() — fatal/warning detection per chart family
  3. load_data() — CSV edge cases (mixed numeric/text, scatter/box long format)
  4. has_cjk() — CJK detection
  5. legend_audit() — empty-legend detection via real matplotlib axes
  6. audit_pdf.py — PDF font-size audit behavior
  7. gen_legend.py — journal legend text for all chart types

Usage:
    python3 tests/run_tests.py            # run all
    python3 tests/run_tests.py -v         # verbose
"""

import contextlib
import math
import re
import glob
import io
import json
import random
import shlex
import os
import subprocess
import sys
import tempfile
import unittest

import numpy as np

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt  # noqa: E402

SCRIPT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
sys.path.insert(0, SCRIPT_DIR)

import gen_figure  # noqa: E402
import gen_legend  # noqa: E402
import audit_pdf  # noqa: E402

GEN = os.path.join(SCRIPT_DIR, "gen_figure.py")


# ── minimal valid sample data per chart family ─────────────────────────

SAMPLE = {
    "bar": {"labels": ["A", "B", "C"], "series": {"Ctrl": [1.0, 2.0, 3.0], "Treated": [2.5, 1.5, 4.0]}},
    "hbar": {"labels": ["A", "B"], "series": {"Ctrl": [1.0, 2.0], "Treated": [2.5, 1.5]}},
    "stacked_bar": {"labels": ["A", "B"], "series": {"x": [1.0, 2.0], "y": [0.5, 1.0]}},
    "heatmap": {"matrix": [[1.0, 2.0], [3.0, 4.0], [5.0, 6.0]]},
    "scatter": {"x": [1.0, 2.0, 3.0, 4.0], "y": [2.0, 4.0, 3.5, 5.0],
                "groups": ["a", "a", "b", "b"]},
    "line": {"labels": ["t0", "t1", "t2"], "series": {"S1": [1.0, 2.0, 3.0], "S2": [3.0, 2.5, 2.0]}},
    "dual_axis": {"labels": ["A", "B"], "left": {"Temp": [20.0, 30.0]}, "right": {"Count": [5, 8]}},
    "box": {"labels": ["G1", "G2"], "series": {"G1": [1.0, 2.0, 3.0], "G2": [2.0, 3.0, 4.0]}},
    "forest": {"labels": ["Study1", "Study2"], "estimates": [1.2, 0.8],
               "ci_low": [0.9, 0.5], "ci_high": [1.5, 1.1]},
    "km": {"groups": {"Placebo": [[0.0, 1.0], [5.0, 0.8], [10.0, 0.6]],
                      "Drug": [[0.0, 1.0], [5.0, 0.9], [10.0, 0.8]]}},
    "roc": {"curves": [{"fpr": [0.0, 0.5, 1.0], "tpr": [0.0, 0.7, 1.0], "label": "Model A",
                        "auc": 0.85}]},
    "violin": {"labels": ["G1", "G2"], "series": {"G1": [1.0, 2.0, 3.0, 2.5],
                                                  "G2": [2.0, 3.0, 4.0, 3.5]}},
    "composite": {"layout": [1, 2], "panels": [
        {"type": "bar", "data": {"labels": ["A", "B"], "series": {"S": [1.0, 2.0]}},
         "title": "Panel A", "pos": [0, 0]},
        {"type": "line", "data": {"labels": ["t0", "t1"], "series": {"S": [1.0, 2.0]}},
         "title": "Panel B", "pos": [0, 1]}]},
    "diagram": {"blocks": [
        {"id": "A", "label": "Input", "x": 0, "y": 0, "w": 2, "h": 1},
        {"id": "B", "label": "Output", "x": 3, "y": 0, "w": 2, "h": 1}],
        "arrows": [{"from": "A", "to": "B"}]},
    "prisma": {"records_identified": 50, "duplicates_removed": 10, "records_excluded": 20,
               "reports_not_retrieved": 2,
               "exclusion_reasons": {"Wrong population": 6, "No usable data": 4},
               "studies_included": 8},
}

# Canonical type names (aliases resolved) — the 15 distinct chart families
SMOKE_TYPES = ["bar", "hbar", "stacked_bar", "heatmap", "scatter", "line", "dual_axis",
               "box", "forest", "km", "roc", "violin", "composite", "diagram", "prisma"]


# ── 1. CLI smoke tests ─────────────────────────────────────────────────

class TestSmokeCLI(unittest.TestCase):
    """Every chart type renders through the real CLI."""

    maxDiff = None

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="af_smoke_")

    def _run(self, chart_type, extra=None):
        data_path = os.path.join(self.tmp, f"{chart_type}.json")
        with open(data_path, "w", encoding="utf-8") as f:
            json.dump(SAMPLE[chart_type], f)
        out = os.path.join(self.tmp, f"{chart_type}.png")
        cmd = [sys.executable, GEN, "--type", chart_type, "--data", data_path,
               "--out", out, "--dpi", "150"]
        if extra:
            cmd += extra
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        return proc, out

    def test_all_14_chart_types_render(self):
        for ct in SMOKE_TYPES:
            with self.subTest(chart_type=ct):
                proc, out = self._run(ct)
                self.assertEqual(proc.returncode, 0,
                                 f"{ct} failed: rc={proc.returncode}\n"
                                 f"STDOUT: {proc.stdout}\nSTDERR: {proc.stderr}")
                self.assertTrue(os.path.exists(out), f"{ct}: output file missing")
                self.assertGreater(os.path.getsize(out), 0, f"{ct}: empty output")

    def test_fatal_data_exits_1(self):
        """Missing required field -> exit code 1, ERROR printed."""
        data_path = os.path.join(self.tmp, "bad.json")
        with open(data_path, "w", encoding="utf-8") as f:
            json.dump({"labels": ["A", "B"]}, f)  # no 'series'
        out = os.path.join(self.tmp, "bad.png")
        proc = subprocess.run(
            [sys.executable, GEN, "--type", "bar", "--data", data_path, "--out", out],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("ERROR", proc.stderr)
        self.assertFalse(os.path.exists(out))

    def test_verify_pdf_exit_0_clean(self):
        """--verify on a clean PDF -> exit 0 + VERIFY OK."""
        data_path = os.path.join(self.tmp, "v.json")
        with open(data_path, "w", encoding="utf-8") as f:
            json.dump(SAMPLE["bar"], f)
        out = os.path.join(self.tmp, "v.pdf")
        proc = subprocess.run(
            [sys.executable, GEN, "--type", "bar", "--data", data_path, "--out", out,
             "--verify"],
            capture_output=True, text=True, timeout=180)
        self.assertEqual(proc.returncode, 0,
                         f"verify failed: rc={proc.returncode}\nSTDERR: {proc.stderr}")
        self.assertIn("VERIFY OK", proc.stderr)

    def test_journal_preset_applies(self):
        """--journal nature --column double -> narrower figure (89mm double column)."""
        data_path = os.path.join(self.tmp, "j.json")
        with open(data_path, "w", encoding="utf-8") as f:
            json.dump(SAMPLE["bar"], f)
        out = os.path.join(self.tmp, "j.png")
        proc = subprocess.run(
            [sys.executable, GEN, "--type", "bar", "--data", data_path, "--out", out,
             "--journal", "nature", "--column", "double"],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("期刊预设 nature", proc.stderr)

    def test_cjk_auto_detection_in_series_keys(self):
        """CJK in series names (dict keys) must trigger font loading, not tofu."""
        data_path = os.path.join(self.tmp, "cjk.json")
        with open(data_path, "w", encoding="utf-8") as f:
            json.dump({"labels": ["A", "B"],
                       "series": {"对照": [1.0, 2.0], "处理": [2.5, 1.5]}}, f)
        out = os.path.join(self.tmp, "cjk.png")
        proc = subprocess.run(
            [sys.executable, GEN, "--type", "bar", "--data", data_path, "--out", out],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("已加载中文字体", proc.stderr,
                      "CJK in series dict keys must auto-load a CJK font")
        self.assertNotIn("Glyph", proc.stderr, "no missing-glyph tofu warnings expected")


# ── 2. validate_data() unit tests ──────────────────────────────────────

class TestValidateData(unittest.TestCase):
    """(fatal, warn) split per chart family."""

    def _fatal(self, data, ct):
        return validate_fatal(data, ct)

    def test_bar_missing_series_fatal(self):
        f, w = gen_figure.validate_data({"labels": ["A", "B"]}, "bar")
        self.assertTrue(any("series" in m for m in f))

    def test_bar_length_mismatch_fatal(self):
        f, _ = gen_figure.validate_data(
            {"labels": ["A", "B", "C"], "series": {"S": [1.0, 2.0]}}, "bar")
        self.assertTrue(any("长度" in m for m in f))

    def test_bar_all_equal_warning(self):
        _, w = gen_figure.validate_data(
            {"labels": ["A", "B"], "series": {"S": [5.0, 5.0]}}, "bar")
        self.assertTrue(any("全部相同" in m and "平线" in m for m in w))

    def test_bar_empty_series_fatal(self):
        f, _ = gen_figure.validate_data(
            {"labels": ["A", "B"], "series": {"S": []}}, "bar")
        self.assertTrue(any("没有数据" in m for m in f))

    def test_heatmap_missing_matrix_fatal(self):
        f, _ = gen_figure.validate_data({"labels": ["A"]}, "heatmap")
        self.assertTrue(any("matrix" in m for m in f))

    def test_heatmap_ragged_rows_fatal(self):
        f, _ = gen_figure.validate_data({"matrix": [[1.0, 2.0], [3.0]]}, "heatmap")
        self.assertTrue(any("长度不一致" in m for m in f))

    def test_heatmap_all_equal_warning(self):
        _, w = gen_figure.validate_data({"matrix": [[2.0, 2.0], [2.0, 2.0]]}, "heatmap")
        self.assertTrue(any("值相同" in m for m in w))

    def test_scatter_missing_x_fatal(self):
        f, _ = gen_figure.validate_data({"y": [1.0, 2.0]}, "scatter")
        self.assertTrue(any("x" in m for m in f))

    def test_scatter_length_mismatch_fatal(self):
        f, _ = gen_figure.validate_data({"x": [1.0, 2.0], "y": [1.0]}, "scatter")
        self.assertTrue(any("长度" in m for m in f))

    def test_scatter_all_x_equal_warning(self):
        _, w = gen_figure.validate_data(
            {"x": [3.0, 3.0, 3.0], "y": [1.0, 2.0, 3.0]}, "scatter")
        self.assertTrue(any("值相同" in m for m in w))

    def test_forest_estimate_outside_ci_warning(self):
        _, w = gen_figure.validate_data(
            {"estimates": [5.0], "ci_low": [0.0], "ci_high": [1.0]}, "forest")
        self.assertTrue(any("落在 CI" in m for m in w))

    def test_forest_length_mismatch_fatal(self):
        f, _ = gen_figure.validate_data(
            {"estimates": [1.0, 2.0], "ci_low": [0.0], "ci_high": [1.0, 2.0]}, "forest")
        self.assertTrue(any("不一致" in m for m in f))

    def test_km_non_monotonic_time_warning(self):
        _, w = gen_figure.validate_data(
            {"groups": {"G": [[0.0, 1.0], [10.0, 0.5], [5.0, 0.8]]}}, "km")
        self.assertTrue(any("自动排序" in m and "不受影响" in m for m in w))

    def test_km_missing_groups_fatal(self):
        f, _ = gen_figure.validate_data({"labels": ["A"]}, "km")
        self.assertTrue(any("km 需要" in m for m in f))

    def test_roc_non_monotonic_fpr_warning(self):
        _, w = gen_figure.validate_data(
            {"curves": [{"fpr": [0.0, 0.9, 0.3], "tpr": [0.0, 0.7, 1.0]}]}, "roc")
        self.assertTrue(any("非单调" in m for m in w))

    def test_roc_auc_out_of_range_warning(self):
        _, w = gen_figure.validate_data(
            {"curves": [{"fpr": [0.0, 1.0], "tpr": [0.0, 1.0], "auc": 1.5}]}, "roc")
        self.assertTrue(any("auc" in m.lower() for m in w))

    def test_roc_missing_data_fatal(self):
        f, _ = gen_figure.validate_data({"labels": ["A"]}, "roc")
        self.assertTrue(any("curves" in m or "fpr" in m for m in f))

    def test_dual_axis_missing_left_fatal(self):
        f, _ = gen_figure.validate_data(
            {"labels": ["A", "B"], "right": {"R": [1.0, 2.0]}}, "dual_axis")
        self.assertTrue(any("left" in m for m in f))

    def test_composite_missing_panels_fatal(self):
        f, _ = gen_figure.validate_data({"layout": [1, 1]}, "composite")
        self.assertTrue(any("panels" in m for m in f))

    def test_composite_panel_missing_type_fatal(self):
        f, _ = gen_figure.validate_data(
            {"panels": [{"data": {"labels": ["A"], "series": {"S": [1.0]}}}]}, "composite")
        self.assertTrue(any("type" in m for m in f))

    def test_diagram_missing_blocks_fatal(self):
        f, _ = gen_figure.validate_data({"background": "light"}, "diagram")
        self.assertTrue(any("blocks" in m for m in f))

    def test_not_a_dict_fatal(self):
        f, _ = gen_figure.validate_data([1, 2, 3], "bar")
        self.assertTrue(any("JSON 对象" in m or "字典" in m for m in f))


# ── 3. load_data() CSV edge cases ──────────────────────────────────────

class TestLoadDataCSV(unittest.TestCase):
    """CSV parsing: long-format scatter/box, mixed numeric/text, TSV."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="af_csv_")
        self._n = 0

    def _csv(self, content, name="data.csv"):
        self._n += 1
        p = os.path.join(self.tmp, f"{self._n}_{name}")
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        return p

    def test_scatter_long_format_groups(self):
        p = self._csv("x,y,group\n1,2,A\n2,4,A\n3,6,B\n4,8,B\n")
        d = gen_figure.load_data(p, chart_type="scatter")
        self.assertEqual(d["x"], [1.0, 2.0, 3.0, 4.0])
        self.assertEqual(d["y"], [2.0, 4.0, 6.0, 8.0])
        self.assertEqual(d["groups"], ["A", "A", "B", "B"])

    def test_box_long_format(self):
        p = self._csv("group,value\nCtrl,1\nCtrl,2\nCtrl,3\nDrug,4\nDrug,5\nDrug,6\n")
        d = gen_figure.load_data(p, chart_type="box")
        self.assertEqual(list(d["labels"]), ["Ctrl", "Drug"])
        self.assertEqual(d["series"]["Ctrl"], [1.0, 2.0, 3.0])
        self.assertEqual(d["series"]["Drug"], [4.0, 5.0, 6.0])

    def test_mixed_numeric_text_rows_skipped(self):
        """Non-numeric cells in numeric columns -> those rows dropped, rest parsed."""
        p = self._csv("label,value\nA,1\nB,NA\nC,3\n")
        d = gen_figure.load_data(p, chart_type="bar")
        self.assertEqual(d["labels"], ["A", "C"])
        self.assertEqual(d["series"]["value"], [1.0, 3.0])

    def test_tsv_delimiter(self):
        p = self._csv("label\tvalue\nA\t1\nB\t2\n", name="data.tsv")
        d = gen_figure.load_data(p, chart_type="bar")
        self.assertEqual(d["labels"], ["A", "B"])
        self.assertEqual(d["series"]["value"], [1.0, 2.0])

    def test_unsupported_format_raises(self):
        p = self._csv("a\n1\n", name="data.xlsx")
        with self.assertRaises(ValueError):
            gen_figure.load_data(p)

    def test_json_load(self):
        p = os.path.join(self.tmp, "d.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"labels": ["A"], "series": {"S": [1.0]}}, f)
        d = gen_figure.load_data(p)
        self.assertEqual(d["series"]["S"], [1.0])


# ── 4. has_cjk() ───────────────────────────────────────────────────────

class TestHasCJK(unittest.TestCase):
    def test_chinese_true(self):
        self.assertTrue(gen_figure.has_cjk("细胞因子"))
        self.assertTrue(gen_figure.has_cjk("IL-6 信号通路"))
        self.assertTrue(gen_figure.has_cjk("𠀀"))  # CJK Ext-B (U+20000) via \U00020000

    def test_ascii_false(self):
        self.assertFalse(gen_figure.has_cjk("IL-6 signaling"))
        self.assertFalse(gen_figure.has_cjk(""))

    def test_japanese_kanji_true(self):
        self.assertTrue(gen_figure.has_cjk("免疫応答"))


# ── 5. legend_audit() ──────────────────────────────────────────────────

class TestLegendAudit(unittest.TestCase):
    """Multi-series without labels must warn; exemptions respected."""

    def _ax(self):
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots()
        self.addCleanup(plt.close, fig)
        return ax

    def test_multi_series_no_labels_warns(self):
        ax = self._ax()
        ax.bar([0, 1], [1.0, 2.0])          # no label
        ax.bar([0.5, 1.5], [2.0, 1.0])      # no label
        msg = gen_figure.legend_audit(ax, "bar", no_legend=False, n_series=2)
        self.assertIsNotNone(msg)
        self.assertIn("图例", msg)

    def test_multi_series_with_labels_ok(self):
        ax = self._ax()
        ax.bar([0, 1], [1.0, 2.0], label="A")
        ax.bar([0.5, 1.5], [2.0, 1.0], label="B")
        self.assertIsNone(gen_figure.legend_audit(ax, "bar", no_legend=False, n_series=2))

    def test_no_legend_flag_skips(self):
        ax = self._ax()
        ax.bar([0, 1], [1.0, 2.0])
        self.assertIsNone(gen_figure.legend_audit(ax, "bar", no_legend=True, n_series=2))

    def test_single_series_ok(self):
        ax = self._ax()
        ax.bar([0, 1], [1.0, 2.0])
        self.assertIsNone(gen_figure.legend_audit(ax, "bar", no_legend=False, n_series=1))

    def test_exempt_types_ok(self):
        ax = self._ax()
        ax.bar([0, 1], [1.0, 2.0])
        ax.bar([0.5, 1.5], [2.0, 1.0])
        for ct in ("box", "boxplot", "violin", "heatmap", "forest",
                   "composite", "diagram", "dual_axis"):
            with self.subTest(chart_type=ct):
                self.assertIsNone(gen_figure.legend_audit(ax, ct, no_legend=False, n_series=2))


# ── 6. audit_pdf.py ────────────────────────────────────────────────────

class TestAuditPdf(unittest.TestCase):
    """Audit finds undersized spans; thresholds work."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="af_audit_")

    def _gen_pdf(self, journal=None):
        """Render a small bar PDF via the CLI; return its path."""
        data_path = os.path.join(self.tmp, "d.json")
        with open(data_path, "w", encoding="utf-8") as f:
            json.dump(SAMPLE["bar"], f)
        out = os.path.join(self.tmp, "fig.pdf")
        cmd = [sys.executable, GEN, "--type", "bar", "--data", data_path, "--out", out]
        if journal:
            cmd += ["--journal", journal, "--column", "double"]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return out

    def test_default_7pt_audit_clean(self):
        pdf = self._gen_pdf()
        offenders = audit_pdf.audit(pdf, min_size=7.0)
        self.assertEqual(offenders, [])

    def test_strict_threshold_finds_small_text(self):
        pdf = self._gen_pdf()
        offenders = audit_pdf.audit(pdf, min_size=12.0)
        self.assertGreater(len(offenders), 0)

    def test_journal_nature_6pt_clean(self):
        pdf = self._gen_pdf(journal="nature")
        offenders = audit_pdf.audit(pdf, min_size=6.0)
        self.assertEqual(offenders, [])


# ── 7. gen_legend.py ───────────────────────────────────────────────────

class TestGenLegend(unittest.TestCase):
    """Journal-format legend for every chart type."""

    def test_all_types_return_figure_text(self):
        for ct in SMOKE_TYPES:
            with self.subTest(chart_type=ct):
                txt = gen_legend.legend_for(SAMPLE[ct], ct, title="Main result")
                self.assertIn("Figure:", txt)
                self.assertNotIn("..", txt, f"{ct}: double period")
                self.assertTrue(txt.endswith("."))

    def test_km_log_rank_note(self):
        data = dict(SAMPLE["km"])
        data["log_rank"] = {"p": 0.03}
        txt = gen_legend.legend_for(data, "km", title="Survival")
        self.assertIn("Log-rank P = 0.03", txt)

    def test_forest_heterogeneity_note(self):
        data = dict(SAMPLE["forest"])
        data["heterogeneity"] = {"I2": 0.42}
        txt = gen_legend.legend_for(data, "forest", title="Meta")
        self.assertIn("I² = 42%", txt)

    def test_error_bars_note(self):
        data = dict(SAMPLE["bar"])
        data["errors"] = {"Ctrl": [0.1, 0.2, 0.1], "Treated": [0.2, 0.1, 0.2]}
        txt = gen_legend.legend_for(data, "bar", title="Bars")
        self.assertIn("Error bars indicate s.e.m.", txt)

    def test_composite_panel_letters(self):
        txt = gen_legend.legend_for(SAMPLE["composite"], "composite", title="Comp")
        self.assertIn("a, Panel A", txt)
        self.assertIn("b, Panel B", txt)

    def test_roc_auc_note(self):
        txt = gen_legend.legend_for(SAMPLE["roc"], "roc", title="ROC")
        self.assertIn("AUC values: 0.850", txt)


# ── v2.0.1: theme system, style presets, demo/explain, setup_env ───────

class TestV201ThemeSystem(unittest.TestCase):
    def test_default_theme_is_glm(self):
        self.assertEqual(gen_figure.resolve_theme(None), "glm")
        self.assertEqual(gen_figure.resolve_theme(""), "glm")

    def test_classic_alias_kept(self):
        self.assertEqual(gen_figure.resolve_theme("default"), "glm")
        self.assertEqual(gen_figure.resolve_theme("classic"), "classic")
        self.assertIn("classic", gen_figure.THEMES)

    def test_aliases_resolve(self):
        cases = {"okabe": "okabe-ito", "colorblind": "okabe-ito",
                 "glm-blog": "glm", "glmblog": "glm", "npg": "nature",
                 "matplotlib": "classic"}
        for alias, canonical in cases.items():
            with self.subTest(alias=alias):
                self.assertEqual(gen_figure.resolve_theme(alias), canonical)

    def test_case_insensitive_and_prefix(self):
        self.assertEqual(gen_figure.resolve_theme("OKABE"), "okabe-ito")
        self.assertEqual(gen_figure.resolve_theme("Nat"), "nature")
        self.assertEqual(gen_figure.resolve_theme("Conserv"), "conservative")

    def test_unknown_theme_none(self):
        self.assertIsNone(gen_figure.resolve_theme("nope"))

    def test_all_themes_have_colors(self):
        for key, theme in gen_figure.THEMES.items():
            with self.subTest(theme=key):
                self.assertTrue(theme["colors"])
                self.assertGreaterEqual(len(theme["colors"]), 2)


class TestV201StylePreset(unittest.TestCase):
    def _render(self, chart_type, extra=None):
        data_path = os.path.join(tempfile.gettempdir(), f"v201_{chart_type}.json")
        with open(data_path, "w", encoding="utf-8") as f:
            json.dump(SAMPLE[chart_type], f)
        out = os.path.join(tempfile.gettempdir(), f"v201_{chart_type}.png")
        cmd = [sys.executable, GEN, "-t", chart_type, "-d", data_path, "-o", out]
        if extra:
            cmd += extra
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        exists = os.path.exists(out)
        if exists:
            os.remove(out)
        os.remove(data_path)
        return r, exists

    def test_style_glm_hatch_bar(self):
        r, ok = self._render("bar", ["--style", "glm-hatch"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(ok)
        self.assertIn("colorblind-safe", r.stderr)

    def test_style_glm_hatch_stacked(self):
        r, ok = self._render("stacked_bar", ["--style", "glm-hatch"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(ok)

    def test_style_glm_hatch_forest(self):
        r, ok = self._render("forest", ["--style", "glm-hatch"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(ok)

    def test_hatch_flag_alone(self):
        r, ok = self._render("bar", ["--hatch"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(ok)

    def test_theme_glm_explicit(self):
        r, ok = self._render("bar", ["--theme", "glm"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(ok)

    def test_theme_alias_okabe(self):
        r, ok = self._render("bar", ["--theme", "okabe"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(ok)


class TestV201MetaCommands(unittest.TestCase):
    def test_list_themes_exits_0(self):
        r = subprocess.run([sys.executable, GEN, "--list-themes"],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0)
        self.assertIn("glm", r.stdout)

    def test_explain_bar(self):
        r = subprocess.run([sys.executable, GEN, "--explain", "bar"],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0)
        self.assertIn("CSV", r.stdout)

    def test_explain_unknown_exits_1(self):
        r = subprocess.run([sys.executable, GEN, "--explain", "nope"],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 1)

    def test_theme_swatch_renders(self):
        out = os.path.join(tempfile.gettempdir(), "v201_swatch.png")
        r = subprocess.run([sys.executable, GEN, "--theme-swatch", "glm", "-o", out],
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.exists(out))
        os.remove(out)

    def test_demo_menu_default(self):
        r = subprocess.run([sys.executable, GEN, "--demo"],
                           input="1\n", capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("已保存:", r.stderr)

    def test_missing_required_exits_2(self):
        r = subprocess.run([sys.executable, GEN], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 2)


class TestSetupEnv(unittest.TestCase):
    def test_setup_env_runs(self):
        r = subprocess.run([sys.executable, os.path.join(SCRIPT_DIR, "setup_env.py")],
                           capture_output=True, text=True, timeout=300)
        self.assertIn("环境自检", r.stdout)
        self.assertIn("环境就绪", r.stdout)


# ── Overlap mechanism regression: inverted axes must not nuke tick labels ──

class TestInvertedAxisTickPreservation(unittest.TestCase):
    """Regression: fix_tick_overlaps used to compute y-gaps as b[i+1].y0 -
    b[i].y1, which is always negative on inverted axes (heatmap/forest/km),
    so overlap was never cleared and labels got thinned to the first one.
    Also, hidden labels' boxes kept feeding the overlap check, so thinning
    never converged. Verify inverted-axis charts keep ALL tick labels."""

    def _render_and_count_labels(self, chart_type, data):
        fig, ax = plt.subplots(figsize=(9, 6))
        gen = gen_figure.GENERATORS[chart_type]
        gen(data, ax, gen_figure.THEMES["glm"], None)
        gen_figure.apply_base_style(ax, gen_figure.THEMES["glm"])
        plt.tight_layout()
        gen_figure.fix_tick_overlaps(fig)
        xl = [l for l in ax.get_xticklabels() if l.get_visible() and l.get_text().strip()]
        yl = [l for l in ax.get_yticklabels() if l.get_visible() and l.get_text().strip()]
        plt.close(fig)
        return xl, yl

    def test_heatmap_keeps_all_6_x_labels(self):
        data = {"rows": ["IL-6", "TNF-a", "CRP", "ESR", "DAS28", "SLEDAI"],
                "cols": ["IL-6", "TNF-a", "CRP", "ESR", "DAS28", "SLEDAI"],
                "matrix": [[1.0, 0.7, 0.8, 0.7, 0.8, 0.5],
                           [0.7, 1.0, 0.7, 0.6, 0.7, 0.3],
                           [0.8, 0.7, 1.0, 0.8, 0.8, 0.5],
                           [0.7, 0.6, 0.8, 1.0, 0.7, 0.4],
                           [0.8, 0.7, 0.8, 0.7, 1.0, 0.6],
                           [0.5, 0.3, 0.5, 0.4, 0.6, 1.0]]}
        xl, yl = self._render_and_count_labels("heatmap", data)
        self.assertEqual(len(xl), 6, f"heatmap x labels thinned: {[l.get_text() for l in xl]}")
        self.assertEqual(len(yl), 6)

    def test_forest_keeps_x_numeric_ticks(self):
        data = {"labels": ["S1", "S2", "S3", "S4"], "estimates": [0.72, 0.80, 0.75, 0.65],
                "ci_low": [0.55, 0.62, 0.56, 0.48], "ci_high": [0.94, 1.03, 1.00, 0.84],
                "overall": {"estimate": 0.73, "ci_low": 0.60, "ci_high": 0.88}}
        xl, yl = self._render_and_count_labels("forest", data)
        self.assertGreaterEqual(len(xl), 3, f"forest x ticks thinned: {[l.get_text() for l in xl]}")
        self.assertEqual(len(yl), 5)  # 4 studies + Overall

    def test_km_keeps_axis_labels(self):
        data = {"groups": {"A": [[0.0, 1.0], [5.0, 0.8]], "B": [[0.0, 1.0], [5.0, 0.9]]}}
        xl, yl = self._render_and_count_labels("km", data)
        self.assertGreaterEqual(len(xl), 2)
        self.assertGreaterEqual(len(yl), 2)


# ── Heatmap default colormap regression ────────────────────────────────

class TestHeatmapDefaultCmap(unittest.TestCase):
    """Regression: kwargs.get("cmap", "RdBu_r") never applied its default
    because main() always passes "cmap": args.cmap (None) — the key exists,
    so .get() returned None and imshow fell through to matplotlib's default
    viridis (yellow-green). The default must be RdBu_r unless overridden."""

    def test_default_cmap_is_rdbu_r(self):
        fig, ax = plt.subplots(figsize=(8, 6))
        data = {"rows": ["A", "B"], "cols": ["A", "B"],
                "matrix": [[1.0, -0.5], [-0.5, 1.0]]}
        kwargs = {"cmap": None}  # simulate main() always passing the key
        gen_figure.gen_heatmap(data, ax, gen_figure.THEMES["glm"], None, **kwargs)
        im = ax.images[0]
        self.assertEqual(im.get_cmap().name, "RdBu_r",
                         f"default cmap should be RdBu_r, got {im.get_cmap().name}")
        plt.close(fig)

    def test_explicit_cmap_still_overrides(self):
        fig, ax = plt.subplots(figsize=(8, 6))
        data = {"rows": ["A", "B"], "cols": ["A", "B"],
                "matrix": [[1.0, 0.5], [0.5, 1.0]]}
        kwargs = {"cmap": "YlOrRd"}
        gen_figure.gen_heatmap(data, ax, gen_figure.THEMES["glm"], None, **kwargs)
        im = ax.images[0]
        self.assertEqual(im.get_cmap().name, "YlOrRd")
        plt.close(fig)

    def test_all_positive_data_uses_warm_cmap_not_rdbu(self):
        """All-positive data must not render with RdBu_r — its white midpoint
        makes low positive values look like a broken band (断层). The default
        cmap switches to a warm sequential colormap for all-positive data."""
        fig, ax = plt.subplots(figsize=(8, 6))
        data = {"rows": ["A", "B"], "cols": ["A", "B"],
                "matrix": [[1.0, 0.5], [0.5, 0.3]]}
        kwargs = {"cmap": None}  # default path
        gen_figure.gen_heatmap(data, ax, gen_figure.THEMES["glm"], None, **kwargs)
        im = ax.images[0]
        self.assertEqual(im.get_cmap().name, "YlOrRd",
                         f"all-positive default should be YlOrRd, got {im.get_cmap().name}")
        plt.close(fig)

    def test_negative_data_keeps_diverging_rdbu(self):
        fig, ax = plt.subplots(figsize=(8, 6))
        data = {"rows": ["A", "B"], "cols": ["A", "B"],
                "matrix": [[1.0, -0.5], [-0.5, 1.0]]}
        kwargs = {"cmap": None}
        gen_figure.gen_heatmap(data, ax, gen_figure.THEMES["glm"], None, **kwargs)
        im = ax.images[0]
        self.assertEqual(im.get_cmap().name, "RdBu_r")
        plt.close(fig)


# ── Demo/example datasets must match the official data schemas ─────────

class TestDemoAndExampleSchemas(unittest.TestCase):
    """Every chart type reachable via --demo and every examples/*.json must
    pass validate_data() and render without raising. This catches schema
    drift between DEMO_DATA / example files and the generator functions
    (e.g. dual_axis used {"name","values"} objects instead of series dicts,
    km used object lists instead of {group: [[t,s],...]}, scatter/roc used
    unsupported formats — all crashed at render time)."""

    def _render(self, chart, data):
        fig, ax = plt.subplots(figsize=(8, 6))
        cjk_fp, _ = gen_figure.load_cjk_font()
        gen = gen_figure.GENERATORS[chart]
        extra = gen(data, ax, gen_figure.THEMES["glm"], cjk_fp)
        if chart not in ("dual_axis",) and extra not in ("composite", "diagram"):
            gen_figure.apply_base_style(ax, gen_figure.THEMES["glm"])
        plt.close(fig)

    def test_all_demo_datasets_valid_and_renderable(self):
        demo = gen_figure.DEMO_DATA
        self.assertTrue(demo, "DEMO_DATA must not be empty")
        for chart, data in demo.items():
            with self.subTest(demo_chart=chart):
                fatal, _warn = gen_figure.validate_data(data, chart)
                self.assertEqual(fatal, [], f"demo {chart} schema invalid: {fatal}")
                self._render(chart, data)

    def test_all_example_files_valid_and_renderable(self):
        examples_dir = os.path.join(os.path.dirname(__file__), "..", "examples")
        if not os.path.isdir(examples_dir):
            self.skipTest("no examples dir")
        canonical = {"dual": "dual_axis", "stacked": "stacked_bar",
                     "box": "box", "roc": "roc", "km": "km"}
        for fname in sorted(os.listdir(examples_dir)):
            if not fname.endswith(".json"):
                continue
            with self.subTest(example=fname):
                data = json.load(open(os.path.join(examples_dir, fname)))
                chart = fname.replace("example_", "").replace(".json", "")
                chart = canonical.get(chart, chart)
                if chart not in gen_figure.GENERATORS:
                    self.fail(f"example file {fname} maps to unknown chart '{chart}'")
                fatal, _warn = gen_figure.validate_data(data, chart)
                self.assertEqual(fatal, [], f"{fname} schema invalid: {fatal}")
                self._render(chart, data)


class TestV210Prisma(unittest.TestCase):
    """v2.1 PRISMA 2020 flow: validation gates + CLI rendering."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="af_prisma_")

    def _validate(self, data):
        return gen_figure.validate_data(data, "prisma")

    def test_valid_data_passes(self):
        fatal, _ = self._validate(SAMPLE["prisma"])
        self.assertEqual(fatal, [])

    def test_missing_required_fields(self):
        fatal, _ = self._validate({})
        self.assertTrue(any("records_identified" in m for m in fatal), fatal)
        self.assertTrue(any("studies_included" in m for m in fatal), fatal)

    def test_negative_or_bool_rejected(self):
        fatal, _ = self._validate({"records_identified": -5, "studies_included": True})
        self.assertTrue(any("records_identified" in m for m in fatal), fatal)
        self.assertTrue(any("studies_included" in m for m in fatal), fatal)

    def test_screened_arithmetic_gate(self):
        data = {"records_identified": 100, "duplicates_removed": 10,
                "records_screened": 95, "studies_included": 5}
        fatal, _ = self._validate(data)
        self.assertTrue(any("数字自洽" in m for m in fatal), fatal)

    def test_reasons_total_gate(self):
        data = {"records_identified": 100, "records_excluded": 10,
                "exclusion_reasons": {"a": 3, "b": 4}, "studies_included": 5}
        fatal, _ = self._validate(data)
        self.assertTrue(any("数字自洽" in m for m in fatal), fatal)

    def test_reasons_nonint_rejected(self):
        data = {"records_identified": 10, "studies_included": 2,
                "exclusion_reasons": {"a": "3"}}
        fatal, _ = self._validate(data)
        self.assertTrue(any("非负整数" in m for m in fatal), fatal)

    def test_lang_gate(self):
        fatal, _ = self._validate({"records_identified": 10, "studies_included": 2,
                                   "lang": "fr"})
        self.assertTrue(any("lang" in m for m in fatal))

    def test_cli_render_en(self):
        data_path = os.path.join(self.tmp, "prisma.json")
        with open(data_path, "w", encoding="utf-8") as f:
            json.dump(SAMPLE["prisma"], f)
        out = os.path.join(self.tmp, "prisma.png")
        proc = subprocess.run([sys.executable, GEN, "-t", "prisma", "-d", data_path,
                               "-o", out, "--dpi", "150"],
                              capture_output=True, text=True, timeout=180)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertGreater(os.path.getsize(out), 5000)

    def test_cli_render_zh(self):
        data = dict(SAMPLE["prisma"], lang="zh")
        data_path = os.path.join(self.tmp, "prisma_zh.json")
        with open(data_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        out = os.path.join(self.tmp, "prisma_zh.png")
        proc = subprocess.run([sys.executable, GEN, "-t", "prisma", "-d", data_path,
                               "-o", out, "--dpi", "150"],
                              capture_output=True, text=True, timeout=180)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertGreater(os.path.getsize(out), 5000)


class TestV210Suggest(unittest.TestCase):
    """v2.1 --suggest: heuristic chart-type recommendation."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="af_suggest_")

    def test_prisma_top(self):
        self.assertEqual(gen_figure.suggest_chart_type(SAMPLE["prisma"])[0][1], "prisma")

    def test_km_top(self):
        self.assertEqual(gen_figure.suggest_chart_type(SAMPLE["km"])[0][1], "km")

    def test_roc_top(self):
        self.assertEqual(gen_figure.suggest_chart_type(SAMPLE["roc"])[0][1], "roc")

    def test_forest_top(self):
        self.assertEqual(gen_figure.suggest_chart_type(SAMPLE["forest"])[0][1], "forest")

    def test_heatmap_top(self):
        self.assertEqual(gen_figure.suggest_chart_type(SAMPLE["heatmap"])[0][1], "heatmap")

    def test_scatter_top(self):
        self.assertEqual(gen_figure.suggest_chart_type(SAMPLE["scatter"])[0][1], "scatter")

    def test_bar_for_labels_series(self):
        recs = gen_figure.suggest_chart_type(SAMPLE["bar"])
        self.assertIn("bar", [r[1] for r in recs[:2]])

    def test_cli_suggest(self):
        data_path = os.path.join(self.tmp, "d.json")
        with open(data_path, "w", encoding="utf-8") as f:
            json.dump(SAMPLE["km"], f)
        proc = subprocess.run([sys.executable, GEN, "--suggest", "-d", data_path],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("km", proc.stdout)
        self.assertIn("Run:", proc.stdout)

    def test_cli_suggest_requires_data(self):
        proc = subprocess.run([sys.executable, GEN, "--suggest"],
                              capture_output=True, text=True, timeout=60)
        self.assertNotEqual(proc.returncode, 0)


class TestV220LayoutFixes(unittest.TestCase):
    """v2.2 staged fixes: no silent label loss, PRISMA side-box auto-fit.

    Background: the overlap ladder used to hide tick labels (stride-thinning)
    before ever trying 90° rotation, silently; and PRISMA side boxes had a
    fixed width so long exclusion reasons spilled outside the frame.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="af_v220_")

    def test_ladder_rotates_90_before_thinning(self):
        fig, ax = plt.subplots(figsize=(4, 3))
        ax.set_xticks(range(6))
        ax.set_xticklabels(["aaaaaaaa", "bbbbbbbb", "cccccccc",
                            "dddddddd", "eeeeeeee", "ffffffff"], fontsize=8)
        self.assertTrue(gen_figure._degrade_axis(ax, 'x', 4))
        labs = [l for l in ax.get_xticklabels() if l.get_text().strip()]
        self.assertTrue(all(l.get_rotation() == 90 for l in labs))
        self.assertTrue(all(l.get_visible() for l in labs),
                        "90° rotation stage must not hide labels")
        plt.close(fig)

    def test_thinning_only_at_level5_and_reports(self):
        fig, ax = plt.subplots(figsize=(4, 3))
        ax.set_xticks(range(6))
        ax.set_xticklabels(["aaaaaaaa"] * 6, fontsize=8)
        self.assertTrue(gen_figure._degrade_axis(ax, 'x', 5))
        vis = [l.get_visible() for l in ax.get_xticklabels() if l.get_text().strip()]
        self.assertEqual(sum(vis), 3)  # stride 2 keeps every 2nd label
        plt.close(fig)

    def test_fix_tick_overlaps_warns_when_labels_hidden(self):
        fig, ax = plt.subplots(figsize=(2.0, 2.0))
        ax.set_xticks(range(12))
        ax.set_xticklabels([f"category {i} long label" for i in range(12)], fontsize=8)
        ax.set_yticks(range(12))
        ax.set_yticklabels([f"row {i}" for i in range(12)], fontsize=8)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            gen_figure.fix_tick_overlaps(fig)
        hidden = [l for l in ax.get_xticklabels() + ax.get_yticklabels()
                  if l.get_text().strip() and not l.get_visible()]
        if hidden:
            self.assertIn("WARNING", err.getvalue())
            self.assertIn("hid", err.getvalue())
        plt.close(fig)

    def test_prisma_long_reasons_stay_inside_side_box(self):
        fig, ax = plt.subplots(figsize=(10, 8))
        data = {"records_identified": 156, "duplicates_removed": 102,
                "records_excluded": 40,
                "exclusion_reasons": {
                    "Did not meet the predefined inclusion criteria": 3,
                    "No extractable quantitative outcome data": 3},
                "studies_included": 8}
        gen_figure.gen_prisma(data, ax, gen_figure.THEMES["glm"], None)
        fig.canvas.draw()
        ren = fig.canvas.get_renderer()
        side = [p for p in ax.patches
                if tuple(round(v, 2) for v in p.get_facecolor()[:3]) == (0.95, 0.95, 0.95)]
        self.assertTrue(side, "side boxes not found")
        right = max(p.get_window_extent(ren).x1 for p in side)
        left = min(p.get_window_extent(ren).x0 for p in side)
        items = [t for t in ax.texts if t.get_text().strip().startswith("·")]
        self.assertEqual(len(items), 2)
        for t in items:
            bb = t.get_window_extent(ren)
            self.assertGreaterEqual(bb.x0, left - 1.0,
                                    f"reason text spills left: {t.get_text()!r}")
            self.assertLessEqual(bb.x1, right + 1.0,
                                 f"reason text spills right: {t.get_text()!r}")
        plt.close(fig)


class TestV220Features(unittest.TestCase):
    """v2.2: journal presets 9, xlsx input, --stats auto, --alt, templates."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="af_v220_")

    def test_journal_presets_complete(self):
        need = {"nature", "lancet", "science", "cell", "nejm", "jama",
                "ieee", "cma", "cn-core"}
        self.assertTrue(need.issubset(gen_figure.JOURNAL_PRESETS.keys()),
                        f"missing: {need - set(gen_figure.JOURNAL_PRESETS)}")
        for cn in ("cma", "cn-core"):
            self.assertTrue(gen_figure.JOURNAL_PRESETS[cn].get("cjk_default"),
                            f"{cn} must auto-enable CJK")
            self.assertIn("widths_mm", gen_figure.JOURNAL_PRESETS[cn])

    def test_load_xlsx_wide_format(self):
        import openpyxl
        p = os.path.join(self.tmp, "wb.xlsx")
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["Group", "Treatment", "Control"])
        ws.append(["Wk12", 62, 28])
        ws.append(["Wk24", 71, 32])
        wb.save(p)
        d = gen_figure.load_data(p, chart_type="bar")
        self.assertEqual(d["labels"], ["Wk12", "Wk24"])
        self.assertEqual(d["series"]["Treatment"], [62.0, 71.0])
        self.assertEqual(d["series"]["Control"], [28.0, 32.0])

    def test_load_xlsx_bad_sheet(self):
        import openpyxl
        p = os.path.join(self.tmp, "wb2.xlsx")
        wb = openpyxl.Workbook()
        wb.active.append(["Group", "V"])
        wb.active.append(["A", 1])
        wb.save(p)
        with self.assertRaises(ValueError) as cm:
            gen_figure.load_data(p, chart_type="bar", sheet="Nope")
        self.assertIn("Nope", str(cm.exception))

    def test_pairwise_vs_first_significant_and_ns(self):
        rng = np.random.default_rng(7)
        lo = list(rng.normal(10.0, 1.0, 40))
        hi = list(rng.normal(14.0, 1.0, 40))
        same = list(rng.normal(10.0, 1.0, 40))
        res = gen_figure.pairwise_vs_first({"Ctl": lo, "Hi": hi, "Same": same})
        by = {r[0]: r for r in res}
        self.assertIn(by["Hi"][2], ("*", "**", "***"))
        self.assertEqual(by["Same"][2], "NS")

    def test_gen_box_stats_auto_draws_brackets(self):
        data = {"labels": ["A", "B"],
                "series": {"A": [1, 2, 3, 4, 5, 6], "B": [4, 5, 6, 7, 8, 9]}}
        fig, ax = plt.subplots(figsize=(6, 4))
        gen_figure.GENERATORS["box"](data, ax, gen_figure.THEMES["glm"],
                                     None, stats_auto=True)
        star_texts = [t.get_text() for t in ax.texts
                      if t.get_text() in ("*", "**", "***", "NS", "n.s.")]
        self.assertTrue(star_texts, "bracket star label missing")
        plt.close(fig)

    def test_generate_alt_text_bar_and_km(self):
        bar = {"labels": ["A", "B"], "series": {"T": [1, 2]}}
        txt = gen_figure.generate_alt_text("bar", bar, "疗效对比")
        self.assertIn("柱状图", txt)
        self.assertIn("疗效对比", txt)
        km = {"groups": {"T": [[1, 1]], "C": [[2, 0]]},
              "log_rank": {"p": 0.03, "method": "Log-rank"}}
        txt2 = gen_figure.generate_alt_text("km", km)
        self.assertIn("Kaplan-Meier", txt2)
        self.assertIn("0.03", txt2)

    def test_cli_alt_sidecar(self):
        data_path = os.path.join(self.tmp, "km.json")
        with open(data_path, "w", encoding="utf-8") as f:
            json.dump({"groups": {"T": [[10, 1], [20, 0]], "C": [[8, 1], [15, 1]]},
                       "log_rank": {"p": 0.2, "method": "Log-rank"}}, f)
        out = os.path.join(self.tmp, "km.png")
        proc = subprocess.run([sys.executable, GEN, "-t", "km", "-d", data_path,
                               "-o", out, "--alt"], capture_output=True,
                              text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        side = os.path.join(self.tmp, "km.alt.txt")
        self.assertTrue(os.path.exists(side), "alt sidecar missing")
        content = open(side, encoding="utf-8").read()
        self.assertIn("Kaplan-Meier", content)

    def test_cli_xlsx_input(self):
        import openpyxl
        p = os.path.join(self.tmp, "data.xlsx")
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["Group", "S1", "S2"])
        ws.append(["A", 10, 12])
        ws.append(["B", 20, 18])
        wb.save(p)
        out = os.path.join(self.tmp, "bar.png")
        proc = subprocess.run([sys.executable, GEN, "-t", "bar", "-d", p,
                               "-o", out], capture_output=True, text=True,
                              timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(os.path.exists(out))

    def test_cli_stats_auto_violin(self):
        data_path = os.path.join(self.tmp, "v.json")
        with open(data_path, "w", encoding="utf-8") as f:
            json.dump({"labels": ["A", "B"],
                       "series": {"A": [1, 2, 3, 4, 5], "B": [5, 6, 7, 8, 9]}}, f)
        out = os.path.join(self.tmp, "v.png")
        proc = subprocess.run([sys.executable, GEN, "-t", "violin", "-d", data_path,
                               "-o", out, "--stats", "auto"], capture_output=True,
                              text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("stats (violin)", proc.stderr)

    def test_desc_card_conversion(self):
        """模块2.1 卡片转化锁：desc 首行=需求词逐字覆盖、品牌词不进首 60 字符。"""
        import re as _re
        root = os.path.normpath(os.path.join(SCRIPT_DIR, ".."))
        for fname, demand_words in (
                ("SKILL_ZH.md", ["论文配图", "学术图表", "科研绘图", "期刊配图"]),
                ("SKILL.md", ["scientific figures", "chart types", "Publication"])):
            with open(os.path.join(root, fname), encoding="utf-8") as f:
                txt = f.read()
            m = _re.search(r"description: >-\n((?:  .*\n)+?)(?=\w)", txt)
            self.assertIsNotNone(m, f"{fname} description 块未找到")
            d = " ".join(l.strip() for l in m.group(1).splitlines())
            self.assertLessEqual(len(d), 1024, f"{fname} desc 超 1024 字符（SH 静默丢弃红线）")
            first_line = d.split("：")[0] if fname.endswith("ZH.md") else d.split(" — ")[0]
            self.assertTrue(d.startswith(first_line[:12]))
            for w in demand_words:
                self.assertIn(w, d[:120], f"{fname} 需求词 {w!r} 不在前 120 字符")
            for brand in ("docsor", "github.com"):
                self.assertNotIn(brand, d[:60], f"{fname} 品牌词 {brand!r} 混进首 60 字符")

    def test_negative_triggers_in_desc(self):
        """模块3.4 正负触发器：负触发器在 desc 内（置换式，非净追加）。"""
        root = os.path.normpath(os.path.join(SCRIPT_DIR, ".."))
        for fname, marker in (("SKILL_ZH.md", "不适用"), ("SKILL.md", "Skip when")):
            with open(os.path.join(root, fname), encoding="utf-8") as f:
                txt = f.read()
            m = re.search(r"description: >-\n((?:  .*\n)+?)(?=\w)", txt)
            self.assertIsNotNone(m, fname)
            d = " ".join(l.strip() for l in m.group(1).splitlines())
            self.assertIn(marker, d, f"{fname} desc 缺负触发器标记 {marker!r}")

    def test_deep_doc_entrances(self):
        """模块3.2 深文档 30 秒入口：每个 references/*.md 有「何时读我」块。"""
        root = os.path.normpath(os.path.join(SCRIPT_DIR, ".."))
        rdir = os.path.join(root, "references")
        md_files = [f for f in sorted(os.listdir(rdir)) if f.endswith(".md")]
        self.assertGreaterEqual(len(md_files), 14)
        for f in md_files:
            with open(os.path.join(rdir, f), encoding="utf-8") as fh:
                head = fh.read(600)
            self.assertIn("何时读我", head, f"{f} 缺 30 秒入口块")

    def test_capability_boundary_table(self):
        """模块3.1 能力×边界成对表：主文件有表+四条评测点名边界行。"""
        root = os.path.normpath(os.path.join(SCRIPT_DIR, ".."))
        for fname in ("SKILL.md", "SKILL_ZH.md"):
            with open(os.path.join(root, fname), encoding="utf-8") as f:
                txt = f.read()
            self.assertIn("Capabilities & Boundaries" if fname.endswith(".md") and not fname.endswith("ZH.md")
                          else "能力与边界", txt, fname)
            for needle in ("--verify", "--journal"):
                self.assertIn(needle, txt, f"{fname} 缺边界要素 {needle!r}")

    def test_antimode_alternatives(self):
        """模块3.3 反模式带替代+防重复规则。"""
        root = os.path.normpath(os.path.join(SCRIPT_DIR, ".."))
        with open(os.path.join(root, "references", "pitfalls.md"), encoding="utf-8") as f:
            txt = f.read()
        self.assertIn("反模式 → ✅ 替代", txt)
        self.assertIn("不要重复烧钱", txt)
        self.assertIn("改变一个输入", txt)


    def test_templates_present_and_valid(self):
        tdir = os.path.join(SCRIPT_DIR, "..", "templates")
        jsons = sorted(glob.glob(os.path.join(tdir, "*.json")))
        self.assertEqual(len(jsons), 39, f"expected 39 templates (+rob v6.1), got {len(jsons)}")
        for p in jsons:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
            self.assertIn("_chart_type", d, p)
            self.assertIn("_command", d, p)
        self.assertTrue(os.path.exists(os.path.join(tdir, "README.md")))

    def test_cli_journal_cma_preset(self):
        data_path = os.path.join(self.tmp, "b.json")
        with open(data_path, "w", encoding="utf-8") as f:
            json.dump({"labels": ["组一", "组二"], "series": {"甲": [3, 4]}}, f)
        out = os.path.join(self.tmp, "cma.pdf")
        proc = subprocess.run([sys.executable, GEN, "-t", "bar", "-d", data_path,
                               "-o", out, "--journal", "cma", "--cjk"],
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("期刊预设 cma", proc.stderr)



class TestV230Features(unittest.TestCase):
    """v2.3 统计深水区：新图型、KM 自动风险表、multi/DeLong、batch/caption。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="af_v23_")
        self.gen = os.path.join(SCRIPT_DIR, "gen_figure.py")

    def _render(self, chart, data_obj, extra_args=None, name=None):
        data_path = os.path.join(self.tmp, (name or chart) + ".json")
        with open(data_path, "w", encoding="utf-8") as f:
            json.dump(data_obj, f, ensure_ascii=False)
        out = os.path.join(self.tmp, (name or chart) + ".png")
        proc = subprocess.run(
            [sys.executable, self.gen, "-t", chart, "-d", data_path, "-o", out]
            + (extra_args or []),
            capture_output=True, text=True, timeout=180)
        return proc, out

    # ── 新图型冒烟 ──

    FUNNEL = {"studies": [
        {"name": "A 2020", "effect": 0.69, "se": 0.12},
        {"name": "B 2021", "effect": 0.55, "se": 0.20},
        {"name": "C 2022", "effect": 0.80, "se": 0.09},
        {"name": "D 2023", "effect": 0.42, "se": 0.31},
        {"name": "E 2024", "effect": 0.61, "se": 0.15},
        {"name": "F 2025", "effect": 0.73, "se": 0.11}]}

    def test_smoke_funnel_with_egger(self):
        proc, out = self._render("funnel", self.FUNNEL, ["--egger"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(os.path.exists(out))
        self.assertIn("Egger", proc.stderr)

    def test_smoke_bland_altman(self):
        proc, out = self._render("bland_altman", {
            "methods": {"新仪器": [5.1, 4.9, 5.3, 5.6, 4.8, 5.2],
                        "金标准": [5.0, 5.1, 5.2, 5.4, 5.0, 5.1]}})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(os.path.exists(out))

    def test_smoke_pca_groups(self):
        proc, out = self._render("pca", {
            "matrix": [[5.1, 3.5, 1.4, 0.2], [4.9, 3.0, 1.4, 0.2], [6.7, 3.1, 4.4, 1.4],
                       [6.0, 2.9, 4.5, 1.5], [6.3, 3.3, 6.0, 2.5], [5.8, 2.7, 5.1, 1.9]],
            "groups": ["a", "a", "b", "b", "c", "c"],
            "feature_names": ["萼长", "萼宽", "瓣长", "瓣宽"]})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(os.path.exists(out))
        self.assertIn("PC1+PC2", proc.stderr)

    def test_smoke_paired_stats(self):
        proc, out = self._render("paired", {
            "series": {"术前": [8.2, 7.5, 9.1, 6.8, 7.9, 8.5],
                       "术后": [6.1, 5.8, 7.2, 5.0, 6.3, 6.9]}}, ["--stats", "auto"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("p=", proc.stderr)

    def test_smoke_venn(self):
        proc, out = self._render("venn", {"sets": {
            "A": ["x", "y", "z", "w"], "B": ["x", "z", "p"], "C": ["x", "q"]}})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(os.path.exists(out))

    def test_smoke_cluster_heatmap(self):
        proc, out = self._render("cluster_heatmap", {
            "matrix": [[2.1, 0.3, 1.8, 0.1], [1.9, 0.2, 2.0, 0.3],
                       [0.2, 2.2, 0.1, 1.9], [0.3, 2.0, 0.4, 2.1]],
            "row_labels": ["s1", "s2", "s3", "s4"],
            "col_labels": ["gA", "gB", "gC", "gD"]})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(os.path.exists(out))
        self.assertIn("聚类顺序", proc.stderr)

    # ── 新图型拒绝（付费前/渲染前拦截）──

    def test_funnel_rejects_missing_se(self):
        bad = {"studies": [{"name": "A", "effect": 0.5}, {"name": "B", "effect": 0.6}]}
        proc, _ = self._render("funnel", bad)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("se", proc.stderr)

    def test_venn_rejects_one_set(self):
        proc, _ = self._render("venn", {"sets": {"A": [1, 2, 3]}})
        self.assertNotEqual(proc.returncode, 0)

    def test_pca_rejects_nan(self):
        proc, _ = self._render("pca", {
            "matrix": [[1.0, 2.0], [2.0, None], [3.0, 4.0], [4.0, 1.0]]})
        self.assertNotEqual(proc.returncode, 0)

    def test_cluster_rejects_nan(self):
        proc, _ = self._render("cluster_heatmap", {
            "matrix": [[1.0, 2.0], [2.0, None], [3.0, 4.0]]})
        self.assertNotEqual(proc.returncode, 0)

    # ── KM 自动风险表 + 自动 log-rank ──

    KM_DATA = {"groups": {
        "对照组": [[1, 1], [2, 1], [3, 1], [4, 0], [5, 1], [6, 1], [7, 1], [8, 1]],
        "治疗组": [[3, 1], [5, 1], [8, 0], [9, 1], [11, 1], [12, 1], [14, 1], [16, 1]]}}

    def test_km_auto_logrank_and_risk_table(self):
        proc, out = self._render("km", dict(self.KM_DATA))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("log-rank", proc.stderr)
        self.assertTrue(os.path.exists(out))

    def test_km_no_risk_table_flag(self):
        proc, out = self._render("km", dict(self.KM_DATA), ["--no-risk-table"],
                                 name="km_nort")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(os.path.exists(out))

    def test_km_risk_times_custom(self):
        proc, out = self._render("km", dict(self.KM_DATA), ["--risk-times", "2,6,10"],
                                 name="km_rt")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_km_precomputed_warns_no_auto(self):
        data = {"time": [1, 2, 3], "survival": {"A": [1.0, 0.5, 0.5], "B": [1.0, 0.9, 0.6]}}
        proc, out = self._render("km", data, name="km_pre")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("原始数据", proc.stderr)

    # ── 统计模块金标准/恒等式 ──

    def test_delong_auc_identity_mwu(self):
        import af_v23_stats as st
        rng = np.random.default_rng(7)
        y = np.array([0] * 40 + [1] * 30)
        s1 = rng.normal(size=70) + y * 0.8
        aucs, _ = st.delong_paired(y, {"m1": s1})
        from scipy import stats as sst
        u = sst.mannwhitneyu(s1[y == 1], s1[y == 0], alternative="greater").statistic
        self.assertAlmostEqual(aucs["m1"], u / (30 * 40), places=10)

    def test_delong_identical_scores_p_is_one(self):
        import af_v23_stats as st
        rng = np.random.default_rng(3)
        y = np.array([0] * 25 + [1] * 25)
        s = rng.normal(size=50) + y
        _, pairs = st.delong_paired(y, {"a": s, "b": s.copy()})
        self.assertAlmostEqual(pairs[0][2], 1.0, places=12)

    def test_delong_separation_small_p(self):
        import af_v23_stats as st
        rng = np.random.default_rng(11)
        y = np.array([0] * 40 + [1] * 40)
        weak = rng.normal(size=80) + y * 0.3
        strong = weak + y * 1.2
        _, pairs = st.delong_paired(y, {"weak": weak, "strong": strong})
        self.assertLess(pairs[0][2], 0.05)

    def test_logrank_separated_groups_significant(self):
        import af_v23_stats as st
        g = {"早发": ([1, 2, 3, 4, 5], [1, 1, 1, 1, 1]),
             "晚发": ([9, 10, 11, 12, 13], [1, 1, 1, 1, 1])}
        chi2, p, df = st.logrank_test(g)
        self.assertEqual(df, 1)
        self.assertLess(p, 0.05)

    def test_km_at_risk_hand_example(self):
        import af_v23_stats as st
        times = [1, 2, 3, 4, 5, 6, 7]
        events = [1, 0, 1, 1, 0, 1, 1]
        self.assertEqual(st.km_at_risk(times, events, [0, 3, 6]), [7, 5, 2])

    def test_km_estimate_survival_values(self):
        import af_v23_stats as st
        t_seq, s_seq, cens = st.km_estimate([1, 2, 2, 3], [1, 1, 0, 1])
        self.assertEqual(s_seq[0], 1.0)
        # t=1: 1/4 事件→0.75；t=2: 1/3 事件→0.5（另一例删失）；t=3: 1/1 事件→0
        self.assertAlmostEqual(s_seq[1], 0.75, places=12)
        self.assertAlmostEqual(s_seq[2], 0.5, places=12)
        self.assertAlmostEqual(s_seq[-1], 0.0, places=12)
        self.assertEqual(len(cens), 1)

    def test_hochberg_hand_example(self):
        import af_v23_stats as st
        adj = st.hochberg([0.01, 0.03, 0.04])
        # 期望值（手工计算）：p_(3)=0.04*3=0.12, p_(2)=min(0.12, 0.03*2=0.06)=0.06,
        # p_(1)=min(0.06, 0.01*1)=0.01
        self.assertAlmostEqual(adj[0], 0.01, places=12)
        self.assertAlmostEqual(adj[1], 0.06, places=12)
        self.assertAlmostEqual(adj[2], 0.12, places=12)

    def test_dl_pool_no_heterogeneity_tau_zero(self):
        import af_v23_stats as st
        # 构造完全一致的真效应：es_i = theta + se_i * z_i，取 z_i 全 0
        res = st.dl_pool([0.7, 0.7, 0.7], [0.1, 0.2, 0.3])
        self.assertAlmostEqual(res["pooled"], 0.7, places=12)
        self.assertAlmostEqual(res["tau2"], 0.0, places=12)
        self.assertAlmostEqual(res["i2"], 0.0, places=9)

    def test_tukey_identical_means_p_one(self):
        import af_v23_stats as st
        pairs = st.tukey_pairs({"a": [1.0, 1.0, 1.0, 1.0],
                                "b": [1.0, 1.0, 1.0, 1.0],
                                "c": [1.0, 1.0, 1.0, 1.0]})
        for (_, _, p, _, _) in pairs:
            self.assertAlmostEqual(p, 1.0, places=9)

    def test_dunn_matches_scikit_posthocs(self):
        try:
            import scikit_posthocs as sp  # noqa: F401
        except ImportError:
            self.skipTest("scikit-posthocs 未安装（可选对拍依赖）")
        import af_v23_stats as st
        rng = np.random.default_rng(42)
        series = {"a": rng.normal(0, 1, 30).tolist(),
                  "b": rng.normal(0.5, 1, 25).tolist(),
                  "c": rng.normal(1.0, 1, 28).tolist()}
        ours = {(a, b): p for a, b, p, _, _ in st.dunn_pairs(series)}
        df_long = []
        for name, vals in series.items():
            for v in vals:
                df_long.append((name, v))
        import pandas as _pd
        dfx = _pd.DataFrame(df_long, columns=["g", "v"])
        ref = sp.posthoc_dunn(dfx, val_col="v", group_col="g", p_adjust=None)
        names = list(series.keys())
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                self.assertAlmostEqual(ours[(names[i], names[j])],
                                       float(ref.iloc[i, j]), places=6)

    def test_multi_pairwise_routes_normal_to_tukey(self):
        import af_v23_stats as st
        rng = np.random.default_rng(5)
        series = {g: rng.normal(loc, 0.25, 60).tolist()
                  for g, loc in (("甲", 0.0), ("乙", 0.0), ("丙", 1.0))}
        pairs = st.multi_pairwise(series)
        methods = {m for _, _, _, _, m in pairs}
        self.assertEqual(methods, {"Tukey HSD"})
        by_pair = {(a, b): p for a, b, p, _, _ in pairs}
        self.assertLess(by_pair[("甲", "丙")], 0.05)
        self.assertGreater(by_pair[("甲", "乙")], 0.05)

    # ── CLI：--stats multi / --compare / batch / caption / 兜底 ──

    def test_cli_stats_multi_brackets(self):
        rng = np.random.default_rng(9)
        data = {"series": {"对照": rng.normal(0, 0.3, 40).tolist(),
                           "低剂": rng.normal(0.1, 0.3, 38).tolist(),
                           "高剂": rng.normal(1.2, 0.3, 42).tolist()}}
        proc, out = self._render("box", data, ["--stats", "multi"], name="box_multi")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        combined = proc.stderr
        self.assertTrue(("Tukey" in combined) or ("Dunn" in combined))

    def test_cli_roc_compare_delong(self):
        rng = np.random.default_rng(21)
        n_pos, n_neg = 30, 40
        y = [1] * n_pos + [0] * n_neg
        base = rng.normal(0, 1, n_pos + n_neg)
        s1 = (base + [v * 0.6 for v in y]).tolist()
        s2 = (base + [v * 1.4 for v in y]).tolist()
        data = {"labels": y, "curves": [
            {"name": "模型弱", "scores": s1},
            {"name": "模型强", "scores": s2}]}
        proc, _ = self._render("roc", data, ["--compare"], name="roc_cmp")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("DeLong", proc.stderr)

    def test_cli_batch_two_figures(self):
        d1 = os.path.join(self.tmp, "b1.json")
        d2 = os.path.join(self.tmp, "b2.json")
        with open(d1, "w", encoding="utf-8") as f:
            json.dump({"labels": ["甲", "乙"], "series": {"S": [1, 2]}}, f)
        with open(d2, "w", encoding="utf-8") as f:
            json.dump({"a": [1, 2, 3, 4], "b": [2, 3, 2, 5]}, f)
        o1 = os.path.join(self.tmp, "batch1.png")
        o2 = os.path.join(self.tmp, "batch2.png")
        manifest = os.path.join(self.tmp, "figures.json")
        with open(manifest, "w", encoding="utf-8") as f:
            json.dump([{"type": "bar", "data": d1, "out": o1},
                       {"type": "bland_altman", "data": d2, "out": o2}], f)
        proc = subprocess.run([sys.executable, self.gen, "--batch", manifest],
                              capture_output=True, text=True, timeout=300)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(os.path.exists(o1) and os.path.exists(o2))
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "figures.batch-report.json")))

    def test_cli_caption_bilingual(self):
        data = {"series": {"甲": [1, 2, 3, 4], "乙": [2, 3, 4, 5]},
                "labels": ["甲", "乙"]}
        data_path = os.path.join(self.tmp, "cap.json")
        with open(data_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        out = os.path.join(self.tmp, "cap.png")
        proc = subprocess.run([sys.executable, self.gen, "-t", "box", "-d", data_path,
                               "-o", out, "--caption", "--title", "疗效对比"],
                              capture_output=True, text=True, timeout=180)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        cap_path = os.path.join(self.tmp, "cap.caption.txt")
        self.assertTrue(os.path.exists(cap_path))
        with open(cap_path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("箱线图", content)
        self.assertIn("Fig.", content)
        self.assertIn("疗效对比", content)

    def test_friendly_error_no_traceback(self):
        bad_path = os.path.join(self.tmp, "bad.json")
        with open(bad_path, "w", encoding="utf-8") as f:
            f.write("this is not {{{ valid json")
        proc = subprocess.run([sys.executable, self.gen, "-t", "bar", "-d", bad_path,
                               "-o", os.path.join(self.tmp, "x.png")],
                              capture_output=True, text=True, timeout=120)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("JSON 语法错误", proc.stderr)  # A2 v3：中文诊断+针对性建议
        self.assertIn("建议", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

    def test_stderr_chinese_gate(self):
        """中文门禁：成功+报错路径 stderr 不得出现纯英文句子（≥2 连续英文词且无中文）。"""
        import re
        data_path = os.path.join(self.tmp, "g.json")
        with open(data_path, "w", encoding="utf-8") as f:
            json.dump({"labels": ["甲", "乙"], "series": {"S1": [1, 2, 3, 4]}}, f, ensure_ascii=False)
        out = os.path.join(self.tmp, "g.png")
        procs = [subprocess.run([sys.executable, self.gen, "-t", "bar", "-d", data_path,
                                 "-o", out, "--cjk"], capture_output=True, text=True, timeout=120)]
        bad_path = os.path.join(self.tmp, "g_bad.json")
        with open(bad_path, "w", encoding="utf-8") as f:
            json.dump({"x": [1], "y": [2]}, f)
        procs.append(subprocess.run([sys.executable, self.gen, "-t", "scatter", "-d", bad_path,
                                     "-o", os.path.join(self.tmp, "g2.png")],
                                    capture_output=True, text=True, timeout=120))
        for proc in procs:
            for line in proc.stderr.splitlines():
                if re.search(r"[A-Za-z]{3,}[ ,][A-Za-z]{3,}", line) and not re.search(r"[一-鿿]", line):
                    self.fail(f"stderr 英文残留: {line.strip()[:100]}")


    def test_heatmap_labels_never_thinned(self):
        """回归锁：热图家族行列标签是数据——50 行 USArrests 渲染后 50 个 y 标签全在。"""
        import gen_figure as gf
        data = {"matrix": [[float(i) + j * 0.1 for j in range(4)] for i in range(50)],
                "row_labels": [f"R{i:02d}" for i in range(50)],
                "col_labels": ["a", "b", "c", "d"]}
        fig, ax = plt.subplots(figsize=(7, 9))
        gf.gen_heatmap(data, ax, gf.THEMES["glm"], None)
        gf.fix_tick_overlaps(fig)
        self.assertEqual(len(ax.get_yticklabels()), 50,
                         "热图 y 标签被防重叠机制剥除——类别轴必须豁免")

    def test_cluster_heatmap_50rows_all_labels(self):
        import gen_figure as gf
        data = {"matrix": [[float(i % 7) + j for j in range(3)] for i in range(50)],
                "row_labels": [f"S{i:02d}" for i in range(50)],
                "col_labels": ["x", "y", "z"]}
        import af_v23_charts
        fig, ax = plt.subplots(figsize=(7, 9))
        af_v23_charts.gen_cluster_heatmap(data, ax, gf.THEMES["glm"], None)
        gf.fix_tick_overlaps(fig)
        self.assertEqual(len(ax.get_yticklabels()), 50)


    def test_pca_dense_labels_zero_overlap(self):
        """回归锁（图9 缺陷重演）：32 车型密集 PCA 的样本标签渲染后零重叠。"""
        import gen_figure as gf
        import af_v23_charts
        rng = np.random.default_rng(1974)
        data = {"matrix": rng.normal(0, 1, (32, 5)).tolist() + [[rng.uniform(-2, 2) for _ in range(5)] for _ in range(2)],
                "row_labels": [f"Hornet Sportabout {i}" if i % 7 == 0 else f"Car Model {i}" for i in range(32)],
                "feature_names": ["f1", "f2", "f3", "f4", "f5"]}
        data["matrix"] = data["matrix"][:32]
        data["row_labels"] = data["row_labels"][:32]
        fig, ax = plt.subplots(figsize=(7, 5))
        af_v23_charts.gen_pca(data, ax, gf.THEMES["glm"], None)
        gf.fix_tick_overlaps(fig)
        fig.canvas.draw()
        renderer = fig.canvas.get_renderer()
        bbs = [t.get_window_extent(renderer) for t in ax.texts]
        n_over = sum(1 for i in range(len(bbs)) for j in range(i + 1, len(bbs))
                     if bbs[i].overlaps(bbs[j]))
        self.assertEqual(n_over, 0, f"PCA 标签仍有 {n_over} 处重叠（declutter/thin 未生效）")
        # 对应关系锁：每个锚点（数据点）附近必须有标签，标签不得漂离锚点过远
        sx = float(np.ptp(np.array(data["matrix"])[:, 0:2].sum(axis=1)))  # 任意跨度参考
        span = 12.0  # 标准化数据 PC 轴跨度参考（宽松下限）
        max_disp = 0.15 * span
        pts = np.array(data["matrix"])[:, :2]
        for pos_text in ax.texts:
            if not pos_text.get_text().strip():
                continue  # 载荷箭头等空对象已由 FancyArrowPatch 消除
            tx, ty = pos_text.get_position()
            near = np.min(np.hypot(pts[:, 0] - tx, pts[:, 1] - ty)) if len(pts) else 0
            self.assertLessEqual(near, max_disp,
                                 f"标签 '{pos_text.get_text()}' 漂离所有数据点 {near:.2f} > {max_disp}")


    def test_cjk_font_enforcement_mechanism(self):
        """回归锁（豆腐块根治）：忘传 fontproperties 的中文文本，保存路径统一兜底。"""
        import gen_figure as gf
        fig, ax = plt.subplots(figsize=(6, 4))
        ax.text(0.5, 0.6, "对照组：无字体属性")   # 故意不传 fontproperties
        ax.text(0.5, 0.3, "plain english only")  # 非中文不受影响
        cjk_fp, _ = gf.load_cjk_font(None)
        if cjk_fp is None:
            self.skipTest("无 CJK 字体环境")
        # 走与 main 相同的兜底逻辑
        import matplotlib.text as mtext
        fixed = 0
        for t_obj in fig.findobj(mtext.Text):
            if t_obj.get_text() and gf.has_cjk(t_obj.get_text()):
                t_obj.set_fontproperties(cjk_fp)
                fixed += 1
        self.assertGreaterEqual(fixed, 1, "兜底未识别到含中文文本")
        # 断言中文 Text 的字体族已指向 CJK 字体
        for t_obj in fig.findobj(mtext.Text):
            if gf.has_cjk(t_obj.get_text()):
                fam = t_obj.get_fontfamily()
                self.assertTrue(fam, "中文文本字体族为空")


    def test_v240_median_survival_lifelines_identity(self):
        """v2.4 锁：KM 中位生存与 lifelines 全等（20 组随机删失数据集）。"""
        import af_v23_stats as st
        try:
            from lifelines import KaplanMeierFitter
        except ImportError:
            self.skipTest("lifelines 未安装（对拍参考）")
        rng = np.random.default_rng(2024)
        for _ in range(20):
            n = int(rng.integers(30, 150))
            t = np.minimum(rng.exponential(1/0.1, n), rng.uniform(5, 40)).round(1)
            e = (rng.exponential(1/0.1, n) <= t).astype(int)
            kmf = KaplanMeierFitter()
            kmf.fit(t, e)
            rm = kmf.median_survival_time_
            om, _, _ = st.km_median_survival(t, e)
            if np.isnan(rm):
                self.assertIsNone(om)
            else:
                self.assertIsNotNone(om)
                self.assertAlmostEqual(rm, om, places=9)

    def test_v240_median_survival_freireich(self):
        import af_v23_stats as st
        pl_t = [1,1,2,2,3,4,4,5,5,8,8,8,8,11,11,12,12,15,17,22,23]
        pl_e = [1]*21
        m, lo, hi = st.km_median_survival(pl_t, pl_e)
        self.assertEqual(m, 8.0)  # 文献标准值
        self.assertIsNotNone(lo)
        mp_t = [6,6,6,7,9,10,10,11,13,16,17,19,20,22,23,25,32,32,34,35]
        mp_e = [1,1,1,1,1,1,1,0,0,0,0,0,0,0,0,0,0,0,0,0,0]
        m2, _, _ = st.km_median_survival(mp_t, mp_e)
        self.assertIsNone(m2)  # 曲线未降到 0.5：不可估计

    def test_v240_leave_one_out_identity(self):
        import af_v23_stats as st
        rng = np.random.default_rng(5)
        es = rng.normal(0.5, 0.2, 10).tolist()
        ses = np.linspace(0.08, 0.3, 10).tolist()
        loo = st.leave_one_out(es, ses)
        self.assertEqual(len(loo), 10)
        for i, pooled, lo, hi in loo:
            sub_e = [es[k] for k in range(10) if k != i]
            sub_s = [ses[k] for k in range(10) if k != i]
            r = st.dl_pool(sub_e, sub_s)
            self.assertAlmostEqual(pooled, r["pooled"], places=12)
            self.assertAlmostEqual(lo, r["pooled"] - 1.96*r["se"], places=12)
        with self.assertRaises(ValueError):
            st.leave_one_out([0.5, 0.6], [0.1, 0.1])  # <4 研究

    def test_v240_cli_km_median_annotation(self):
        data = {"groups": {"对照": [[1,1],[2,1],[3,1],[4,0],[5,1],[6,1],[7,1],[8,1]],
                            "治疗": [[3,1],[5,1],[8,0],[9,1],[11,1],[12,1],[14,1],[16,1]]}}
        proc, out = self._render("km", data, name="km_med")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("自动中位生存", proc.stderr)

    def test_v240_cli_forest_sensitivity(self):
        data = {"labels": [f"S{i}" for i in range(8)],
                "estimates": [0.5, 0.7, 0.4, 0.6, 0.55, 0.65, 0.45, 0.6],
                "se_list": [0.1, 0.2, 0.12, 0.15, 0.18, 0.14, 0.22, 0.16],
                "ci_low": [0.3, 0.3, 0.16, 0.3, 0.2, 0.37, 0.02, 0.29],
                "ci_high": [0.7, 1.1, 0.64, 0.9, 0.9, 0.93, 0.88, 0.91]}
        proc, out = self._render("forest", data, ["--sensitivity"], name="forest_sens")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("留一法", proc.stderr)

    def test_risk_times_bad_arg_rejected(self):
        data_path = os.path.join(self.tmp, "rt.json")
        with open(data_path, "w", encoding="utf-8") as f:
            json.dump({"groups": {"A": [[1, 1], [2, 1], [3, 1], [4, 1]]}}, f)
        proc = subprocess.run([sys.executable, self.gen, "-t", "km", "-d", data_path,
                               "-o", os.path.join(self.tmp, "rt.png"), "--risk-times", "abc,x"],
                              capture_output=True, text=True, timeout=120)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("无法解析为数字", proc.stderr)


    def test_alt_for_new_charts(self):
        proc, out = self._render("funnel", self.FUNNEL, ["--alt"], name="funnel_alt")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        alt_path = os.path.splitext(out)[0] + ".alt.txt"
        self.assertTrue(os.path.exists(alt_path))
        with open(alt_path, encoding="utf-8") as f:
            self.assertIn("漏斗图", f.read())


class TestV250SubmissionPolish(unittest.TestCase):
    """v2.5.0 投稿精修：--annotate / 期刊配色主题 / venn --area / 图例控制。"""

    def _run(self, chart_type, data, extra=(), name="v250"):
        data_path = os.path.join(tempfile.gettempdir(), f"v250_{name}.json")
        with open(data_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        out = os.path.join(tempfile.gettempdir(), f"v250_{name}.png")
        if os.path.exists(out):
            os.remove(out)
        cmd = [sys.executable, GEN, "-t", chart_type, "-d", data_path, "-o", out,
               "--dpi", "110"] + list(extra)
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        return proc, out

    # ── 期刊配色主题 ──

    def test_v250_theme_nejm_science_registered(self):
        import gen_figure as gf
        self.assertEqual(gf.resolve_theme("nejm"), "nejm")
        self.assertEqual(gf.resolve_theme("new-england"), "nejm")
        self.assertEqual(gf.resolve_theme("science"), "science")
        self.assertEqual(gf.resolve_theme("aaas"), "science")
        for t in ("nejm", "science"):
            self.assertIn(t, gf.THEMES)
            self.assertIn(t, gf.THEME_ORDER)
            self.assertIn(t, gf.THEME_SWATCH_DESCRIPTIONS)
        self.assertEqual(len(gf.THEMES["nejm"]["colors"]), 8)
        self.assertEqual(len(gf.THEMES["science"]["colors"]), 10)

    def test_v250_theme_science_smoke(self):
        proc, out = self._run("bar", SAMPLE["bar"], ["--theme", "science"], "sci")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(os.path.exists(out))

    def test_v250_journal_theme_linkage(self):
        # --journal nejm 且未显式给 --theme → 自动联动 nejm 配色
        proc, _ = self._run("scatter", SAMPLE["scatter"], ["--journal", "nejm"], "link")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("配色已联动 nejm", proc.stderr)
        # 显式 --theme 优先，不联动
        proc2, _ = self._run("scatter", SAMPLE["scatter"],
                             ["--journal", "nejm", "--theme", "glm"], "link2")
        self.assertEqual(proc2.returncode, 0, proc2.stderr)
        self.assertNotIn("配色已联动", proc2.stderr)

    # ── --annotate ──

    def test_v250_annotate_cli_scatter(self):
        data = {"x": [1, 2, 3, 4, 5], "y": [2, 4, 3.5, 5, 4.8],
                "groups": ["a", "a", "b", "b", "b"]}
        proc, out = self._run("scatter", data,
                              ["--annotate", "3,3.5:拐点", "--annotate", "5,4.8:峰值",
                               "--cjk"], "ann")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(os.path.exists(out))
        self.assertNotIn("ERROR", proc.stderr)

    def test_v250_annotate_bad_syntax_rejected(self):
        data = SAMPLE["scatter"]
        proc, _ = self._run("scatter", data, ["--annotate", "3,3.5"], "ann_bad1")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("annotate", proc.stderr)
        self.assertIn("语法", proc.stderr)
        proc2, _ = self._run("scatter", data, ["--annotate", "3,3.5:"], "ann_bad2")
        self.assertNotEqual(proc2.returncode, 0)
        self.assertIn("文字不能为空", proc2.stderr)

    def test_v250_annotate_category_axis_bar(self):
        data = {"labels": ["对照组", "低剂量", "高剂量"], "series": {"表达量": [1.0, 2.5, 4.2]}}
        proc, out = self._run("bar", data,
                              ["--annotate", "高剂量,4.2:显著上调", "--cjk"], "ann_bar")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(os.path.exists(out))
        # 未知道类别 → 中文报错
        proc2, _ = self._run("bar", data, ["--annotate", "不存在组,1:高", "--cjk"], "ann_bar2")
        self.assertNotEqual(proc2.returncode, 0)
        self.assertIn("无法定位坐标", proc2.stderr)

    def test_v250_annotate_inverted_axis_forest(self):
        """回归锁（B 电池抓出的 bug）：forest 的 y 轴倒置（get_ylim 返回 大→小），
        合法坐标曾被升序假设的范围检查误判越界 → 修复后必须通过。"""
        data = {"labels": ["S1", "S2"], "estimates": [1.2, 0.8],
                "ci_low": [0.9, 0.5], "ci_high": [1.5, 1.1]}
        proc, out = self._run("forest", data, ["--annotate", "1.2,0:S1 效应量"], "ann_for")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(os.path.exists(out))

    def test_v250_annotate_zero_overlap_and_arrows_follow(self):
        """回归锁（审稿注释）：同点三注释 declutter 后零重叠，箭头终点锚回数据点。"""
        import gen_figure as gf
        fig, ax = plt.subplots(figsize=(8, 5.5))
        data = {"x": [1, 2, 3, 4], "y": [3, 3, 3, 3]}
        gen = gf.GENERATORS["scatter"]
        gen(data, ax, gf.THEMES["glm"], None)
        anns = gf._parse_annotations(["2.5,3:注释甲", "2.5,3:注释乙", "2.5,3:注释丙"])
        gf._apply_annotations(fig, anns, gf.THEMES["glm"], None)
        gf.fix_tick_overlaps(fig)
        fig.canvas.draw()
        renderer = fig.canvas.get_renderer()
        bbs = [t.get_window_extent(renderer) for t in ax.texts if t.get_text()]
        n_over = sum(1 for i in range(len(bbs)) for j in range(i + 1, len(bbs))
                     if bbs[i].overlaps(bbs[j]))
        self.assertEqual(n_over, 0, "注释文字仍有重叠")
        arrows = [p for p in ax.patches if type(p).__name__ == "FancyArrowPatch"]
        self.assertEqual(len(arrows), 3, "应有 3 条注释箭头")
        for arr in arrows:
            if hasattr(arr, "get_positions"):
                _pa, pb = arr.get_positions()
            else:
                _pa, pb = arr._posA_posB
            # 注释坐标 (2.5, 3)：箭头终点必须精确锚定在请求的坐标上
            self.assertAlmostEqual(pb[0], 2.5, places=9, msg="箭头终点 x 未锚定")
            self.assertAlmostEqual(pb[1], 3.0, places=9, msg="箭头终点 y 未锚定")

    # ── 图例控制 ──

    def test_v250_legend_loc_applied(self):
        import gen_figure as gf
        fig, ax = plt.subplots(figsize=(8, 5))
        data = {"labels": ["t0", "t1", "t2"], "series": {"S1": [1, 2, 3], "S2": [3, 2.5, 2]}}
        gen = gf.GENERATORS["line"]
        gen(data, ax, gf.THEMES["glm"], None)
        ax.legend()
        gf._apply_legend_control(fig, loc="lower right", theme=gf.THEMES["glm"])
        leg = ax.get_legend()
        self.assertIsNotNone(leg)
        self.assertEqual(int(leg._loc), 4)  # lower right 的 loc 代码

    def test_v250_legend_loc_invalid_rejected(self):
        proc, _ = self._run("line", SAMPLE["line"], ["--legend-loc", "top-left"], "legbad")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("未知图例位置", proc.stderr)

    def test_v250_legend_outside_composite_shared(self):
        """回归锁：组合图 --legend-outside 合并为全图共享图例（去重）。"""
        import gen_figure as gf
        fig, ax = plt.subplots(figsize=(10, 4))
        data = {"layout": [1, 2], "panels": [
            {"type": "bar", "data": {"labels": ["A", "B"], "series": {"S1": [1, 2], "S2": [2, 1]}},
             "title": "Panel A", "pos": [0, 0]},
            {"type": "line", "data": {"labels": ["t0", "t1"], "series": {"S1": [1, 2], "S2": [2, 1]}},
             "title": "Panel B", "pos": [0, 1]}]}
        gen = gf.GENERATORS["composite"]
        extra = gen(data, ax, gf.THEMES["glm"], None)
        self.assertEqual(extra, "composite")
        panel_legs = [a for a in fig.axes if a.get_legend() is not None]
        self.assertGreaterEqual(len(panel_legs), 2)
        gf._apply_legend_control(fig, loc=None, outside=True, theme=gf.THEMES["glm"])
        self.assertEqual(len(list(fig.legends)), 1, "应有唯一全图共享图例")
        remaining = [a for a in fig.axes if a.get_legend() is not None]
        self.assertEqual(len(remaining), 0, "面板图例应已移除")
        labels = [t.get_text() for t in fig.legends[0].get_texts()]
        self.assertEqual(sorted(labels), ["S1", "S2"], "共享图例应去重为 S1/S2")

    # ── venn --area（Euler）──

    def test_v250_venn2_area_geometry_exact(self):
        import math
        import gen_figure as gf
        import af_v23_charts as ac
        n1, n2, inter = 40, 60, 30
        centers, radii, _notes = ac._venn2_geometry(n1, n2, inter)
        d = math.hypot(centers[0][0] - centers[1][0], centers[0][1] - centers[1][1])
        a1 = math.pi * radii[0] ** 2
        a2 = math.pi * radii[1] ** 2
        ov = ac._circle_overlap_area(d, radii[0], radii[1])
        # 缩放不破坏比例：三个比值必须全等
        self.assertAlmostEqual(a1 / (n1 + inter), a2 / (n2 + inter), places=12)
        self.assertAlmostEqual(ov / inter, a1 / (n1 + inter), places=12)
        # 子集：内切 d = r_out - r_in
        c2, r2, notes2 = ac._venn2_geometry(0, 60, 30)
        self.assertAlmostEqual(abs(c2[0][0] - c2[1][0]), abs(r2[0] - r2[1]), places=9)
        # 不相交：d >= r1 + r2
        c3, r3, _ = ac._venn2_geometry(50, 50, 0)
        d3 = math.hypot(c3[0][0] - c3[1][0], c3[0][1] - c3[1][1])
        self.assertGreaterEqual(d3, r3[0] + r3[1] - 1e-9)
        # 完全相同：同心 + 说明
        c4, _r4, notes4 = ac._venn2_geometry(0, 0, 50)
        self.assertAlmostEqual(c4[0][0], c4[1][0], places=9)
        self.assertTrue(any("相同" in s for s in notes4))

    def test_v250_venn3_area_fit(self):
        import af_v23_charts as ac
        regions = {"A": 100, "B": 80, "C": 60, "AB": 30, "AC": 20, "BC": 15, "ABC": 8}
        _c, _r, fit = ac._venn3_geometry(regions, ("A", "B", "C"))
        self.assertLessEqual(fit["max_rel_err"], 0.15,
                             f"常规三集合拟合偏差过大: {fit['max_rel_err']:.3f}")
        # 三集合互不相交 → 应近乎精确
        regions0 = {"A": 30, "B": 30, "C": 30, "AB": 0, "AC": 0, "BC": 0, "ABC": 0}
        _c0, _r0, fit0 = ac._venn3_geometry(regions0, ("A", "B", "C"))
        self.assertLessEqual(fit0["max_rel_err"], 0.02)

    def test_v250_venn_area_cli_and_regions_format(self):
        # regions 计数格式（v2.3 的数字 sets 从未可用，v2.5 修复）
        data3 = {"regions": {"A": 100, "B": 80, "C": 60, "AB": 30, "AC": 20,
                             "BC": 15, "ABC": 8}}
        proc, out = self._run("venn", data3, ["--area"], "v3area")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(os.path.exists(out))
        self.assertIn("面积", proc.stderr)
        # 元素列表 + --area
        data2 = {"sets": {"A": list(range(40)) + [f"x{i}" for i in range(30)],
                          "B": list(range(100, 160)) + [f"x{i}" for i in range(30)]}}
        proc2, out2 = self._run("venn", data2, ["--area"], "v2area")
        self.assertEqual(proc2.returncode, 0, proc2.stderr)
        self.assertTrue(os.path.exists(out2))
        # 非法 regions 键名 → 中文报错
        bad = {"regions": {"X": 1, "Y": 2, "Z": 3}}
        proc3, _ = self._run("venn", bad, [], "vbad")
        self.assertNotEqual(proc3.returncode, 0)
        self.assertIn("regions", proc3.stderr)
        # sets + regions 同时给 → 报错（validate 层提示两种格式的正确用法）
        proc4, _ = self._run("venn", {"sets": {"A": [1], "B": [2]},
                                      "regions": {"A": 1, "B": 1, "AB": 0}}, [], "vboth")
        self.assertNotEqual(proc4.returncode, 0)
        self.assertIn("venn", proc4.stderr)

    def test_v250_area_flag_venn_only(self):
        proc, _ = self._run("bar", SAMPLE["bar"], ["--area"], "areabad")
        self.assertEqual(proc.returncode, 0)  # v3.6：降级为 [ignored] 告知，非致命
        self.assertIn("仅用于 venn", proc.stderr)

    # ── 异常映射扩充 ──

    def test_v250_exc_map_expanded(self):
        import gen_figure as gf
        try:
            import pandas as pd
        except ImportError:
            pd = None
        cases = [(MemoryError(), "内存"), (ZeroDivisionError(), "除零"),
                 (AttributeError(), "结构")]
        if pd is not None:
            cases.append((pd.errors.ParserError("bad csv"), "解析"))
        try:
            from openpyxl.utils.exceptions import InvalidFileException
            cases.append((InvalidFileException("bad xlsx"), "xlsx"))
        except ImportError:
            pass
        self.assertGreaterEqual(len(cases), 3, "核心异常映射用例不足")
        for exc, keyword in cases:
            msg = gf._zh_exception(exc)
            self.assertIn(keyword, msg, f"{type(exc).__name__} 映射缺中文释义")
            self.assertIn(type(exc).__name__, msg, "应保留英文类名便于搜索")


class TestV260Features(unittest.TestCase):
    """v2.6.0：km 长表陷阱拦截（A1）+ 校验消息中文化（A2）+ multi-format/pipeline（C1/C2）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="af_v26_")
        self.gen = os.path.join(SCRIPT_DIR, "gen_figure.py")

    def test_a1_km_longtable_series_fatal(self):
        # CSV 长表被通用转换装进 series：{time:[...], event:[...]} —— 必须 fatal（曾静默出废图）
        long_table = {"labels": ["12", "24", "36"],
                      "series": {"time": [12.0, 24.0, 36.0],
                                 "event": [1.0, 0.0, 1.0]}}
        f, _ = gen_figure.validate_data(long_table, "km")
        self.assertTrue(any("长表" in m and "groups" in m for m in f),
                        "长表应被拦截并给转换指引，实际 fatal=%r" % (f,))

    def test_a1_km_flat_groups_fatal(self):
        # groups 值是普通数值数组（不是 [t,e] 对）→ fatal 且给格式示例
        f, _ = gen_figure.validate_data({"groups": {"A": [1.0, 2.0, 3.0]}}, "km")
        self.assertTrue(any("成对" in m for m in f))

    def test_a1_km_pairs_still_pass(self):
        data = {"groups": {"A": [[1, 1], [2, 0], [3, 1]],
                           "B": [[1, 0], [2, 0], [4, 1]]}}
        f, _ = gen_figure.validate_data(data, "km")
        self.assertEqual(f, [], "合法 [t,e] 对不应报错：%r" % (f,))

    def test_a1_km_csv_end_to_end_fatal(self):
        # 原始问题输入重演：CSV 长表文件 → load_data → validate_data 必须 fatal
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "km_long.csv")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write("time,event,group\n12,1,A\n24,0,A\n36,1,B\n")
            data = gen_figure.load_data(p, chart_type="km")
            f, _ = gen_figure.validate_data(data, "km")
            self.assertTrue(any("长表" in m for m in f),
                            "CSV 长表应 fatal，实际 load=%r fatal=%r" % (data, f))

    def test_a2_chinese_messages(self):
        # 曾经的纯英文消息路径，现在必须含中文关键词
        _, w_flat = gen_figure.validate_data(
            {"labels": [1, 2, 3], "series": {"A": [5, 5, 5]}}, "bar")
        self.assertTrue(any("全部相同" in m for m in w_flat))
        _, w_nn = gen_figure.validate_data(
            {"labels": [1, 2], "series": {"A": ["NA", 2]}}, "bar")
        self.assertTrue(any("非数值" in m for m in w_nn))
        f_type, _ = gen_figure.validate_data({"series": "not-a-dict"}, "bar")
        self.assertTrue(any("JSON 对象" in m for m in f_type))
        f_empty, _ = gen_figure.validate_data({"series": {}, "labels": [1]}, "bar")
        self.assertTrue(any("空" in m for m in f_empty))
        f_box, _ = gen_figure.validate_data(
            {"labels": ["A", "B", "C"], "series": {"A": [1.0], "B": [2.0]}}, "box")
        self.assertTrue(any("组名" in m for m in f_box))

    def test_a2_no_english_leftovers(self):
        # prisma 两处残留英文必须清除
        f, _ = gen_figure.validate_data(
            {"records_identified": 100, "studies_included": 5,
             "exclusion_reasons": {"x": -1}}, "prisma")
        self.assertTrue(any("非负整数" in m for m in f))
        self.assertFalse(any("integers, got" in m for m in f))
        f2, _ = gen_figure.validate_data(
            {"records_identified": 100, "records_screened": 90,
             "reports_assessed": 50, "studies_included": 5,
             "exclusion_reasons": {"a": 20, "b": 30}}, "prisma")
        self.assertFalse(any("flow does not add up" in m for m in f2))
        self.assertTrue(any("排除人数" in m for m in f2))


    # ── C1: --multi-format ──

    def test_c1_multi_format_cli(self):
        d = os.path.join(self.tmp, "mf.json")
        with open(d, "w", encoding="utf-8") as f:
            json.dump({"labels": ["甲", "乙"], "series": {"S": [1, 2]}}, f)
        out = os.path.join(self.tmp, "mf1")
        proc = subprocess.run(
            [sys.executable, self.gen, "-t", "bar", "-d", d, "-o", out,
             "--multi-format", "png,pdf", "--dpi", "150"],
            capture_output=True, text=True, timeout=180)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(os.path.exists(out + ".png"), proc.stderr)
        self.assertTrue(os.path.exists(out + ".pdf"), proc.stderr)
        self.assertIn(".pdf", proc.stderr)

    def test_c1_multi_format_bad_value(self):
        d = os.path.join(self.tmp, "mf2.json")
        with open(d, "w", encoding="utf-8") as f:
            json.dump({"labels": ["甲", "乙"], "series": {"S": [1, 2]}}, f)
        proc = subprocess.run(
            [sys.executable, self.gen, "-t", "bar", "-d", d,
             "-o", os.path.join(self.tmp, "mf2"), "--multi-format", "bmp"],
            capture_output=True, text=True, timeout=60)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("不支持", proc.stderr)

    # ── C2: --pipeline ──

    def test_c2_pipeline_json_defaults(self):
        d1 = os.path.join(self.tmp, "p_bar.json")
        d2 = os.path.join(self.tmp, "p_ba.json")
        with open(d1, "w", encoding="utf-8") as f:
            json.dump({"labels": ["甲", "乙"], "series": {"S": [1, 2]}}, f)
        with open(d2, "w", encoding="utf-8") as f:
            json.dump({"a": [1, 2, 3, 4], "b": [2, 3, 2, 5]}, f)
        pipe = os.path.join(self.tmp, "analysis.json")
        with open(pipe, "w", encoding="utf-8") as f:
            json.dump({"defaults": {"dpi": 150},
                       "figures": [
                           {"type": "bar", "data": "p_bar.json", "out": "fig_p_bar"},
                           {"type": "bland_altman", "data": d2, "out": "fig_p_ba"}]}, f)
        proc = subprocess.run([sys.executable, self.gen, "--pipeline", pipe],
                              capture_output=True, text=True, timeout=300)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # 相对路径 data 相对流水线文件所在目录解析；out 落在同目录
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "fig_p_bar.png")), proc.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "fig_p_ba.png")), proc.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "analysis.batch-report.json")))

    def test_c2_pipeline_yaml(self):
        try:
            import yaml  # noqa: F401
        except ImportError:
            self.skipTest("pyyaml 未安装")
        d1 = os.path.join(self.tmp, "p_bar.json")
        with open(d1, "w", encoding="utf-8") as f:
            json.dump({"labels": ["甲", "乙"], "series": {"S": [1, 2]}}, f)
        pipe = os.path.join(self.tmp, "analysis.yaml")
        with open(pipe, "w", encoding="utf-8") as f:
            f.write("defaults:\n  dpi: 150\nfigures:\n"
                    "  - type: bar\n    data: p_bar.json\n    out: fig_yaml2\n")
        proc = subprocess.run([sys.executable, self.gen, "--pipeline", pipe],
                              capture_output=True, text=True, timeout=180)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "fig_yaml2.png")), proc.stderr)

    def test_c2_pipeline_bad_ext_and_conflict(self):
        p = os.path.join(self.tmp, "pipe.txt")
        with open(p, "w", encoding="utf-8") as f:
            f.write("x")
        proc = subprocess.run([sys.executable, self.gen, "--pipeline", p],
                              capture_output=True, text=True, timeout=60)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(".yaml", proc.stderr)
        d = os.path.join(self.tmp, "c.json")
        with open(d, "w", encoding="utf-8") as f:
            json.dump({"labels": ["甲"], "series": {"S": [1]}}, f)
        proc2 = subprocess.run(
            [sys.executable, self.gen, "--batch", d, "--pipeline", d],
            capture_output=True, text=True, timeout=60)
        self.assertNotEqual(proc2.returncode, 0)
        self.assertIn("二选一", proc2.stderr)

    # ── 审核修复：forest 缺省无效线按 measure 自动 ──

    def test_v260_forest_refline_auto_ratio(self):
        # 自然尺度 OR/HR（全正、measure 默认 OR）→ 缺省无效线必须 1.0（旧默认 0 会误导显著性判读）
        import matplotlib
        import matplotlib.pyplot as plt
        theme = gen_figure.THEMES[gen_figure.resolve_theme("glm")]
        data = {"labels": ["全部", "亚组"], "estimates": [0.69, 0.81],
                "ci_low": [0.54, 0.59], "ci_high": [0.89, 1.12]}
        fig, ax = plt.subplots()
        gen_figure.gen_forest(data, ax, theme, None)
        verts = [float(ln.get_xdata()[0]) for ln in ax.lines
                 if len(ln.get_xdata()) >= 2
                 and ln.get_xdata()[0] == ln.get_xdata()[-1]]
        self.assertIn(1.0, verts, "自然尺度 OR 缺省无效线应为 1.0")
        plt.close(fig)

    def test_v260_forest_refline_auto_difference(self):
        import matplotlib
        import matplotlib.pyplot as plt
        theme = gen_figure.THEMES[gen_figure.resolve_theme("glm")]
        data = {"labels": ["A", "B"], "estimates": [-0.3, 0.2],
                "ci_low": [-0.6, -0.1], "ci_high": [0.0, 0.5], "measure": "SMD"}
        fig, ax = plt.subplots()
        gen_figure.gen_forest(data, ax, theme, None)
        verts = [float(ln.get_xdata()[0]) for ln in ax.lines
                 if len(ln.get_xdata()) >= 2
                 and ln.get_xdata()[0] == ln.get_xdata()[-1]]
        self.assertIn(0.0, verts, "MD/SMD 缺省无效线应为 0.0")
        plt.close(fig)

    def test_v260_forest_refline_explicit_wins(self):
        import matplotlib
        import matplotlib.pyplot as plt
        theme = gen_figure.THEMES[gen_figure.resolve_theme("glm")]
        data = {"labels": ["A", "B"], "estimates": [0.69, 0.81],
                "ci_low": [0.54, 0.59], "ci_high": [0.89, 1.12], "ref_line": 0}
        fig, ax = plt.subplots()
        gen_figure.gen_forest(data, ax, theme, None)
        verts = [float(ln.get_xdata()[0]) for ln in ax.lines
                 if len(ln.get_xdata()) >= 2
                 and ln.get_xdata()[0] == ln.get_xdata()[-1]]
        self.assertIn(0.0, verts, "显式 ref_line=0 应优先于自动")
        self.assertNotIn(1.0, verts)
        plt.close(fig)


# ── B3/B2(v2.7) 回归锁 ──
class TestB3PracticalFixes(unittest.TestCase):
    def test_composite_hbar_panel(self):
        """B3：composite 内 hbar 面板必须自动横排（实战坑回归锁）。"""
        data = {"layout": [1, 2], "panels": [
            {"title": "A", "type": "bar", "pos": [0, 0],
             "data": {"labels": ["A", "B"], "series": {"C": [1.0, 2.0]}}},
            {"title": "B", "type": "hbar", "pos": [0, 1],
             "data": {"labels": ["X", "Y"], "series": {"S": [1.5, 2.5]}}},
        ]}
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "comp.json")
            json.dump(data, open(p, "w", encoding="utf-8"), ensure_ascii=False)
            r = subprocess.run([sys.executable, GEN, "-t", "composite", "--data", p,
                                "-o", os.path.join(td, "out"), "--dpi", "80"],
                               capture_output=True, text=True, timeout=120)
            self.assertEqual(r.returncode, 0, r.stderr[-400:])
            self.assertTrue(os.path.exists(os.path.join(td, "out.png")))

    def test_roc_duplicate_labels_disambiguated(self):
        """B3：两条 AUC 相同的 ROC 曲线 → 图例标签自动消歧并 stderr 告知。"""
        xs = [0.0, 0.5, 1.0]
        data = {"curves": [
            {"name": "模型", "fpr": xs, "tpr": [0.0, 0.7, 1.0], "auc": 0.85},
            {"name": "模型", "fpr": xs, "tpr": [0.0, 0.7, 1.0], "auc": 0.85},
        ]}
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "roc.json")
            json.dump(data, open(p, "w", encoding="utf-8"), ensure_ascii=False)
            r = subprocess.run([sys.executable, GEN, "-t", "roc", "--data", p,
                                "-o", os.path.join(td, "out"), "--dpi", "80"],
                               capture_output=True, text=True, timeout=120)
            self.assertEqual(r.returncode, 0, r.stderr[-400:])
            self.assertIn("已自动消歧", r.stderr)

    def test_pca_feature_names_length_notice(self):
        """B3：pca feature_names 长度不符 → 自动替换并 stderr 提示（原为静默）。"""
        data = {"matrix": [[1.0, 2.0, 3.0, 4.0], [2.0, 1.0, 4.0, 3.0],
                           [3.0, 4.0, 1.0, 2.0], [4.0, 3.0, 2.0, 1.0]],
                "feature_names": ["只有", "两个"]}
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "pca.json")
            json.dump(data, open(p, "w", encoding="utf-8"), ensure_ascii=False)
            r = subprocess.run([sys.executable, GEN, "-t", "pca", "--data", p,
                                "-o", os.path.join(td, "out"), "--dpi", "80"],
                               capture_output=True, text=True, timeout=120)
            self.assertEqual(r.returncode, 0, r.stderr[-400:])
            self.assertIn("已自动改用默认名", r.stderr)

    def test_size_presets(self):
        """B2：--size 16:9 与 9:16 渲染成功且 figsize 提示出现。"""
        data = {"labels": ["A", "B"], "series": {"S": [1.0, 2.0]}}
        for size in ("16:9", "9:16"):
            with tempfile.TemporaryDirectory() as td:
                p = os.path.join(td, "d.json")
                json.dump(data, open(p, "w", encoding="utf-8"), ensure_ascii=False)
                r = subprocess.run([sys.executable, GEN, "-t", "bar", "--data", p,
                                    "-o", os.path.join(td, "out"), "--dpi", "80",
                                    "--size", size],
                                   capture_output=True, text=True, timeout=120)
                self.assertEqual(r.returncode, 0, r.stderr[-400:])
                self.assertIn(f"画幅预设 {size}", r.stderr)


# ── A3(v2.7) 稳定性回归锁 ──
class TestA3Stability(unittest.TestCase):
    def test_deterministic_output(self):
        """A3：同输入两次渲染 → PNG 逐字节一致（确定性输出）。"""
        data = {"labels": ["甲", "乙", "丙"], "series": {"组1": [1.0, 2.0, 3.0], "组2": [2.0, 1.0, 2.5]}}
        blobs = []
        for _ in range(2):
            with tempfile.TemporaryDirectory() as td:
                p = os.path.join(td, "d.json")
                json.dump(data, open(p, "w", encoding="utf-8"), ensure_ascii=False)
                r = subprocess.run([sys.executable, GEN, "-t", "bar", "--data", p,
                                    "-o", os.path.join(td, "out"), "--dpi", "100"],
                                   capture_output=True, text=True, timeout=120)
                self.assertEqual(r.returncode, 0, r.stderr[-300:])
                blobs.append(open(os.path.join(td, "out.png"), "rb").read())
        self.assertEqual(blobs[0], blobs[1], "同输入渲染结果不一致（确定性被破坏）")

    def test_low_memory_graceful(self):
        """A3：低内存环境（ulimit 512MB）渲染 → 不裸 traceback（中文诊断或正常完成）。"""
        data = {"labels": [str(i) for i in range(40)], "series": {"S": [float(i) for i in range(40)]}}
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "d.json")
            json.dump(data, open(p, "w", encoding="utf-8"), ensure_ascii=False)
            cmd = ("ulimit -v 524288; exec python3 %s -t bar --data %s -o %s/out --dpi 80"
                   % (shlex.quote(GEN), shlex.quote(p), shlex.quote(td)))
            r = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=180)
            self.assertNotIn("Traceback", r.stderr, r.stderr[-400:])

    def test_soak_30_charts(self):
        """A3：30 张混合图型连跑零失败（浸泡）。"""
        rng = random.Random(11)
        ok = 0
        with tempfile.TemporaryDirectory() as td:
            for i in range(30):
                t = ["bar", "line", "scatter", "box", "violin", "roc", "forest", "pca"][i % 8]
                if t == "bar":
                    d = {"labels": [f"g{j}" for j in range(5)],
                         "series": {f"s{k}": [rng.random() * 5 for _ in range(5)] for k in range(3)}}
                elif t == "line":
                    d = {"x": list(range(20)), "series": {"a": [rng.random() for _ in range(20)]}}
                elif t == "scatter":
                    d = {"x": [rng.random() for _ in range(50)], "y": [rng.random() for _ in range(50)]}
                elif t in ("box", "violin"):
                    d = {"labels": ["A", "B"], "series": {"A": [rng.random() for _ in range(20)],
                                                          "B": [rng.random() for _ in range(20)]}}
                elif t == "roc":
                    d = {"curves": [{"name": "m1", "fpr": [0, .5, 1], "tpr": [0, .8, 1], "auc": 0.9}]}
                elif t == "forest":
                    d = {"labels": ["s1", "s2", "s3"], "estimates": [1.1, 0.9, 1.3],
                         "ci_low": [0.8, 0.6, 1.0], "ci_high": [1.5, 1.2, 1.7], "measure": "RR"}
                else:
                    d = {"matrix": [[rng.random() for _ in range(4)] for _ in range(6)]}
                p = os.path.join(td, f"d{i}.json")
                json.dump(d, open(p, "w", encoding="utf-8"), ensure_ascii=False)
                r = subprocess.run([sys.executable, GEN, "-t", t, "--data", p,
                                    "-o", os.path.join(td, f"o{i}"), "--dpi", "70"],
                                   capture_output=True, text=True, timeout=120)
                self.assertEqual(r.returncode, 0, f"{t} #{i}: {r.stderr[-300:]}")
                ok += 1
        self.assertEqual(ok, 30)


# ── v2.8.0 B1：文档图型计数与代码注册表一致性守门 ──────────────────────────
class TestV280DocCount(unittest.TestCase):
    """B1(v2.8)：全部在售文档「N 种图表 / N 种图型 / N chart types」必须等于
    代码注册表去纯别名后的图型数。起因：v2.7.0 官方评测抓到 SKILL_ZH 写
    「21种图表」而实际 22 种——文档计数表述从此由测试锁死，新增图型时文档同步更新。"""

    _ALIASES = {"horizontal_bar", "boxplot", "survival"}  # 纯别名，不计为独立图型
    _DOC_FILES = ["SKILL.md", "SKILL_ZH.md",
                  os.path.join("templates", "README.md"),
                  os.path.join("references", "limits.md"),
                  os.path.join("references", "faq.md"),
                  os.path.join("references", "advanced.md"),
                  os.path.join("references", "data-formats.md"),
                  os.path.join("references", "pitfalls.md"),
                  os.path.join("references", "python-api.md"),
                  os.path.join("references", "composite-layouts.md")]

    def test_chart_count_claims_match_registry(self):
        import re
        n = len(gen_figure.GENERATORS) - len(self._ALIASES)
        self.assertGreaterEqual(n, 22, f"注册表图型数异常减少: {n}")
        root = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
        pat = re.compile(r"(\d+)\s*(?:种图表|种图型|chart types)")
        claims, bad = [], []
        for rel in self._DOC_FILES:
            p = os.path.join(root, rel)
            if not os.path.exists(p):
                continue
            with open(p, encoding="utf-8") as f:
                txt = f.read()
            for m in pat.finditer(txt):
                v = int(m.group(1))
                ln = txt.count("\n", 0, m.start()) + 1
                claims.append((rel, ln, v))
                if v != n:
                    bad.append(f"{rel}:{ln} 写「{v}」，注册表实际 {n}")
        self.assertTrue(claims, "没有扫描到任何图型计数表述——正则或文档路径可能已变化")
        self.assertEqual(bad, [], "文档图型计数与代码注册表不一致：\n" + "\n".join(bad))


# ── v2.8.0 A 线：看门狗 / 退出码分级 / 降采样出口 ─────────────────────────
class TestV280Watchdog(unittest.TestCase):
    """A1/A3(v2.8)：看门狗硬中断 exit 5、--timeout 0 禁用、退出码分类。"""

    def _big_cluster(self, td, rows=3000, cols=16):
        import random as _r
        rng = _r.Random(7)
        data = {"matrix": [[rng.random() for _ in range(cols)] for _ in range(rows)],
                "row_labels": [f"r{i}" for i in range(rows)],
                "col_labels": [f"c{j}" for j in range(cols)]}
        p = os.path.join(td, "big.json")
        json.dump(data, open(p, "w", encoding="utf-8"))
        return p

    def test_watchdog_kills_slow_render_exit5(self):
        """看门狗：--timeout 1 而渲染未完 → exit 5 + 中文诊断（不无限挂起）。"""
        import random as _r
        rng = _r.Random(7)
        with tempfile.TemporaryDirectory() as td:
            data = {"matrix": [[rng.random() for _ in range(16)] for _ in range(3000)],
                    "row_labels": [f"r{i}" for i in range(3000)],
                    "col_labels": [f"c{j}" for j in range(16)]}
            p = os.path.join(td, "big.json")
            json.dump(data, open(p, "w", encoding="utf-8"))
            r = subprocess.run([sys.executable, GEN, "-t", "cluster_heatmap", "--data", p,
                                "-o", os.path.join(td, "out"), "--dpi", "200",
                                "--timeout", "1"],
                               capture_output=True, text=True, timeout=120)
            self.assertEqual(r.returncode, 5,
                             f"期望看门狗 exit 5，实际 {r.returncode}：{r.stderr[-300:]}")
            self.assertIn("看门狗", r.stderr)
            self.assertIn("--timeout", r.stderr)

    def test_timeout_zero_disables_watchdog(self):
        """--timeout 0 → 禁用看门狗，正常渲染完成 exit 0。"""
        with tempfile.TemporaryDirectory() as td:
            p = self._big_cluster(td, rows=800, cols=8)
            r = subprocess.run([sys.executable, GEN, "-t", "cluster_heatmap", "--data", p,
                                "-o", os.path.join(td, "out"), "--dpi", "60",
                                "--timeout", "0"],
                               capture_output=True, text=True, timeout=300)
            self.assertEqual(r.returncode, 0, r.stderr[-300:])
            self.assertTrue(os.path.exists(os.path.join(td, "out.png")))

    def test_exit_code_classification(self):
        """A3：v3 兜底退出码分类——数据侧=3、环境侧=4、内存=6、其余=2。"""
        f = gen_figure._classify_exit_code
        self.assertEqual(f(ValueError("x")), 3)
        self.assertEqual(f(KeyError("k")), 3)
        self.assertEqual(f(IndexError("i")), 3)
        self.assertEqual(f(TypeError("t")), 3)
        self.assertEqual(f(FileNotFoundError("f")), 3)
        try:
            json.loads("{bad json")
        except json.JSONDecodeError as e:
            self.assertEqual(f(e), 3)
        self.assertEqual(f(ImportError("no mod")), 4)
        self.assertEqual(f(ModuleNotFoundError("no mod")), 4)
        self.assertEqual(f(OSError("io")), 4)
        self.assertEqual(f(PermissionError("denied")), 4)
        self.assertEqual(f(RuntimeError("rt")), 4)
        self.assertEqual(f(MemoryError("oom")), 6)
        self.assertEqual(f(Exception("misc")), 2)


class TestV280Downsample(unittest.TestCase):
    """A4(v2.8)：cluster_heatmap 超限指引 + --downsample 等距采样出口。"""

    def test_over_limit_hint_mentions_downsample(self):
        import random as _r
        rng = _r.Random(3)
        with tempfile.TemporaryDirectory() as td:
            data = {"matrix": [[rng.random() for _ in range(5)] for _ in range(3500)]}
            p = os.path.join(td, "big.json")
            json.dump(data, open(p, "w", encoding="utf-8"))
            r = subprocess.run([sys.executable, GEN, "-t", "cluster_heatmap", "--data", p,
                                "-o", os.path.join(td, "out"), "--dpi", "60"],
                               capture_output=True, text=True, timeout=240)
            self.assertEqual(r.returncode, 0, r.stderr[-300:])
            self.assertIn("AUTO-DOWNSAMPLE", r.stderr)

    def test_downsample_renders_under_limit(self):
        import random as _r
        rng = _r.Random(3)
        with tempfile.TemporaryDirectory() as td:
            data = {"matrix": [[rng.random() for _ in range(5)] for _ in range(3500)],
                    "row_labels": [f"g{i}" for i in range(3500)]}
            p = os.path.join(td, "big.json")
            json.dump(data, open(p, "w", encoding="utf-8"))
            r = subprocess.run([sys.executable, GEN, "-t", "cluster_heatmap", "--data", p,
                                "-o", os.path.join(td, "out"), "--dpi", "60",
                                "--downsample", "1500"],
                               capture_output=True, text=True, timeout=300)
            self.assertEqual(r.returncode, 0, r.stderr[-300:])
            self.assertIn("等距采样", r.stderr)
            self.assertTrue(os.path.exists(os.path.join(td, "out.png")))


# ── v2.8.0 C 线：异常词条覆盖（评测点名 ImportError 裸英文）────────────────
class TestV280ExceptionUX(unittest.TestCase):
    """C1(v2.8)：ImportError/OSError 等不再落「未知类型」，中文诊断含依赖指引。"""

    def test_importerror_has_chinese_diagnosis(self):
        zh, orig, tips = gen_figure._diagnose_exception(ImportError("No module named 'scipy'"))
        self.assertNotEqual(zh, "未知类型的错误")
        self.assertIn("依赖", zh)
        joined = " ".join(tips)
        self.assertIn("pip install", joined)

    def test_modulenotfounderror_and_oserror_covered(self):
        zh1, _, _ = gen_figure._diagnose_exception(ModuleNotFoundError("No module named 'x'"))
        zh2, _, _ = gen_figure._diagnose_exception(OSError("disk full"))
        self.assertIn("依赖", zh1)
        self.assertNotEqual(zh2, "未知类型的错误")

    def test_timeout_entry_mentions_flag(self):
        zh, _, tips = gen_figure._diagnose_exception(TimeoutError("slow"))
        self.assertIn("超时", zh)
        self.assertTrue(any("--timeout" in t for t in tips),
                        "TimeoutError 建议应包含 --timeout 指引")


# ── v2.8.0 D1：venn 4 集合椭圆 ──────────────────────────────────────────
class TestV280Venn4(unittest.TestCase):
    """D1(v2.8)：4 集合椭圆布局（默认确定性 / --area 拟合+披露）与区域契约。"""

    def test_default_layout_all_15_regions_nonempty(self):
        import itertools
        import importlib
        ac = importlib.import_module("af_v23_charts")
        ells = ac._venn4_ells()
        areas, cents, _ = ac._venn4_region_masks(ells, (-0.5, -0.5, 0.5, 0.5), 400)
        self.assertEqual(len(areas), 15)
        for k, v in areas.items():
            self.assertGreater(v, 0, f"默认布局区域 {k} 为空")
            self.assertIsNotNone(cents[k])

    def _sets4_data(self):
        return {"sets": {
            "组A": [f"a{i}" for i in range(40)],
            "组B": [f"a{i}" for i in range(15, 55)],
            "组C": [f"a{i}" for i in range(30, 70)],
            "组D": [f"a{i}" for i in range(5, 45, 2)] + [f"d{i}" for i in range(10)]}}

    def test_venn4_sets_mode_cli(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "v4.json")
            json.dump(self._sets4_data(), open(p, "w", encoding="utf-8"),
                      ensure_ascii=False)
            r = subprocess.run([sys.executable, GEN, "-t", "venn", "--data", p,
                                "-o", os.path.join(td, "out"), "--dpi", "70"],
                               capture_output=True, text=True, timeout=300)
            self.assertEqual(r.returncode, 0, r.stderr[-300:])
            self.assertTrue(os.path.exists(os.path.join(td, "out.png")))

    def test_venn4_regions15_and_bad_keys(self):
        import itertools
        sets = self._sets4_data()["sets"]
        keys = list(sets)
        ss = {k: set(v) for k, v in sets.items()}
        regs = {}
        for n in (1, 2, 3, 4):
            for combo in itertools.combinations(range(4), n):
                inter = set(ss[keys[combo[0]]])
                for i in combo[1:]:
                    inter &= ss[keys[i]]
                for i in range(4):
                    if i not in combo:
                        inter -= ss[keys[i]]
                regs["".join(keys[i] for i in combo)] = len(inter)
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "r15.json")
            json.dump({"regions": regs}, open(p, "w", encoding="utf-8"),
                      ensure_ascii=False)
            r = subprocess.run([sys.executable, GEN, "-t", "venn", "--data", p,
                                "-o", os.path.join(td, "out"), "--dpi", "70"],
                               capture_output=True, text=True, timeout=300)
            self.assertEqual(r.returncode, 0, r.stderr[-300:])
            bad = dict(list(regs.items())[:14])
            p2 = os.path.join(td, "r14.json")
            json.dump({"regions": bad}, open(p2, "w", encoding="utf-8"),
                      ensure_ascii=False)
            r2 = subprocess.run([sys.executable, GEN, "-t", "venn", "--data", p2,
                                 "-o", os.path.join(td, "bad"), "--dpi", "70"],
                                capture_output=True, text=True, timeout=120)
            self.assertEqual(r2.returncode, 1)
            self.assertIn("15 键", r2.stderr)

    def test_venn4_area_mode_discloses_and_deterministic(self):
        sets = self._sets4_data()["sets"]
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "v4.json")
            json.dump({"sets": sets}, open(p, "w", encoding="utf-8"),
                      ensure_ascii=False)
            blobs = []
            errs = []
            for i in range(2):
                out = os.path.join(td, f"area{i}")
                r = subprocess.run([sys.executable, GEN, "-t", "venn", "--data", p,
                                    "-o", out, "--dpi", "70", "--area"],
                                   capture_output=True, text=True, timeout=600)
                self.assertEqual(r.returncode, 0, r.stderr[-300:])
                self.assertIn("面积偏差", r.stderr)  # 拟合完成/最优拟合两分支均含
                blobs.append(open(out + ".png", "rb").read())
                errs.append(r.stderr)
            self.assertEqual(blobs[0], blobs[1], "--area 两次渲染不一致（确定性破坏）")
            self.assertIn("面积偏差", errs[0])


# ── v2.8.0 D2：--wizard 向导 ────────────────────────────────────────────
class TestV280Wizard(unittest.TestCase):
    """D2(v2.8)：非 TTY 不挂起打印决策树；AF_WIZARD_FORCE 管道走交互并出命令。"""

    def test_wizard_non_tty_prints_guide(self):
        r = subprocess.run([sys.executable, GEN, "--wizard"],
                           capture_output=True, text=True, timeout=60, input="")
        self.assertEqual(r.returncode, 0, r.stderr[-200:])
        self.assertIn("决策树", r.stdout)
        self.assertIn("-t", r.stdout)

    def test_wizard_interactive_pipe_generates_command(self):
        env = dict(os.environ, AF_WIZARD_FORCE="1")
        r = subprocess.run([sys.executable, GEN, "--wizard"],
                           capture_output=True, text=True, timeout=60,
                           input="1\n1\n1\n\n", env=env)
        self.assertEqual(r.returncode, 0, r.stderr[-200:])
        self.assertIn("-t bar", r.stdout, "向导应生成 bar 命令")
        self.assertIn("-d templates/", r.stdout)
        self.assertIn("-o figure.png", r.stdout)

    def test_wizard_eof_falls_back_to_defaults(self):
        env = dict(os.environ, AF_WIZARD_FORCE="1")
        r = subprocess.run([sys.executable, GEN, "--wizard"],
                           capture_output=True, text=True, timeout=60,
                           input="", env=env)
        self.assertEqual(r.returncode, 0, r.stderr[-200:])
        self.assertIn("-t bar", r.stdout)


class TestV280Help(unittest.TestCase):
    """回归锁（v2.8）：--help 可用且含新参数。v2.7.0 曾因 --stats help 文本含
    裸 %（95%CI）触发 argparse %-格式化崩溃——--help 整体不可用。"""

    def test_help_works_and_lists_v28_flags(self):
        r = subprocess.run([sys.executable, GEN, "--help"],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, f"--help 崩溃: {r.stderr[-200:]}")
        for flag in ("--timeout", "--downsample", "--wizard", "--stats"):
            self.assertIn(flag, r.stdout + r.stderr, f"--help 缺 {flag}")



class TestV20900DocsAndAdvisories(unittest.TestCase):
    """v2.9.0：边界收拢（limits §六/§七）、独立快速入门、预判 advisory、KeyError 纠错。"""

    @classmethod
    def setUpClass(cls):
        cls.root = os.path.dirname(SCRIPT_DIR)
        cls.qs = os.path.join(cls.root, "references", "quickstart.md")
        cls.lim = os.path.join(cls.root, "references", "limits.md")
        cls.zh = io.open(os.path.join(cls.root, "SKILL_ZH.md"), encoding="utf-8").read()
        cls.en = io.open(os.path.join(cls.root, "SKILL.md"), encoding="utf-8").read()

    def test_quickstart_exists_with_sections(self):
        self.assertTrue(os.path.isfile(self.qs), "quickstart.md 缺失")
        t = io.open(self.qs, encoding="utf-8").read()
        for kw in ("完整选图决策树", "上手四步", "Python 内调用", "常见第一坑", "--wizard"):
            self.assertIn(kw, t, "quickstart 缺小节: " + kw)

    def test_limits_has_new_sections(self):
        t = io.open(self.lim, encoding="utf-8").read()
        for kw in ("输出与默认行为交互", "运行环境边界", "--verify", "--width",
                   "matplotlib.use('Agg')"):
            self.assertIn(kw, t, "limits.md 缺: " + kw)

    def test_main_docs_point_to_quickstart(self):
        self.assertIn("references/quickstart.md", self.zh)
        self.assertIn("references/quickstart.md", self.en)
        self.assertIn("分组比较", self.zh)          # 精简映射保留（trigger 面）
        self.assertIn("Group comparison", self.en)
        self.assertIn("version: 6.2.0", self.zh)
        self.assertIn("version: 6.2.0", self.en)

    def test_cluster_advisory_preflight(self):
        data = {"matrix": [[float(i), float(i) + 1] for i in range(1600)]}
        msgs = gen_figure._preflight_advisories(data, "cluster_heatmap", None)
        self.assertEqual(len(msgs), 1)
        self.assertIn("--downsample", msgs[0])
        self.assertIn("1500", msgs[0])
        self.assertEqual(
            gen_figure._preflight_advisories(data, "cluster_heatmap", 500), [])
        small = {"matrix": [[1.0, 2.0], [3.0, 4.0]]}
        self.assertEqual(
            gen_figure._preflight_advisories(small, "cluster_heatmap", None), [])
        self.assertEqual(gen_figure._preflight_advisories(data, "bar", None), [])

    def test_keyerror_near_miss_hint(self):
        zh, orig, tips = gen_figure._diagnose_exception(KeyError("lables"))
        joined = " ".join(tips)
        self.assertIn("labels", joined)
        self.assertIn("lables", joined)
        zh2, o2, t2 = gen_figure._diagnose_exception(KeyError("zzzzzz"))
        self.assertIsInstance(t2, list)
        zh3, o3, t3 = gen_figure._diagnose_exception(ValueError("x"))
        self.assertTrue(isinstance(t3, list))




class TestV290FieldNearMiss(unittest.TestCase):
    """v2.9：字段名近似匹配告警。"""

    def test_typo_labels_warns(self):
        msgs = gen_figure._field_nearmiss_warnings(
            {"lables": ["a", "b"], "series": {"s": [1, 2]}})
        self.assertEqual(len(msgs), 1)
        self.assertIn("lables", msgs[0])
        self.assertIn("labels", msgs[0])

    def test_known_and_underscore_keys_silent(self):
        self.assertEqual(gen_figure._field_nearmiss_warnings(
            {"labels": ["a"], "series": {"s": [1]}, "_command": "x"}), [])

    def test_random_unknown_key_silent(self):
        self.assertEqual(gen_figure._field_nearmiss_warnings(
            {"labels": ["a"], "my_custom_note": "x"}), [])




class TestV30000Docs(unittest.TestCase):
    """v3.0.0：类别列、速查卡、行内警示、模块拆分。"""

    @classmethod
    def setUpClass(cls):
        root = os.path.dirname(SCRIPT_DIR)
        cls.zh = io.open(os.path.join(root, "SKILL_ZH.md"), encoding="utf-8").read()
        cls.en = io.open(os.path.join(root, "SKILL.md"), encoding="utf-8").read()
        cs_p = os.path.join(root, "references", "cheatsheet.md")
        cls.cs = io.open(cs_p, encoding="utf-8").read() if os.path.isfile(cs_p) else ""

    def test_category_column(self):
        self.assertIn("| 类别 | 类型 | 命令 |", self.zh)
        self.assertIn("| Category | Type | Command |", self.en)
        for cat in ("统计推断", "生存分析", "系统综述"):
            self.assertIn(cat, self.zh)
        for cat in ("Inference", "Survival", "Review"):
            self.assertIn(cat, self.en)

    def test_cheatsheet_complete(self):
        for kw in ("速查卡", "图型命令骨架", "全量参数速查", "退出码", "边界 Top 5", "--journal"):
            self.assertIn(kw, self.cs, "cheatsheet 缺: " + kw)

    def test_inline_warnings(self):
        self.assertIn("自动联动同款配色", self.zh)
        self.assertIn("auto-links its theme", self.en)
        self.assertIn("references/cheatsheet.md", self.zh)
        self.assertIn("references/cheatsheet.md", self.en)

    def test_modules_exist(self):
        self.assertTrue(os.path.isfile(os.path.join(SCRIPT_DIR, "af_wizard.py")))
        self.assertTrue(os.path.isfile(os.path.join(SCRIPT_DIR, "af_diagnostics.py")))
        self.assertTrue(callable(gen_figure._run_wizard))
        self.assertTrue(callable(gen_figure._diagnose_exception))

    def test_medwiki_section(self):
        """v3.1：MedWiki 导流节（v3.8.0 起并入『相关工具』中性节：免责+负向触发行保留）。"""
        self.assertIn("docsor.cn", self.zh)
        self.assertIn("## 相关工具", self.zh)
        self.assertIn("不构成诊疗或用药建议", self.zh)
        self.assertIn("不提及 MedWiki", self.zh)
        self.assertIn("docsor.cn", self.en)
        self.assertIn("## Related tools", self.en)
        self.assertIn("prescription-level pages are gated", self.en)

    def test_version_2100(self):
        self.assertIn("version: 6.2.0", self.zh)
        self.assertIn("version: 6.2.0", self.en)
        self.assertNotIn("version: 2.9.0", self.zh)
        self.assertNotIn("version: 2.9.0", self.en)




    def test_toolkit_billboard(self):
        """v3.8.0 相关工具中性化（用户 09-29 改版令）：功能导向一句话提示；
        禁止家族/全家桶/搜招牌名等营销修辞回潮；裸 paper-polisher 非我方不得出现。"""
        for kw in ("## 相关工具", "pubmed-verifier", "cite-holmes",
                   "paper-polisher-pro", "paper-rewriter", "docsor.cn"):
            self.assertIn(kw, self.zh)
        self.assertIn("## Related tools", self.en)
        self.assertIn("docsor.cn", self.en)
        for banned in ("全家桶", "搜招牌名", "全家族", "同一作者"):
            self.assertNotIn(banned, self.zh)
        for banned in ("Paper-workflow toolkit", "Entire family", "same author"):
            self.assertNotIn(banned, self.en)
        # v4.9 Pro 导流信息校准（用户 10-10 令）：价格/分辨率以服务端事实为准，
        # 旧值与内部推广话术禁回潮；¥3 与 600dpi 必须在双语 Pro 段可见
        for banned in ("¥0.5", "0.5/次", "转推入口", "300dpi"):
            self.assertNotIn(banned, self.zh)
            self.assertNotIn(banned, self.en)
        self.assertIn("¥3", self.zh)
        self.assertIn("600dpi", self.zh)
        self.assertIn("¥3", self.en)
        self.assertIn("600dpi", self.en)
        self.assertNotIn("paper-polisher", self.zh.replace("paper-polisher-pro", ""))



    def test_version_current(self):
        self.assertIn("version: 6.2.0", self.zh)
        self.assertIn("version: 6.2.0", self.en)




    def test_km_cjk_risk_table_no_regression(self):
        """v3.0.1 热修回归锁：加载 CJK 字体+英文组名的 km，风险表不得渲染失败
        （v3.0.0 曾因未定义 _auto_cjk_hdr 丢整个风险表）。"""
        data = {"groups": {"ArmA": [[3.1, 1], [7.4, 0], [12.2, 1], [20.5, 1]],
                           "ArmB": [[2.3, 1], [9.8, 0], [15.1, 1], [25.0, 1]]}}
        f = os.path.join(tempfile.gettempdir(), "af_km_cjk_lock.json")
        io.open(f, "w", encoding="utf-8").write(json.dumps(data))
        r = subprocess.run(
            [sys.executable, GEN, "-t", "km", "-d", f,
             "-o", os.path.join(tempfile.gettempdir(), "af_km_cjk_lock.png"),
             "--cjk", "--dpi", "100"],
            capture_output=True, text=True, timeout=240)
        self.assertEqual(r.returncode, 0, r.stderr[-300:])
        self.assertNotIn("自动风险表渲染失败", r.stderr, r.stderr[-300:])




class TestV30100(unittest.TestCase):
    """v3.1.0：--quick 一键出图 + 自动降采样 + 文档口径。"""

    def test_quick_renders(self):
        with tempfile.TemporaryDirectory() as td:
            data = {"labels": ["A", "B", "C"],
                    "series": {"s1": [1, 2, 3], "s2": [2, 3, 4]}}
            p = os.path.join(td, "d.json")
            json.dump(data, open(p, "w", encoding="utf-8"))
            out = os.path.join(td, "q.png")
            r = subprocess.run([sys.executable, GEN, "--quick", "--data", p,
                                "--out", out, "--dpi", "80"],
                               capture_output=True, text=True, timeout=240)
            self.assertEqual(r.returncode, 0, r.stderr[-300:])
            self.assertTrue(os.path.isfile(out))
            self.assertIn("自动选择图型", r.stderr)

    def test_quick_requires_data(self):
        r = subprocess.run([sys.executable, GEN, "--quick"],
                           capture_output=True, text=True, timeout=60)
        self.assertNotEqual(r.returncode, 0)

    def test_auto_downsample_4000(self):
        import random as _r
        rng = _r.Random(11)
        with tempfile.TemporaryDirectory() as td:
            data = {"matrix": [[rng.random() for _ in range(4)] for _ in range(4000)],
                    "row_labels": [f"r{i}" for i in range(4000)]}
            p = os.path.join(td, "big.json")
            json.dump(data, open(p, "w", encoding="utf-8"))
            out = os.path.join(td, "auto.png")
            r = subprocess.run([sys.executable, GEN, "-t", "cluster_heatmap", "--data", p,
                                "-o", out, "--dpi", "60"],
                               capture_output=True, text=True, timeout=300)
            self.assertEqual(r.returncode, 0, r.stderr[-300:])
            self.assertIn("AUTO-DOWNSAMPLE", r.stderr)

    def test_docs_310(self):
        root = os.path.dirname(SCRIPT_DIR)
        zh = io.open(os.path.join(root, "SKILL_ZH.md"), encoding="utf-8").read()
        lim = io.open(os.path.join(root, "references", "limits.md"), encoding="utf-8").read()
        df = io.open(os.path.join(root, "references", "data-formats.md"), encoding="utf-8").read()
        self.assertIn("version: 6.2.0", zh)
        self.assertIn("--quick", zh)
        self.assertIn("行自动降采样", zh)
        self.assertIn("性能参考表", lim)
        self.assertIn("JSON vs CSV 能力对照", df)




class TestV3200(unittest.TestCase):
    """v3.2.0：自动行为透明化 + 安全声明 + FAQ 扩充。"""

    @classmethod
    def setUpClass(cls):
        root = os.path.dirname(SCRIPT_DIR)
        cls.zh = io.open(os.path.join(root, "SKILL_ZH.md"), encoding="utf-8").read()
        cls.en = io.open(os.path.join(root, "SKILL.md"), encoding="utf-8").read()
        cls.faq = io.open(os.path.join(root, "references", "faq.md"), encoding="utf-8").read()

    def test_journal_width_lock_announced(self):
        with tempfile.TemporaryDirectory() as td:
            data = {"labels": ["A", "B"], "series": {"s": [1, 2]}}
            p = os.path.join(td, "d.json")
            json.dump(data, open(p, "w", encoding="utf-8"))
            r = subprocess.run([sys.executable, GEN, "-t", "bar", "--data", p,
                                "-o", os.path.join(td, "o.pdf"), "--journal", "nature",
                                "--width", "10", "--dpi", "80"],
                               capture_output=True, text=True, timeout=240)
            self.assertEqual(r.returncode, 0, r.stderr[-300:])
            self.assertIn("[auto]", r.stderr)
            self.assertIn("已被忽略", r.stderr)

    def test_verify_non_pdf_announced(self):
        with tempfile.TemporaryDirectory() as td:
            data = {"labels": ["A", "B"], "series": {"s": [1, 2]}}
            p = os.path.join(td, "d.json")
            json.dump(data, open(p, "w", encoding="utf-8"))
            r = subprocess.run([sys.executable, GEN, "-t", "bar", "--data", p,
                                "-o", os.path.join(td, "o.png"), "--verify", "--dpi", "80"],
                               capture_output=True, text=True, timeout=240)
            self.assertEqual(r.returncode, 0, r.stderr[-300:])
            self.assertIn("仅对 PDF", r.stderr)

    def test_safety_statement_bilingual(self):
        self.assertIn("## 安全与数据（行为声明）", self.zh)
        self.assertIn("不发起任何网络请求", self.zh)
        self.assertIn("## Safety & Data (behavior statement)", self.en)
        self.assertIn("zero network requests", self.en)

    def test_faq_conflicts_expanded(self):
        for kw in ("参数冲突与边界 FAQ", "--journal", "--verify", "--hatch"):
            self.assertIn(kw, self.faq)

    def test_top3_boundary_banner(self):
        self.assertIn("三条最常用边界", self.zh)
        self.assertIn("Top-3 boundaries", self.en)



class TestV3700(unittest.TestCase):
    """v3.7.0：Aalen-Johansen 竞争风险——三重对拍锁 + 渲染 E2E。"""

    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "af_v23_stats", os.path.join(SCRIPT_DIR, "af_v23_stats.py"))
        cls.st = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.st)

    def test_no_competing_degenerates_to_1_minus_km(self):
        times = [1, 2, 2, 3, 4, 4, 5, 6, 7, 8]
        events = [1, 0, 1, 1, 0, 1, 0, 1, 1, 0]
        res = self.st.aalen_johansen(times, events)
        km = self.st.km_estimate(times, [1 if e == 1 else 0 for e in events])
        max_d = 0.0
        for tt, ss in zip(km[0], km[1]):
            if tt <= 0:
                continue
            idx = [j for j, at in enumerate(res["times"]) if abs(at - tt) < 1e-9]
            if idx:
                max_d = max(max_d, abs((1 - ss) - res["cif"][1][idx[0]]))
        self.assertLess(max_d, 1e-10, f"退化恒等式破坏 max={max_d}")

    def test_completeness_identity_with_competing(self):
        res = self.st.aalen_johansen([1, 2, 2, 3, 3, 4, 5], [1, 2, 0, 1, 2, 1, 2])
        for i, s_ in enumerate(res["survival"]):
            total = s_ + sum(res["cif"][j][i] for j in res["cif"])
            self.assertLess(abs(1 - total), 1e-12)

    def test_literature_six_patient_example(self):
        res = self.st.aalen_johansen([1, 2, 2, 3, 3, 4], [1, 2, 0, 1, 2, 1])
        self.assertAlmostEqual(res["cif"][1][-1], 0.6111, places=4)
        self.assertAlmostEqual(res["cif"][2][-1], 0.3889, places=4)
        self.assertAlmostEqual(res["survival"][-1], 0.0, places=6)

    def test_competing_km_render_e2e(self):
        km_cr = {"groups": {
            "移植组": [[1,1],[2,2],[4,1],[6,0],[8,1],[10,2],[12,1]],
            "化疗组": [[2,1],[3,2],[5,1],[7,0],[9,2],[11,1],[13,0]]},
            "cause_names": {"2": "移植相关死亡"}}
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "kmcr.json")
            json.dump(km_cr, open(p, "w", encoding="utf-8"), ensure_ascii=False)
            r = subprocess.run([sys.executable, GEN, "-t", "km", "--data", p,
                                "-o", os.path.join(td, "kmcr.png"), "--cjk", "--dpi", "100"],
                               capture_output=True, text=True, timeout=240)
            self.assertEqual(r.returncode, 0, r.stderr[-250:])
            self.assertIn("Aalen-Johansen", r.stderr)



class TestV30300(unittest.TestCase):
    """v3.3.0：frontmatter 冒号炸弹防御 + README 官方化。"""

    @classmethod
    def setUpClass(cls):
        root = os.path.dirname(SCRIPT_DIR)
        cls.readme = os.path.join(root, "README.md")
        cls._fm = {}
        for name in ("SKILL.md", "SKILL_ZH.md"):
            t = io.open(os.path.join(root, name), encoding="utf-8").read()
            cls._fm[name] = re.match(r"^---\n(.*?)\n---\n", t, re.S).group(1)

    def test_no_colon_bomb_in_frontmatter(self):
        import re as _re
        for name, fm in self._fm.items():
            hits = []
            for ln in fm.splitlines():
                if _re.match(r"^[A-Za-z_][\w-]*:\s", ln):
                    continue  # 键行合法（YAML 键语法）
                if _re.search(r"[a-zA-Z]:\s", ln):
                    hits.append(ln.strip()[:60])  # 折叠值续行里的 word: value = 炸弹
            self.assertEqual(len(hits), 0, f"{name} frontmatter 折叠值仍有冒号+空格: {hits[:3]}")

    def test_readme_official(self):
        self.assertTrue(os.path.isfile(self.readme), "README.md 缺失")
        t = io.open(self.readme, encoding="utf-8").read()
        for kw in ("Quick Start", "39 chart types", "docsor.cn", "paper-polisher-pro",
                   "zero telemetry", "setup_env.py"):
            self.assertIn(kw, t)


    def test_nature_clean_style(self):
        """v3.3：--style nature-clean 顶刊版式（渲染成功+stderr 告知+与默认输出不同）。"""
        import tempfile as _tf
        with _tf.TemporaryDirectory() as td:
            data = {"labels": ["A", "B", "C"], "series": {"s1": [1, 2, 3], "s2": [2, 3, 4]}}
            p = os.path.join(td, "d.json")
            json.dump(data, open(p, "w", encoding="utf-8"))
            o1 = os.path.join(td, "plain.png")
            o2 = os.path.join(td, "nc.png")
            r1 = subprocess.run([sys.executable, GEN, "-t", "bar", "--data", p,
                                 "-o", o1, "--dpi", "80"],
                                capture_output=True, text=True, timeout=240)
            r2 = subprocess.run([sys.executable, GEN, "-t", "bar", "--data", p,
                                 "-o", o2, "--style", "nature-clean", "--dpi", "80"],
                                capture_output=True, text=True, timeout=240)
            self.assertEqual(r1.returncode, 0, r1.stderr[-200:])
            self.assertEqual(r2.returncode, 0, r2.stderr[-200:])
            self.assertIn("nature-clean", r2.stderr)
            self.assertNotEqual(open(o1, "rb").read(), open(o2, "rb").read())



    def test_version_330(self):
        root = os.path.dirname(SCRIPT_DIR)
        for name in ("SKILL.md", "SKILL_ZH.md"):
            t = io.open(os.path.join(root, name), encoding="utf-8").read()
            self.assertIn("version: 6.2.0", t, name)




class TestV30400(unittest.TestCase):
    """v3.4.0：直接标注 + 标题层级 + Pro 透传。"""

    def test_direct_label_renders(self):
        import tempfile as _tf
        with _tf.TemporaryDirectory() as td:
            data = {"labels": [1, 2, 3, 4, 5],
                    "series": {"干预组": [10, 8, 6, 4, 2], "对照组": [2, 3, 5, 7, 9]}}
            p = os.path.join(td, "d.json")
            json.dump(data, open(p, "w", encoding="utf-8"))
            r = subprocess.run([sys.executable, GEN, "-t", "line", "--data", p,
                                "-o", os.path.join(td, "dl.png"), "--direct-label",
                                "--dpi", "80"],
                               capture_output=True, text=True, timeout=240)
            self.assertEqual(r.returncode, 0, r.stderr[-250:])

    def test_theme_default_direct_label(self):
        with tempfile.TemporaryDirectory() as td:
            data = {"labels": [1, 2, 3], "series": {"A": [1, 2, 3], "B": [3, 2, 1]}}
            p = os.path.join(td, "d.json")
            json.dump(data, open(p, "w", encoding="utf-8"))
            r = subprocess.run([sys.executable, GEN, "-t", "line", "--data", p,
                                "-o", os.path.join(td, "o.png"), "--style", "glm-brand",
                                "--dpi", "80"],
                               capture_output=True, text=True, timeout=240)
            self.assertEqual(r.returncode, 0, r.stderr[-250:])


    def test_composite_km_median_auto_off(self):
        """v3.4.1（55 线发现）：composite 里 KM 面板的 median_auto 必须被消费。"""
        import random as _rr
        _rr.seed(4)
        km_panel = {"type": "km",
                    "data": {"groups": {"A": [[round(_rr.random()*20, 2), 1 if _rr.random() < .5 else 0] for _ in range(30)],
                                        "B": [[round(_rr.random()*20, 2), 1 if _rr.random() < .5 else 0] for _ in range(30)]}},
                    "median_auto": False}
        body = {"panels": [km_panel, {"type": "bar",
                                       "data": {"labels": ["x", "y"], "series": {"s": [1, 2]}}}],
                "layout": [1, 2]}
        p = os.path.join(tempfile.gettempdir(), "af_ckm.json")
        json.dump(body, open(p, "w", encoding="utf-8"))
        r = subprocess.run([sys.executable, GEN, "-t", "composite", "--data", p,
                            "-o", os.path.join(tempfile.gettempdir(), "af_ckm.png"),
                            "--no-median", "--dpi", "60"],
                           capture_output=True, text=True, timeout=240)
        self.assertEqual(r.returncode, 0, r.stderr[-250:])
        self.assertNotIn("自动中位生存", r.stderr, "median_auto:false 被 composite 面板忽略")




    def test_dual_axis_legend_right_axis(self):
        """v3.4.1（55 线发现）：dual_axis 图例必须含右轴系列（55 修复移植）。
        渲染自带模板 15 → PDF → 文本提取应含右轴系列名（CRP）。"""
        tpl = os.path.join(os.path.dirname(SCRIPT_DIR), "templates", "15-lab-trend-dual-axis.json")
        d = json.load(open(tpl, encoding="utf-8"))
        right_names = list((d.get("right") or {}).keys())
        self.assertTrue(right_names, "模板缺右轴系列")
        want = right_names[0]
        with tempfile.TemporaryDirectory() as td:
            r = subprocess.run([sys.executable, GEN, "-t", "dual_axis", "--data", tpl,
                                "-o", os.path.join(td, "da.pdf"), "--dpi", "80"],
                               capture_output=True, text=True, timeout=240)
            self.assertEqual(r.returncode, 0, r.stderr[-250:])
            import fitz
            doc = fitz.open(os.path.join(td, "da.pdf"))
            text = "".join(pg.get_text() for pg in doc)
            self.assertIn(want, text, f"dual_axis 图例丢失右轴系列 {want}")

    def test_km_median_unit_neutral(self):
        """v3.4.1（55 线发现）：KM 注释框单位中性化——不得再出现『中位生存月』。"""
        root = os.path.dirname(SCRIPT_DIR)
        # v4.0.0 模块化后生成器源在 af_draw.py——扫描全部引擎文件
        g = "".join(io.open(os.path.join(root, "scripts", f), encoding="utf-8").read()
                    for f in ("gen_figure.py", "af_draw.py", "af_shared.py"))
        self.assertEqual(g.count("中位生存月"), 0)
        self.assertIn("中位生存（95%CI）", g)



    def test_version_340(self):
        root = os.path.dirname(SCRIPT_DIR)
        for name in ("SKILL.md", "SKILL_ZH.md"):
            t = io.open(os.path.join(root, name), encoding="utf-8").read()
            self.assertIn("version: 6.2.0", t, name)
            self.assertIn("--direct-label", t, name)




class TestV3500(unittest.TestCase):
    """v3.5.0：Bootstrap CI——金标准对拍+确定性+渲染。"""

    def test_bootstrap_converges_to_analytic(self):
        """金标准①：大样本正态数据，bootstrap CI ≈ 解析 CI（x̄±1.96SE）。"""
        import numpy as _np
        import importlib
        spec = importlib.util.spec_from_file_location(
            "af_v23_stats", os.path.join(SCRIPT_DIR, "af_v23_stats.py"))
        st = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(st)
        rng = _np.random.default_rng(7)
        vals = rng.normal(50, 10, 5000)
        stat, lo, hi = st.bootstrap_ci(list(vals), n_boot=5000, seed=7)
        se = 10 / _np.sqrt(5000)
        analytic = (50 - 1.96 * se, 50 + 1.96 * se)
        self.assertLess(abs(lo - analytic[0]), 0.4, (lo, analytic[0]))
        self.assertLess(abs(hi - analytic[1]), 0.4, (hi, analytic[1]))
        self.assertLess(abs(stat - 50), 0.5)

    def test_bootstrap_deterministic(self):
        import importlib
        spec = importlib.util.spec_from_file_location(
            "af_v23_stats", os.path.join(SCRIPT_DIR, "af_v23_stats.py"))
        st = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(st)
        a = st.bootstrap_ci([1.5, 2.2, 3.1, 2.8, 4.0, 3.3], seed=42)
        b = st.bootstrap_ci([1.5, 2.2, 3.1, 2.8, 4.0, 3.3], seed=42)
        self.assertEqual(a, b)

    def test_bootstrap_vs_first_shape(self):
        import importlib
        spec = importlib.util.spec_from_file_location(
            "af_v23_stats", os.path.join(SCRIPT_DIR, "af_v23_stats.py"))
        st = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(st)
        out = st.bootstrap_vs_first({"对照": [1, 2, 3, 2, 1], "处理": [4, 5, 6, 5, 4]})
        self.assertEqual(len(out), 1)
        name, diff, lo, hi = out[0]
        self.assertEqual(name, "处理")
        self.assertGreater(diff, 0)
        self.assertLess(lo, hi)

    def test_stats_bootstrap_renders(self):
        import tempfile as _tf
        with _tf.TemporaryDirectory() as td:
            data = {"labels": ["对照", "低", "高"],
                    "series": {"对照": [10, 12, 11, 9, 10.5], "低": [9, 10, 10.2, 8.8],
                               "高": [14, 13, 15, 14.5]}}
            p = os.path.join(td, "d.json")
            json.dump(data, open(p, "w", encoding="utf-8"))
            r = subprocess.run([sys.executable, GEN, "-t", "box", "--data", p,
                                "-o", os.path.join(td, "b.png"), "--stats", "bootstrap",
                                "--dpi", "80"],
                               capture_output=True, text=True, timeout=240)
            self.assertEqual(r.returncode, 0, r.stderr[-250:])
            self.assertIn("stats bootstrap", r.stderr)
            self.assertIn("95%CI", r.stderr)




class TestV30600(unittest.TestCase):
    """v3.6.0：静默忽略显式化 + CSV 误差棒前置 + 历史披露。"""

    def test_stats_ignored_announced(self):
        import tempfile as _tf
        with _tf.TemporaryDirectory() as td:
            data = {"labels": [1, 2, 3], "series": {"A": [1, 2, 3]}}
            p = os.path.join(td, "d.json")
            json.dump(data, open(p, "w", encoding="utf-8"))
            r = subprocess.run([sys.executable, GEN, "-t", "line", "--data", p,
                                "-o", os.path.join(td, "o.png"), "--stats", "auto",
                                "--dpi", "80"],
                               capture_output=True, text=True, timeout=240)
            self.assertEqual(r.returncode, 0, r.stderr[-200:])
            self.assertIn("[ignored]", r.stderr)
            self.assertIn("仅 box/violin", r.stderr)

    def test_multiple_ignored_announced(self):
        with tempfile.TemporaryDirectory() as td:
            data = {"labels": [1, 2, 3], "series": {"A": [1, 2, 3]}}
            p = os.path.join(td, "d.json")
            json.dump(data, open(p, "w", encoding="utf-8"))
            r = subprocess.run([sys.executable, GEN, "-t", "line", "--data", p,
                                "-o", os.path.join(td, "o2.png"), "--stats", "auto",
                                "--egger", "--area", "--hatch", "--sheet", "S1",
                                "--dpi", "80"],
                               capture_output=True, text=True, timeout=240)
            self.assertEqual(r.returncode, 0)
            for kw in ("--egger", "--area", "--hatch"):
                self.assertIn(kw, r.stderr)

    def test_csv_errorbar_hint(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "d.csv")
            open(p, "w", encoding="utf-8").write("group,value\nA,1\nB,2")
            r = subprocess.run([sys.executable, GEN, "-t", "bar", "--data", p,
                                "-o", os.path.join(td, "o.png"), "--dpi", "80"],
                               capture_output=True, text=True, timeout=240)
            self.assertEqual(r.returncode, 0, r.stderr[-200:])
            self.assertIn("CSV 不支持误差棒", r.stderr)

    def test_labreport_disclosure(self):
        root = os.path.dirname(SCRIPT_DIR)
        p = io.open(os.path.join(root, "references", "pitfalls.md"), encoding="utf-8").read()
        self.assertIn("Lab Report PDF", p)
        self.assertIn("v2.0.1", p)

    def test_version_360(self):
        root = os.path.dirname(SCRIPT_DIR)
        for name in ("SKILL.md", "SKILL_ZH.md"):
            t = io.open(os.path.join(root, name), encoding="utf-8").read()
            self.assertIn("version: 6.2.0", t, name)




class TestV30800(unittest.TestCase):
    """v3.8.0：--pub-ready 一键投稿包 + 中位数 bootstrap CI。"""

    def test_pub_ready_e2e(self):
        import tempfile as _tf
        with _tf.TemporaryDirectory() as td:
            data = {"labels": ["A", "B"], "series": {"s": [1, 2]}}
            p = os.path.join(td, "d.json")
            json.dump(data, open(p, "w", encoding="utf-8"))
            r = subprocess.run([sys.executable, GEN, "-t", "bar", "--data", p,
                                "-o", os.path.join(td, "pr"), "--pub-ready", "--dpi", "80"],
                               capture_output=True, text=True, timeout=240)
            self.assertEqual(r.returncode, 0, r.stderr[-250:])
            self.assertIn("[pub-ready]", r.stderr)
            outs = os.listdir(td)
            self.assertTrue(any(f.endswith(".pdf") for f in outs))
            self.assertTrue(any(f.endswith(".png") for f in outs))

    def test_pub_ready_explicit_mf_wins(self):
        import tempfile as _tf
        with _tf.TemporaryDirectory() as td:
            data = {"labels": ["A", "B"], "series": {"s": [1, 2]}}
            p = os.path.join(td, "d.json")
            json.dump(data, open(p, "w", encoding="utf-8"))
            r = subprocess.run([sys.executable, GEN, "-t", "bar", "--data", p,
                                "-o", os.path.join(td, "pr2"), "--pub-ready",
                                "--multi-format", "svg", "--dpi", "80"],
                               capture_output=True, text=True, timeout=240)
            self.assertEqual(r.returncode, 0, r.stderr[-250:])
            self.assertTrue(any(f.endswith(".svg") for f in os.listdir(td)))

    def test_bootstrap_median_ci(self):
        import numpy as _np
        import importlib.util as _ilu
        _spec = _ilu.spec_from_file_location(
            "af_v23_stats", os.path.join(SCRIPT_DIR, "af_v23_stats.py"))
        _st = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_st)
        med, lo, hi = _st.bootstrap_median_ci(list(_np.random.default_rng(3).normal(30, 5, 800)), seed=3)
        self.assertLess(lo, med)
        self.assertLess(med, hi)
        self.assertLess(abs(med - 30), 1.5)

    def test_bootstrap_median_reported(self):
        import tempfile as _tf
        with _tf.TemporaryDirectory() as td:
            data = {"labels": ["A", "B"],
                    "series": {"A": [10, 12, 11, 9, 10.5], "B": [9, 10, 10.2, 8.8, 9.5]}}
            p = os.path.join(td, "d.json")
            json.dump(data, open(p, "w", encoding="utf-8"))
            r = subprocess.run([sys.executable, GEN, "-t", "box", "--data", p,
                                "-o", os.path.join(td, "b.png"), "--stats", "bootstrap",
                                "--dpi", "80"],
                               capture_output=True, text=True, timeout=240)
            self.assertEqual(r.returncode, 0, r.stderr[-250:])
            self.assertIn("中位数=", r.stderr)
            self.assertIn("偏态数据建议引用中位数 CI", r.stderr)

    def test_version_380(self):
        root = os.path.dirname(SCRIPT_DIR)
        for name in ("SKILL.md", "SKILL_ZH.md"):
            t = io.open(os.path.join(root, name), encoding="utf-8").read()
            self.assertIn("version: 6.2.0", t, name)
            self.assertIn("--pub-ready", t, name)



class TestV30900(unittest.TestCase):
    """v3.9.0：--order 类别顺序 + --normalize 归一化 + --doctor 体检。"""

    def test_order_function_bar(self):
        import gen_figure as g
        d = {"labels": ["A", "B", "C"], "series": {"s1": [1, 2, 3], "s2": [2, 4, 6]},
             "errors": {"s1": [0.1, 0.2, 0.3]},
             "significance": {"s1:0": "*", "s2:2": "NS"}}
        g.apply_series_order(d, "C,A,B", "bar")
        self.assertEqual(d["labels"], ["C", "A", "B"])
        self.assertEqual(d["series"]["s1"], [3, 1, 2])
        self.assertEqual(d["series"]["s2"], [6, 2, 4])
        self.assertEqual(d["errors"]["s1"], [0.3, 0.1, 0.2])
        # significance 列索引随重排改写（A:旧0→新1；C:旧2→新0）
        self.assertEqual(d["significance"], {"s1:1": "*", "s2:0": "NS"})

    def test_order_auto_box_median(self):
        import gen_figure as g
        d = {"series": {"g1": [1, 2, 3, 4], "g2": [5, 6, 7, 8], "g3": [2, 3, 2, 3]}}
        g.apply_series_order(d, "auto", "box")
        self.assertEqual(list(d["series"].keys()), ["g2", "g1", "g3"])

    def test_order_unknown_label_fatal(self):
        import gen_figure as g
        d = {"labels": ["A", "B"], "series": {"s": [1, 2]}}
        with self.assertRaises(ValueError):
            g.apply_series_order(d, "A,X", "bar")

    def test_normalize_baseline_mean_is_one(self):
        import gen_figure as g
        d = {"series": {"a": [2, 4, 6], "b": [1, 1, 3]}}
        g.apply_normalize(d, "baseline", "bar")
        self.assertAlmostEqual(sum(d["series"]["a"]) / 3, 1.0)  # 对照均值=1
        self.assertAlmostEqual(d["series"]["b"][1], 0.25)

    def test_normalize_pct100_first_point(self):
        import gen_figure as g
        d = {"series": {"a": [2, 4], "b": [5, 10]}}
        g.apply_normalize(d, "pct100", "line")
        self.assertEqual(d["series"]["a"], [100.0, 200.0])
        self.assertEqual(d["series"]["b"], [100.0, 200.0])

    def test_order_e2e_reorder_renders(self):
        with tempfile.TemporaryDirectory() as td:
            f = os.path.join(td, "d.json")
            io.open(f, "w", encoding="utf-8").write(json.dumps(
                {"labels": ["A", "B", "C"], "series": {"s1": [1, 2, 3], "s2": [2, 4, 6]}}))
            r = subprocess.run(
                [sys.executable, GEN, "-t", "bar", "-d", f,
                 "-o", os.path.join(td, "o.pdf"), "--order", "C,A,B", "--dpi", "80"],
                capture_output=True, text=True, timeout=240)
            self.assertEqual(r.returncode, 0, r.stderr[-300:])
            self.assertIn("order: 类别顺序已重排 → C → A → B", r.stderr)

    def test_order_wrong_chart_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            f = os.path.join(td, "h.json")
            io.open(f, "w", encoding="utf-8").write(json.dumps(
                {"matrix": [[1, 2], [3, 4]], "row_labels": ["r1", "r2"],
                 "x_labels": ["c1", "c2"]}))
            r = subprocess.run(
                [sys.executable, GEN, "-t", "heatmap", "-d", f,
                 "-o", os.path.join(td, "o.png"), "--order", "c2,c1", "--dpi", "80"],
                capture_output=True, text=True, timeout=240)
            self.assertEqual(r.returncode, 0, r.stderr[-300:])
            self.assertIn("[ignored] --order", r.stderr)

    def test_normalize_box_ignored(self):
        # 分布图不做均值归一（统计语义防错）
        with tempfile.TemporaryDirectory() as td:
            f = os.path.join(td, "b.json")
            io.open(f, "w", encoding="utf-8").write(json.dumps({"series": {"g1": [1, 2, 3]}}))
            r = subprocess.run(
                [sys.executable, GEN, "-t", "box", "-d", f,
                 "-o", os.path.join(td, "o.png"), "--normalize", "baseline", "--dpi", "80"],
                capture_output=True, text=True, timeout=240)
            self.assertEqual(r.returncode, 0, r.stderr[-300:])
            self.assertIn("[ignored] --normalize", r.stderr)

    def test_doctor_check_only_exit_codes(self):
        with tempfile.TemporaryDirectory() as td:
            f = os.path.join(td, "d.json")
            io.open(f, "w", encoding="utf-8").write(json.dumps(
                {"labels": ["A", "B"], "series": {"s": [1, 2]}}))
            r = subprocess.run(
                [sys.executable, GEN, "-t", "bar", "-d", f, "--doctor"],
                capture_output=True, text=True, timeout=240)
            self.assertEqual(r.returncode, 0, r.stderr[-300:])
            self.assertIn("doctor 完成", r.stderr)
            self.assertIn("未发现问题", r.stderr)
            r = subprocess.run(
                [sys.executable, GEN, "-t", "bar", "-d", f, "--doctor",
                 "--width", "5", "--journal", "nature"],
                capture_output=True, text=True, timeout=240)
            self.assertEqual(r.returncode, 1)
            self.assertIn("--width 将被覆盖", r.stderr)

    def test_doctor_with_render(self):
        with tempfile.TemporaryDirectory() as td:
            f = os.path.join(td, "d.json")
            io.open(f, "w", encoding="utf-8").write(json.dumps(
                {"labels": ["A", "B"], "series": {"s": [1, 2]}}))
            out = os.path.join(td, "o.pdf")
            r = subprocess.run(
                [sys.executable, GEN, "-t", "bar", "-d", f, "-o", out,
                 "--doctor", "--dpi", "80"],
                capture_output=True, text=True, timeout=240)
            self.assertEqual(r.returncode, 0, r.stderr[-300:])
            self.assertTrue(os.path.exists(out))
            self.assertIn("doctor 参数/环境体检", r.stderr)


class TestV30910(unittest.TestCase):
    """v3.9.1 文档准确性热修回归锁。"""

    @classmethod
    def setUpClass(cls):
        cls.root = os.path.dirname(SCRIPT_DIR)
        cls.zh = io.open(os.path.join(cls.root, "SKILL_ZH.md"), encoding="utf-8").read()
        cls.en = io.open(os.path.join(cls.root, "SKILL.md"), encoding="utf-8").read()
        cls.REF = os.path.join(cls.root, "references")

    def test_type_table_complete_22(self):
        """类型表须含全部 22 图型（v3.9.0 曾缺 5 种=docQuality 病灶）。"""
        for flag in ("-t grouped_bar", "-t paired", "-t bland_altman",
                     "-t pca", "-t funnel"):
            self.assertIn(flag, self.zh)
            self.assertIn(flag, self.en)

    def test_limits_flag_behavior_aligned(self):
        """limits §二行为列须与 [ignored] 透明化机制一致（boundary 靶点）。"""
        lm = io.open(os.path.join(self.REF, "limits.md"), encoding="utf-8").read()
        self.assertNotIn("中文报错 exit 1 |", lm.split("## 三")[0].split("--area")[1].split("\n")[0])
        self.assertIn("v3.7 起不再致命", lm)
        for row_key in ("--stats auto", "--egger", "--hatch"):
            for line in lm.splitlines():
                if row_key in line and "静默忽略" in line:
                    self.fail(f"limits.md 过时行为描述: {line}")
        self.assertEqual(lm.count("静默忽略"), 4,
                         "limits.md '静默忽略' 仅限 cmap/trend/show-ratio/lang 四行")
        for flag in ("--order", "--normalize", "--doctor", "--stats cox"):
            self.assertIn(flag, lm)

    def test_faq_covers_new_flags(self):
        """FAQ 须覆盖 v3.8/v3.9 新 flag（antiPatternFaq 靶点）。"""
        fq = io.open(os.path.join(self.REF, "faq.md"), encoding="utf-8").read()
        for kw in ("--pub-ready", "--order auto", "--normalize baseline", "--doctor"):
            self.assertIn(kw, fq)
        self.assertNotIn("其他图型静默忽略", fq)

    def test_quickstart_cheatsheet_guidance(self):
        qs = io.open(os.path.join(self.REF, "quickstart.md"), encoding="utf-8").read()
        self.assertIn("--pub-ready", qs)
        self.assertIn("--doctor", qs)
        cs = io.open(os.path.join(self.REF, "cheatsheet.md"), encoding="utf-8").read()
        self.assertIn("典型场景参数组合", cs)

    def test_references_all_listed(self):
        """文件结构节须列全 references 文档（progressive 靶点）。"""
        for kw in ("clinical-lab-trends", "composite-layouts",
                   "reverse-engineering-colors", "cheatsheet", "quickstart"):
            self.assertIn(kw, self.zh)
            self.assertIn(kw, self.en)

    def test_advanced_no_empty_dup_headings(self):
        ad = io.open(os.path.join(self.REF, "advanced.md"), encoding="utf-8").read()
        self.assertNotIn("(English)\n\n## Statistics", ad)
        self.assertNotIn("(English)\n\n## Submission", ad)


class TestV31000(unittest.TestCase):
    """v3.10.0：dual_axis 左柱右线重构（A1）+图例不遮挡（A2/A4）+单系列视觉增强。"""

    @classmethod
    def setUpClass(cls):
        import matplotlib
        matplotlib.use("Agg")
        sys.path.insert(0, SCRIPT_DIR)
        cls.g = __import__("gen_figure")

    def _theme(self):
        return {"colors": ["#3B6FA0", "#E6B84C"], "font_size": 9,
                "spines": ["top", "right"], "grid_alpha": 0.3,
                "colorblind_safe": False}

    def _dual_data(self):
        return {"labels": ["W1", "W4", "W8", "W12", "W19"],
                "left": {"样本量": [120, 118, 115, 110, 108]},
                "right": {"指标B占比": [45.2, 52.1, 58.3, 60.0, 61.5],
                          "指标C占比": [30.1, 33.5, 38.2, 40.1, 43.6]}}

    def test_dualaxis_left_bar_right_line(self):
        """A1：默认左轴柱+右轴虚线（经典形态）。"""
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(6, 4))
        ax2 = self.g.gen_dual_axis(self._dual_data(), ax, self._theme(), None)
        bars = [p for p in ax.patches
                if isinstance(p, matplotlib.patches.Rectangle) and p.get_height() > 0]
        self.assertGreaterEqual(len(bars), 5)
        self.assertEqual(len(ax2.get_lines()), 2)
        plt.close(fig)

    def test_dualaxis_type_override(self):
        """left_type/right_type 数据键可互换形态（向后兼容全折线用法）。"""
        import matplotlib.pyplot as plt
        d = dict(self._dual_data(), left_type="line", right_type="bar")
        fig, ax = plt.subplots(figsize=(6, 4))
        ax2 = self.g.gen_dual_axis(d, ax, self._theme(), None)
        rbars = [p for p in ax2.patches
                 if isinstance(p, matplotlib.patches.Rectangle) and p.get_height() > 0]
        self.assertGreaterEqual(len(rbars), 5)
        self.assertEqual(len(ax.get_lines()), 1)
        plt.close(fig)

    def test_dualaxis_legend_complete_no_occlusion(self):
        """A2+A4：图例含左右轴全部系列，且嵌入顶部预留带（不遮数据）。"""
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(6, 4))
        ax2 = self.g.gen_dual_axis(self._dual_data(), ax, self._theme(), None)
        l1 = ax.get_legend_handles_labels()[1]
        l2 = ax2.get_legend_handles_labels()[1]
        self.assertEqual(l1 + l2, ["样本量", "指标B占比", "指标C占比"])
        leg = ax.get_legend()
        self.assertIsNotNone(leg)
        self.assertEqual(len(leg.get_texts()), 3)
        self.assertFalse(leg.get_frame_on())
        self.assertGreater(ax.get_ylim()[1], 130)     # 左轴预留带
        self.assertGreater(ax2.get_ylim()[1], 67)     # 右轴预留带（峰值 61.5 之上）
        plt.close(fig)

    def test_dualaxis_legend_loc_override(self):
        """显式 --legend-loc 仍被尊重。"""
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(6, 4))
        self.g.gen_dual_axis(self._dual_data(), ax, self._theme(), None,
                             legend_loc="lower right")
        import matplotlib.legend as _mlg
        self.assertEqual(ax.get_legend()._loc, _mlg.Legend.codes["lower right"])
        plt.close(fig)

    def test_single_series_emphasis(self):
        """单系列：最大条强调色+其余淡化；斜纹豁免；图例抑制。"""
        import matplotlib.pyplot as plt
        d = {"labels": ["A", "B", "C", "D"], "series": {"n": [10, 40, 20, 30]}}
        fig, ax = plt.subplots(figsize=(5, 4))
        self.g.gen_bar(d, ax, self._theme(), None,
                       hatch=True, show_values=True)
        rects = [r for r in ax.patches if isinstance(r, matplotlib.patches.Rectangle)
                 and r.get_height() > 0]
        self.assertEqual(len(rects), 4)
        faces = {tuple(round(c, 4) for c in r.get_facecolor()) for r in rects}
        self.assertEqual(len(faces), 2, f"应有强调/淡化两色: {faces}")
        all_hatches = {r.get_hatch() for r in rects}
        self.assertEqual(all_hatches, {None}, "单系列斜纹应豁免")
        self.assertEqual(ax.get_legend_handles_labels()[1], [])
        plt.close(fig)

    def test_single_series_int_labels(self):
        """整数值标签不出现 .0（620 而非 620.0）。"""
        import matplotlib.pyplot as plt
        d = {"labels": ["A", "B"], "series": {"n": [620, 160]}}
        fig, ax = plt.subplots(figsize=(5, 4))
        self.g.gen_bar(d, ax, self._theme(), None, horizontal=True,
                       show_values=True)
        texts = [t.get_text() for t in ax.texts]
        self.assertIn("620", texts)
        self.assertNotIn("620.0", texts)
        plt.close(fig)

    def test_dualaxis_axis_floor(self):
        """left_floor/right_floor 轴下限覆盖（真金交付反馈采纳项）。"""
        import matplotlib.pyplot as plt
        d = self._dual_data()
        d["right_floor"] = 35
        fig, ax = plt.subplots(figsize=(6, 4))
        ax2 = self.g.gen_dual_axis(d, ax, self._theme(), None)
        self.assertAlmostEqual(ax2.get_ylim()[0], 35.0)
        self.assertGreater(ax2.get_ylim()[1], 61.5)  # 峰值仍完整可见
        plt.close(fig)

    def test_dualaxis_e2e_renders(self):
        with tempfile.TemporaryDirectory() as td:
            f = os.path.join(td, "d.json")
            io.open(f, "w", encoding="utf-8").write(json.dumps(self._dual_data(), ensure_ascii=False))
            r = subprocess.run(
                [sys.executable, GEN, "-t", "dual_axis", "-d", f,
                 "-o", os.path.join(td, "da.png"), "--dpi", "80"],
                capture_output=True, text=True, timeout=240)
            self.assertEqual(r.returncode, 0, r.stderr[-300:])
            self.assertTrue(os.path.exists(os.path.join(td, "da.png")))


class TestV40000(unittest.TestCase):
    """v4.0.0：模块化第四刀（af_shared/af_draw 拆分）+ 结构化标题栏（--subtitle/--source）。"""

    def test_modules_exist_and_importable(self):
        root = os.path.dirname(SCRIPT_DIR)
        for f in ("af_shared.py", "af_draw.py"):
            self.assertTrue(os.path.isfile(os.path.join(root, "scripts", f)), f)
        sys.path.insert(0, SCRIPT_DIR)
        import af_shared
        import af_draw
        import gen_figure
        self.assertTrue(hasattr(af_draw, "gen_km"))
        self.assertTrue(hasattr(af_draw, "GENERATORS"))
        # 再导出：import gen_figure 用法不变
        for name in ("gen_bar", "gen_dual_axis", "apply_base_style", "has_cjk",
                     "GENERATORS", "annotate_auto_stats", "pairwise_vs_first"):
            self.assertTrue(hasattr(gen_figure, name), name)
        # 主文件瘦身断言（模块化第四刀实效）
        gf_lines = sum(1 for _ in open(os.path.join(root, "scripts", "gen_figure.py"),
                                       encoding="utf-8"))
        # v5.8.0 松绑 3400→3600：subgroup --stats cox 一键亚组拟合入列；
        # 拆分计划见 docs/ADR-0001（af_draw/gen_figure 巨石模块分阶段拆解）
        self.assertLess(gf_lines, 3600, f"gen_figure.py 应 <3600 行（当前 {gf_lines}）")

    def test_subtitle_source_flags_render(self):
        with tempfile.TemporaryDirectory() as td:
            f = os.path.join(td, "d.json")
            io.open(f, "w", encoding="utf-8").write(json.dumps(
                {"labels": ["A", "B", "C"], "series": {"s": [1, 2, 3]}}))
            out = os.path.join(td, "s.png")
            r = subprocess.run(
                [sys.executable, GEN, "-t", "bar", "-d", f, "-o", out, "--dpi", "80",
                 "--title", "主标题", "--subtitle", "副标题 n=3", "--source", "数据来源：测试"],
                capture_output=True, text=True, timeout=240)
            self.assertEqual(r.returncode, 0, r.stderr[-300:])
            self.assertTrue(os.path.exists(out))

    def test_dualaxis_with_subtitle_source(self):
        with tempfile.TemporaryDirectory() as td:
            f = os.path.join(td, "d.json")
            io.open(f, "w", encoding="utf-8").write(json.dumps(
                {"labels": ["W1", "W2"], "left": {"样本量": [100, 98]},
                 "right": {"占比": [45.0, 50.5]}}, ensure_ascii=False))
            out = os.path.join(td, "da.png")
            r = subprocess.run(
                [sys.executable, GEN, "-t", "dual_axis", "-d", f, "-o", out, "--dpi", "80",
                 "--subtitle", "双轴副标题", "--source", "来源：测试"],
                capture_output=True, text=True, timeout=240)
            self.assertEqual(r.returncode, 0, r.stderr[-300:])


class TestV40100(unittest.TestCase):
    """v4.1.0：composite 自动面板标签（panel_labels）+ --summary 自动数据摘要副标题。"""

    @classmethod
    def setUpClass(cls):
        import matplotlib
        matplotlib.use("Agg")
        sys.path.insert(0, SCRIPT_DIR)
        cls.g = __import__("gen_figure")
        cls.theme = {"colors": ["#3B6FA0", "#E6B84C"], "font_size": 9,
                     "spines": ["top", "right"], "grid_alpha": 0.3,
                     "colorblind_safe": False}

    def _composite(self, panel_labels):
        return {"layout": [1, 2], "panel_labels": panel_labels,
                "panels": [
                    {"pos": [0, 1], "type": "bar", "data": {"labels": ["a"], "series": {"s": [1]}}},
                    {"pos": [0, 0], "type": "bar", "data": {"labels": ["a"], "series": {"s": [1]}}}]}

    def test_panel_labels_row_major(self):
        """乱序 pos 也按行序标 A/B（期刊规范）。"""
        import matplotlib.text as mtext
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(6, 3))
        self.g.gen_composite(self._composite(True), ax, self.theme, None)
        labs = [t.get_text() for t in fig.findobj(mtext.Text) if t.get_text() in ("A", "B")]
        self.assertEqual(sorted(labs), ["A", "B"])
        plt.close(fig)

    def test_panel_labels_custom_seq(self):
        import matplotlib.text as mtext
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(6, 3))
        self.g.gen_composite(self._composite(["I", "II"]), ax, self.theme, None)
        labs = [t.get_text() for t in fig.findobj(mtext.Text) if t.get_text() in ("I", "II")]
        self.assertEqual(sorted(labs), ["I", "II"])
        plt.close(fig)

    def test_panel_labels_default_off(self):
        """缺省关闭——兼容既有图（手写 Panel A 标题不重复标注）。"""
        import matplotlib.text as mtext
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(6, 3))
        d = self._composite(False)
        del d["panel_labels"]
        self.g.gen_composite(d, ax, self.theme, None)
        labs = [t.get_text() for t in fig.findobj(mtext.Text) if t.get_text() in ("A", "B")]
        self.assertEqual(labs, [])
        plt.close(fig)

    def test_summary_bar(self):
        with tempfile.TemporaryDirectory() as td:
            f = os.path.join(td, "d.json")
            io.open(f, "w", encoding="utf-8").write(json.dumps(
                {"labels": ["A", "B"], "series": {"s": [1, 2]}}))
            r = subprocess.run(
                [sys.executable, GEN, "-t", "bar", "-d", f,
                 "-o", os.path.join(td, "s.png"), "--dpi", "70", "--summary"],
                capture_output=True, text=True, timeout=240)
            self.assertEqual(r.returncode, 0, r.stderr[-250:])
            self.assertIn("summary: n=2 · 2 组", r.stderr)

    def test_summary_km_events(self):
        with tempfile.TemporaryDirectory() as td:
            f = os.path.join(td, "k.json")
            io.open(f, "w", encoding="utf-8").write(json.dumps(
                {"groups": {"对照": [[3, 1], [5, 0]], "处理": [[4, 1], [6, 1]]}}))
            r = subprocess.run(
                [sys.executable, GEN, "-t", "km", "-d", f,
                 "-o", os.path.join(td, "k.png"), "--dpi", "70", "--summary"],
                capture_output=True, text=True, timeout=240)
            self.assertEqual(r.returncode, 0, r.stderr[-250:])
            self.assertIn("n=4 例 · 事件 3 例 · 2 组", r.stderr)


class TestV42000(unittest.TestCase):
    """v4.2.0：slope 斜率图（第 23 种图型）+ --peak-label 峰值自动注记。"""

    def test_slope_e2e_renders(self):
        with tempfile.TemporaryDirectory() as td:
            f = os.path.join(td, "s.json")
            io.open(f, "w", encoding="utf-8").write(json.dumps(
                {"left_label": "基线", "right_label": "12 周",
                 "items": {"药物A": [72, 85], "药物B": [65, 61], "对照": [70, 71]}},
                ensure_ascii=False))
            out = os.path.join(td, "slope.png")
            r = subprocess.run(
                [sys.executable, GEN, "-t", "slope", "-d", f, "-o", out, "--dpi", "80",
                 "--title", "治疗前后", "--cjk"],
                capture_output=True, text=True, timeout=240)
            self.assertEqual(r.returncode, 0, r.stderr[-300:])
            self.assertTrue(os.path.exists(out))

    def test_slope_validate_fatals(self):
        with tempfile.TemporaryDirectory() as td:
            f = os.path.join(td, "b1.json")
            io.open(f, "w", encoding="utf-8").write(json.dumps({"items": {"A": [1]}}))
            r = subprocess.run([sys.executable, GEN, "-t", "slope", "-d", f,
                                "-o", os.path.join(td, "x.png")],
                               capture_output=True, text=True, timeout=120)
            self.assertEqual(r.returncode, 1)
            self.assertIn("至少 2 项", r.stderr)
            f2 = os.path.join(td, "b2.json")
            io.open(f2, "w", encoding="utf-8").write(json.dumps(
                {"items": {"A": [1, 2], "B": [1, 2, 3]}}))
            r = subprocess.run([sys.executable, GEN, "-t", "slope", "-d", f2,
                                "-o", os.path.join(td, "y.png")],
                               capture_output=True, text=True, timeout=120)
            self.assertEqual(r.returncode, 1)
            self.assertIn("恰好 2 个数值", r.stderr)

    def test_slope_registry_count_23(self):
        import gen_figure
        n = len(gen_figure.GENERATORS) - 3  # 纯别名 horizontal_bar/boxplot/survival
        self.assertGreaterEqual(n, 23, f"注册表图型数: {n}")

    def test_peak_label_bar_line(self):
        with tempfile.TemporaryDirectory() as td:
            f = os.path.join(td, "d.json")
            io.open(f, "w", encoding="utf-8").write(json.dumps(
                {"labels": ["W1", "W2", "W3", "W4"], "series": {"量": [10, 40, 25, 30]}}))
            for t in ("bar", "line"):
                r = subprocess.run(
                    [sys.executable, GEN, "-t", t, "-d", f,
                     "-o", os.path.join(td, f"p_{t}.png"), "--dpi", "70", "--peak-label"],
                    capture_output=True, text=True, timeout=240)
                self.assertEqual(r.returncode, 0, r.stderr[-250:])
                self.assertIn("峰值 40（W2）", r.stderr)


class TestV43000(unittest.TestCase):
    """v4.3.0：--profile 分层配置文件 + composite span 跨行跨列。"""

    def test_profile_applies_cli_wins(self):
        with tempfile.TemporaryDirectory() as td:
            prof = os.path.join(td, "p.json")
            io.open(prof, "w", encoding="utf-8").write(json.dumps(
                {"theme": "okabe-ito", "dpi": 150, "verify": True, "unknown_key": 1}))
            f = os.path.join(td, "d.json")
            io.open(f, "w", encoding="utf-8").write(json.dumps(
                {"labels": ["A", "B"], "series": {"s": [1, 2]}}))
            out = os.path.join(td, "s.pdf")
            r = subprocess.run(
                [sys.executable, GEN, "-t", "bar", "-d", f, "-o", out,
                 "--profile", prof],
                capture_output=True, text=True, timeout=240)
            self.assertEqual(r.returncode, 0, r.stderr[-250:])
            self.assertIn("[profile] 已应用 3 项默认", r.stderr)  # unknown_key 单独警告
            self.assertIn("unknown_key", r.stderr)
            self.assertTrue(os.path.exists(out))  # verify=True 走 PDF 校验并成功

    def test_profile_bad_file_fatal(self):
        with tempfile.TemporaryDirectory() as td:
            prof = os.path.join(td, "p.json")
            io.open(prof, "w", encoding="utf-8").write("[1, 2]")
            f = os.path.join(td, "d.json")
            io.open(f, "w", encoding="utf-8").write(json.dumps(
                {"labels": ["A"], "series": {"s": [1]}}))
            r = subprocess.run(
                [sys.executable, GEN, "-t", "bar", "-d", f, "-o", os.path.join(td, "s.png"),
                 "--profile", prof],
                capture_output=True, text=True, timeout=120)
            self.assertEqual(r.returncode, 1)
            self.assertIn("JSON 对象", r.stderr)

    def test_composite_span_render(self):
        with tempfile.TemporaryDirectory() as td:
            f = os.path.join(td, "c.json")
            io.open(f, "w", encoding="utf-8").write(json.dumps(
                {"layout": [2, 2], "panels": [
                    {"pos": [0, 0], "span": [2, 1], "type": "bar",
                     "data": {"labels": ["a"], "series": {"s": [1]}}},
                    {"pos": [0, 1], "type": "box",
                     "data": {"labels": ["g"], "series": {"g": [1, 2, 3]}}},
                    {"pos": [1, 1], "type": "line",
                     "data": {"labels": ["x", "y"], "series": {"s": [1, 2]}}}]},
                ensure_ascii=False))
            out = os.path.join(td, "c.png")
            r = subprocess.run(
                [sys.executable, GEN, "-t", "composite", "-d", f, "-o", out, "--dpi", "70"],
                capture_output=True, text=True, timeout=240)
            self.assertEqual(r.returncode, 0, r.stderr[-250:])
            self.assertTrue(os.path.exists(out))

    def test_composite_span_overlap_fatal(self):
        with tempfile.TemporaryDirectory() as td:
            f = os.path.join(td, "c.json")
            io.open(f, "w", encoding="utf-8").write(json.dumps(
                {"layout": [1, 2], "panels": [
                    {"pos": [0, 0], "span": [1, 2], "type": "bar",
                     "data": {"labels": ["a"], "series": {"s": [1]}}},
                    {"pos": [0, 1], "type": "bar",
                     "data": {"labels": ["a"], "series": {"s": [1]}}}]},
                ensure_ascii=False))
            r = subprocess.run(
                [sys.executable, GEN, "-t", "composite", "-d", f,
                 "-o", os.path.join(td, "c.png")],
                capture_output=True, text=True, timeout=120)
            self.assertEqual(r.returncode, 1)
            self.assertIn("占位重叠", r.stderr)


class TestV44000(unittest.TestCase):
    """v4.4.0：volcano 火山图（第 24 种图型，组学差异表达）。"""

    def test_volcano_e2e_renders(self):
        with tempfile.TemporaryDirectory() as td:
            f = os.path.join(td, "v.json")
            io.open(f, "w", encoding="utf-8").write(json.dumps(
                {"log2fc": [-2.3, -1.4, -1.1, -0.4, 0.2, 0.6, 1.2, 1.6, 2.4],
                 "pvalue": [3e-4, 0.012, 0.04, 0.5, 0.8, 0.3, 0.02, 3e-5, 8e-7],
                 "names": ["SLC6A4", "MAOA", "FKBP5", "NR3C1", "CRH", "AVP",
                           "BDNF", "COMT", "HTR1A"]}, ensure_ascii=False))
            out = os.path.join(td, "v.png")
            r = subprocess.run(
                [sys.executable, GEN, "-t", "volcano", "-d", f, "-o", out,
                 "--dpi", "80", "--cjk"],
                capture_output=True, text=True, timeout=240)
            self.assertEqual(r.returncode, 0, r.stderr[-250:])
            self.assertTrue(os.path.exists(out))

    def test_volcano_validate_fatals(self):
        with tempfile.TemporaryDirectory() as td:
            cases = [
                ({"log2fc": [1], "pvalue": [0.1, 0.2]}, "等长"),
                ({"log2fc": [1], "pvalue": [0]}, "(0, 1]"),
                ({"log2fc": [1], "pvalue": [0.1], "names": ["A", "B"]}, "names"),
            ]
            for i, (data, want) in enumerate(cases):
                f = os.path.join(td, f"b{i}.json")
                io.open(f, "w", encoding="utf-8").write(json.dumps(data))
                r = subprocess.run(
                    [sys.executable, GEN, "-t", "volcano", "-d", f,
                     "-o", os.path.join(td, f"x{i}.png")],
                    capture_output=True, text=True, timeout=120)
                self.assertEqual(r.returncode, 1, r.stderr[-200:])
                self.assertIn(want, r.stderr)

    def test_volcano_registry_count_24(self):
        import gen_figure
        n = len(gen_figure.GENERATORS) - 3
        self.assertGreaterEqual(n, 24, f"注册表图型数: {n}")

    def test_volcano_demo_data_valid(self):
        """DEMO_DATA 的 volcano 条目可直接渲染（--demo 交互流的非交互等价验证）。"""
        import gen_figure
        demo = gen_figure.DEMO_DATA["volcano"]
        with tempfile.TemporaryDirectory() as td:
            f = os.path.join(td, "d.json")
            io.open(f, "w", encoding="utf-8").write(json.dumps(demo, ensure_ascii=False))
            r = subprocess.run(
                [sys.executable, GEN, "-t", "volcano", "-d", f,
                 "-o", os.path.join(td, "v.png"), "--dpi", "70"],
                capture_output=True, text=True, timeout=240)
            self.assertEqual(r.returncode, 0, r.stderr[-250:])


class TestV46000(unittest.TestCase):
    """v4.6.0：ROC 默认语义轴标题 + venn n 消歧 + 聚类热图行标签自适应字号（P3 打磨包）。"""

    def test_roc_default_axis_titles(self):
        """ROC 未显式给轴标题时默认 FPR/TPR 语义轴标题（期刊级）。"""
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        sys.path.insert(0, SCRIPT_DIR)
        g = __import__("gen_figure")
        d = {"curves": [{"name": "M", "fpr": [0, 0.5, 1], "tpr": [0, 0.8, 1]}]}
        fig, ax = plt.subplots(figsize=(5, 5))
        g.gen_roc(d, ax, {"colors": ["#3B6FA0"], "font_size": 9, "spines": ["top", "right"],
                          "grid_alpha": 0.3, "colorblind_safe": False}, None)
        self.assertIn("False Positive Rate", ax.get_xlabel())
        self.assertIn("True Positive Rate", ax.get_ylabel())
        plt.close(fig)

    def test_venn_title_disambiguation(self):
        """venn 无 title 时标题明示"并集元素数"（不再裸数字 n=3 易误读为例数）。"""
        with tempfile.TemporaryDirectory() as td:
            f = os.path.join(td, "v.json")
            io.open(f, "w", encoding="utf-8").write(json.dumps(
                {"sets": {"A": [1, 2, 3], "B": [2, 3, 4]}}))
            out = os.path.join(td, "v.png")
            r = subprocess.run(
                [sys.executable, GEN, "-t", "venn", "-d", f, "-o", out, "--dpi", "70"],
                capture_output=True, text=True, timeout=240)
            self.assertEqual(r.returncode, 0, r.stderr[-250:])
            self.assertTrue(os.path.exists(out))

    def test_cluster_heatmap_label_font_adaptive(self):
        """聚类热图行标签字号随行数自适应缩小（>60 行缩 1 号）。"""
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        sys.path.insert(0, SCRIPT_DIR)
        import af_v23_charts as ac
        d = {"matrix": [[i % 7 + j * 0.1 for j in range(6)] for i in range(80)],
             "row_labels": [f"基因{i}" for i in range(80)]}
        fig, ax = plt.subplots(figsize=(6, 6))
        ac.gen_cluster_heatmap(d, ax, {"colors": ["#3B6FA0"], "font_size": 9,
                                       "spines": ["top", "right"], "grid_alpha": 0.3,
                                       "colorblind_safe": False}, None)
        labels = ax.get_yticklabels()
        self.assertTrue(labels)
        fs = labels[0].get_fontsize()
        self.assertLessEqual(fs, 6.5, f"80 行时标签字号应缩小：{fs}")
        plt.close(fig)


class TestV47000(unittest.TestCase):
    """v4.7.0：upset 集合交集图（第 25 种图型）+ 场景化指导文档 + 真实科研场景示例。"""

    def test_upset_e2e_renders(self):
        with tempfile.TemporaryDirectory() as td:
            f = os.path.join(td, "u.json")
            io.open(f, "w", encoding="utf-8").write(json.dumps(
                {"title": "五平台基因集交集",
                 "sets": {"转录组": ["TP53", "KRAS", "EGFR", "BRCA1", "MYC", "PTEN"],
                          "蛋白组": ["TP53", "EGFR", "MYC", "AKT1"],
                          "甲基化": ["TP53", "BRCA1", "CDKN2A"],
                          "eQTL": ["KRAS", "EGFR", "PTEN", "AKT1"],
                          "GWAS": ["TP53", "BRCA1", "ERBB2", "ATM"]}}, ensure_ascii=False))
            out = os.path.join(td, "u.png")
            r = subprocess.run(
                [sys.executable, GEN, "-t", "upset", "-d", f, "-o", out,
                 "--dpi", "80", "--cjk"],
                capture_output=True, text=True, timeout=240)
            self.assertEqual(r.returncode, 0, r.stderr[-250:])
            self.assertTrue(os.path.exists(out))
            self.assertIn("upset:", r.stderr)

    def test_upset_validate_fatals(self):
        with tempfile.TemporaryDirectory() as td:
            cases = [
                ({"sets": {"A": [1, 2]}}, "≥2 个集合"),
                ({"sets": {"A": [1, 2], "B": []}}, "非空元素列表"),
                ({"sets": {"A": [1, 2], "B": [True, 3]}}, "不支持的元素类型"),
                ({"sets": {"A": [1, 2], "B": [2, 3]}, "top_n": 99}, "top_n"),
                ({"sets": {"A": [1, 2], "B": [2, 3]}, "sort": "big"}, "sort"),
                ({"sets": {"A%d" % i: [1, 2] for i in range(31)}}, "超上限 30"),
            ]
            for i, (data, want) in enumerate(cases):
                f = os.path.join(td, f"b{i}.json")
                io.open(f, "w", encoding="utf-8").write(json.dumps(data))
                r = subprocess.run(
                    [sys.executable, GEN, "-t", "upset", "-d", f,
                     "-o", os.path.join(td, f"x{i}.png")],
                    capture_output=True, text=True, timeout=120)
                self.assertEqual(r.returncode, 1, (i, r.stderr[-200:]))
                self.assertIn(want, r.stderr)

    def test_upset_registry_count_25(self):
        import gen_figure
        sys.path.insert(0, SCRIPT_DIR)
        n = len(gen_figure.GENERATORS) - 3
        self.assertGreaterEqual(n, 25, f"注册表图型数: {n}")

    def test_upset_demo_data_valid(self):
        """DEMO_DATA 的 upset 条目可直接渲染（--demo 交互流的非交互等价验证）。"""
        import gen_figure
        sys.path.insert(0, SCRIPT_DIR)
        demo = gen_figure.DEMO_DATA["upset"]
        with tempfile.TemporaryDirectory() as td:
            f = os.path.join(td, "d.json")
            io.open(f, "w", encoding="utf-8").write(json.dumps(demo, ensure_ascii=False))
            r = subprocess.run(
                [sys.executable, GEN, "-t", "upset", "-d", f,
                 "-o", os.path.join(td, "u.png"), "--dpi", "70"],
                capture_output=True, text=True, timeout=240)
            self.assertEqual(r.returncode, 0, r.stderr[-250:])

    def test_upset_suggest_routing(self):
        """--suggest：≥5 集合数据推 upset，2 集合仍推 venn。"""
        with tempfile.TemporaryDirectory() as td:
            f5 = os.path.join(td, "s5.json")
            io.open(f5, "w", encoding="utf-8").write(json.dumps(
                {"sets": {"S%d" % i: [1, 2, 3, 4, 5] for i in range(5)}}))
            r = subprocess.run(
                [sys.executable, GEN, "--suggest", "-d", f5],
                capture_output=True, text=True, timeout=120)
            self.assertEqual(r.returncode, 0, r.stderr[-200:])
            self.assertIn("upset", r.stdout)
            f2 = os.path.join(td, "s2.json")
            io.open(f2, "w", encoding="utf-8").write(json.dumps(
                {"sets": {"A": [1, 2, 3], "B": [2, 3, 4]}}))
            r2 = subprocess.run(
                [sys.executable, GEN, "--suggest", "-d", f2],
                capture_output=True, text=True, timeout=120)
            self.assertEqual(r2.returncode, 0, r2.stderr[-200:])
            self.assertIn("venn", r2.stdout)

    def test_upset_composite_panel_rejected(self):
        """composite 面板 type=upset 前置拒绝（自管类型不可作面板，exit 1 中文报错）。"""
        with tempfile.TemporaryDirectory() as td:
            f = os.path.join(td, "c.json")
            io.open(f, "w", encoding="utf-8").write(json.dumps(
                {"layout": [1, 2], "panels": [
                    {"type": "bar", "data": {"labels": ["A", "B"], "series": {"s": [1, 2]}},
                     "title": "Panel A", "pos": [0, 0]},
                    {"type": "upset", "data": {"sets": {"X": [1, 2], "Y": [2, 3]}},
                     "title": "Panel B", "pos": [0, 1]}]}, ensure_ascii=False))
            r = subprocess.run(
                [sys.executable, GEN, "-t", "composite", "-d", f,
                 "-o", os.path.join(td, "c.png")],
                capture_output=True, text=True, timeout=120)
            self.assertEqual(r.returncode, 1, r.stderr[-200:])
            self.assertIn("upset", r.stderr)
            self.assertIn("无效", r.stderr)


    def test_upset_variant_paths(self):
        """degree 排序/min_size 过滤/截断事实行/过滤后无交集报错（审查 D-18①②③）。"""
        with tempfile.TemporaryDirectory() as td:
            f = os.path.join(td, "v.json")
            io.open(f, "w", encoding="utf-8").write(json.dumps(
                {"sets": {"A": [1, 2, 3, 4], "B": [2, 3, 4], "C": [3, 4, 5],
                          "D": [5, 6], "E": [6, 7, 8]},
                 "sort": "degree", "top_n": 3, "min_size": 1}))
            out = os.path.join(td, "v.png")
            r = subprocess.run(
                [sys.executable, GEN, "-t", "upset", "-d", f, "-o", out, "--dpi", "70"],
                capture_output=True, text=True, timeout=240)
            self.assertEqual(r.returncode, 0, r.stderr[-250:])
            self.assertIn("sort=degree", r.stderr)
            self.assertIn("另有", r.stderr)  # 截断事实行
            self.assertTrue(os.path.exists(out))
            # min_size 过滤到空 → 报错并给出当前最大交集
            f2 = os.path.join(td, "m.json")
            io.open(f2, "w", encoding="utf-8").write(json.dumps(
                {"sets": {"A": [1, 2], "B": [2, 3]}, "min_size": 10}))
            r2 = subprocess.run(
                [sys.executable, GEN, "-t", "upset", "-d", f2,
                 "-o", os.path.join(td, "m.png")],
                capture_output=True, text=True, timeout=120)
            # ValueError 属数据/格式错 → 退出码 3（v4.5.0 语义：与参数校验 1 分离）
            self.assertEqual(r2.returncode, 3, r2.stderr[-200:])
            self.assertIn("过滤后无交集", r2.stderr)
            self.assertIn("当前最大交集为 1", r2.stderr)

    def test_upset_bar_matrix_alignment(self):
        """几何断言：交集柱轴与点阵轴同列同宽（审查 D-18④，防 D-04 回潮）。"""
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        sys.path.insert(0, SCRIPT_DIR)
        import af_draw
        d = {"sets": {"A": [1, 2, 3], "B": [2, 3, 4], "C": [3, 4, 5],
                      "D": [4, 5, 6], "E": [1, 5, 6]}}
        fig, ax = plt.subplots(figsize=(10, 6))
        af_draw.gen_upset(d, ax, {"colors": ["#2E5E8F"], "font_size": 9}, None)
        fig.canvas.draw()
        isz, mat = fig.axes[0].get_position(), fig.axes[1].get_position()
        plt.close(fig)
        self.assertLess(abs(isz.x0 - mat.x0), 1e-6,
                        f"交集柱轴 x0 {isz.x0} 与点阵轴 x0 {mat.x0} 错位（D-04 回潮）")
        self.assertLess(abs(isz.width - mat.width), 1e-6, "交集柱轴与点阵轴宽度不一致")

    def test_upset_realworld_commands_run(self):
        """examples/realworld 三条 _command 逐字可执行（审查 D-18⑤，防 --top 类回归）。"""
        base = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))
        for name in ("realworld_rct_response.json", "realworld_omics_volcano.json",
                     "realworld_survival_km.json", "realworld_oncology_waterfall.json"):
            fp = os.path.join(base, "examples", "realworld", name)
            d = json.load(io.open(fp, encoding="utf-8"))
            cmd = d["_command"]
            # 相对路径以仓库根为工作目录执行；-o 落到临时目录
            with tempfile.TemporaryDirectory() as td:
                cmd = cmd.rsplit("-o ", 1)[0] + "-o " + os.path.join(td, "out.png")
                r = subprocess.run(cmd, shell=True, cwd=base,
                                   capture_output=True, text=True, timeout=240)
                self.assertEqual(r.returncode, 0, f"{name}: {r.stderr[-250:]}")
                self.assertTrue(os.path.exists(os.path.join(td, "out.png")), name)


    def test_v470_scenario_docs_and_realworld(self):
        """场景化指导文档（color-guide/combo-guide）与真实科研场景示例在包内。"""
        base = os.path.join(SCRIPT_DIR, "..")
        for rel in ("references/color-guide.md", "references/combo-guide.md"):
            self.assertTrue(os.path.isfile(os.path.join(base, rel)), f"{rel} 缺失")
        rw = os.path.join(base, "examples", "realworld")
        self.assertTrue(os.path.isdir(rw), "examples/realworld/ 缺失")
        rws = sorted(os.listdir(rw))
        self.assertEqual(len([x for x in rws if x.endswith(".json")]), 17,
                         f"realworld 示例应 17 个: {rws}")


class TestV48000(unittest.TestCase):
    """v4.8.0：waterfall 肿瘤缓解瀑布图（第 26 种图型，RECIST 着色+阈值线）。"""

    def test_waterfall_e2e_group(self):
        """分组（RECIST）模式 E2E：渲染+stderr 事实行。"""
        with tempfile.TemporaryDirectory() as td:
            f = os.path.join(td, "w.json")
            io.open(f, "w", encoding="utf-8").write(json.dumps(
                {"change": [-72.4, -65.1, -58.9, -52.0, -47.3, -41.8, -35.6, -32.1,
                            -28.7, -24.5, -19.8, -15.2, -8.9, -3.4, 0.8, 12.6, 25.3, 41.7],
                 "names": [f"P{i:03d}" for i in range(1, 19)],
                 "group": ["PR", "CR", "PR", "PR", "PR", "PR", "PR", "PR",
                           "PR", "PR", "SD", "SD", "SD", "SD", "SD", "PD", "PD", "PD"]},
                ensure_ascii=False))
            out = os.path.join(td, "w.png")
            r = subprocess.run(
                [sys.executable, GEN, "-t", "waterfall", "-d", f, "-o", out,
                 "--dpi", "80", "--cjk"],
                capture_output=True, text=True, timeout=240)
            self.assertEqual(r.returncode, 0, r.stderr[-250:])
            self.assertTrue(os.path.exists(out))
            self.assertIn("waterfall: 18 例", r.stderr)
            self.assertIn("PD 3", r.stderr)

    def test_waterfall_bucket_and_show_values(self):
        """阈值分桶模式（无 group）+ --show-values 渲染成功。"""
        with tempfile.TemporaryDirectory() as td:
            f = os.path.join(td, "b.json")
            io.open(f, "w", encoding="utf-8").write(json.dumps(
                {"change": [-72.4, -58.9, -52.0, -41.8, -32.1, -28.7,
                            -19.8, -8.9, -3.4, 0.8, 12.6, 41.7]}))
            out = os.path.join(td, "b.png")
            r = subprocess.run(
                [sys.executable, GEN, "-t", "waterfall", "-d", f, "-o", out,
                 "--dpi", "80", "--show-values"],
                capture_output=True, text=True, timeout=240)
            self.assertEqual(r.returncode, 0, r.stderr[-250:])
            self.assertTrue(os.path.exists(out))

    def test_waterfall_validate_fatals(self):
        with tempfile.TemporaryDirectory() as td:
            cases = [
                ({}, "需要 'change'"),
                ({"change": []}, "非空数值数组"),
                ({"change": [1, "x"]}, "必须全为数值"),
                ({"change": [-1, 2], "names": ["a"]}, "names"),
                ({"change": [-1, 2], "group": ["PR"]}, "group"),
                ({"change": [-1, 2], "pr_cut": 30}, "pr_cut 必须为负数"),
                ({"change": [-1, 2], "pd_cut": -20}, "pd_cut 必须为正数"),
                ({"change": [-1, 2], "sort": "asc"}, "sort"),
            ]
            for i, (data, want) in enumerate(cases):
                f = os.path.join(td, f"b{i}.json")
                io.open(f, "w", encoding="utf-8").write(json.dumps(data))
                r = subprocess.run(
                    [sys.executable, GEN, "-t", "waterfall", "-d", f,
                     "-o", os.path.join(td, f"x{i}.png")],
                    capture_output=True, text=True, timeout=120)
                self.assertEqual(r.returncode, 1, (i, r.stderr[-200:]))
                self.assertIn(want, r.stderr)

    def test_waterfall_registry_count_26(self):
        import gen_figure
        sys.path.insert(0, SCRIPT_DIR)
        n = len(gen_figure.GENERATORS) - 3
        self.assertGreaterEqual(n, 26, f"注册表图型数: {n}")

    def test_waterfall_demo_data_valid(self):
        """DEMO_DATA 的 waterfall 条目可直接渲染。"""
        import gen_figure
        sys.path.insert(0, SCRIPT_DIR)
        demo = gen_figure.DEMO_DATA["waterfall"]
        with tempfile.TemporaryDirectory() as td:
            f = os.path.join(td, "d.json")
            io.open(f, "w", encoding="utf-8").write(json.dumps(demo, ensure_ascii=False))
            r = subprocess.run(
                [sys.executable, GEN, "-t", "waterfall", "-d", f,
                 "-o", os.path.join(td, "w.png"), "--dpi", "70"],
                capture_output=True, text=True, timeout=240)
            self.assertEqual(r.returncode, 0, r.stderr[-250:])

    def test_waterfall_sort_input(self):
        """sort=input 保持给定顺序（不重排）；stderr 标明 sort=input。"""
        with tempfile.TemporaryDirectory() as td:
            f = os.path.join(td, "s.json")
            io.open(f, "w", encoding="utf-8").write(json.dumps(
                {"change": [10.0, -50.0, 5.0, -20.0], "names": ["甲", "乙", "丙", "丁"],
                 "sort": "input"}))
            out = os.path.join(td, "s.png")
            r = subprocess.run(
                [sys.executable, GEN, "-t", "waterfall", "-d", f, "-o", out,
                 "--dpi", "70"],
                capture_output=True, text=True, timeout=240)
            self.assertEqual(r.returncode, 0, r.stderr[-250:])
            self.assertIn("sort=input", r.stderr)
            self.assertTrue(os.path.exists(out))

    def test_waterfall_long_labels_vertical(self):
        """>14 例时 x 标签竖排（旋转 90°，不丢标签）。"""
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        sys.path.insert(0, SCRIPT_DIR)
        import af_draw
        n = 20
        d = {"change": [(-1) ** i * (5 + i % 9) * 3.0 for i in range(n)],
             "names": [f"PT-{i:02d}" for i in range(1, n + 1)]}
        fig, ax = plt.subplots(figsize=(10, 6))
        af_draw.gen_waterfall(d, ax, {"colors": ["#2E5E8F"], "font_size": 9,
                                      "spines": ["top", "right"], "grid_alpha": 0.3}, None)
        labels = ax.get_xticklabels()
        plt.close(fig)
        self.assertEqual(len(labels), n, "标签数量=例数（竖排不丢标签）")
        self.assertAlmostEqual(labels[0].get_rotation(), 90.0, msg=">14 例应竖排")

    def test_waterfall_no_false_field_warnings(self):
        """官方字段 change/pr_cut/pd_cut 不触发「无效字段」假警告（审查 P1-1 回潮锁）。"""
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(SCRIPT_DIR, "..", "templates", "waterfall.json")
            out = os.path.join(td, "w.png")
            r = subprocess.run(
                [sys.executable, GEN, "-t", "waterfall", "-d", src, "-o", out,
                 "--dpi", "70", "--cjk"],
                capture_output=True, text=True, timeout=240)
            self.assertEqual(r.returncode, 0, r.stderr[-250:])
            self.assertNotIn("不是有效字段", r.stderr, "官方字段不得触发假警告")

    def test_waterfall_alt_text_informative(self):
        """--alt 含例数与类别计数（审查 P1-2 回潮锁；26 种图型不允许零信息 alt）。"""
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(SCRIPT_DIR, "..", "examples", "realworld",
                               "realworld_oncology_waterfall.json")
            out = os.path.join(td, "w.png")
            r = subprocess.run(
                [sys.executable, GEN, "-t", "waterfall", "-d", src, "-o", out,
                 "--alt", "--dpi", "70"],
                capture_output=True, text=True, timeout=240)
            self.assertEqual(r.returncode, 0, r.stderr[-250:])
            alt = io.open(out.rsplit(".", 1)[0] + ".alt.txt",
                          encoding="utf-8").read()
            self.assertIn("26 例", alt)
            self.assertIn("PR", alt)

    def test_waterfall_threshold_equality_and_nan(self):
        """阈值等值归类（-30=缓解、20=进展）+ NaN/Inf 拒绝（审查 P2-3/P3-6）。"""
        import gen_figure
        sys.path.insert(0, SCRIPT_DIR)
        d = {"change": [-30.0, 20.0, -29.9999, 19.9999]}
        fig, ax = plt.subplots(figsize=(8, 5)) if False else (None, None)
        fatals = gen_figure.validate_waterfall(d)
        self.assertEqual(fatals, [])
        fatals2 = gen_figure.validate_waterfall(
            {"change": [float("nan"), -40, float("inf"), -35]})
        self.assertTrue(any("非有限值" in f for f in fatals2))
        # 等值归类走生成器内部语义（≤ / ≥）——直接渲染断言 stderr 桶计数
        with tempfile.TemporaryDirectory() as td:
            f = os.path.join(td, "e.json")
            io.open(f, "w", encoding="utf-8").write(json.dumps(
                {"change": [-30.0, 20.0, -29.9999, 19.9999]}))
            out = os.path.join(td, "e.png")
            r = subprocess.run(
                [sys.executable, GEN, "-t", "waterfall", "-d", f, "-o", out,
                 "--dpi", "70"],
                capture_output=True, text=True, timeout=240)
            self.assertEqual(r.returncode, 0, r.stderr[-250:])
            self.assertIn("≤-30: 1", r.stderr)
            self.assertIn("(-30, 20): 2", r.stderr)
            self.assertIn("≥20: 1", r.stderr)

    def test_waterfall_many_patients_labels_survive(self):
        """26 例竖排标签全保留（不丢、rotation=90）；realworld 示例可渲染。"""
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        sys.path.insert(0, SCRIPT_DIR)
        import af_draw
        n = 26
        d = {"change": [(-1) ** i * (5 + i % 9) * 3.0 for i in range(n)],
             "names": [f"PT-{i:02d}" for i in range(1, n + 1)]}
        fig, ax = plt.subplots(figsize=(10, 6))
        af_draw.gen_waterfall(d, ax, {"colors": ["#2E5E8F"], "font_size": 9,
                                      "spines": ["top", "right"], "grid_alpha": 0.3}, None)
        labels = ax.get_xticklabels()
        plt.close(fig)
        self.assertEqual(len(labels), n)
        self.assertTrue(all(lb.get_text() for lb in labels), "26 例不抽稀")


class TestV49000(unittest.TestCase):
    """v4.9.0：raincloud 雨云图（第 27 种图型，分布三合一）+ setup_env --fix。"""

    def test_raincloud_e2e_renders(self):
        with tempfile.TemporaryDirectory() as td:
            f = os.path.join(td, "r.json")
            io.open(f, "w", encoding="utf-8").write(json.dumps(
                {"labels": ["活动期", "低活动期", "健康对照"],
                 "datasets": {"活动期": [12.1, 14.2, 15.8, 13.4, 16.1, 12.9, 14.8, 13.9, 15.2, 14.0],
                              "低活动期": [6.2, 7.1, 5.8, 6.9, 7.5, 6.4, 5.5, 7.0, 6.8, 6.0],
                              "健康对照": [3.1, 2.8, 3.5, 3.0, 2.6, 3.3, 3.2, 2.9, 3.4, 3.0]}},
                ensure_ascii=False))
            out = os.path.join(td, "r.png")
            r = subprocess.run(
                [sys.executable, GEN, "-t", "raincloud", "-d", f, "-o", out,
                 "--dpi", "80", "--cjk"], capture_output=True, text=True, timeout=240)
            self.assertEqual(r.returncode, 0, r.stderr[-250:])
            self.assertTrue(os.path.exists(out))
            self.assertIn("raincloud: 3 组", r.stderr)
            self.assertIn("原始点", r.stderr)

    def test_raincloud_validate_fatals(self):
        with tempfile.TemporaryDirectory() as td:
            cases = [
                ({}, "需要 'datasets'"),
                ({"datasets": {}}, "需要 'datasets'"),
                ({"datasets": {"A": []}}, "非空数值数组"),
                ({"datasets": {"A": [1, "x"]}}, "非数值"),
                ({"datasets": {"A": [1, float("nan")]}}, "非有限"),
                ({"labels": ["a"], "datasets": {"A": [1, 2], "B": [3, 4]}}, "一一对应"),
                ({"datasets": {"A": [1, 2]}, "jitter": 9}, "jitter"),
                ({"datasets": {"A": [1, 2]}, "cloud_scale": 9}, "cloud_scale"),
            ]
            for i, (data, want) in enumerate(cases):
                f = os.path.join(td, f"b{i}.json")
                io.open(f, "w", encoding="utf-8").write(json.dumps(data))
                r = subprocess.run(
                    [sys.executable, GEN, "-t", "raincloud", "-d", f,
                     "-o", os.path.join(td, f"x{i}.png")],
                    capture_output=True, text=True, timeout=120)
                self.assertEqual(r.returncode, 1, (i, r.stderr[-200:]))
                self.assertIn(want, r.stderr, (i, want))

    def test_raincloud_registry_count_27(self):
        import gen_figure
        sys.path.insert(0, SCRIPT_DIR)
        n = len(gen_figure.GENERATORS) - 3
        self.assertGreaterEqual(n, 27, f"注册表图型数: {n}")

    def test_raincloud_demo_data_valid(self):
        import gen_figure
        sys.path.insert(0, SCRIPT_DIR)
        demo = gen_figure.DEMO_DATA["raincloud"]
        with tempfile.TemporaryDirectory() as td:
            f = os.path.join(td, "d.json")
            io.open(f, "w", encoding="utf-8").write(json.dumps(demo, ensure_ascii=False))
            r = subprocess.run(
                [sys.executable, GEN, "-t", "raincloud", "-d", f,
                 "-o", os.path.join(td, "d.png"), "--dpi", "70"],
                capture_output=True, text=True, timeout=240)
            self.assertEqual(r.returncode, 0, r.stderr[-250:])

    def test_raincloud_suggest_and_zero_variance(self):
        with tempfile.TemporaryDirectory() as td:
            f = os.path.join(td, "s.json")
            io.open(f, "w", encoding="utf-8").write(json.dumps(
                {"datasets": {"A": [1, 2, 3], "B": [2, 3, 4]}}))
            r = subprocess.run([sys.executable, GEN, "--suggest", "-d", f],
                               capture_output=True, text=True, timeout=120)
            self.assertEqual(r.returncode, 0, r.stderr[-200:])
            self.assertIn("raincloud", r.stdout)
            f2 = os.path.join(td, "z.json")
            io.open(f2, "w", encoding="utf-8").write(json.dumps(
                {"datasets": {"恒定组": [5.0, 5.0, 5.0], "正常组": [1, 2, 3, 4]}}, ensure_ascii=False))
            out = os.path.join(td, "z.png")
            r2 = subprocess.run(
                [sys.executable, GEN, "-t", "raincloud", "-d", f2, "-o", out, "--dpi", "70"],
                capture_output=True, text=True, timeout=240)
            self.assertEqual(r2.returncode, 0, r2.stderr[-250:])
            self.assertIn("方差为 0", r2.stderr)
            self.assertTrue(os.path.exists(out))

    def test_raincloud_n1_and_hidden_cloud_and_stats(self):
        """n=1 单点组不崩（云退化标记线）+ cloud_scale=0 藏云 + --stats auto 透传（审查 P1-1/P2-1/P1-2 回归锁）。"""
        with tempfile.TemporaryDirectory() as td:
            f = os.path.join(td, "p.json")
            io.open(f, "w", encoding="utf-8").write(json.dumps({"datasets": {"one": [7.5]}}))
            out = os.path.join(td, "p.png")
            r = subprocess.run([sys.executable, GEN, "-t", "raincloud", "-d", f, "-o", out,
                                "--dpi", "70"], capture_output=True, text=True, timeout=240)
            self.assertEqual(r.returncode, 0, r.stderr[-250:])
            self.assertIn("退化", r.stderr)
            f2 = os.path.join(td, "h.json")
            io.open(f2, "w", encoding="utf-8").write(json.dumps(
                {"datasets": {"A": [1, 2, 3, 4, 5], "B": [2, 3, 4, 5, 6]}, "cloud_scale": 0}))
            r2 = subprocess.run([sys.executable, GEN, "-t", "raincloud", "-d", f2,
                                 "-o", os.path.join(td, "h.png"), "--dpi", "70", "--stats", "auto"],
                                capture_output=True, text=True, timeout=240)
            self.assertEqual(r2.returncode, 0, r2.stderr[-250:])
            self.assertIn("云已隐藏", r2.stderr)
            self.assertIn("stats (raincloud)", r2.stderr)  # stats 真透传（非 [ignored]）
            self.assertNotIn("[ignored] --stats", r2.stderr)

    def test_setup_env_fix_flag(self):
        # 密闭：不真跑 --fix（缺依赖机器会触发真实 pip）——源码静态断言
        src = io.open(os.path.join(SCRIPT_DIR, "setup_env.py"), encoding="utf-8").read()
        self.assertIn("--fix", src)
        self.assertIn("tuna.tsinghua", src)
        self.assertIn("aliyun", src)


class TestV41000(unittest.TestCase):
    """v4.10.0：swimmer 临床试验游泳图（第 28 种图型，逐例时长+持续箭头+里程碑）。"""

    def test_swimmer_e2e_group(self):
        """分组（RECIST）模式 E2E：渲染+stderr 事实行。"""
        with tempfile.TemporaryDirectory() as td:
            f = os.path.join(td, "s.json")
            io.open(f, "w", encoding="utf-8").write(json.dumps(
                {"end": [56.0, 51.5, 47.0, 43.5, 40.0, 38.5, 35.0, 31.0,
                         28.5, 25.0, 21.5, 17.0, 13.5, 9.0],
                 "names": [f"C{i:02d}" for i in range(1, 15)],
                 "group": ["CR", "CR", "PR", "PR", "PR", "PR", "SD", "SD",
                           "SD", "SD", "PD", "PR", "CR", "PD"],
                 "milestones": [[11.0], [15.5], [9.0], [13.0], [8.5], [17.5],
                                [], [], [], [], [], [19.0], [12.0], []],
                 "ongoing": [True, True, False, True, False, False, False,
                             True, False, False, False, False, False, False],
                 "ref_lines": [26, 52]}, ensure_ascii=False))
            out = os.path.join(td, "s.png")
            r = subprocess.run(
                [sys.executable, GEN, "-t", "swimmer", "-d", f, "-o", out,
                 "--dpi", "80", "--cjk"],
                capture_output=True, text=True, timeout=240)
            self.assertEqual(r.returncode, 0, r.stderr[-250:])
            self.assertTrue(os.path.exists(out))
            self.assertIn("swimmer: 14 例", r.stderr)
            self.assertIn("持续中 4 例", r.stderr)
            self.assertIn("里程碑 8 个", r.stderr)
            self.assertIn("PD 2", r.stderr)

    def test_swimmer_no_group_start_sort_input(self):
        """无分组+start 偏移+sort=input 渲染成功（单色+符号图例路径）。"""
        with tempfile.TemporaryDirectory() as td:
            f = os.path.join(td, "n.json")
            io.open(f, "w", encoding="utf-8").write(json.dumps(
                {"end": [12.0, 30.5, 22.0, 8.0],
                 "start": [2.0, 0, 4.0, 1.0],
                 "ongoing": [False, True, True, False],
                 "milestones": [[6.0], [10.0, 24.0], [], []],
                 "sort": "input"}))
            out = os.path.join(td, "n.png")
            r = subprocess.run(
                [sys.executable, GEN, "-t", "swimmer", "-d", f, "-o", out,
                 "--dpi", "80"],
                capture_output=True, text=True, timeout=240)
            self.assertEqual(r.returncode, 0, r.stderr[-250:])
            self.assertTrue(os.path.exists(out))
            self.assertIn("swimmer: 4 例", r.stderr)

    def test_swimmer_milestone_beyond_end(self):
        """里程碑晚于条端（停药后应答持续）合法渲染。"""
        with tempfile.TemporaryDirectory() as td:
            f = os.path.join(td, "m.json")
            io.open(f, "w", encoding="utf-8").write(json.dumps(
                {"end": [20.0, 14.0], "milestones": [[30.0], [18.0]],
                 "group": ["CR", "PR"]}))
            out = os.path.join(td, "m.png")
            r = subprocess.run(
                [sys.executable, GEN, "-t", "swimmer", "-d", f, "-o", out,
                 "--dpi", "80"],
                capture_output=True, text=True, timeout=240)
            self.assertEqual(r.returncode, 0, r.stderr[-250:])
            self.assertTrue(os.path.exists(out))

    def test_swimmer_validate_fatals(self):
        with tempfile.TemporaryDirectory() as td:
            cases = [
                ({}, "需要 'end'"),
                ({"end": []}, "非空数值数组"),
                ({"end": [1, "x"]}, "必须全为数值"),
                ({"end": [1, None]}, "必须全为数值"),
                ({"end": [5, -2]}, "必须全为正数"),
                ({"end": [5, 0]}, "必须全为正数"),
                ({"end": [5, 2], "start": [1, 3]}, "start 必须小于 end"),
                ({"end": [5, 2], "start": [1]}, "start"),
                ({"end": [5, 2], "names": ["a"]}, "names"),
                ({"end": [5, 2], "group": ["CR"]}, "group"),
                ({"end": [5, 2], "milestones": [[1]]}, "milestones"),
                ({"end": [5, 2], "milestones": [[1], ["x"]]}, "必须是数值数组"),
                ({"end": [5, 2], "milestones": [[9], []],
                  "start": [10, 0]}, "早于该例 start"),
                ({"end": [5, 2], "ongoing": [True]}, "ongoing"),
                ({"end": [5, 2], "ongoing": [True, 1]}, "ongoing"),
                ({"end": [5, 2], "ref_lines": ["a"]}, "ref_lines"),
                ({"end": [5, 2], "sort": "asc"}, "sort"),
            ]
            for i, (data, want) in enumerate(cases):
                f = os.path.join(td, f"b{i}.json")
                io.open(f, "w", encoding="utf-8").write(json.dumps(data))
                out = os.path.join(td, f"b{i}.png")
                r = subprocess.run(
                    [sys.executable, GEN, "-t", "swimmer", "-d", f, "-o", out],
                    capture_output=True, text=True, timeout=120)
                self.assertNotEqual(r.returncode, 0, f"case{i} 应失败: {data}")
                self.assertIn(want, r.stderr, f"case{i} 缺少提示 {want!r}")

    def test_swimmer_alt_and_suggest(self):
        """--alt 文本与 --suggest 路由。"""
        import gen_figure
        sys.path.insert(0, SCRIPT_DIR)
        d = {"end": [12, 6], "ongoing": [True, False],
             "milestones": [[3], []], "group": ["CR", "PD"]}
        alt = gen_figure.generate_alt_text("swimmer", d, "队列")
        self.assertIn("游泳图", alt)
        self.assertIn("2 例", alt)
        self.assertIn("持续中 1 例", alt)
        recs = gen_figure.suggest_chart_type(
            {"end": [12, 6, 9], "names": ["a", "b", "c"]})
        self.assertTrue(any(t == "swimmer" for _, t, _ in recs), recs)

    def test_swimmer_demo_data_valid(self):
        """DEMO_DATA 的 swimmer 条目可直接渲染（--demo 流的非交互等价验证）。"""
        import gen_figure
        sys.path.insert(0, SCRIPT_DIR)
        demo = gen_figure.DEMO_DATA["swimmer"]
        with tempfile.TemporaryDirectory() as td:
            f = os.path.join(td, "d.json")
            io.open(f, "w", encoding="utf-8").write(
                json.dumps(demo, ensure_ascii=False))
            out = os.path.join(td, "d.png")
            r = subprocess.run(
                [sys.executable, GEN, "-t", "swimmer", "-d", f, "-o", out,
                 "--dpi", "80", "--cjk"],
                capture_output=True, text=True, timeout=240)
            self.assertEqual(r.returncode, 0, r.stderr[-250:])
            self.assertTrue(os.path.exists(out))

    def test_swimmer_template_and_realworld_commands_run(self):
        """模板与 realworld 示例的 _command 逐字可执行（防字段/路径回归）。"""
        base = os.path.normpath(os.path.join(SCRIPT_DIR, ".."))
        for name in (os.path.join("templates", "swimmer.json"),
                     os.path.join("examples", "realworld",
                                  "realworld_csle_swimmer.json")):
            with io.open(os.path.join(base, name), encoding="utf-8") as fh:
                cmd = json.load(fh)["_command"]
            # -o 重定向到临时目录：逐字执行但产物不落 trunk（v5.6 哨兵泄漏教训机制化）
            with tempfile.TemporaryDirectory() as td:
                cmd = cmd.rsplit("-o ", 1)[0] + "-o " + os.path.join(td, "out.png")
                r = subprocess.run(cmd, shell=True, cwd=base,
                                   capture_output=True, text=True, timeout=240)
                self.assertEqual(r.returncode, 0, f"{name}: {r.stderr[-250:]}")
                self.assertNotIn("未知字段", r.stderr, name)

    def test_swimmer_realworld_self_consistency(self):
        """realworld 数据程序化自洽断言（v4.8 RECIST 断言器同纪律）。"""
        base = os.path.normpath(os.path.join(SCRIPT_DIR, ".."))
        with io.open(os.path.join(base, "examples", "realworld",
                                  "realworld_csle_swimmer.json"),
                     encoding="utf-8") as fh:
            d = json.load(fh)
        n = len(d["end"])
        self.assertEqual(n, 14)
        for k in ("names", "group", "milestones", "ongoing"):
            self.assertEqual(len(d[k]), n, k)
        for i in range(n):
            e, s = float(d["end"][i]), float(d.get("start", [0] * n)[i])
            self.assertGreater(e, 0)
            self.assertLess(s, e, f"例{i+1} start>=end")
            for ms in d["milestones"][i]:
                self.assertGreaterEqual(float(ms), s, f"例{i+1} 里程碑早于入组")
            g = str(d["group"][i]).upper()
            has_ms = bool(d["milestones"][i])
            if g in ("CR", "PR"):
                self.assertTrue(has_ms, f"{d['names'][i]} {g} 应有首次应答里程碑")
            else:
                self.assertFalse(has_ms, f"{d['names'][i]} {g} 不应有应答里程碑")
            self.assertIsInstance(d["ongoing"][i], bool)
        # 持续中例数与事实行口径一致
        self.assertEqual(sum(d["ongoing"]), 4)

    def test_swimmer_registry_count_28(self):
        import gen_figure
        sys.path.insert(0, SCRIPT_DIR)
        n = len(gen_figure.GENERATORS) - 3
        self.assertGreaterEqual(n, 28, f"注册表图型数: {n}")

    def test_doctor_submission_hint(self):
        """--doctor 对投稿格式输出且未开 --verify 给出行动建议（v4.10 消潜规则）。"""
        with tempfile.TemporaryDirectory() as td:
            f = os.path.join(td, "h.json")
            io.open(f, "w", encoding="utf-8").write(json.dumps(
                {"labels": ["A", "B"], "series": {"组1": [1.0, 2.0]}}))
            out = os.path.join(td, "h.pdf")
            r = subprocess.run(
                [sys.executable, GEN, "-t", "bar", "-d", f, "-o", out,
                 "--doctor"],
                capture_output=True, text=True, timeout=240)
            self.assertEqual(r.returncode, 0, r.stderr[-250:])
            self.assertIn("--verify", r.stderr)
            self.assertIn("--pub-ready", r.stderr)



class TestV50000(unittest.TestCase):
    """v5.0.0：ridgeline 山脊图（第 29 种图型）+ --audit 数字对账侧车。"""

    def test_ridgeline_e2e(self):
        with tempfile.TemporaryDirectory() as td:
            f = os.path.join(td, "r.json")
            io.open(f, "w", encoding="utf-8").write(json.dumps(
                {"labels": ["基线", "4周", "8周", "12周"],
                 "datasets": {"基线": [42, 48, 52, 55, 58, 60, 63, 65, 68, 72, 78, 85, 42, 50, 56, 61, 66, 70, 74, 80],
                              "4周": [28, 33, 38, 41, 45, 48, 52, 55, 58, 62, 68, 30, 36, 43, 49, 54, 60, 65],
                              "8周": [18, 22, 26, 30, 33, 37, 41, 45, 49, 54, 20, 25, 31, 39, 47, 52],
                              "12周": [12, 15, 19, 23, 27, 31, 36, 41, 46, 14, 18, 24, 33, 43]},
                 "overlap": 0.55}, ensure_ascii=False))
            out = os.path.join(td, "r.png")
            r = subprocess.run(
                [sys.executable, GEN, "-t", "ridgeline", "-d", f, "-o", out,
                 "--dpi", "80", "--cjk"],
                capture_output=True, text=True, timeout=240)
            self.assertEqual(r.returncode, 0, r.stderr[-250:])
            self.assertTrue(os.path.exists(out))
            self.assertIn("ridgeline: 4 组（overlap=0.55，sort=input）", r.stderr)

    def test_ridgeline_degenerate_and_median_sort(self):
        with tempfile.TemporaryDirectory() as td:
            f = os.path.join(td, "d.json")
            io.open(f, "w", encoding="utf-8").write(json.dumps(
                {"labels": ["A", "B", "C"],
                 "datasets": {"A": [5, 6, 5, 7, 6], "B": [9], "C": [1, 2, 3, 4, 5, 6]},
                 "sort": "median"}))
            out = os.path.join(td, "d.png")
            r = subprocess.run(
                [sys.executable, GEN, "-t", "ridgeline", "-d", f, "-o", out, "--dpi", "80"],
                capture_output=True, text=True, timeout=240)
            self.assertEqual(r.returncode, 0, r.stderr[-250:])
            self.assertTrue(os.path.exists(out))
            self.assertIn("单值退化 1 组: B", r.stderr)
            self.assertIn("sort=median", r.stderr)

    def test_ridgeline_validate_fatals(self):
        with tempfile.TemporaryDirectory() as td:
            cases = [
                ({}, "datasets"),
                ({"datasets": {}}, "datasets"),
                ({"datasets": {"A": [1]}, "labels": ["A", "B"]}, "labels"),
                ({"datasets": {"A": [1, "x", 3]}}, "非数值项"),
                ({"datasets": {"A": [1, 2]}, "overlap": 1.2}, "overlap"),
                ({"datasets": {"A": [1, 2]}, "sort": "asc"}, "sort"),
            ]
            for i, (data, want) in enumerate(cases):
                f = os.path.join(td, f"b{i}.json")
                io.open(f, "w", encoding="utf-8").write(json.dumps(data))
                out = os.path.join(td, f"b{i}.png")
                r = subprocess.run(
                    [sys.executable, GEN, "-t", "ridgeline", "-d", f, "-o", out],
                    capture_output=True, text=True, timeout=120)
                self.assertNotEqual(r.returncode, 0, f"case{i} 应失败: {data}")
                self.assertIn(want, r.stderr, f"case{i} 缺提示 {want!r}")

    def test_ridgeline_alt_and_suggest(self):
        import gen_figure
        sys.path.insert(0, SCRIPT_DIR)
        d = {"labels": ["A", "B"], "datasets": {"A": [1, 2, 3], "B": [4, 5, 6]}}
        alt = gen_figure.generate_alt_text("ridgeline", d, "队列")
        self.assertIn("山脊图", alt)
        self.assertIn("2 组", alt)
        recs = gen_figure.suggest_chart_type({"datasets": {"A": [1, 2], "B": [3, 4]}})
        self.assertTrue(any(t == "ridgeline" for _, t, _ in recs), recs)

    def test_ridgeline_template_command(self):
        base = os.path.normpath(os.path.join(SCRIPT_DIR, ".."))
        with io.open(os.path.join(base, "templates", "ridgeline.json"), encoding="utf-8") as fh:
            cmd = json.load(fh)["_command"]
        with tempfile.TemporaryDirectory() as td:  # 产物不落 trunk（哨兵纪律）
            cmd = cmd.rsplit("-o ", 1)[0] + "-o " + os.path.join(td, "out.png")
            r = subprocess.run(cmd, shell=True, cwd=base, capture_output=True, text=True, timeout=240)
        self.assertEqual(r.returncode, 0, r.stderr[-250:])

    def test_audit_sidecar_deterministic(self):
        with tempfile.TemporaryDirectory() as td:
            f = os.path.join(td, "a.json")
            io.open(f, "w", encoding="utf-8").write(json.dumps(
                {"labels": ["A", "B", "C"], "series": {"组1": [10, 20, 30], "组2": [5, 15, 25]}}))
            outs = []
            for i in range(2):
                out = os.path.join(td, f"a{i}.png")
                r = subprocess.run(
                    [sys.executable, GEN, "-t", "bar", "-d", f, "-o", out,
                     "--audit", "--dpi", "80"],
                    capture_output=True, text=True, timeout=240)
                self.assertEqual(r.returncode, 0, r.stderr[-250:])
                ap = out + ".audit.json"
                self.assertTrue(os.path.exists(ap), r.stderr[-250:])
                outs.append(io.open(ap, encoding="utf-8").read())
            self.assertEqual(outs[0], outs[1], "audit 侧车须同输入同字节")
            d = json.loads(outs[0])
            s1 = d["stats"]["series.组1"]
            self.assertEqual(s1["n"], 3)
            self.assertAlmostEqual(s1["mean"], 20.0)
            self.assertAlmostEqual(s1["median"], 20.0)
            self.assertIn("input_sha256_12", d)

    def test_registry_count_29(self):
        import gen_figure
        sys.path.insert(0, SCRIPT_DIR)
        n = len(gen_figure.GENERATORS) - 3
        self.assertGreaterEqual(n, 29, f"注册表图型数: {n}")



class TestV50100(unittest.TestCase):
    """v5.1.0：dumbbell 哑铃图（第 30 种图型）+ --repro 复跑交付包。"""

    def test_dumbbell_e2e(self):
        with tempfile.TemporaryDirectory() as td:
            f = os.path.join(td, "d.json")
            io.open(f, "w", encoding="utf-8").write(json.dumps(
                {"labels": ["中心A", "中心B", "中心C"],
                 "from": [32, 41, 28], "to": [58, 63, 49],
                 "from_label": "基线", "to_label": "12周", "show_gap": True},
                ensure_ascii=False))
            out = os.path.join(td, "d.png")
            r = subprocess.run(
                [sys.executable, GEN, "-t", "dumbbell", "-d", f, "-o", out,
                 "--dpi", "80", "--cjk"],
                capture_output=True, text=True, timeout=240)
            self.assertEqual(r.returncode, 0, r.stderr[-250:])
            self.assertTrue(os.path.exists(out))
            self.assertIn("dumbbell: 3 行（sort=gap，差距 21~26）", r.stderr)

    def test_dumbbell_gap_sort_order(self):
        """Δ 标注与排序配对正确性（回归：gaps 按原序取用曾致配对错位）。"""
        with tempfile.TemporaryDirectory() as td:
            f = os.path.join(td, "g.json")
            # A=26, B=22, C=21 → gap 降序应为 A, B, C
            io.open(f, "w", encoding="utf-8").write(json.dumps(
                {"labels": ["A", "B", "C"],
                 "from": [32, 41, 28], "to": [58, 63, 49], "sort": "gap"}))
            out = os.path.join(td, "g.png")
            r = subprocess.run(
                [sys.executable, GEN, "-t", "dumbbell", "-d", f, "-o", out,
                 "--dpi", "80", "--show-values"],
                capture_output=True, text=True, timeout=240)
            self.assertEqual(r.returncode, 0, r.stderr[-250:])
            self.assertIn("起点均值 33.67 → 终点均值 56.67", r.stderr)

    def test_dumbbell_validate_fatals(self):
        with tempfile.TemporaryDirectory() as td:
            cases = [
                ({}, "from"),
                ({"from": [1, 2]}, "to"),
                ({"from": [1, 2], "to": [3]}, "长度必须一致"),
                ({"from": [1, "x"], "to": [2, 3]}, "必须全为数值"),
                ({"from": [1, None], "to": [2, 3]}, "必须全为数值"),
                ({"from": [1, 2], "to": [3, 4], "labels": ["A"]}, "labels"),
                ({"from": [1, 2], "to": [3, 4], "from_label": 5}, "from_label"),
                ({"from": [1, 2], "to": [3, 4], "sort": "asc"}, "sort"),
            ]
            for i, (data, want) in enumerate(cases):
                f = os.path.join(td, f"b{i}.json")
                io.open(f, "w", encoding="utf-8").write(json.dumps(data))
                out = os.path.join(td, f"b{i}.png")
                r = subprocess.run(
                    [sys.executable, GEN, "-t", "dumbbell", "-d", f, "-o", out],
                    capture_output=True, text=True, timeout=120)
                self.assertNotEqual(r.returncode, 0, f"case{i} 应失败: {data}")
                self.assertIn(want, r.stderr, f"case{i} 缺提示 {want!r}")

    def test_dumbbell_alt_and_suggest(self):
        import gen_figure
        sys.path.insert(0, SCRIPT_DIR)
        d = {"labels": ["A", "B"], "from": [10, 20], "to": [30, 25]}
        alt = gen_figure.generate_alt_text("dumbbell", d, "队列")
        self.assertIn("哑铃图", alt)
        self.assertIn("A 10→30", alt)
        recs = gen_figure.suggest_chart_type(
            {"labels": ["A", "B"], "from": [1, 2], "to": [3, 4]})
        self.assertTrue(any(t == "dumbbell" for _, t, _ in recs), recs)

    def test_dumbbell_template_command(self):
        base = os.path.normpath(os.path.join(SCRIPT_DIR, ".."))
        with io.open(os.path.join(base, "templates", "dumbbell.json"), encoding="utf-8") as fh:
            cmd = json.load(fh)["_command"]
        with tempfile.TemporaryDirectory() as td:  # 产物不落 trunk（哨兵纪律）
            cmd = cmd.rsplit("-o ", 1)[0] + "-o " + os.path.join(td, "out.png")
            r = subprocess.run(cmd, shell=True, cwd=base, capture_output=True, text=True, timeout=240)
        self.assertEqual(r.returncode, 0, r.stderr[-250:])
        self.assertNotIn("未知字段", r.stderr)

    def test_repro_bundle(self):
        """--repro 复跑交付包：五件齐+数据副本逐字节一致+命令逐字+幂等。"""
        with tempfile.TemporaryDirectory() as td:
            f = os.path.join(td, "r.json")
            io.open(f, "w", encoding="utf-8").write(json.dumps(
                {"labels": ["A", "B", "C"], "series": {"组1": [10, 20, 30], "组2": [5, 15, 25]}}))
            out = os.path.join(td, "r.png")
            r = subprocess.run(
                [sys.executable, GEN, "-t", "bar", "-d", f, "-o", out,
                 "--repro", "--dpi", "80"],
                capture_output=True, text=True, timeout=240)
            self.assertEqual(r.returncode, 0, r.stderr[-250:])
            bundle = out + ".repro"
            for name in ("command.txt", "README_REPRODUCE.md", "audit.json",
                         "r.json", "r.png"):
                self.assertTrue(os.path.exists(os.path.join(bundle, name)), name)
            self.assertEqual(io.open(os.path.join(bundle, "r.json"), encoding="utf-8").read(),
                             io.open(f, encoding="utf-8").read(), "数据副本须逐字节一致")
            self.assertIn("--repro", io.open(os.path.join(bundle, "command.txt"),
                                             encoding="utf-8").read())
            # 幂等：重跑重建不报错
            r2 = subprocess.run(
                [sys.executable, GEN, "-t", "bar", "-d", f, "-o", out,
                 "--repro", "--dpi", "80"],
                capture_output=True, text=True, timeout=240)
            self.assertEqual(r2.returncode, 0, r2.stderr[-250:])
            self.assertTrue(os.path.exists(os.path.join(bundle, "command.txt")))

    def test_cli_flags_change_output(self):
        """机制锁：CLI 旗标必须真实改变输出字节（防旗标静默无效类——v5.1
        dumbbell --show-values 曾只读数据字典导致 CLI 无效）。"""
        import hashlib as _hb
        with tempfile.TemporaryDirectory() as td:
            cases = [
                ("dumbbell", {"labels": ["A", "B", "C"], "from": [10, 20, 30],
                              "to": [25, 35, 45]}, ["--show-values"]),
                ("waterfall", {"change": [-40, -20, -5, 10, 30]}, ["--show-values"]),
            ]
            for ctype, data, flags in cases:
                f = os.path.join(td, f"{ctype}.json")
                io.open(f, "w", encoding="utf-8").write(json.dumps(data))
                digest = []
                for with_flag in (False, True):
                    out = os.path.join(td, f"{ctype}_{with_flag}.png")
                    cmd = [sys.executable, GEN, "-t", ctype, "-d", f, "-o", out,
                           "--dpi", "70"] + (flags if with_flag else [])
                    r = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
                    self.assertEqual(r.returncode, 0, r.stderr[-200:])
                    digest.append(_hb.md5(open(out, "rb").read()).hexdigest())
                self.assertNotEqual(digest[0], digest[1],
                                    f"{ctype} {' '.join(flags)} 输出与无旗标完全相同——旗标静默无效")


    def test_registry_count_30(self):
        import gen_figure
        sys.path.insert(0, SCRIPT_DIR)
        n = len(gen_figure.GENERATORS) - 3
        self.assertGreaterEqual(n, 30, f"注册表图型数: {n}")


class TestV50200(unittest.TestCase):
    """v5.2.0：calibration 校准曲线（第 31）+ dca 决策曲线（第 32）
    + --pub-ready 合流复跑包（机制）+ 预留带 helper（机制）+ 辅助输出双语化。"""

    @classmethod
    def _rw(cls, name):
        return os.path.join(SCRIPT_DIR, "..", "examples", "realworld", name)

    def test_calibration_e2e_realworld(self):
        with tempfile.TemporaryDirectory() as td:
            out = os.path.join(td, "c.png")
            r = subprocess.run(
                [sys.executable, GEN, "-t", "calibration",
                 "-d", self._rw("realworld_sepsis_calibration.json"),
                 "-o", out, "--dpi", "80", "--cjk"],
                capture_output=True, text=True, timeout=240)
            self.assertEqual(r.returncode, 0, r.stderr[-250:])
            self.assertTrue(os.path.exists(out))
            self.assertIn("calibration: 10 箱，总 n=500", r.stderr)
            self.assertIn("平均偏差(观察-预测)", r.stderr)

    def test_calibration_bins_schema(self):
        """bins 对象数组与平行数组两种给法均可渲染（同一语义）。"""
        with tempfile.TemporaryDirectory() as td:
            f = os.path.join(td, "b.json")
            io.open(f, "w", encoding="utf-8").write(json.dumps({"bins": [
                {"predicted": 0.10, "observed": 0.13, "n": 30, "ci_lo": 0.04, "ci_hi": 0.28},
                {"predicted": 0.30, "observed": 0.26, "n": 40, "ci_lo": 0.14, "ci_hi": 0.42},
                {"predicted": 0.52, "observed": 0.55, "n": 44, "ci_lo": 0.41, "ci_hi": 0.68},
                {"predicted": 0.75, "observed": 0.79, "n": 36, "ci_lo": 0.64, "ci_hi": 0.90}]}))
            out = os.path.join(td, "b.png")
            r = subprocess.run(
                [sys.executable, GEN, "-t", "calibration", "-d", f, "-o", out,
                 "--dpi", "80", "--alt"],
                capture_output=True, text=True, timeout=240)
            self.assertEqual(r.returncode, 0, r.stderr[-250:])
            alt = io.open(os.path.splitext(out)[0] + ".alt.txt", encoding="utf-8").read()
            self.assertIn("校准曲线（4 箱，总样本 150）", alt)

    def test_calibration_range_fatals(self):
        for bad, msg in (
                ({"predicted": [0.2, 1.4], "observed": [0.3, 0.4]}, "须在 [0,1]"),
                ({"predicted": [0.2, 0.4], "observed": [0.3]}, "长度必须一致"),
                ({"predicted": [0.2], "observed": [0.3], "hl_p": 1.5}, "hl_p")):
            with tempfile.TemporaryDirectory() as td:
                f = os.path.join(td, "x.json")
                io.open(f, "w", encoding="utf-8").write(json.dumps(bad))
                r = subprocess.run(
                    [sys.executable, GEN, "-t", "calibration", "-d", f,
                     "-o", os.path.join(td, "x.png"), "--dpi", "70"],
                    capture_output=True, text=True, timeout=120)
                self.assertEqual(r.returncode, 1, (bad, r.stderr[-200:]))
                self.assertIn(msg, r.stderr)

    def test_dca_e2e_prevalence_theory(self):
        with tempfile.TemporaryDirectory() as td:
            out = os.path.join(td, "d.png")
            r = subprocess.run(
                [sys.executable, GEN, "-t", "dca",
                 "-d", self._rw("realworld_sepsis_dca.json"),
                 "-o", out, "--dpi", "80", "--cjk"],
                capture_output=True, text=True, timeout=240)
            self.assertEqual(r.returncode, 0, r.stderr[-250:])
            self.assertTrue(os.path.exists(out))
            self.assertIn("患病率 0.21", r.stderr)   # 理论 treat-all 由 prevalence 推出
            self.assertIn("峰值", r.stderr)

    def test_dca_validate_fatals(self):
        for bad, msg in (
                ({"thresholds": [0.2, 0.1], "models": [{"name": "X", "net_benefit": [1, 2]}]},
                 "严格升序"),
                ({"thresholds": [0.1, 0.2], "models": [{"name": "X", "net_benefit": [1]}]},
                 "长度必须与 thresholds 一致"),
                ({"thresholds": [0.1, 0.2], "models": [{"name": "X", "net_benefit": [1, 2]}],
                  "prevalence": 1.5}, "prevalence"),
                ({"thresholds": [0.0, 0.2], "models": [{"name": "X", "net_benefit": [1, 2]}]},
                 "(0,1)")):
            with tempfile.TemporaryDirectory() as td:
                f = os.path.join(td, "x.json")
                io.open(f, "w", encoding="utf-8").write(json.dumps(bad))
                r = subprocess.run(
                    [sys.executable, GEN, "-t", "dca", "-d", f,
                     "-o", os.path.join(td, "x.png"), "--dpi", "70"],
                    capture_output=True, text=True, timeout=120)
                self.assertEqual(r.returncode, 1, (bad, r.stderr[-200:]))
                self.assertIn(msg, r.stderr)

    def test_pub_ready_emits_repro_bundle(self):
        """机制锁：--pub-ready 必须产出复跑包（v5.2 合流；多格式副本进包）。"""
        with tempfile.TemporaryDirectory() as td:
            f = os.path.join(td, "p.json")
            io.open(f, "w", encoding="utf-8").write(json.dumps(
                {"predicted": [0.1, 0.4, 0.7], "observed": [0.12, 0.38, 0.72]}))
            out = os.path.join(td, "p.png")
            r = subprocess.run(
                [sys.executable, GEN, "-t", "calibration", "-d", f, "-o", out,
                 "--pub-ready", "--dpi", "80"],
                capture_output=True, text=True, timeout=240)
            self.assertEqual(r.returncode, 0, r.stderr[-250:])
            d = out + ".repro"
            self.assertTrue(os.path.isdir(d), "--pub-ready 未产出复跑包")
            files = os.listdir(d)
            for need in ("command.txt", "audit.json", "README_REPRODUCE.md",
                         "p.pdf", "p.png"):
                self.assertIn(need, files, f"复跑包缺 {need}")

    def test_repro_multiformat_no_longer_silent(self):
        """机制锁：--repro 遇 --multi-format 曾静默失效（v5.1 前缺陷），现必须产出。"""
        with tempfile.TemporaryDirectory() as td:
            f = os.path.join(td, "m.json")
            io.open(f, "w", encoding="utf-8").write(json.dumps(
                {"predicted": [0.1, 0.4, 0.7], "observed": [0.12, 0.38, 0.72]}))
            out = os.path.join(td, "m.png")
            r = subprocess.run(
                [sys.executable, GEN, "-t", "calibration", "-d", f, "-o", out,
                 "--multi-format", "png,pdf", "--repro", "--dpi", "80"],
                capture_output=True, text=True, timeout=240)
            self.assertEqual(r.returncode, 0, r.stderr[-250:])
            self.assertTrue(os.path.isdir(out + ".repro"), "--multi-format 下 --repro 静默失效")

    def test_registry_count_32(self):
        import gen_figure
        sys.path.insert(0, SCRIPT_DIR)
        n = len(gen_figure.GENERATORS) - 3  # 纯别名 horizontal_bar/boxplot/survival
        self.assertGreaterEqual(n, 32, f"注册表图型数: {n}")

    def test_templates_meta_complete(self):
        """机制锁（docQuality）：32 模板 _command/_note 全覆盖非空。"""
        tdir = os.path.join(SCRIPT_DIR, "..", "templates")
        jsons = sorted(glob.glob(os.path.join(tdir, "*.json")))
        self.assertEqual(len(jsons), 39)
        for p in jsons:
            d = json.load(io.open(p, encoding="utf-8"))
            for key in ("_command", "_note"):
                self.assertTrue(str(d.get(key) or "").strip(), f"{p} 缺 {key}")
            self.assertIn("-t", d.get("_command", ""), p)

    def test_realworld_sepsis_pair_run(self):
        """脓毒症双示例 _command 逐字可执行 + n 逐箱自洽=500。"""
        cal = json.load(io.open(self._rw("realworld_sepsis_calibration.json"),
                                encoding="utf-8"))
        self.assertEqual(sum(cal["n"]), 500)
        with tempfile.TemporaryDirectory() as td:
            for name, ctype, fact in (
                    ("realworld_sepsis_calibration.json", "calibration", "10 箱"),
                    ("realworld_sepsis_dca.json", "dca", "2 个模型")):
                out = os.path.join(td, name + ".png")
                r = subprocess.run(
                    [sys.executable, GEN, "-t", ctype, "-d", self._rw(name),
                     "-o", out, "--dpi", "70"],
                    capture_output=True, text=True, timeout=240)
                self.assertEqual(r.returncode, 0, r.stderr[-250:])
                self.assertIn(fact, r.stderr)

    def test_suggest_bilingual_zh(self):
        """domestic 修复锁：--suggest 推荐行带图型中文名。"""
        with tempfile.TemporaryDirectory() as td:
            f = os.path.join(td, "s.json")
            io.open(f, "w", encoding="utf-8").write(json.dumps(
                {"thresholds": [0.1, 0.3, 0.5],
                 "models": [{"name": "A", "net_benefit": [0.1, 0.05, 0.01]}]}))
            r = subprocess.run(
                [sys.executable, GEN, "--suggest", "-d", f],
                capture_output=True, text=True, timeout=120)
            self.assertEqual(r.returncode, 0, r.stderr[-200:])
            self.assertIn("（决策曲线）", r.stdout)

    def test_reserve_band_helper(self):
        """机制锁：行型图底部预留带共享 helper（swimmer/dumbbell 不再各写各的）。"""
        sys.path.insert(0, SCRIPT_DIR)
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import af_draw
        fig, ax = plt.subplots()
        try:
            band = af_draw.reserve_bottom_band(ax, 6, top_pad=1.1)
            self.assertEqual(tuple(ax.get_ylim()), (6 + 1.1, -0.8))
            self.assertEqual(band, (-0.8, 0.0))
            af_draw.reserve_bottom_band(ax, 3, top_pad=0.4)
            self.assertEqual(tuple(ax.get_ylim()), (3.4, -0.8))
        finally:
            plt.close(fig)

    def test_swimmer_dumbbell_use_helper(self):
        """防回潮：两处调用点必须经 reserve_bottom_band（不允许回退手写 set_ylim）。"""
        src = io.open(os.path.join(SCRIPT_DIR, "af_draw.py"), encoding="utf-8").read()
        self.assertIn("def reserve_bottom_band(", src)
        self.assertGreaterEqual(src.count("reserve_bottom_band(ax, n,"), 2,
                                "swimmer/dumbbell 应调用共享预留带 helper")


class TestV50300(unittest.TestCase):
    """v5.3.0：nomogram 列线图（第 33）+ 版本一致性锁（accuracy 机制）。"""

    @classmethod
    def _rw(cls, name):
        return os.path.join(SCRIPT_DIR, "..", "examples", "realworld", name)

    def test_nomogram_e2e_realworld(self):
        with tempfile.TemporaryDirectory() as td:
            out = os.path.join(td, "n.png")
            r = subprocess.run(
                [sys.executable, GEN, "-t", "nomogram",
                 "-d", self._rw("realworld_hcc_nomogram.json"),
                 "-o", out, "--dpi", "80", "--cjk"],
                capture_output=True, text=True, timeout=240)
            self.assertEqual(r.returncode, 0, r.stderr[-250:])
            self.assertTrue(os.path.exists(out))
            self.assertIn("nomogram: 5 个变量 + 3 个结局尺（Total Points 0~350）",
                          r.stderr)

    def test_nomogram_validate_fatals(self):
        cases = [
            ({"variables": [], "outcome_scales": [{"label": "Y", "ticks": [0.1, 0.2], "points_at": [0, 100]}]},
             "variables"),
            ({"variables": [{"name": "A", "ticks": [1]}],
              "outcome_scales": [{"label": "Y", "ticks": [0.1, 0.2], "points_at": [0, 100]}]},
             "2~12"),
            ({"variables": [{"name": "A", "ticks": [1, 2], "points_at": [0, 150]}],
              "outcome_scales": [{"label": "Y", "ticks": [0.1, 0.2], "points_at": [0, 100]}]},
             "[0,100]"),
            ({"variables": [{"name": "A", "ticks": [1, 2]}],
              "outcome_scales": [{"label": "Y", "ticks": [0.1, 0.2], "points_at": [-5, 100]}]},
             "非负"),
            ({"variables": [{"name": "A", "ticks": [1, 2]}]}, "outcome_scales"),
        ]
        for bad, msg in cases:
            with tempfile.TemporaryDirectory() as td:
                f = os.path.join(td, "x.json")
                io.open(f, "w", encoding="utf-8").write(json.dumps(bad))
                r = subprocess.run(
                    [sys.executable, GEN, "-t", "nomogram", "-d", f,
                     "-o", os.path.join(td, "x.png"), "--dpi", "70"],
                    capture_output=True, text=True, timeout=120)
                self.assertEqual(r.returncode, 1, (bad, r.stderr[-200:]))
                self.assertIn(msg, r.stderr)

    def test_nomogram_alt_and_suggest(self):
        with tempfile.TemporaryDirectory() as td:
            f = os.path.join(td, "n.json")
            io.open(f, "w", encoding="utf-8").write(json.dumps(
                {"variables": [{"name": "年龄", "ticks": [40, 60, 80]}],
                 "outcome_scales": [{"label": "3 年生存", "ticks": [0.9, 0.5],
                                     "points_at": [60, 180]}]}))
            out = os.path.join(td, "n.png")
            r = subprocess.run(
                [sys.executable, GEN, "-t", "nomogram", "-d", f, "-o", out,
                 "--dpi", "80", "--alt"],
                capture_output=True, text=True, timeout=240)
            self.assertEqual(r.returncode, 0, r.stderr[-250:])
            alt = io.open(os.path.splitext(out)[0] + ".alt.txt",
                          encoding="utf-8").read()
            self.assertIn("列线图（1 个预测变量", alt)
            self.assertIn("3 年生存", alt)
            r2 = subprocess.run(
                [sys.executable, GEN, "--suggest", "-d", f],
                capture_output=True, text=True, timeout=120)
            self.assertIn("nomogram", r2.stdout)

    def test_registry_count_33(self):
        import gen_figure
        sys.path.insert(0, SCRIPT_DIR)
        n = len(gen_figure.GENERATORS) - 3  # 纯别名 horizontal_bar/boxplot/survival
        self.assertGreaterEqual(n, 33, f"注册表图型数: {n}")

    def test_version_consistency_lock(self):
        """accuracy 机制锁：随包 evals/evals.json 的 skill_version 必须=SKILL 版本。"""
        import re as _re
        root = os.path.join(SCRIPT_DIR, "..")
        sk = io.open(os.path.join(root, "SKILL.md"), encoding="utf-8").read()
        m = _re.search(r"version: ([\d.]+)", sk)
        sk_ver = m.group(1)
        ev = json.load(io.open(os.path.join(root, "evals", "evals.json"),
                               encoding="utf-8"))
        self.assertEqual(ev.get("skill_version"), sk_ver,
                         "evals.json skill_version 与 SKILL.md 版本漂移——"
                         "发版必同步（accuracy 4.6 根因之一）")


class TestV50400(unittest.TestCase):
    """v5.4.0：rcs 限制性立方样条（第 34）+ DCA 获益区着色 + 刻度假阳性机制修复
    + doctor 共享尺度预检 + 场景卡片。"""

    @classmethod
    def _rw(cls, name):
        return os.path.join(SCRIPT_DIR, "..", "examples", "realworld", name)

    def test_rcs_e2e_realworld(self):
        with tempfile.TemporaryDirectory() as td:
            out = os.path.join(td, "r.png")
            r = subprocess.run(
                [sys.executable, GEN, "-t", "rcs",
                 "-d", self._rw("realworld_bmi_mortality_rcs.json"),
                 "-o", out, "--dpi", "80", "--cjk"],
                capture_output=True, text=True, timeout=240)
            self.assertEqual(r.returncode, 0, r.stderr[-250:])
            self.assertTrue(os.path.exists(out))
            self.assertIn("rcs: 10 个网格点，value 15~36，ref=23.5", r.stderr)

    def test_rcs_validate_fatals(self):
        for bad, msg in (
                ({"points": [{"value": 1, "est": 1.1, "ci_lo": 0.9, "ci_hi": 1.2}]}, "≥5"),
                ({"points": [{"value": i, "est": -1.0, "ci_lo": -2.0,
                              "ci_hi": 1.2} for i in range(6)]}, "log_y=true 时 est/ci 必须 >0"),
                ({"points": [{"value": 1, "est": 1.2, "ci_lo": 1.3, "ci_hi": 1.4}] * 6}, "ci_lo ≤ est ≤ ci_hi")):
            with tempfile.TemporaryDirectory() as td:
                payload = {k: v for k, v in bad.items() if k != "__hack__"}
                if bad.get("__hack__"):
                    payload["points"] = [dict(p, est=-1.0) if k2 == "est" else p
                                         for p in payload["points"] for k2 in ("est",)][:5]
                f = os.path.join(td, "x.json")
                io.open(f, "w", encoding="utf-8").write(json.dumps(payload))
                r = subprocess.run(
                    [sys.executable, GEN, "-t", "rcs", "-d", f,
                     "-o", os.path.join(td, "x.png"), "--dpi", "70"],
                    capture_output=True, text=True, timeout=120)
                self.assertEqual(r.returncode, 1, (msg, r.stderr[-200:]))
                self.assertIn(msg, r.stderr)

    def test_rcs_log_axis_plain_ticks(self):
        """验收 P1 机制锁：对数轴必须出平实刻度（禁 10⁰ 式科学计数法）。"""
        with tempfile.TemporaryDirectory() as td:
            out = os.path.join(td, "r.png")
            r = subprocess.run(
                [sys.executable, GEN, "-t", "rcs", "-d", self._rw("realworld_bmi_mortality_rcs.json"),
                 "-o", out, "--dpi", "80"],
                capture_output=True, text=True, timeout=240)
            self.assertEqual(r.returncode, 0, r.stderr[-250:])

    def test_dca_benefit_shade_reported(self):
        """获益区着色：stderr 报告着色区段，且 benefit_shade=false 时不报。"""
        with tempfile.TemporaryDirectory() as td:
            f = os.path.join(td, "d.json")
            io.open(f, "w", encoding="utf-8").write(json.dumps(
                {"thresholds": [0.1, 0.3, 0.5, 0.7], "prevalence": 0.2,
                 "models": [{"name": "A", "net_benefit": [0.18, 0.15, 0.10, 0.04]}]}))
            out = os.path.join(td, "d.png")
            r = subprocess.run(
                [sys.executable, GEN, "-t", "dca", "-d", f, "-o", out, "--dpi", "80"],
                capture_output=True, text=True, timeout=240)
            self.assertEqual(r.returncode, 0, r.stderr[-250:])
            self.assertIn("获益区", r.stderr)
            d2 = json.loads(io.open(f, encoding="utf-8").read())
            d2["benefit_shade"] = False
            io.open(f, "w", encoding="utf-8").write(json.dumps(d2))
            r2 = subprocess.run(
                [sys.executable, GEN, "-t", "dca", "-d", f, "-o", out, "--dpi", "80"],
                capture_output=True, text=True, timeout=240)
            self.assertEqual(r2.returncode, 0)
            self.assertNotIn("获益区", r2.stderr)

    def test_tick_boxes_ignores_out_of_view_ticks(self):
        """刻度假阳性机制锁：视图区间外的刻度不得参与碰撞检测。"""
        sys.path.insert(0, SCRIPT_DIR)
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import importlib
        gf = importlib.import_module("gen_figure")
        fig, ax = plt.subplots(figsize=(6, 4))
        ax.plot([0.1, 0.9], [0, 1])
        ax.set_xlim(0.016, 0.884)   # 0.0 与 1.0 刻度在视图外
        fig.canvas.draw()
        xl, yl, xb, yb = gf._tick_boxes(ax, fig.canvas.get_renderer())
        self.assertLessEqual(len(xb), 8, "视图外刻度仍被计入碰撞检测")
        for lab, box in zip(xl, xb):
            self.assertNotEqual(lab.get_text(), "0.0", "视图外刻度 0.0 不应参与")
        plt.close(fig)

    def test_doctor_shared_scale_precheck(self):
        """--doctor 跨面板共享尺度预检：同 y 标签范围悬殊须提示。"""
        sys.path.insert(0, SCRIPT_DIR)
        import af_diagnostics
        data = {"panels": [
            {"type": "bar", "data": {"labels": ["A"], "series": {"s": [1, 2]},
                                     "y_label": "表达量"}},
            {"type": "bar", "data": {"labels": ["B"], "series": {"s": [300, 500]},
                                     "y_label": "表达量"}}]}
        n = af_diagnostics.doctor_composite_shared_scale(data, print_fn=lambda s: None)
        self.assertGreaterEqual(n, 1, "同变量跨面板范围悬殊未提示")

    def test_scenario_cards_exists_with_entrance(self):
        """场景卡片单一入口：文件在位、带入口块与 12 卡结构。"""
        p = os.path.join(SCRIPT_DIR, "..", "references", "scenario-cards.md")
        self.assertTrue(os.path.isfile(p), "references/scenario-cards.md 缺失")
        txt = io.open(p, encoding="utf-8").read()
        self.assertIn("何时读我", txt)
        self.assertGreaterEqual(txt.count("## 卡"), 12, "场景卡不足 12 张")

    def test_registry_count_34(self):
        import gen_figure
        sys.path.insert(0, SCRIPT_DIR)
        n = len(gen_figure.GENERATORS) - 3  # 纯别名 horizontal_bar/boxplot/survival
        self.assertGreaterEqual(n, 34, f"注册表图型数: {n}")


class TestV50500(unittest.TestCase):
    """v5.5.0：subgroup 亚组分析森林图（第 35）+ FAQ 可发现性 + 边界前置。"""

    @classmethod
    def _rw(cls, name):
        return os.path.join(SCRIPT_DIR, "..", "examples", "realworld", name)

    def test_subgroup_e2e_realworld(self):
        with tempfile.TemporaryDirectory() as td:
            out = os.path.join(td, "s.png")
            r = subprocess.run(
                [sys.executable, GEN, "-t", "subgroup",
                 "-d", self._rw("realworld_immunotherapy_subgroup.json"),
                 "-o", out, "--dpi", "80", "--cjk"],
                capture_output=True, text=True, timeout=240)
            self.assertEqual(r.returncode, 0, r.stderr[-250:])
            self.assertTrue(os.path.exists(out))
            self.assertIn("subgroup: 4 个亚组变量 / 8 层+overall", r.stderr)
            self.assertIn("交互显著（P<0.05）0 个", r.stderr)

    def test_subgroup_validate_fatals(self):
        for bad, msg in (
                ({"subgroups": []}, "1~12"),
                ({"subgroups": [{"name": "A", "levels": [{"name": "x", "est": 1.5,
                  "ci_low": 2.0, "ci_high": 1.0}, {"name": "y", "est": 0.9,
                  "ci_low": 0.7, "ci_high": 1.1}]}]}, "ci_low ≤ est ≤ ci_high"),
                ({"subgroups": [{"name": "A", "levels": [{"name": "x", "est": 1.0,
                  "ci_low": 0.8, "ci_high": 1.2}, {"name": "y", "est": 0.9,
                  "ci_low": 0.7, "ci_high": 1.1}], "p_interaction": 1.5}]}, "p_interaction"),
                ({"subgroups": [{"name": "A", "levels": [{"name": "x", "est": 1.0,
                  "ci_low": 0.8, "ci_high": 1.2}]}]}, "2~8")):
            with tempfile.TemporaryDirectory() as td:
                f = os.path.join(td, "x.json")
                io.open(f, "w", encoding="utf-8").write(json.dumps(bad))
                r = subprocess.run(
                    [sys.executable, GEN, "-t", "subgroup", "-d", f,
                     "-o", os.path.join(td, "x.png"), "--dpi", "70"],
                    capture_output=True, text=True, timeout=120)
                self.assertEqual(r.returncode, 1, (msg, r.stderr[-200:]))
                self.assertIn(msg, r.stderr)

    def test_subgroup_alt_and_suggest(self):
        with tempfile.TemporaryDirectory() as td:
            f = os.path.join(td, "s.json")
            io.open(f, "w", encoding="utf-8").write(json.dumps(
                {"subgroups": [{"name": "年龄", "p_interaction": 0.4, "levels": [
                    {"name": "A", "est": 0.7, "ci_low": 0.5, "ci_high": 0.9, "p": 0.01},
                    {"name": "B", "est": 0.8, "ci_low": 0.6, "ci_high": 1.0, "p": 0.05}]}],
                  "overall": {"est": 0.75, "ci_low": 0.6, "ci_high": 0.9}}))
            out = os.path.join(td, "s.png")
            r = subprocess.run(
                [sys.executable, GEN, "-t", "subgroup", "-d", f, "-o", out,
                 "--dpi", "80", "--alt"],
                capture_output=True, text=True, timeout=240)
            self.assertEqual(r.returncode, 0, r.stderr[-250:])
            alt = io.open(os.path.splitext(out)[0] + ".alt.txt", encoding="utf-8").read()
            self.assertIn("亚组分析森林图（1 个亚组变量）", alt)
            self.assertIn("P 交互=0.4", alt)
            r2 = subprocess.run(
                [sys.executable, GEN, "--suggest", "-d", f],
                capture_output=True, text=True, timeout=120)
            self.assertIn("subgroup", r2.stdout)

    def test_registry_count_35(self):
        import gen_figure
        sys.path.insert(0, SCRIPT_DIR)
        n = len(gen_figure.GENERATORS) - 3  # 纯别名 horizontal_bar/boxplot/survival
        self.assertGreaterEqual(n, 35, f"注册表图型数: {n}")

    def test_registry_count_36(self):
        import gen_figure
        sys.path.insert(0, SCRIPT_DIR)
        n = len(gen_figure.GENERATORS) - 3  # 纯别名 horizontal_bar/boxplot/survival
        self.assertGreaterEqual(n, 36, f"注册表图型数: {n}")

    def test_faq_discoverability_and_boundaries_first(self):
        """评测对策锁：FAQ 顶部指针 + 能力边界在快速开始之前（双语）。"""
        root = os.path.join(SCRIPT_DIR, "..")
        for doc, faq_needle, cap, quick in (
                ("SKILL.md", "references/faq.md", "## Capabilities & Boundaries", "## Quick Start"),
                ("SKILL_ZH.md", "references/faq.md", "## 能力与边界", "## 快速开始")):
            txt = io.open(os.path.join(root, doc), encoding="utf-8").read()
            self.assertIn(faq_needle, txt.split("## Quick Start")[0] if doc == "SKILL.md" else txt[:6000],
                          f"{doc} 顶部缺 FAQ 指针")
            self.assertLess(txt.index(cap), txt.index(quick), f"{doc} 边界节未前置")


class TestV50600(unittest.TestCase):
    """v5.6.0：galbraith Galbraith 径向图（第 36 种图型）+ composite share_y 自动统一
    + prompt-cards 提示词卡片 + changelog 版本索引拆分。"""

    GAL_DEMO = {
        "est_label": "OR",
        "studies": [
            {"name": "Smith 2019", "effect": 0.58, "se": 0.34},
            {"name": "Lee 2020", "effect": 0.71, "se": 0.28},
            {"name": "王 2021", "effect": 0.62, "se": 0.19},
            {"name": "Garcia 2021", "effect": 0.55, "se": 0.15},
            {"name": "陈 2022", "effect": 0.67, "se": 0.12},
            {"name": "Kim 2022", "effect": 0.49, "se": 0.11},
            {"name": "Rossi 2023", "effect": 0.73, "se": 0.10},
            {"name": "Nakamura 2023", "effect": 0.60, "se": 0.09},
            {"name": "李 2024", "effect": 0.64, "se": 0.08},
            {"name": "Dubois 2024", "effect": 1.55, "se": 0.22},
        ]}

    def test_galbraith_e2e_and_outlier_report(self):
        """E2E：demo 数据渲染 + stderr 报固定效应/DL 合并/I²/带外离群点名。"""
        with tempfile.TemporaryDirectory() as td:
            f = os.path.join(td, "g.json")
            io.open(f, "w", encoding="utf-8").write(json.dumps(self.GAL_DEMO))
            out = os.path.join(td, "g.png")
            r = subprocess.run(
                [sys.executable, GEN, "-t", "galbraith", "-d", f, "-o", out,
                 "--dpi", "80", "--cjk"],
                capture_output=True, text=True, timeout=240)
            self.assertEqual(r.returncode, 0, r.stderr[-300:])
            self.assertTrue(os.path.exists(out) and os.path.getsize(out) > 10000)
            self.assertIn("固定效应合并 b=", r.stderr)
            self.assertIn("I²=", r.stderr)
            self.assertIn("带外离群 1 个：Dubois 2024", r.stderr)

    def test_galbraith_arcs_and_smd_auto_off(self):
        """OR 默认画效应量弧线；MD/SMD 硬传 arcs=true 自动关并告知（pitfalls 类级铁律）。"""
        sys.path.insert(0, SCRIPT_DIR)
        import af_v23_charts
        import matplotlib.pyplot as plt
        theme = {"colors": ["#2E5E8F", "#FFB627"], "font_size": 10}
        fig, ax = plt.subplots()
        af_v23_charts.gen_galbraith(dict(self.GAL_DEMO), ax, theme, None)
        n_arc_or = sum(1 for c in ax.patches if type(c).__name__ == "Arc")
        self.assertGreater(n_arc_or, 0, "OR 尺度应有效应量弧线")
        plt.close(fig)
        fig2, ax2 = plt.subplots()
        smd = {"est_label": "SMD", "arcs": True,
               "studies": self.GAL_DEMO["studies"]}
        af_v23_charts.gen_galbraith(smd, ax2, theme, None)
        self.assertEqual(sum(1 for c in ax2.patches if type(c).__name__ == "Arc"), 0,
                         "MD/SMD 尺度弧线必须自动关闭")
        plt.close(fig2)

    def test_galbraith_validation_fatals(self):
        with tempfile.TemporaryDirectory() as td:
            for bad, msg in (
                ({"studies": [{"name": "a", "effect": 0.5, "se": 0.2},
                              {"name": "b", "effect": 0.6, "se": 0.1}]},
                 "至少需要 3 个研究"),
                ({"studies": [{"name": "a", "effect": 0.5, "se": 0.2},
                              {"name": "b", "effect": 0.6, "se": 0.1},
                              {"name": "c", "effect": 0.4, "se": 0.15}],
                  "arcs": "yes"},
                 "arcs 须为 true/false/auto"),
                ({}, "需要 'studies'")):
                f = os.path.join(td, "x.json")
                io.open(f, "w", encoding="utf-8").write(json.dumps(bad))
                r = subprocess.run(
                    [sys.executable, GEN, "-t", "galbraith", "-d", f,
                     "-o", os.path.join(td, "x.png"), "--dpi", "70"],
                    capture_output=True, text=True, timeout=120)
                self.assertEqual(r.returncode, 1, (msg, r.stderr[-200:]))
                self.assertIn(msg, r.stderr)

    def test_galbraith_alt_and_suggest(self):
        with tempfile.TemporaryDirectory() as td:
            f = os.path.join(td, "g.json")
            io.open(f, "w", encoding="utf-8").write(json.dumps(self.GAL_DEMO))
            out = os.path.join(td, "g.png")
            r = subprocess.run(
                [sys.executable, GEN, "-t", "galbraith", "-d", f, "-o", out,
                 "--dpi", "70", "--alt"],
                capture_output=True, text=True, timeout=240)
            self.assertEqual(r.returncode, 0, r.stderr[-250:])
            alt = io.open(os.path.splitext(out)[0] + ".alt.txt", encoding="utf-8").read()
            self.assertIn("Galbraith 径向图", alt)
            self.assertIn("10 个研究", alt)
            r2 = subprocess.run(
                [sys.executable, GEN, "--suggest", "-d", f],
                capture_output=True, text=True, timeout=120)
            self.assertIn("galbraith", r2.stdout)

    def test_share_y_unifies_and_noop_without(self):
        """composite share_y：同 ylabel 面板 y 轴统一；缺省不动（机制锁）。"""
        sys.path.insert(0, SCRIPT_DIR)
        import gen_figure
        theme = dict(gen_figure.THEMES["glm"])
        panels = [
            {"title": "A", "type": "bar", "ylabel": "应答率(%)", "pos": [0, 0],
             "data": {"labels": ["a", "b"], "series": {"药": [60.0, 55.0]}}},
            {"title": "B", "type": "bar", "ylabel": "应答率(%)", "pos": [0, 1],
             "data": {"labels": ["c", "d"], "series": {"药": [0.4, 0.3]}}},
        ]
        fig, ax = plt.subplots(figsize=(8, 4))
        gen_figure.GENERATORS["composite"](
            {"layout": [1, 2], "share_y": True, "panels": panels}, ax, theme, None)
        y1, y2 = fig.axes[0].get_ylim(), fig.axes[1].get_ylim()
        self.assertEqual(y1, y2, f"share_y 未统一: {y1} vs {y2}")
        plt.close(fig)
        fig2, ax2 = plt.subplots(figsize=(8, 4))
        gen_figure.GENERATORS["composite"](
            {"layout": [1, 2], "panels": panels}, ax2, theme, None)
        self.assertNotEqual(fig2.axes[0].get_ylim(), fig2.axes[1].get_ylim(),
                            "无 share_y 时不得改动面板轴")
        plt.close(fig2)

    def test_doctor_shared_scale_mentions_share_y(self):
        import af_diagnostics
        buf = []
        af_diagnostics.doctor_composite_shared_scale(
            {"panels": [
                {"type": "bar", "ylabel": "值", "data": {"series": {"s": [1.0, 2.0]}}},
                {"type": "bar", "ylabel": "值", "data": {"series": {"s": [300.0, 400.0]}}}]},
            buf.append)
        self.assertTrue(any("share_y" in s for s in buf), "doctor 提示须给出 share_y 出口")

    def test_prompt_cards_doc(self):
        """提示词卡片：≥12 卡 + 「对智能体说」可粘贴结构 + 双语头。"""
        p = os.path.join(SCRIPT_DIR, "..", "references", "prompt-cards.md")
        t = io.open(p, encoding="utf-8").read()
        self.assertGreaterEqual(t.count("\n## 卡"), 12, "提示词卡应 ≥12 张")
        self.assertGreaterEqual(t.count("**对智能体说**"), 12)
        self.assertIn("应产出", t)
        self.assertIn("验收点", t)
        self.assertIn("Prompt cards", t)

    def test_changelog_split(self):
        """changelog 拆分：索引+最近三版瘦身版 + 全量存档零删减。"""
        root = os.path.join(SCRIPT_DIR, "..")
        slim = io.open(os.path.join(root, "references", "changelog.md"), encoding="utf-8").read()
        full = io.open(os.path.join(root, "references", "changelog-full.md"), encoding="utf-8").read()
        self.assertLess(len(slim.split("\n")), 400, f"changelog.md 应 <400 行（现 {len(slim.splitlines())}）")
        self.assertGreaterEqual(slim.count("\n| v"), 15, "版本索引应 ≥15 行")
        self.assertIn("changelog-full.md", slim)
        self.assertIn("## 4.9.0", full, "全量存档须保留早期版本")
        self.assertIn("## 5.5.0", full)

    def test_cli_help_scenario_quickref(self):
        """评测 summary 建议：CLI 帮助带场景速查（epilog）。"""
        r = subprocess.run([sys.executable, GEN, "--help"],
                           capture_output=True, text=True, timeout=120)
        self.assertIn("场景速查", r.stdout + r.stderr)
        self.assertIn("galbraith", r.stdout + r.stderr)

    def test_galbraith_duplicate_outliers_deep_path(self):
        """回归锁（v5.6.1）：多个相同离群点强制 declutter 深分支——
        曾因变量名不一致 NameError（>12 研究+离群标注相撞才触发，首测漏）。"""
        with tempfile.TemporaryDirectory() as td:
            studies = [{"name": f"dup{i}", "effect": 1.55, "se": 0.22}
                       for i in range(6)] + [
                      {"name": f"ok{i}", "effect": 0.60, "se": 0.10} for i in range(9)]
            f = os.path.join(td, "g.json")
            io.open(f, "w", encoding="utf-8").write(json.dumps({"est_label": "OR", "studies": studies}))
            r = subprocess.run(
                [sys.executable, GEN, "-t", "galbraith", "-d", f,
                 "-o", os.path.join(td, "g.png"), "--dpi", "70"],
                capture_output=True, text=True, timeout=240)
            self.assertEqual(r.returncode, 0, r.stderr[-300:])
            self.assertTrue(os.path.exists(os.path.join(td, "g.png")))

    def test_v56_counts_and_fields(self):
        """计数锁收口：模板 37 / realworld 12 / 新字段白名单（v5.7 起 cif+mgus2 入列）。"""
        root = os.path.abspath(os.path.join(SCRIPT_DIR, ".."))
        tpls = [x for x in os.listdir(os.path.join(root, "templates")) if x.endswith(".json")]
        self.assertEqual(len(tpls), 39, f"模板应 39 个: {len(tpls)}")
        rws = os.listdir(os.path.join(root, "examples", "realworld"))
        self.assertEqual(len([x for x in rws if x.endswith(".json")]), 17,
                         f"realworld 应 14 个: {rws}")
        from af_diagnostics import _KNOWN_DATA_FIELDS
        for fld in ("arcs", "share_y", "est_label"):
            self.assertIn(fld, _KNOWN_DATA_FIELDS, f"新字段 {fld} 须进白名单（铁律#1）")


class TestV50700(unittest.TestCase):
    """v5.7.0：cif 竞争风险累计发生率（第 37 种图型，Aalen-Johansen+1−KM 高估对照）
    + 反模式集中页 antipatterns.md + Galbraith 弧线密度放宽/无效应线小标
    + --list-types 权威清单 + MCP 清单机制化。"""

    CIF_DEMO = {"time": [3, 5, 6, 7, 8, 10, 12, 12, 14, 16, 18, 20, 22, 24, 26, 28,
                          30, 32, 34, 36, 38, 40, 42, 44],
                 "event": [1, 2, 1, 0, 1, 2, 0, 1, 0, 2, 1, 0, 0, 2, 1, 0,
                           2, 0, 1, 0, 0, 2, 0, 0],
                 "cause_labels": {"1": "复发", "2": "移植相关死亡"},
                 "time_unit": "月"}

    # ── 数学锁（独立第二实现对拍 + 完备性恒等式 + 高估定理） ──

    def test_cif_math_identity_and_second_impl(self):
        """手算数据：AJ 双实现对拍 + sum(CIF)+S=1 恒等式 + 1−KM ≥ CIF1（高估定理）。"""
        sys.path.insert(0, SCRIPT_DIR)
        import af_v23_stats as st
        times = [1, 2, 2, 3, 3, 4]
        events = [1, 2, 0, 1, 2, 1]
        res = st.aalen_johansen(times, events)
        # 手算期望：t=1: C1=1/6, S=5/6; t=2: C2=1/6, S=2/3; t=3: C1=7/18, C2=7/18,
        # S=2/9; t=4: C1=11/18, S=0
        self.assertAlmostEqual(res["cif"][1][-1], 11 / 18, places=10)
        self.assertAlmostEqual(res["cif"][2][-1], 7 / 18, places=10)
        self.assertAlmostEqual(res["survival"][-1], 0.0, places=10)
        for k, tk in enumerate(res["times"]):
            tot = res["survival"][k] + sum(res["cif"][j][k] for j in res["cif"])
            self.assertAlmostEqual(tot, 1.0, places=10, msg=f"恒等式在 t={tk} 破坏")
        # 独立第二实现（逐次循环重算）
        pairs = sorted(zip(times, events))
        S, c1 = 1.0, 0.0
        for tk in sorted(set(x for x, _ in pairs)):
            Y = sum(1 for x, _ in pairs if x >= tk)
            d1 = sum(1 for x, e in pairs if x == tk and e == 1)
            dall = sum(1 for x, e in pairs if x == tk and e >= 1)
            Sb = S
            if dall:
                S *= 1 - dall / Y
            if d1:
                c1 += Sb * d1 / Y
        self.assertAlmostEqual(c1, res["cif"][1][-1], places=10, msg="第二实现对拍失败")
        # 高估定理：1−KM（竞争当删失）在每个事件时间 ≥ CIF1
        naive = st.km_estimate(times, [1 if e == 1 else 0 for e in events])
        one_minus = [1 - s for s in naive[1]]
        for k, tk in enumerate(naive[0]):
            cif1_at = 0.0
            for tt, vv in zip(res["times"], res["cif"][1]):
                if tt <= tk:
                    cif1_at = vv
            self.assertGreaterEqual(one_minus[k] + 1e-12, cif1_at,
                                    f"1−KM 在 t={tk} 低于 CIF1（高估定理破坏）")

    # ── E2E 渲染与报告 ──

    def test_cif_e2e_render_and_report(self):
        """E2E：demo 渲染 + stderr 报事件计数/末点 CIF/高估百分点。"""
        with tempfile.TemporaryDirectory() as td:
            f = os.path.join(td, "c.json")
            io.open(f, "w", encoding="utf-8").write(json.dumps(self.CIF_DEMO))
            out = os.path.join(td, "c.png")
            r = subprocess.run(
                [sys.executable, GEN, "-t", "cif", "-d", f, "-o", out,
                 "--dpi", "80", "--cjk"],
                capture_output=True, text=True, timeout=240)
            self.assertEqual(r.returncode, 0, r.stderr[-300:])
            self.assertTrue(os.path.exists(out) and os.path.getsize(out) > 10000)
            self.assertIn("目标事件 7", r.stderr)
            self.assertIn("竞争事件 6", r.stderr)
            self.assertIn("删失 11", r.stderr)
            self.assertIn("高估 +", r.stderr)
            self.assertIn("1−KM", r.stderr)
            self.assertIn("cif: 竞争风险下 1−KM 系统性高估", r.stderr)

    def test_cif_realworld_mgus2_renders(self):
        """真实公开数据（Mayo mgus2 1384 例）E2E：恒等式与计数自洽。"""
        src = os.path.join(SCRIPT_DIR, "..", "examples", "realworld",
                           "hematology_mgus_cif.json")
        self.assertTrue(os.path.isfile(src), "mgus2 真实数据示例缺失")
        with tempfile.TemporaryDirectory() as td:
            out = os.path.join(td, "m.png")
            r = subprocess.run(
                [sys.executable, GEN, "-t", "cif", "-d", src, "-o", out,
                 "--dpi", "60"],
                capture_output=True, text=True, timeout=300)
            self.assertEqual(r.returncode, 0, r.stderr[-300:])
            self.assertIn("n=1384", r.stderr)
            self.assertIn("目标事件 115", r.stderr)
            self.assertIn("竞争事件 860", r.stderr)
            # AJ 恒等式：末点两因 CIF 之和 = 1（stderr 报告 3 位小数）
            m = re.search(r"进展（MGUS→浆细胞病） (0\.\d+)，死亡（竞争事件） (0\.\d+)",
                          r.stderr)
            self.assertTrue(m, f"末点报告缺失: {r.stderr[-300:]}")
            self.assertAlmostEqual(float(m.group(1)) + float(m.group(2)), 1.0,
                                   delta=0.002)

    def test_cif_km_autoswitch_guidance(self):
        """机制锁：km 撞到竞争编码自动切 AJ 并引导专用 -t cif。"""
        data = {"groups": {"A": [[1, 1], [2, 2], [3, 0], [4, 1], [5, 2], [6, 1]]}}
        with tempfile.TemporaryDirectory() as td:
            f = os.path.join(td, "k.json")
            io.open(f, "w", encoding="utf-8").write(json.dumps(data))
            out = os.path.join(td, "k.png")
            r = subprocess.run(
                [sys.executable, GEN, "-t", "km", "-d", f, "-o", out,
                 "--dpi", "60", "--no-risk-table"],
                capture_output=True, text=True, timeout=240)
            self.assertEqual(r.returncode, 0, r.stderr[-300:])
            self.assertIn("Aalen-Johansen", r.stderr)
            self.assertIn("-t cif", r.stderr)

    def test_cif_validate_fatals(self):
        """校验：缺 event/groups 多臂/非整数编码/负时间/全删失 全部中文拒绝。"""
        sys.path.insert(0, SCRIPT_DIR)
        import af_v23_charts
        cases = [
            ({"time": [1, 2]}, "'time'+'event'"),
            ({"time": [1, 2], "event": [1]}, "长度必须一致"),
            ({"groups": {"A": [[1, 1]]}, "time": [1], "event": [1]}, "groups"),
            ({"time": [1, 2], "event": [1.5, 0]}, "整数编码"),
            ({"time": [-1, 2], "event": [1, 0]}, "非数值/负数"),
            ({"time": [1, 2], "event": [0, 0]}, "全部为删失"),
            ({"time": [1, 2], "event": [1, 0], "compare_km": "yes"},
             "compare_km"),
        ]
        for data, frag in cases:
            fatal, _w = af_v23_charts.validate_extra("cif", data)
            self.assertTrue(fatal, f"应拒绝: {data}")
            self.assertTrue(any(frag in x for x in fatal),
                            f"报错须含「{frag}」: {fatal}")

    def test_cif_no_competing_auto_off(self):
        """无竞争事件：compare_km=true 自动关（KM 口径正确无需对照）。"""
        data = {"time": [1, 2, 3, 4, 5], "event": [1, 0, 1, 0, 1],
                "compare_km": True}
        with tempfile.TemporaryDirectory() as td:
            f = os.path.join(td, "n.json")
            io.open(f, "w", encoding="utf-8").write(json.dumps(data))
            out = os.path.join(td, "n.png")
            r = subprocess.run(
                [sys.executable, GEN, "-t", "cif", "-d", f, "-o", out,
                 "--dpi", "60"],
                capture_output=True, text=True, timeout=240)
            self.assertEqual(r.returncode, 0, r.stderr[-300:])
            self.assertIn("无竞争事件", r.stderr)
            self.assertNotIn("高估 +", r.stderr)

    def test_cif_percent_axis_and_legend(self):
        """percent 百分轴 + cause_labels 进图例（matplotlib 内省）。"""
        sys.path.insert(0, SCRIPT_DIR)
        import af_v23_charts
        import matplotlib.pyplot as plt
        theme = {"colors": ["#2E5E8F", "#FFB627"], "font_size": 10}
        data = dict(self.CIF_DEMO)
        data["percent"] = True
        fig = plt.figure(figsize=(6, 4))
        ax = fig.add_subplot(111)
        try:
            af_v23_charts.gen_cif(data, ax, theme, None)
            self.assertIn("%", ax.get_ylabel())
            labels = ax.get_legend().get_texts()
            words = " ".join(t.get_text() for t in labels)
            self.assertIn("复发", words)
            self.assertIn("移植相关死亡", words)
            self.assertIn("1−KM", words)
            self.assertLessEqual(max(max(ln.get_ydata()) for ln in ax.lines),
                                 100.0 + 1e-9, "percent 轴下曲线应 ≤100")
        finally:
            plt.close(fig)

    def test_cif_template_and_realworld_commands_run(self):
        """模板与 realworld 示例 _command 逐字可执行（-o 重定向临时目录防泄漏）。"""
        with tempfile.TemporaryDirectory() as td:
            for rel in ("templates/cif.json",
                        "examples/realworld/hematology_mgus_cif.json"):
                src = os.path.join(SCRIPT_DIR, "..", rel)
                d = json.load(io.open(src, encoding="utf-8"))
                cmd = d["_command"].split()
                assert "-o" in cmd, rel
                out = os.path.join(td, os.path.basename(rel) + ".png")
                i = cmd.index("-o")
                cmd[i + 1] = out
                r = subprocess.run(cmd, capture_output=True, text=True,
                                   timeout=300, cwd=os.path.join(SCRIPT_DIR, ".."))
                self.assertEqual(r.returncode, 0, f"{rel}: {r.stderr[-300:]}")
                self.assertTrue(os.path.exists(out) and
                                os.path.getsize(out) > 10000, rel)

    def test_list_types_authoritative(self):
        """--list-types 输出权威注册表（MCP 动态清单数据源）。"""
        r = subprocess.run([sys.executable, GEN, "--list-types"],
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0)
        ts = [x.strip() for x in r.stdout.splitlines() if x.strip()]
        self.assertIn("cif", ts)
        self.assertIn("galbraith", ts)
        self.assertGreaterEqual(len(ts), 37)
        sys.path.insert(0, SCRIPT_DIR)
        import gen_figure
        self.assertEqual(set(ts), set(gen_figure.GENERATORS),
                         "--list-types 与注册表不一致")

    def test_antipatterns_page_wired(self):
        """反模式集中页存在、含五类反模式与交叉链接，SKILL 双语路由直达。"""
        root = os.path.join(SCRIPT_DIR, "..")
        p = os.path.join(root, "references", "antipatterns.md")
        self.assertTrue(os.path.isfile(p), "antipatterns.md 缺失")
        t = io.open(p, encoding="utf-8").read()
        for kw in ("pitfalls.md", "faq.md", "limits.md", "Aalen-Johansen"):
            self.assertIn(kw, t)
        for sk in ("SKILL.md", "SKILL_ZH.md"):
            s = io.open(os.path.join(root, sk), encoding="utf-8").read()
            self.assertIn("antipatterns.md", s, f"{sk} 未链接反模式集中页")

    def test_v570_counts_and_docs(self):
        """v5.7.0 计数锁：38 模板/注册表≥38/README 38 chart types/realworld 13（v5.9 +rmst/+colon）。"""
        root = os.path.join(SCRIPT_DIR, "..")
        jsons = glob.glob(os.path.join(root, "templates", "*.json"))
        self.assertEqual(len(jsons), 39, f"templates 应 39: {len(jsons)}")
        sys.path.insert(0, SCRIPT_DIR)
        import gen_figure
        self.assertGreaterEqual(len(gen_figure.GENERATORS) - 3, 39)
        readme = io.open(os.path.join(root, "README.md"), encoding="utf-8").read()
        self.assertIn("39 chart types", readme)
        rws = [x for x in os.listdir(os.path.join(root, "examples", "realworld"))
               if x.endswith(".json")]
        self.assertEqual(len(rws), 17, f"realworld 应 17: {rws}")
        from af_diagnostics import _KNOWN_DATA_FIELDS
        for fld in ("cause_labels", "compare_km", "percent", "time_unit", "y_max"):
            self.assertIn(fld, _KNOWN_DATA_FIELDS, f"cif 字段 {fld} 须进白名单")

    def test_cif_galbraith_polish_locked(self):
        """v5.7 galbraith polish 机制锁：弧线最小间距 0.075 + 无效应线小标
        + 弧线-读数原子性（读数放不下→整条弧线省略，弧 patch 数==读数数）。"""
        sys.path.insert(0, SCRIPT_DIR)
        src = io.open(os.path.join(SCRIPT_DIR, "af_v23_charts.py"),
                      encoding="utf-8").read()
        self.assertIn("* 0.075", src, "弧线最小间距应放宽为轴跨度 7.5%")
        self.assertIn('_rl = f"{est_label}=1"', src, "无效应线小标缺失")
        self.assertIn("整条省略", src, "弧线-读数原子性机制缺失")
        # 行为锁：galbraith 模板渲染，弧 patch 数==RR 读数标注数（一一配对）
        import matplotlib.pyplot as plt
        import af_v23_charts
        from matplotlib.patches import Arc
        demo = json.load(io.open(os.path.join(SCRIPT_DIR, "..",
                                              "templates", "galbraith.json"),
                                 encoding="utf-8"))
        theme = {"colors": ["#2E5E8F", "#FFB627"], "font_size": 10}
        fig = plt.figure(figsize=(7, 5))
        ax = fig.add_subplot(111)
        try:
            af_v23_charts.gen_galbraith(demo, ax, theme, None)
            _lbl = str(demo.get("est_label") or "OR").upper()
            n_arc = sum(1 for p in ax.patches if isinstance(p, Arc))
            n_rr = sum(1 for t in ax.texts if t.get_text().startswith(f"{_lbl}=")
                       and t.get_text() != f"{_lbl}=1")
            self.assertGreater(n_arc, 0, "应至少保留一条弧线")
            self.assertEqual(n_arc, n_rr,
                             f"弧线({n_arc})与读数({n_rr})必须一一配对")
        finally:
            plt.close(fig)



class TestV50800(unittest.TestCase):
    """v5.8.0：subgroup --stats cox 一键亚组拟合（逐层 Cox HR+似然比 P 交互+Overall）。
    金标=veteran 真实队列（R survival 公开数据，tests/fixtures/veteran.csv）——
    含 ep35 制备脚本标签-掩膜对调事故的回归锁（层名-层值-层 n 全钉死）。"""

    @classmethod
    def _cli(cls, extra, out):
        cmd = [sys.executable, GEN, "-t", "subgroup", "--stats", "cox", "-o", out, "--cjk",
               "-d", os.path.join(SCRIPT_DIR, "..", "tests", "fixtures", "veteran.csv"),
               "--cox-event", "status", "--treatment", "trt"] + extra
        return subprocess.run(cmd, capture_output=True, text=True)

    def test_veteran_golden_full_table(self):
        """veteran 全表金标：总体/四组 P 交互/层 est-n 逐项钉死（ep35 标签对调事故回归锁）。"""
        out = os.path.join(tempfile.mkdtemp(), "sg_gold.png")
        r = self._cli(["--subgroup-vars", "celltype,prior", "--cut", "age@60",
                       "--cut", "karno@50"], out)
        self.assertEqual(r.returncode, 0, r.stderr[-600:])
        self.assertTrue(os.path.exists(out))
        # 总体
        self.assertIn("总体 HR=1.02（0.71~1.45）p=0.9218", r.stderr)
        # P 交互四组（似然比，与 ep35 制备脚本同核）
        self.assertIn("celltype（P 交互=0.042）", r.stderr)
        self.assertIn("prior（P 交互=0.087）", r.stderr)
        self.assertIn("age（P 交互=0.239）", r.stderr)
        self.assertIn("karno（P 交互=0.024）", r.stderr)   # --cut karno@50 即 ＜50/≥50 切法
        # 层值+层 n（标签-掩膜对调事故锁：n 必须与层名对得上）
        self.assertIn("squamous:0.54(0.25~1.18,n=35)", r.stderr)
        self.assertIn("large:1.54(0.69~3.41,n=27)", r.stderr)
        self.assertIn("≥60:1.17(0.75~1.82,n=84)", r.stderr)
        self.assertIn("＜60:0.79(0.43~1.45,n=53)", r.stderr)
        self.assertIn("≥50:0.87(0.57~1.33,n=99)", r.stderr)
        self.assertIn("＜50:1.35(0.67~2.73,n=38)", r.stderr)

    def test_synthetic_hr_and_interaction_identities(self):
        """合成恒等式：指数生存已知 HR≈2；无交互构造 p>0.05；强交互构造 p<0.01。"""
        sys.path.insert(0, SCRIPT_DIR)
        import af_v23_stats as st
        rng = np.random.default_rng(58)
        n = 900

        def sim(lam_trt, lam_ctl):
            t, e, trt = [], [], []
            for i in range(n):
                arm = i % 2
                lam = lam_trt if arm else lam_ctl
                t.append(float(rng.exponential(1.0 / lam)))
                e.append(1)
                trt.append(float(arm))
            return np.array(t), np.array(e), np.array(trt, dtype=float)

        t, e, trt = sim(2.0, 1.0)                       # HR 真值=2
        grp = np.repeat([0, 1], n // 2)                  # 组=块状（与臂 i%2 独立）
        r = st.cox_subgroup_analysis(t, e, trt, [("A", grp)])
        self.assertAlmostEqual(r["overall"]["est"], 2.0, delta=0.3)
        # 无交互：两层层内 HR 都≈2 → P 交互应不显著
        self.assertGreater(r["subgroups"][0]["p_interaction"], 0.05)
        # 强交互：层 0 无效应（HR=1），层 1 强效应（HR=3）
        lam = np.where(np.repeat(np.array([0, 1]), n // 2) == 0, 1.0, 1.0)
        hr_layer = np.repeat([1.0, 3.0], n // 2)
        t2 = np.where(trt == 1, rng.exponential(1.0 / (1.0 * hr_layer)),
                      rng.exponential(1.0, size=n))   # ⛔标量参数只抽一个值（size 必须显式）
        r2 = st.cox_subgroup_analysis(t2, e, trt, [("B", grp)])
        self.assertLess(r2["subgroups"][0]["p_interaction"], 0.01)
        self.assertAlmostEqual(r2["subgroups"][0]["levels"][0]["est"], 1.0, delta=0.3)
        self.assertGreater(r2["subgroups"][0]["levels"][1]["est"], 2.0)

    def test_label_order_rule(self):
        """层序规则：文本层=数据出现序（尊重临床/因子惯例），数值层=数值序。"""
        sys.path.insert(0, SCRIPT_DIR)
        import af_v23_stats as st
        rng = np.random.default_rng(7)
        t = np.abs(rng.normal(5, 2, 120)) + 0.1
        e = np.ones(120)
        trt = np.tile([0.0, 1.0], 60)
        # 文本：故意让 "乙" 先出现
        g = np.array(["乙"] * 60 + ["甲"] * 60)
        r = st.cox_subgroup_analysis(t, e, trt, [("组", g)])
        self.assertEqual([lv["name"] for lv in r["subgroups"][0]["levels"]], ["乙", "甲"])
        gn = np.repeat([3, 1], 60)                       # 块状：层内 trt 仍交替
        r2 = st.cox_subgroup_analysis(t, e, trt, [("分档", gn)])
        self.assertEqual([lv["name"] for lv in r2["subgroups"][0]["levels"]], ["1", "3"])

    def test_guards_chinese_messages(self):
        """守卫路径：缺参/连续列/多臂/小层/处理臂兼任亚组——中文报错一一对应。"""
        out = os.path.join(tempfile.mkdtemp(), "x.png")
        r = self._cli([], out)                                   # 缺 --subgroup-vars
        self.assertEqual(r.returncode, 1)
        self.assertIn("--subgroup-vars", r.stderr)
        r = self._cli(["--subgroup-vars", "karno"], out)         # 连续列未 cut
        self.assertIn("--cut", r.stderr)
        r = self._cli(["--subgroup-vars", "celltype", "--treatment", "celltype"], out)
        self.assertIn("处理臂", r.stderr)
        # 三取值处理臂
        import csv as _csv
        fixture = os.path.join(SCRIPT_DIR, "..", "tests", "fixtures", "veteran.csv")
        rows = list(_csv.DictReader(open(fixture, encoding="utf-8")))
        rows[0]["trt"] = "9"
        p3 = fixture + ".tmp3.csv"
        with open(p3, "w", newline="", encoding="utf-8") as fh:
            w = _csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        r = subprocess.run([sys.executable, GEN, "-t", "subgroup", "--stats", "cox",
                            "-o", out, "-d", p3, "--cox-event", "status",
                            "--treatment", "trt", "--subgroup-vars", "celltype"],
                           capture_output=True, text=True)
        os.remove(p3)
        self.assertIn("两个取值", r.stderr)
        rows[0]["trt"] = "1"                             # 还原：小层夹具不带三臂变异
        # 小层（两臂混合抽样 27 行：celltype 各层必 <10 例）
        p4 = fixture + ".tmp4.csv"
        mixed = []
        for ct in ("squamous", "smallcell", "adeno", "large"):
            for arm in ("1", "2"):
                mixed += [r for r in rows if r["trt"] == arm and r["celltype"] == ct][:4]
        # 每病理层 8 例（<10）；四层齐全（≥2 层先过层数守卫）
        with open(p4, "w", newline="", encoding="utf-8") as fh:
            w = _csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(mixed)
        r = subprocess.run([sys.executable, GEN, "-t", "subgroup", "--stats", "cox",
                            "-o", out, "-d", p4, "--cox-event", "status",
                            "--treatment", "trt", "--subgroup-vars", "celltype"],
                           capture_output=True, text=True)
        os.remove(p4)
        self.assertIn("至少约 10 例", r.stderr)




class TestV50900(unittest.TestCase):
    """v5.9.0：rmst 限制平均生存时间（第 38 种图型）——阶梯积分 RMST+
    Greenwood 型方差+两臂差值 CI。含 τ 截断回归锁（10-23 双实现对拍抓出
    方差循环未按 τ 截断的出厂前 bug）+ colon 真实队列金标。"""

    # math 用模块级导入（类属性不进方法作用域）

    HAND = {"times": [1, 2, 3, 1, 2, 3], "events": [1, 1, 1, 1, 0, 0],
            "arm": ["A", "A", "A", "B", "B", "B"]}

    def test_rmst_hand_calc_identity(self):
        """手算恒等式：A=[1,2,3]全事件 RMST=2.0 Var=2/9；B=[1,2,3]仅 t=1 事件
        RMST=7/3 Var=8/27；Δ=A−B=−1/3，SE=√(14/27)，p=erfc(|Z|/√2)。"""
        sys.path.insert(0, SCRIPT_DIR)
        import af_v23_stats as st
        h = self.HAND
        r = st.rmst_analysis(h["times"], h["events"], h["arm"], tau=3.0)
        g = r["groups"]
        self.assertAlmostEqual(g[0]["rmst"], 2.0, places=10)
        self.assertAlmostEqual(g[0]["var"], 2 / 9, places=10)
        self.assertAlmostEqual(g[1]["rmst"], 7 / 3, places=10)
        self.assertAlmostEqual(g[1]["var"], 8 / 27, places=10)
        d = r["difference"]
        self.assertAlmostEqual(d["diff"], -1 / 3, places=10)
        self.assertAlmostEqual(d["se"], math.sqrt(14 / 27), places=10)
        self.assertAlmostEqual(d["p"], math.erfc((1 / 3) / math.sqrt(14 / 27) / math.sqrt(2)), places=10)

    def test_rmst_second_impl_and_tau_truncation_lock(self):
        """τ 截断回归锁：含 τ 后事件时间的资料，方差必须与独立循环实现一致
        （10-23 出厂前 bug：方差循环未按 τ 截断，混入 t>τ 垃圾项）。"""
        sys.path.insert(0, SCRIPT_DIR)
        import af_v23_stats as st

        def loop_rmst(pairs, tau):
            pairs = sorted(pairs)
            ev = {}
            for t, e in pairs:
                if e == 1:
                    ev[t] = ev.get(t, 0) + 1
            uts = sorted(ev)
            S, area, prev = 1.0, 0.0, 0.0
            for ut in uts:
                if ut >= tau:
                    break
                d = ev[ut]; Y = sum(1 for t, _ in pairs if t >= ut)
                area += S * (ut - prev); prev = ut; S *= (1 - d / Y)
            area += S * (tau - prev)
            def at(T):
                Sx, a_, p_ = 1.0, 0.0, 0.0
                for ut in uts:
                    if ut >= T:
                        break
                    dd = ev[ut]; Yn = sum(1 for t, _ in pairs if t >= ut)
                    a_ += Sx * (ut - p_); p_ = ut; Sx *= (1 - dd / Yn)
                return a_ + Sx * (T - p_)
            var = 0.0
            for ut in uts:
                if ut > tau:
                    break
                dd = ev.get(ut, 0); Yn = sum(1 for t, _ in pairs if t >= ut)
                den = Yn * (Yn - dd)
                if den > 0:
                    var += (area - at(ut)) ** 2 * dd / den
            return area, var

        rng = np.random.default_rng(59)
        t0 = np.abs(rng.normal(400, 250, 260)) + 1
        t0[::5] += 900                       # 制造 τ 之后的事件时间
        e0 = np.where(rng.random(260) < 0.7, 1, 0)
        a0 = np.tile([0.0, 1.0], 130)
        tau = 800.0
        r = st.rmst_analysis(t0, e0, a0, tau)
        for gi, m in ((0, a0 == 0.0), (1, a0 == 1.0)):
            A, V = loop_rmst(list(zip(t0[m].tolist(), e0[m].tolist())), tau)
            self.assertAlmostEqual(r["groups"][gi]["rmst"], A, places=6)
            self.assertAlmostEqual(r["groups"][gi]["var"], V, places=6)

    def test_rmst_extrapolation_and_coverage(self):
        """随访不足 τ：平尾外推标注 + 覆盖率报告。"""
        sys.path.insert(0, SCRIPT_DIR)
        import af_v23_stats as st
        t = [10, 20, 30, 40, 50, 10, 20, 30, 40, 50]
        e = [1, 1, 0, 1, 1, 1, 0, 1, 1, 0]
        r = st.rmst_analysis(t, e, ["X"] * 5 + ["Y"] * 5, tau=200)
        for g in r["groups"]:
            self.assertTrue(g["extrapolated"])
            self.assertLess(g["coverage"], 1.0)

    def test_rmst_guards(self):
        """守卫：三臂/τ 非正/组例数过少——中文报错。"""
        sys.path.insert(0, SCRIPT_DIR)
        import af_v23_stats as st
        with self.assertRaises(ValueError) as cm:
            st.rmst_analysis([1, 2, 3], [1, 1, 1], ["A", "B", "C"], tau=2)
        self.assertIn("两个取值", str(cm.exception))
        with self.assertRaises(ValueError) as cm:
            st.rmst_analysis([1, 2], [1, 1], ["A", "B"], tau=0)
        self.assertIn("正数", str(cm.exception))
        r_ok = st.rmst_analysis([1, 2, 3, 4], [1, 1, 1, 1], ["A", "A", "B", "B"], tau=2)
        self.assertEqual(r_ok["groups"][0]["n"], 2)   # 4 例合法（组≥5 对的守卫在渲染层）

    def test_rmst_cli_colon_golden(self):
        """colon 真实队列（R survival 公开数据，tests/fixtures/colon_rmst.csv，
        rx=Obs vs Lev+5FU，etype=2 复发终点，n=619）CLI 端到端金标：
        τ 自动=1607；RMST 1308.2/1220.1；Δ=88.1（95%CI 10.5~165.6）P=0.026。"""
        import csv as _csv
        fixture = os.path.join(SCRIPT_DIR, "..", "tests", "fixtures", "colon_rmst.csv")
        rows = list(_csv.DictReader(open(fixture, encoding="utf-8")))
        groups = {}
        for r in rows:
            groups.setdefault(r["rx"], []).append([float(r["time"]), float(r["status"])])
        for g in groups:
            groups[g].sort()
        payload = {"groups": groups, "time_unit": "天"}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
            dj = fh.name
        out = os.path.join(tempfile.mkdtemp(), "rmst_gold.png")
        r = subprocess.run([sys.executable, GEN, "-t", "rmst", "-d", dj, "-o", out, "--cjk"],
                           capture_output=True, text=True)
        os.remove(dj)
        self.assertEqual(r.returncode, 0, r.stderr[-500:])
        self.assertTrue(os.path.exists(out))
        self.assertIn("τ=1607天（自动=最小最大随访之半）", r.stderr)
        self.assertIn("Lev+5FU: RMST=1308.2天（95%CI 1253.6~1362.7，n=304，事件=123", r.stderr)
        self.assertIn("Obs: RMST=1220.1天（95%CI 1165.0~1275.2，n=315，事件=168", r.stderr)
        self.assertIn("ΔRMST（1−0）=88.1天（95%CI 10.5~165.6），Z=2.226，P=0.026", r.stderr)




class TestV59100(unittest.TestCase):
    """v5.9.1：cif cause_labels 键↔标签一致性守卫（10-24 全谱审计——ep35 标签
    对调同类的唯一键间接映射点补防：错码一票否决、缺标软提示默认名）。"""

    BASE = {"time": [3, 5, 6, 7, 8, 10, 12, 14, 16, 18, 20, 22, 24, 26, 28, 30,
                     32, 34, 36, 38, 40, 42, 44, 46],
            "event": [1, 2, 1, 0, 1, 2, 0, 1, 0, 2, 1, 0, 0, 2, 1, 0, 2, 0, 1, 0,
                      0, 2, 0, 1],
            "time_unit": "月"}

    def _run(self, labels):
        d = dict(self.BASE)
        d["cause_labels"] = labels
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8") as fh:
            json.dump(d, fh, ensure_ascii=False)
            dj = fh.name
        out = os.path.join(tempfile.mkdtemp(), "cif.png")
        r = subprocess.run([sys.executable, GEN, "-t", "cif", "-d", dj, "-o", out,
                            "--cjk"], capture_output=True, text=True)
        os.remove(dj)
        return r

    def test_wrong_code_fatal(self):
        """cause_labels 引用数据中不存在的码 → exit 3 + 可搜索中文报错。"""
        r = self._run({"1": "复发", "3": "写错的码"})
        self.assertEqual(r.returncode, 3, r.stderr[-400:])
        self.assertIn("不存在的事件码 3", r.stderr)
        self.assertIn("码与含义写反", r.stderr)

    def test_missing_label_soft_note(self):
        """在场的竞争码未给标签 → exit 0 + stderr 默认名提示。"""
        r = self._run({})
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        self.assertIn("竞争事件码 2 未给 cause_labels", r.stderr)
        self.assertIn("原因2", r.stderr)

    def test_valid_labels_pass(self):
        r = self._run({"1": "复发", "2": "移植相关死亡"})
        self.assertEqual(r.returncode, 0, r.stderr[-400:])




class TestV60000(unittest.TestCase):
    """v6.0.0：CSV 误差列约定（官方评测点名 CSV 不支持误差棒的机制根治）——
    X_se/X_sem/X_sd/X_err 后缀列自动配对主列转 errors 契约；X_sd+X_n →
    SEM=sd/√n；孤儿/行数不符告警保留；--csv-no-errors 逃生门。"""

    def _csv(self, content, name="err.csv"):
        p = os.path.join(tempfile.mkdtemp(), name)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(content)
        return p

    def _run(self, p, extra=()):
        out = os.path.join(tempfile.mkdtemp(), "b.png")
        r = subprocess.run([sys.executable, GEN, "-t", "bar", "-d", p, "-o", out,
                            "--cjk"] + list(extra), capture_output=True, text=True)
        return r

    def test_sem_pairing_and_errors_contract(self):
        """X_sem → errors 契约 + 误差列不再画成柱 + stderr 识别行。"""
        p = self._csv("组,评分,评分_sem\nA,7.2,0.4\nB,8.1,0.6\nC,6.5,0.3\n")
        sys.path.insert(0, SCRIPT_DIR)
        import gen_figure as gf
        d = gf.load_data(p, chart_type="bar")
        self.assertEqual(d["errors"], {"评分": [0.4, 0.6, 0.3]})
        self.assertNotIn("评分_sem", d["series"])
        r = self._run(p)
        self.assertEqual(r.returncode, 0, r.stderr[-300:])
        self.assertIn("CSV 误差列识别：评分_sem → 评分（±1SEM）", r.stderr)

    def test_sd_plus_n_to_sem(self):
        """X_sd+X_n → SEM=sd/√n 数值级断言。"""
        p = self._csv("组,活力,活力_sd,活力_n\nA,72.5,8.4,36\nB,61.3,9.1,49\n")
        sys.path.insert(0, SCRIPT_DIR)
        import gen_figure as gf
        d = gf.load_data(p, chart_type="bar")
        import math
        self.assertAlmostEqual(d["errors"]["活力"][0], 8.4 / 6, places=12)
        self.assertAlmostEqual(d["errors"]["活力"][1], 9.1 / 7, places=12)
        self.assertNotIn("活力_n", d["series"])
        r = self._run(p)
        self.assertIn("SD+标本量→SEM=sd/√n", r.stderr)

    def test_orphan_and_mismatch_warnings(self):
        """孤儿列/行数不符 → 告警 + 按普通数据列保留。"""
        p = self._csv("组,评分,孤儿_sem\nA,7.2,0.4\nB,8.1,0.6\n")
        sys.path.insert(0, SCRIPT_DIR)
        import gen_figure as gf
        d = gf.load_data(p, chart_type="bar")
        self.assertIn("孤儿_sem", d["series"])      # 保留为普通列
        self.assertNotIn("errors", d)
        r = self._run(p)
        self.assertIn("找不到主列「孤儿」", r.stderr)

    def test_escape_hatch(self):
        """--csv-no-errors：后缀列按普通数据画，零识别输出。"""
        p = self._csv("组,评分,评分_sem\nA,7.2,0.4\nB,8.1,0.6\n")
        r = self._run(p, ["--csv-no-errors"])
        self.assertEqual(r.returncode, 0, r.stderr[-300:])
        self.assertNotIn("CSV 误差列识别", r.stderr)
        sys.path.insert(0, SCRIPT_DIR)
        import gen_figure as gf
        d = gf.load_data(p, chart_type="bar", csv_errors=False)
        self.assertIn("评分_sem", d["series"])

    def test_plain_csv_unchanged(self):
        """无后缀常规 CSV：产出结构零变化（防误伤回归锁）。"""
        p = self._csv("组,甲,乙\nA,1.0,2.0\nB,3.0,4.0\n")
        sys.path.insert(0, SCRIPT_DIR)
        import gen_figure as gf
        d = gf.load_data(p, chart_type="bar")
        self.assertEqual(d["series"], {"甲": [1.0, 3.0], "乙": [2.0, 4.0]})
        self.assertNotIn("errors", d)




class TestV61000(unittest.TestCase):
    """v6.1.0：rob 偏倚风险交通灯图（第 39 图型，robvis 同款）——真实数据
    （robvis 包 data_rob2，9 研究×5 域+Overall）各域计数金标+守卫三态+
    --suggest 两条新规则。"""

    GOLDEN_CSV = os.path.join(SCRIPT_DIR, "..", "examples", "realworld",
                              "evidence_rob2_metformin.json")

    def test_real_data_counts_golden(self):
        """真实数据（robvis data_rob2）各域计数金标：D1 6/1/1/1、D4 8/0/1/0，
        Overall 不入摘要；CLI e2e exit 0。"""
        out = os.path.join(tempfile.mkdtemp(), "rob.png")
        r = subprocess.run([sys.executable, GEN, "-t", "rob", "-d", self.GOLDEN_CSV,
                            "-o", out, "--cjk"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr[-500:])
        self.assertTrue(os.path.exists(out))
        self.assertIn("9 项研究 × 5 个偏倚域（含 Overall 列，未计入摘要）", r.stderr)
        self.assertIn("偏倚来自随机化: 低 67%（6）｜顾虑 11%（1）｜高 11%（1）｜极高 0%（0）｜无信息 11%（1）", r.stderr)
        self.assertIn("偏倚来自结局测量: 低 89%（8）｜顾虑 0%（0）｜高 11%（1）｜极高 0%（0）", r.stderr)
        self.assertIn("偏倚来自结局数据缺失: 低 44%（4）｜顾虑 33%（3）｜高 22%（2）｜极高 0%（0）", r.stderr)

    def test_guards(self):
        """守卫：未知判定值/缺域/研究数不足——中文报错。"""
        import json as _json
        base = _json.load(open(self.GOLDEN_CSV, encoding="utf-8"))
        bad = _json.loads(_json.dumps(base))
        bad["studies"][0]["judgements"]["D1"] = "不确定"
        p = os.path.join(tempfile.mkdtemp(), "bad.json")
        _json.dump(bad, open(p, "w", encoding="utf-8"), ensure_ascii=False)
        out = os.path.join(tempfile.mkdtemp(), "x.png")
        r = subprocess.run([sys.executable, GEN, "-t", "rob", "-d", p, "-o", out],
                           capture_output=True, text=True)
        os.remove(p)
        self.assertEqual(r.returncode, 3, r.stderr[-300:])
        self.assertIn("无法识别", r.stderr)
        bad2 = _json.loads(_json.dumps(base))
        del bad2["studies"][2]["judgements"]["D3"]
        p = os.path.join(tempfile.mkdtemp(), "bad2.json")
        _json.dump(bad2, open(p, "w", encoding="utf-8"), ensure_ascii=False)
        r = subprocess.run([sys.executable, GEN, "-t", "rob", "-d", p, "-o", out],
                           capture_output=True, text=True)
        os.remove(p)
        self.assertIn("缺少域", r.stderr)
        r = subprocess.run([sys.executable, GEN, "-t", "rob", "-d", "/dev/null", "-o", out],
                           capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)

    def test_suggest_rules(self):
        """--suggest：rob 结构与原始 time/event/group 长表两条新规则。"""
        import json as _json
        base = _json.load(open(self.GOLDEN_CSV, encoding="utf-8"))
        slim = {"studies": base["studies"][:3]}
        p = os.path.join(tempfile.mkdtemp(), "rob_suggest.json")
        _json.dump(slim, open(p, "w", encoding="utf-8"), ensure_ascii=False)
        r = subprocess.run([sys.executable, GEN, "--suggest", "-d", p],
                           capture_output=True, text=True)
        os.remove(p)
        self.assertIn("rob", r.stdout, r.stdout[:400])
        csvp = os.path.join(tempfile.mkdtemp(), "raw.csv")
        with open(csvp, "w", encoding="utf-8") as fh:
            fh.write("time,event,group\n100,1,A\n200,0,A\n150,1,B\n80,0,B\n")
        r = subprocess.run([sys.executable, GEN, "--suggest", "-d", csvp],
                           capture_output=True, text=True)
        os.remove(csvp)
        self.assertIn("--stats cox", r.stdout, r.stdout[:400])




class TestV61100(unittest.TestCase):
    """v6.1.1：rob 判定扩展（Very high 极高档+Unclear 归顾虑档）——三种官方工具
    真实数据（robvis 包 data_rob1/data_robins_e/data_quadas）金标+图例全域扫描
    回归锁（循环变量泄漏事故：图例在场判定只查最后一域导致漏档）。"""

    def _run(self, name):
        p = os.path.join(SCRIPT_DIR, "..", "examples", "realworld", name)
        out = os.path.join(tempfile.mkdtemp(), "rob.png")
        r = subprocess.run([sys.executable, GEN, "-t", "rob", "-d", p, "-o", out,
                            "--cjk"], capture_output=True, text=True)
        return r

    def test_rob1_unclear_mapping(self):
        """ROB1：Unclear→顾虑档；D1 计数 6/2/1 金标。"""
        r = self._run("evidence_rob1_cochrane_rct.json")
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        self.assertIn("9 项研究 × 7 个偏倚域（含 Overall 列，未计入摘要）", r.stderr)
        self.assertIn("随机序列生成: 低 67%（6）｜顾虑 22%（2）｜高 11%（1）｜极高 0%（0）｜无信息 0%（0）", r.stderr)
        self.assertIn("结局评估盲法: 低 44%（4）｜顾虑 33%（3）｜高 22%（2）", r.stderr)

    def test_robins_very_high(self):
        """ROBINS-I：Very high→极高档；含 VH 域与 Overall 计数金标。"""
        r = self._run("evidence_robins_nonrandomized.json")
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        self.assertIn("10 项研究 × 7 个偏倚域", r.stderr)
        self.assertIn("混杂: 低 0%（0）｜顾虑 60%（6）｜高 30%（3）｜极高 0%（0）｜无信息 10%（1）", r.stderr)
        self.assertIn("研究对象选择: 低 40%（4）｜顾虑 40%（4）｜高 10%（1）｜极高 10%（1）", r.stderr)

    def test_quadas_golden(self):
        """QUADAS-2：12 研究×4 域；含 NI 域金标。"""
        r = self._run("evidence_quadas_diagnostic.json")
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        self.assertIn("12 项研究 × 4 个偏倚域", r.stderr)
        self.assertIn("流程与时点: 低 75%（9）｜顾虑 8%（1）｜高 8%（1）｜极高 0%（0）｜无信息 8%（1）", r.stderr)
        self.assertIn("参考标准: 低 50%（6）｜顾虑 33%（4）｜高 17%（2）", r.stderr)

    def test_unknown_still_fatal(self):
        """未知判定仍一票否决（扩展同义词后不放松）。"""
        import json as _json
        base = _json.load(open(os.path.join(
            SCRIPT_DIR, "..", "examples", "realworld", "evidence_quadas_diagnostic.json"),
            encoding="utf-8"))
        base["studies"][0]["judgements"]["D1"] = "大概没问题"
        p = os.path.join(tempfile.mkdtemp(), "bad.json")
        _json.dump(base, open(p, "w", encoding="utf-8"), ensure_ascii=False)
        out = os.path.join(tempfile.mkdtemp(), "x.png")
        r = subprocess.run([sys.executable, GEN, "-t", "rob", "-d", p, "-o", out],
                           capture_output=True, text=True)
        os.remove(p)
        self.assertEqual(r.returncode, 3, r.stderr[-300:])
        self.assertIn("无法识别", r.stderr)





class TestV62000(unittest.TestCase):
    """v6.2.0：稳定性双修——瞬时失败自动重试（--retries）+ --timeout 0 硬保险。
    另含看门狗小预算精度修复（_monitor 步长跟随预算，<1s 预算此前永不触发）。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.data = os.path.join(cls.tmp, "bar.json")
        with open(cls.data, "w", encoding="utf-8") as f:
            json.dump({"title": "稳定性探针", "x_label": "组别", "y_label": "数值",
                       "labels": ["W1", "W2", "W3", "W4", "W5"],
                       "series": {"对照": [10.2, 11.1, 10.8, 11.3, 10.9],
                                  "处理": [13.8, 14.2, 13.5, 14.6, 14.1]}},
                      f, ensure_ascii=False)

    def _run(self, *extra, out="o.png", data=None):
        p = os.path.join(self.tmp, out)
        r = subprocess.run([sys.executable, GEN, "-t", "bar",
                            "-d", data or self.data, "-o", p, *extra],
                           capture_output=True, text=True, timeout=300)
        return r, p

    def test_retry_success_transparent(self):
        """极小预算逼出超时→自动重试链最终成功：exit 0+stderr 透明记录+与一次成功逐字节一致。"""
        r, p = self._run("--timeout", "0.2", "--retries", "5", out="retry_ok.png")
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        self.assertIn("——自动重试第", r.stderr)
        self.assertTrue(os.path.exists(p) and os.path.getsize(p) > 10000)
        r1, p1 = self._run(out="once.png")
        self.assertEqual(r1.returncode, 0, r1.stderr[-400:])
        self.assertNotIn("——自动重试第", r1.stderr)
        with open(p, "rb") as a, open(p1, "rb") as b:
            self.assertEqual(a.read(), b.read(), "重试成功输出必须与一次成功输出逐字节一致")

    def test_retries_zero_disables(self):
        """--retries 0：超时单代失败 exit 5，无重试。"""
        r, _ = self._run("--timeout", "0.1", "--retries", "0", out="r0.png")
        self.assertEqual(r.returncode, 5, r.stderr[-300:])
        self.assertNotIn("——自动重试第", r.stderr)

    def test_retries_exhausted_exact_count(self):
        """重试用尽：仍 exit 5，重试次数恰为 --retries 值。"""
        r, _ = self._run("--timeout", "0.1", "--retries", "1", out="r1.png")
        self.assertEqual(r.returncode, 5, r.stderr[-300:])
        self.assertEqual(r.stderr.count("——自动重试第"), 1)

    def test_deterministic_errors_never_retry(self):
        """确定性数据错误（exit 3）不重试：--retries 3 也只跑一代。"""
        bad = os.path.join(self.tmp, "bad.json")
        with open(bad, "w", encoding="utf-8") as f:
            json.dump({"title": "x", "series": {"对照": [10.2, 11.1, 10.8]}},
                      f, ensure_ascii=False)
        r, _ = self._run("--retries", "3", out="bad.png", data=bad)
        self.assertEqual(r.returncode, 3, r.stderr[-300:])
        self.assertNotIn("——自动重试第", r.stderr)

    def test_timeout_negative_rejected(self):
        """负数预算 exit 1 并给出准确指引。"""
        r, _ = self._run("--timeout", "-5", out="neg.png")
        self.assertEqual(r.returncode, 1, r.stderr[-300:])
        self.assertIn("不接受负数", r.stderr)
        self.assertIn("--timeout 0", r.stderr)

    def test_timeout_zero_keeps_hard_cap(self):
        """--timeout 0：禁用自适应预算但硬保险武装（警告+渲染成功）。"""
        r, p = self._run("--timeout", "0", out="hard.png")
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        self.assertIn("硬保险", r.stderr)
        self.assertTrue(os.path.exists(p))

    def test_retries_flag_documented(self):
        """--retries 进 CLI help（中文描述完整）。"""
        r = subprocess.run([sys.executable, GEN, "--help"],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0)
        self.assertIn("--retries", r.stdout)
        self.assertIn("瞬时失败", r.stdout)

    def test_mcp_mirror_import_closure(self):
        """mcp 镜像依赖闭包：gen_figure 本地 import 的每个 af_* 模块在镜像中都存在。
        （v6.2.0 事故固化：af_resilience 新增后未进 sync 清单，MCP 侧 import 断链，
        全量测试绿也抓不到——tests 不跑镜像 import。）"""
        import re as _re
        src = open(os.path.join(SCRIPT_DIR, "gen_figure.py"), encoding="utf-8").read()
        deps = set(_re.findall(r"^from (af_\w+) import", src, _re.M))
        deps |= set(_re.findall(r"^import (af_\w+)", src, _re.M))
        self.assertTrue(deps, "应至少解析出一个本地依赖")
        mirror = os.path.join(SCRIPT_DIR, "..", "mcp", "af_engine")
        for d in sorted(deps):
            self.assertTrue(os.path.exists(os.path.join(mirror, d + ".py")),
                            f"mcp 镜像缺 {d}.py —— 同步进 mcp/sync_engine.sh FILES 清单")
        sync = open(os.path.join(SCRIPT_DIR, "..", "mcp", "sync_engine.sh"),
                    encoding="utf-8").read()
        for d in sorted(deps):
            self.assertIn(d + ".py", sync,
                          f"sync_engine.sh FILES 清单缺 {d}.py —— 镜像将随版本漂移")

    def test_diagnostics_advice_matches_mechanism(self):
        """错误诊断建议与机制一致：不再保留无机制时代的「重跑一次」空头指引。"""
        src = open(os.path.join(SCRIPT_DIR, "af_diagnostics.py"), encoding="utf-8").read()
        self.assertNotIn("重跑一次（瞬态问题已支持自动重试）", src)
        self.assertIn("瞬态失败（超时/内存）会自动重试", src)

if __name__ == "__main__":
    unittest.main(verbosity=2)

