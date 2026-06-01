#!/usr/bin/env python3
"""Plot read-bandwidth-vs-time across the READ_RECLAIM_THRESHOLD (RRT) sweep.

Emits self-contained SVG figures (+ PNG when matplotlib is installed) for one or
more workload variants:

  QD1/psync  (~954 MiB/s baseline) -- the canonical reclaim test:
    reports/figures/rrt_bw_timeseries_overlay.svg
    reports/figures/rrt_bw_timeseries_panels.svg
  libaio iodepth=16 (~3357 MiB/s max-BW baseline), if those logs are present:
    reports/figures/rrt_bw_timeseries_overlay_iod16.svg
    reports/figures/rrt_bw_timeseries_panels_iod16.svg

Each variant overlays all RRT curves + the reclaim-OFF baseline, and also draws a
small-multiples panel per RRT. The y-axis is auto-scaled to the variant's data
(truncated below the baseline, clearly labelled, so reclaim dips stay legible).

Dependency-free: pure Python stdlib only (numpy/matplotlib optional). Headless.

Bw-log format (fio write_bw_log, log_avg_msec): comma-separated,
  col1 = time_ms, col2 = bandwidth_KiB/s, rest unused.
So  time_s = col1/1000  and  MiB/s = col2/1024.

Regenerate:  python3 plot_rrt_bw.py
"""
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LOGDIR = os.path.join(HERE, "rr_results")
OUTDIR = os.path.join(HERE, "reports", "figures")

X_MAX = 180.0

# Series tuple: (label, log filename, RRT display, reclaims/instance, color).
# Order = draw/legend order (baseline first as the reference line).
SERIES_QD1 = [
    ("OFF",    "bw_baseline_off_16g.log", "OFF (1e9)", 0,      "#555555"),
    ("RRT=1024", "bw_rrt1024_16g.log",    "1024",      256,    "#1f77b4"),
    ("RRT=256",  "bw_rrt256_16g.log",     "256",       1280,   "#2ca02c"),
    ("RRT=64",   "bw_rrt64_16g.log",      "64",        5120,   "#ff7f0e"),
    ("RRT=16",   "bw_rrt16_16g.log",      "16",        19712,  "#d62728"),
]
# iodepth=16 max-bandwidth variant (reclaim counts from the 2026-06-01 hi-QD run).
SERIES_IOD16 = [
    ("OFF",    "bw_baseline_off_16g_iod16.log", "OFF (1e9)", 0,     "#555555"),
    ("RRT=1024", "bw_rrt1024_16g_iod16.log",    "1024",      1024,  "#1f77b4"),
    ("RRT=256",  "bw_rrt256_16g_iod16.log",     "256",       4608,  "#2ca02c"),
    ("RRT=64",   "bw_rrt64_16g_iod16.log",      "64",        17152, "#ff7f0e"),
    ("RRT=16",   "bw_rrt16_16g_iod16.log",      "16",        54886, "#d62728"),
]

VARIANTS = [
    {"key": "qd1", "series": SERIES_QD1, "prefer_fine": True,
     "overlay": "rrt_bw_timeseries_overlay", "panels": "rrt_bw_timeseries_panels",
     "title": "Read bandwidth vs time across RRT sweep (16 GiB, QD1/psync)",
     "workload": "QD1/psync"},
    {"key": "iod16", "series": SERIES_IOD16, "prefer_fine": False,
     "overlay": "rrt_bw_timeseries_overlay_iod16",
     "panels": "rrt_bw_timeseries_panels_iod16",
     "title": "Read bandwidth vs time across RRT sweep (16 GiB, libaio iodepth=16)",
     "workload": "libaio iodepth=16"},
]


def load_log(path):
    """Return (times_s, bw_mibps) parsed from a fio write_bw_log file."""
    xs, ys = [], []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            parts = line.split(",")
            if len(parts) < 2:
                continue
            xs.append(float(parts[0]) / 1000.0)
            ys.append(float(parts[1]) / 1024.0)
    return xs, ys


def stats(ys):
    if not ys:
        return (0.0, 0.0, 0.0)
    return (min(ys), sum(ys) / len(ys), max(ys))


def nice_step(span, target=7):
    """A 'nice' tick step (1/2/2.5/5 x10^n) giving roughly `target` ticks."""
    raw = span / max(target, 1)
    mag = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1
    for m in (1, 2, 2.5, 5, 10):
        if mag * m >= raw:
            return mag * m
    return mag * 10


