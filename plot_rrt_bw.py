#!/usr/bin/env python3
"""Plot read-bandwidth-vs-time across the READ_RECLAIM_THRESHOLD (RRT) sweep.

Reads the fio per-interval bandwidth logs from the 0601 RRT sweep (and any
re-run with finer log_avg_msec) and emits two self-contained SVG figures plus,
if matplotlib happens to be installed, matching PNGs:

  reports/figures/rrt_bw_timeseries_overlay.svg   all RRT curves + OFF baseline
  reports/figures/rrt_bw_timeseries_panels.svg    small-multiples, one per RRT

Dependency-free: pure Python stdlib only (numpy/matplotlib optional). Headless;
never opens a window.

Bw-log format (fio write_bw_log, log_avg_msec): comma-separated,
  col1 = time_ms, col2 = bandwidth_KiB/s, rest unused.
So  time_s = col1/1000  and  MiB/s = col2/1024.

Regenerate:  python3 plot_rrt_bw.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LOGDIR = os.path.join(HERE, "rr_results")
OUTDIR = os.path.join(HERE, "reports", "figures")

# Series: (label, log filename, RRT display, reclaims/instance from 0601 report, color)
# Order = draw/legend order (baseline first as the reference line).
SERIES = [
    ("OFF",    "bw_baseline_off_16g.log", "OFF (1e9)", 0,      "#555555"),
    ("RRT=1024", "bw_rrt1024_16g.log",    "1024",      256,    "#1f77b4"),
    ("RRT=256",  "bw_rrt256_16g.log",     "256",       1280,   "#2ca02c"),
    ("RRT=64",   "bw_rrt64_16g.log",      "64",        5120,   "#ff7f0e"),
    ("RRT=16",   "bw_rrt16_16g.log",      "16",        19712,  "#d62728"),
]

# Plot ranges. y truncated to 600 to make reclaim dips legible (clearly labelled).
X_MAX = 180.0
Y_MIN = 550.0   # below the deepest observed dip (~594 MiB/s at 250 ms) so nothing clips
Y_MAX = 1000.0


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
            t_ms = float(parts[0])
            bw_kib = float(parts[1])
            xs.append(t_ms / 1000.0)
            ys.append(bw_kib / 1024.0)
    return xs, ys


def stats(ys):
    if not ys:
        return (0.0, 0.0, 0.0)
    return (min(ys), sum(ys) / len(ys), max(ys))


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


class Svg:
    """Minimal SVG canvas with a px<->data coordinate mapper per plot rect."""

    def __init__(self, w, h):
        self.w, self.h = w, h
        self.parts = []

    def rect(self, x, y, w, h, fill="none", stroke="none", sw=1.0, opacity=1.0):
        self.parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" '
            f'fill="{fill}" stroke="{stroke}" stroke-width="{sw}" opacity="{opacity}"/>'
        )

    def line(self, x1, y1, x2, y2, stroke="#000", sw=1.0, dash=None, opacity=1.0):
        d = f' stroke-dasharray="{dash}"' if dash else ""
        self.parts.append(
            f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
            f'stroke="{stroke}" stroke-width="{sw}"{d} opacity="{opacity}"/>'
        )

    def polyline(self, pts, stroke="#000", sw=1.5, opacity=1.0):
        if not pts:
            return
        d = " ".join(f"{x:.2f},{y:.2f}" for x, y in pts)
        self.parts.append(
            f'<polyline points="{d}" fill="none" stroke="{stroke}" '
            f'stroke-width="{sw}" opacity="{opacity}" stroke-linejoin="round"/>'
        )

    def text(self, x, y, s, size=13, anchor="start", fill="#111", weight="normal", rotate=None):
        tr = f' transform="rotate({rotate} {x:.1f} {y:.1f})"' if rotate is not None else ""
        self.parts.append(
            f'<text x="{x:.1f}" y="{y:.1f}" font-family="sans-serif" '
            f'font-size="{size}" text-anchor="{anchor}" fill="{fill}" '
            f'font-weight="{weight}"{tr}>{esc(s)}</text>'
        )

    def render(self):
        head = (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.w}" '
            f'height="{self.h}" viewBox="0 0 {self.w} {self.h}">'
        )
        bg = f'<rect width="{self.w}" height="{self.h}" fill="white"/>'
        return head + bg + "".join(self.parts) + "</svg>"


def axes(svg, px, py, pw, ph, x_max, y_min, y_max, title=None, xlabel=None,
         ylabel=None, yticks=None, xticks=None, baseline=None):
    """Draw a plot frame with grid, ticks, labels. Returns mapper(t,bw)->(x,y)."""
    def mx(t):
        return px + (t / x_max) * pw

    def my(bw):
        return py + ph - ((bw - y_min) / (y_max - y_min)) * ph

    # plot background + frame
    svg.rect(px, py, pw, ph, fill="#fbfbfb", stroke="#888", sw=1.0)
    if yticks is None:
        yticks = list(range(int(y_min), int(y_max) + 1, 50))
    if xticks is None:
        xticks = list(range(0, int(x_max) + 1, 30))
    # gridlines + tick labels
    for v in yticks:
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
        svg.text(px - 46, py + ph / 2, ylabel, size=13, anchor="middle",
                 rotate=-90)
    return mx, my


def build_overlay(data):
    W, H = 1040, 600
    px, py, pw, ph = 70, 56, 700, 470
    svg = Svg(W, H)
    base_avg = data["OFF"]["avg"]
    mx, my = axes(
        svg, px, py, pw, ph, X_MAX, Y_MIN, Y_MAX,
        title="Read bandwidth vs time across RRT sweep (16 GiB device)",
        xlabel="elapsed time (s)",
        ylabel="read bandwidth (MiB/s)",
        baseline=base_avg,
    )
    # axis-break note
    svg.text(px, py + ph + 58,
             "y-axis truncated at 550 MiB/s to make reclaim dips legible; "
             "dashed grey = OFF baseline avg %.0f MiB/s; BW-log resolution %s"
             % (base_avg, data.get("_res", "1 s")),
             size=11, fill="#666")
    # curves (baseline last-ish but drawn first so colored curves sit on top)
    for key, _fn, _disp, _rec, color in SERIES:
        d = data[key]
        pts = [(mx(t), my(min(max(bw, Y_MIN), Y_MAX)))
               for t, bw in zip(d["xs"], d["ys"])]
        sw = 1.2 if key == "OFF" else 1.4
        svg.polyline(pts, stroke=color, sw=sw,
                     opacity=0.55 if key == "OFF" else 0.9)
    # legend
    lx, ly = px + pw + 24, py + 6
    svg.text(lx, ly, "RRT  (reclaims/inst)", size=12, weight="bold")
    ly += 22
    for key, _fn, disp, rec, color in SERIES:
        d = data[key]
        svg.line(lx, ly - 4, lx + 26, ly - 4, stroke=color, sw=3.0)
        label = ("%s  (%s)" % (disp, "{:,}".format(rec)) if rec else
                 "%s  (0)" % disp)
        svg.text(lx + 32, ly, label, size=12)
        ly += 17
        svg.text(lx + 32, ly, "avg %.0f  min %.0f" % (d["avg"], d["min"]),
                 size=10, fill="#666")
        ly += 22
    return svg.render()


def build_panels(data):
    n = len(SERIES)
    pw, ph = 720, 96
    px = 80
    gap = 30
    top = 50
    W = px + pw + 250
    H = top + n * (ph + gap) + 40
    svg = Svg(W, H)
    base_avg = data["OFF"]["avg"]
    svg.text(px, 28,
             "Read bandwidth vs time, per RRT (small multiples, shared axes)",
             size=16, weight="bold")
    for i, (key, _fn, disp, rec, color) in enumerate(SERIES):
        d = data[key]
        py = top + i * (ph + gap)
        is_last = (i == n - 1)
        mx, my = axes(
            svg, px, py, pw, ph, X_MAX, Y_MIN, Y_MAX,
            xlabel=("elapsed time (s)" if is_last else None),
            yticks=[600, 700, 800, 900, 1000],
            baseline=base_avg,
        )
        pts = [(mx(t), my(min(max(bw, Y_MIN), Y_MAX)))
               for t, bw in zip(d["xs"], d["ys"])]
        svg.polyline(pts, stroke=color, sw=1.3)
        # panel annotation
        ax = px + pw + 16
        ttl = "RRT=%s" % disp
        svg.text(ax, py + 16, ttl, size=13, weight="bold", fill=color)
        svg.text(ax, py + 36, "reclaims/inst: %s" %
                 ("{:,}".format(rec) if rec else "0"), size=11, fill="#444")
        svg.text(ax, py + 53, "avg %.1f MiB/s" % d["avg"], size=11, fill="#444")
        svg.text(ax, py + 70, "min(dip) %.1f" % d["min"], size=11, fill="#444")
        svg.text(ax, py + 87, "secs<900: %d/%d" % (d["below900"], len(d["ys"])),
                 size=11, fill="#444")
    svg.text(px, H - 12,
             "y-axis 550-1000 MiB/s (truncated); dashed grey = OFF baseline "
             "avg %.0f MiB/s. Lower RRT => more frequent dips." % base_avg,
             size=11, fill="#666")
    return svg.render()


def try_png(data):
    """Emit PNG versions iff matplotlib is importable; otherwise skip quietly."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as e:
        print("matplotlib not available (%s) -- SVG only, PNG skipped." %
              type(e).__name__)
        return
    base_avg = data["OFF"]["avg"]
    # overlay
    fig, ax = plt.subplots(figsize=(11, 6))
    for key, _fn, disp, rec, color in SERIES:
        d = data[key]
        ax.plot(d["xs"], d["ys"], color=color, lw=1.2,
                alpha=0.6 if key == "OFF" else 0.9,
                label="%s (%s)" % (disp, "{:,}".format(rec)))
    ax.axhline(base_avg, color="#999", ls="--", lw=1)
    ax.set_ylim(Y_MIN, Y_MAX)
    ax.set_xlim(0, X_MAX)
    ax.set_xlabel("elapsed time (s)")
    ax.set_ylabel("read bandwidth (MiB/s)")
    ax.set_title("Read bandwidth vs time across RRT sweep (16 GiB device)")
    ax.grid(True, alpha=0.3)
    ax.legend(title="RRT (reclaims/inst)", loc="lower left", fontsize=9)
    fig.tight_layout()
    fig.savefig(os.path.join(OUTDIR, "rrt_bw_timeseries_overlay.png"), dpi=130)
    plt.close(fig)
    # panels
    fig, axs = plt.subplots(len(SERIES), 1, figsize=(11, 9), sharex=True,
                            sharey=True)
    for ax, (key, _fn, disp, rec, color) in zip(axs, SERIES):
        d = data[key]
        ax.plot(d["xs"], d["ys"], color=color, lw=1.0)
        ax.axhline(base_avg, color="#999", ls="--", lw=0.8)
        ax.set_ylim(Y_MIN, Y_MAX)
        ax.set_ylabel("MiB/s")
        ax.grid(True, alpha=0.3)
        ax.text(0.99, 0.05,
                "RRT=%s  avg %.0f  min %.0f  reclaims/inst %s" %
                (disp, d["avg"], d["min"],
                 "{:,}".format(rec) if rec else "0"),
                transform=ax.transAxes, ha="right", fontsize=9)
    axs[0].set_title("Read bandwidth vs time, per RRT (small multiples)")
    axs[-1].set_xlabel("elapsed time (s)")
    fig.tight_layout()
    fig.savefig(os.path.join(OUTDIR, "rrt_bw_timeseries_panels.png"), dpi=130)
    plt.close(fig)
    print("Wrote PNGs (matplotlib).")


