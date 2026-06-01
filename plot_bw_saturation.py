#!/usr/bin/env python3
"""Plot the 0602 §1B bandwidth-saturation sweep (BW vs concurrency).

Reads rr_results/bw_saturation.csv (written by bw_saturation.sh) and emits a
dependency-free SVG (+ PNG if matplotlib is installed):

  reports/figures/bw_saturation.svg

Left panel: read BW (MB/s) vs iodepth at numjobs=1.
Right panel: read BW (MB/s) vs numjobs at iodepth=32.
Reference lines: 3360 MB/s model ceiling = min(NAND 8x800, PCIe 3360) and the
970 PRO datasheet 3500 MB/s. Regenerate:  python3 plot_bw_saturation.py
"""
import csv
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CSV = os.path.join(HERE, "rr_results", "bw_saturation.csv")
OUTDIR = os.path.join(HERE, "reports", "figures")

CEILING = 3360.0   # model: min(NAND 8x800, PCIe 3360) MB/s
SPEC = 3500.0      # 970 PRO datasheet sequential read

# reuse the SVG canvas from the sibling plotter
sys.path.insert(0, HERE)
from plot_rrt_bw import Svg, axes  # noqa: E402


def load():
    iod, njob = [], []
    with open(CSV) as fh:
        for row in csv.DictReader(fh):
            try:
                mb = float(row["bw_MBps"])
            except (ValueError, KeyError):
                continue
            if row["mode"] == "iodepth":
                iod.append((int(row["iodepth"]), mb))
            elif row["mode"] == "numjobs":
                njob.append((int(row["numjobs"]), mb))
    # the iodepth=32,nj=1 point doubles as numjobs=1 for the right panel
    id32 = [mb for d, mb in iod if d == 32]
    if id32:
        njob = sorted(set([(1, id32[0])] + njob))
    iod.sort()
    njob.sort()
    return iod, njob


def panel(svg, px, py, pw, ph, pts, xlabel, title, y_max):
    xs = [p[0] for p in pts] or [1]
    # categorical x (evenly spaced) so {1,4,16,32,64} aren't crushed at the left
    n = len(pts)
    def mx(i):
        return px + (pw * (i + 0.5) / max(n, 1))
    def my(v):
        return py + ph - (v / y_max) * ph
    svg.rect(px, py, pw, ph, fill="#fbfbfb", stroke="#888", sw=1.0)
    for v in range(0, int(y_max) + 1, 500):
        gy = my(v)
        svg.line(px, gy, px + pw, gy, stroke="#e3e3e3", sw=1.0)
        svg.text(px - 6, gy + 4, v, size=10, anchor="end", fill="#444")
    # reference lines
    for ref, col, lab in ((CEILING, "#d62728", "model ceiling 3360"),
                          (SPEC, "#888", "spec 3500")):
        if ref <= y_max:
            gy = my(ref)
            svg.line(px, gy, px + pw, gy, stroke=col, sw=1.2, dash="4,3")
            svg.text(px + pw - 4, gy - 4, lab, size=9, anchor="end", fill=col)
    # data
    poly = []
    for i, (xv, mb) in enumerate(pts):
        cx, cy = mx(i), my(mb)
        poly.append((cx, cy))
        svg.parts.append('<circle cx="%.1f" cy="%.1f" r="3.5" fill="#1f77b4"/>'
                         % (cx, cy))
        svg.text(cx, cy - 9, "%.0f" % mb, size=10, anchor="middle", fill="#1f77b4")
        svg.text(cx, py + ph + 16, xv, size=11, anchor="middle", fill="#444")
    svg.polyline(poly, stroke="#1f77b4", sw=1.8)
    svg.text(px, py - 10, title, size=13, weight="bold")
    svg.text(px + pw / 2, py + ph + 36, xlabel, size=12, anchor="middle")
    svg.text(px - 40, py + ph / 2, "read BW (MB/s)", size=12, anchor="middle",
             rotate=-90)


def main():
    if not os.path.exists(CSV):
        sys.exit("missing %s — run ./bw_saturation.sh first" % CSV)
    iod, njob = load()
    peak = max([mb for _, mb in iod + njob] + [0])
    y_max = max(3700.0, ((peak // 500) + 1) * 500)
    W, H = 980, 460
    svg = Svg(W, H)
    svg.text(50, 28,
             "Bandwidth saturation sweep (reclaim OFF, libaio, 8 GiB region) — "
             "peak %.0f MB/s" % peak, size=15, weight="bold")
    panel(svg, 70, 70, 380, 320, iod, "iodepth (numjobs=1)",
          "(a) BW vs iodepth", y_max)
    panel(svg, 560, 70, 360, 320, njob, "numjobs (iodepth=32)",
          "(b) BW vs numjobs", y_max)
    os.makedirs(OUTDIR, exist_ok=True)
    out = os.path.join(OUTDIR, "bw_saturation.svg")
    with open(out, "w") as f:
        f.write(svg.render())
    print("Wrote %s (peak %.0f MB/s)" % (out, peak))

    # optional PNG
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        print("matplotlib not available -- SVG only.")
        return
    fig, axs = plt.subplots(1, 2, figsize=(11, 4.5))
    for ax, pts, xl, ti in ((axs[0], iod, "iodepth (numjobs=1)", "(a) BW vs iodepth"),
                            (axs[1], njob, "numjobs (iodepth=32)", "(b) BW vs numjobs")):
        xs = [str(p[0]) for p in pts]
        ys = [p[1] for p in pts]
        ax.plot(xs, ys, "o-", color="#1f77b4")
        for x, y in zip(xs, ys):
            ax.annotate("%.0f" % y, (x, y), textcoords="offset points",
                        xytext=(0, 6), ha="center", fontsize=8)
        ax.axhline(CEILING, color="#d62728", ls="--", lw=1, label="model 3360")
        ax.axhline(SPEC, color="#888", ls="--", lw=1, label="spec 3500")
        ax.set_ylim(0, y_max)
        ax.set_xlabel(xl)
        ax.set_ylabel("read BW (MB/s)")
        ax.set_title(ti)
        ax.grid(True, alpha=0.3)
        ax.legend(fontsize=8)
    fig.suptitle("Bandwidth saturation sweep (reclaim OFF, libaio, 8 GiB)")
    fig.tight_layout()
    fig.savefig(os.path.join(OUTDIR, "bw_saturation.png"), dpi=130)
    plt.close(fig)
    print("Wrote bw_saturation.png")


if __name__ == "__main__":
    main()