def axis_range(data, series):
    """Auto y-range: floor below deepest dip, ceil above peak, + nice ticks."""
    lo = min(min(data[k]["ys"]) for k, *_ in series if data[k]["ys"])
    hi = max(max(data[k]["ys"]) for k, *_ in series if data[k]["ys"])
    span = hi - lo
    pad = max(span * 0.06, 15)
    step = nice_step((hi + pad) - (lo - pad))
    y_min = max(0.0, math.floor((lo - pad) / step) * step)
    y_max = math.ceil((hi + pad) / step) * step
    ticks = []
    v = y_min
    while v <= y_max + 1e-6:
        ticks.append(int(round(v)))
        v += step
    return y_min, y_max, ticks


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


class Svg:
    def __init__(self, w, h):
        self.w, self.h = w, h
        self.parts = []

    def rect(self, x, y, w, h, fill="none", stroke="none", sw=1.0, opacity=1.0):
        self.parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" '
            f'fill="{fill}" stroke="{stroke}" stroke-width="{sw}" opacity="{opacity}"/>')

    def line(self, x1, y1, x2, y2, stroke="#000", sw=1.0, dash=None, opacity=1.0):
        d = f' stroke-dasharray="{dash}"' if dash else ""
        self.parts.append(
            f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
            f'stroke="{stroke}" stroke-width="{sw}"{d} opacity="{opacity}"/>')

    def polyline(self, pts, stroke="#000", sw=1.5, opacity=1.0):
        if not pts:
            return
        d = " ".join(f"{x:.2f},{y:.2f}" for x, y in pts)
        self.parts.append(
            f'<polyline points="{d}" fill="none" stroke="{stroke}" '
            f'stroke-width="{sw}" opacity="{opacity}" stroke-linejoin="round"/>')

    def text(self, x, y, s, size=13, anchor="start", fill="#111", weight="normal", rotate=None):
        tr = f' transform="rotate({rotate} {x:.1f} {y:.1f})"' if rotate is not None else ""
        self.parts.append(
            f'<text x="{x:.1f}" y="{y:.1f}" font-family="sans-serif" '
            f'font-size="{size}" text-anchor="{anchor}" fill="{fill}" '
            f'font-weight="{weight}"{tr}>{esc(s)}</text>')

    def render(self):
        head = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.w}" '
                f'height="{self.h}" viewBox="0 0 {self.w} {self.h}">')
        bg = f'<rect width="{self.w}" height="{self.h}" fill="white"/>'
        return head + bg + "".join(self.parts) + "</svg>"


def axes(svg, px, py, pw, ph, x_max, y_min, y_max, yticks, title=None,
         xlabel=None, ylabel=None, xticks=None, baseline=None):
    """Draw a plot frame with grid, ticks, labels. Returns mappers (mx, my)."""
    def mx(t):
        return px + (t / x_max) * pw

    def my(bw):
        return py + ph - ((bw - y_min) / (y_max - y_min)) * ph

    svg.rect(px, py, pw, ph, fill="#fbfbfb", stroke="#888", sw=1.0)
    if xticks is None:
        xticks = list(range(0, int(x_max) + 1, 30))
    for v in yticks:
        if v < y_min - 1e-6 or v > y_max + 1e-6:
            continue
        gy = my(v)
        svg.line(px, gy, px + pw, gy, stroke="#e3e3e3", sw=1.0)
        svg.text(px - 6, gy + 4, v, size=11, anchor="end", fill="#444")
    for v in xticks:
        gx = mx(v)
        svg.line(gx, py, gx, py + ph, stroke="#e3e3e3", sw=1.0)
        svg.text(gx, py + ph + 16, v, size=11, anchor="middle", fill="#444")
    if baseline is not None and y_min <= baseline <= y_max:
        by = my(baseline)
        svg.line(px, by, px + pw, by, stroke="#999", sw=1.2, dash="2,3")
    if title:
        svg.text(px, py - 12, title, size=15, anchor="start", weight="bold")
    if xlabel:
        svg.text(px + pw / 2, py + ph + 40, xlabel, size=13, anchor="middle")
    if ylabel:
        svg.text(px - 46, py + ph / 2, ylabel, size=13, anchor="middle", rotate=-90)
    return mx, my


def clamp(v, lo, hi):
    return lo if v < lo else (hi if v > hi else v)