def resolve_log(fn):
    """Prefer the finer-grained *_250ms variant of a log if it exists."""
    fine = os.path.join(LOGDIR, fn[:-4] + "_250ms.log")
    if os.path.exists(fine):
        return fine, True
    return os.path.join(LOGDIR, fn), False


def main():
    os.makedirs(OUTDIR, exist_ok=True)
    data = {}
    fine_used = False
    for key, fn, disp, rec, color in SERIES:
        path, is_fine = resolve_log(fn)
        fine_used = fine_used or is_fine
        if not os.path.exists(path):
            sys.exit("missing log: %s" % path)
        xs, ys = load_log(path)
        mn, avg, mx_ = stats(ys)
        data[key] = {
            "xs": xs, "ys": ys, "min": mn, "avg": avg, "max": mx_,
            "below900": sum(1 for v in ys if v < 900),
        }
        print("%-9s n=%3d  avg=%.1f  min=%.1f  max=%.1f  secs<900=%d" %
              (key, len(ys), avg, mn, mx_, data[key]["below900"]))

    res = "250 ms" if fine_used else "1 s"
    print("BW-log resolution: %s%s" %
          (res, " (fine re-run)" if fine_used else " (0601 logs)"))
    data["_res"] = res
    overlay = build_overlay(data)
    panels = build_panels(data)
    with open(os.path.join(OUTDIR, "rrt_bw_timeseries_overlay.svg"), "w") as f:
        f.write(overlay)
    with open(os.path.join(OUTDIR, "rrt_bw_timeseries_panels.svg"), "w") as f:
        f.write(panels)
    print("Wrote %s" % os.path.join(OUTDIR, "rrt_bw_timeseries_overlay.svg"))
    print("Wrote %s" % os.path.join(OUTDIR, "rrt_bw_timeseries_panels.svg"))
    try_png(data)


if __name__ == "__main__":
    main()