def build_overlay(data, cfg, y_min, y_max, yticks, res):
    series = cfg["series"]
    W, H = 1040, 600
    px, py, pw, ph = 70, 56, 700, 470
    svg = Svg(W, H)
    base_avg = data["OFF"]["avg"]
    mx, my = axes(svg, px, py, pw, ph, X_MAX, y_min, y_max, yticks,
                  title=cfg["title"], xlabel="elapsed time (s)",
                  ylabel="read bandwidth (MiB/s)", baseline=base_avg)
    svg.text(px, py + ph + 58,
             "y-axis truncated at %d MiB/s to make reclaim dips legible; dashed "
             "grey = OFF baseline avg %.0f MiB/s; %s, %s resolution"
             % (int(y_min), base_avg, cfg["workload"], res), size=11, fill="#666")
    for key, _fn, _disp, _rec, color in series:
        d = data[key]
        pts = [(mx(t), my(clamp(bw, y_min, y_max)))
               for t, bw in zip(d["xs"], d["ys"])]
        sw = 1.2 if key == "OFF" else 1.4
        svg.polyline(pts, stroke=color, sw=sw,
                     opacity=0.55 if key == "OFF" else 0.9)
    lx, ly = px + pw + 24, py + 6
    svg.text(lx, ly, "RRT  (reclaims/inst)", size=12, weight="bold")
    ly += 22
    for key, _fn, disp, rec, color in series:
        d = data[key]
        svg.line(lx, ly - 4, lx + 26, ly - 4, stroke=color, sw=3.0)
        svg.text(lx + 32, ly, "%s  (%s)" % (disp, "{:,}".format(rec)), size=12)
        ly += 17
        svg.text(lx + 32, ly, "avg %.0f  min %.0f" % (d["avg"], d["min"]),
                 size=10, fill="#666")
        ly += 22
    return svg.render()


def build_panels(data, cfg, y_min, y_max, yticks, res):
    series = cfg["series"]
    n = len(series)
    pw, ph = 720, 96
    px, gap, top = 80, 30, 50
    W = px + pw + 250
    H = top + n * (ph + gap) + 40
    svg = Svg(W, H)
    base_avg = data["OFF"]["avg"]
    svg.text(px, 28, "Read bandwidth vs time, per RRT — %s (small multiples)"
             % cfg["workload"], size=16, weight="bold")
    for i, (key, _fn, disp, rec, color) in enumerate(series):
        d = data[key]
        py = top + i * (ph + gap)
        is_last = (i == n - 1)
        mx, my = axes(svg, px, py, pw, ph, X_MAX, y_min, y_max, yticks,
                      xlabel=("elapsed time (s)" if is_last else None))
        if y_min <= base_avg <= y_max:
            by = my(base_avg)
            svg.line(px, by, px + pw, by, stroke="#999", sw=1.0, dash="2,3")
        pts = [(mx(t), my(clamp(bw, y_min, y_max)))
               for t, bw in zip(d["xs"], d["ys"])]
        svg.polyline(pts, stroke=color, sw=1.3)
        ax = px + pw + 16
        svg.text(ax, py + 16, "RRT=%s" % disp, size=13, weight="bold", fill=color)
        svg.text(ax, py + 36, "reclaims/inst: %s" % "{:,}".format(rec),
                 size=11, fill="#444")
        svg.text(ax, py + 53, "avg %.1f MiB/s" % d["avg"], size=11, fill="#444")
        svg.text(ax, py + 70, "min(dip) %.1f" % d["min"], size=11, fill="#444")
        svg.text(ax, py + 87, "dips: %d/%d intervals" % (d["dips"], len(d["ys"])),
                 size=11, fill="#444")
    svg.text(px, H - 12,
             "y-axis %d-%d MiB/s (truncated); dashed grey = OFF baseline avg "
             "%.0f. Lower RRT => more frequent dips." % (int(y_min), int(y_max),
                                                         base_avg),
             size=11, fill="#666")
    return svg.render()


def matplotlib_pngs(data, cfg, y_min, y_max):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return False
    series = cfg["series"]
    base_avg = data["OFF"]["avg"]
    fig, ax = plt.subplots(figsize=(11, 6))
    for key, _fn, disp, rec, color in series:
        d = data[key]
        ax.plot(d["xs"], d["ys"], color=color, lw=1.2,
                alpha=0.6 if key == "OFF" else 0.9,
                label="%s (%s)" % (disp, "{:,}".format(rec)))
    ax.axhline(base_avg, color="#999", ls="--", lw=1)
    ax.set_ylim(y_min, y_max)
    ax.set_xlim(0, X_MAX)
    ax.set_xlabel("elapsed time (s)")
    ax.set_ylabel("read bandwidth (MiB/s)")
    ax.set_title(cfg["title"])
    ax.grid(True, alpha=0.3)
    ax.legend(title="RRT (reclaims/inst)", loc="lower left", fontsize=9)
    fig.tight_layout()
    fig.savefig(os.path.join(OUTDIR, cfg["overlay"] + ".png"), dpi=130)
    plt.close(fig)

    fig, axs = plt.subplots(len(series), 1, figsize=(11, 9), sharex=True, sharey=True)
    for ax, (key, _fn, disp, rec, color) in zip(axs, series):
        d = data[key]
        ax.plot(d["xs"], d["ys"], color=color, lw=1.0)
        ax.axhline(base_avg, color="#999", ls="--", lw=0.8)
        ax.set_ylim(y_min, y_max)
        ax.set_ylabel("MiB/s")
        ax.grid(True, alpha=0.3)
        ax.text(0.99, 0.06, "RRT=%s  avg %.0f  min %.0f  reclaims/inst %s"
                % (disp, d["avg"], d["min"], "{:,}".format(rec)),
                transform=ax.transAxes, ha="right", fontsize=9)
    axs[0].set_title("Read bandwidth vs time, per RRT — %s" % cfg["workload"])
    axs[-1].set_xlabel("elapsed time (s)")
    fig.tight_layout()
    fig.savefig(os.path.join(OUTDIR, cfg["panels"] + ".png"), dpi=130)
    plt.close(fig)
    return True


def resolve_log(fn, prefer_fine):
    if prefer_fine:
        fine = os.path.join(LOGDIR, fn[:-4] + "_250ms.log")
        if os.path.exists(fine):
            return fine, True
    return os.path.join(LOGDIR, fn), False


def dip_threshold(base_avg):
    """Count intervals dipping >5% below the OFF baseline (a dip-frequency proxy)."""
    return 0.95 * base_avg


def main():
    os.makedirs(OUTDIR, exist_ok=True)
    any_png = False
    for cfg in VARIANTS:
        # all logs present for this variant?
        paths = [resolve_log(fn, cfg["prefer_fine"]) for _l, fn, *_ in cfg["series"]]
        if not all(os.path.exists(p) for p, _ in paths):
            print("[%s] logs not present -- skipping" % cfg["key"])
            continue
        fine_used = False
        data = {}
        for (key, fn, disp, rec, color), (path, is_fine) in zip(cfg["series"], paths):
            fine_used = fine_used or is_fine
            xs, ys = load_log(path)
            mn, avg, mxv = stats(ys)
            data[key] = {"xs": xs, "ys": ys, "min": mn, "avg": avg, "max": mxv}
        thr = dip_threshold(data["OFF"]["avg"])
        for key in data:
            data[key]["dips"] = sum(1 for v in data[key]["ys"] if v < thr)
        res = "250 ms" if (fine_used or not cfg["prefer_fine"]) else "1 s"
        y_min, y_max, yticks = axis_range(data, cfg["series"])
        print("[%s] %s  y=[%d,%d]  baseline avg %.0f" %
              (cfg["key"], cfg["workload"], int(y_min), int(y_max),
               data["OFF"]["avg"]))
        for key, _fn, _disp, _rec, _c in cfg["series"]:
            d = data[key]
            print("   %-9s n=%3d avg=%.1f min=%.1f max=%.1f dips=%d" %
                  (key, len(d["ys"]), d["avg"], d["min"], d["max"], d["dips"]))
        overlay = build_overlay(data, cfg, y_min, y_max, yticks, res)
        panels = build_panels(data, cfg, y_min, y_max, yticks, res)
        with open(os.path.join(OUTDIR, cfg["overlay"] + ".svg"), "w") as f:
            f.write(overlay)
        with open(os.path.join(OUTDIR, cfg["panels"] + ".svg"), "w") as f:
            f.write(panels)
        print("   wrote %s.svg, %s.svg" % (cfg["overlay"], cfg["panels"]))
        if matplotlib_pngs(data, cfg, y_min, y_max):
            any_png = True
            print("   wrote %s.png, %s.png" % (cfg["overlay"], cfg["panels"]))
    if not any_png:
        print("matplotlib not available -- SVG only, PNG skipped.")


if __name__ == "__main__":
    main()
