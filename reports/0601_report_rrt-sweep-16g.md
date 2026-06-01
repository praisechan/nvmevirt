# Report — Read-Reclaim on a 16 GiB Device with a Read-Count (RRT) Sweep

**Date:** 2026-06-01 · **Module:** `nvmev_rr` · **Base config:** `SAMSUNG_970PRO` (`CONFIG_NVMEVIRT_SSD := y`) · **Kernel:** 6.8.0-111-generic · **Branch:** `read-reclaim`

> Follow-up to `reports/0531_report_read-reclaim.md` (small 4 GiB device). Same read-reclaim
> implementation (per-physical-block `read_cnt`, line-granular reclaim = Option A), re-run on a
> **larger emulated device (16 GiB region → ~15 GiB logical)** so NAND blocks hold many flash pages,
> across a sweep of `READ_RECLAIM_THRESHOLD` (RRT). Design: `read_reclaim_research.md` §6. Runbook:
> `prompts/experiment.md`. Turnkey: `QUICKSTART.md`.

---

## 1. Summary

The read-reclaim mechanism was characterized on a 16 GiB device (4× the prior 4 GiB device) by
sweeping `READ_RECLAIM_THRESHOLD` ∈ {16, 64, 256, 1024} against a reclaim-OFF baseline, on an
identical looping sequential-read workload (`RR_SIZE=512m`, 180 s, `set_perf_rr max`).

**Headline finding:** at 16 GiB each NAND block spans **4 flash pages** (vs 1 on the 4 GiB device),
so a sequential pass increments each block's `read_cnt` by **+4** (not +1). Reclaim frequency — and
therefore the read-bandwidth/write-amplification cost — scales **inversely and cleanly with RRT**:

| RRT | reclaims/instance | avg read BW | vs baseline |
|----:|------------------:|------------:|------------:|
| OFF (1e9) | 0 | 964 MiB/s | reference (flat) |
| 16  | 19,712 | 838.6 MiB/s | **−13.0%** |
| 64  |  5,120 | 930.2 MiB/s | −3.5% |
| 256 |  1,280 | 956.8 MiB/s | −0.8% |
| 1024 |   256 | 963.8 MiB/s | −0.03% |

Each ×4 step in RRT cuts reclaim events ~×4 and shrinks the bandwidth penalty roughly
proportionally. Reclaim only ever depresses bandwidth in bursts; lowering RRT raises the **burst
frequency** (not the burst depth), so the *average* BW falls while the *dip floor* stays ~650–740
MiB/s across all thresholds. **0 fio errors** in every run. Causation is confirmed by the OFF
baseline (flat 964 MiB/s, 0 reclaims) and by the RRT=1024 run, whose single dip lands exactly where
the per-pass arithmetic predicts (t≈136 s).

---

## 2. Setup

| Item | Value |
|---|---|
| Reserved region (ours) | **`memmap=16G$64G`** (64–80 GiB; was `4G$64G`). Other user's `16G$96G` kept. |
| Module | `nvmev_rr` (renamed for coexistence), `/proc/nvmev_rr` |
| `memmap_start` / `memmap_size` | `68719476736` (64G) / `17179869184` (16G) — cross-checked each load |
| CPUS | `22,23` (1 dispatcher + 1 IO worker; single-worker assumption is load-bearing, see 0531 §2.5) |
| Perf knob | `nvmev-evaluation/common/set_perf_rr.py max` (targets `/proc/nvmev_rr`; stock `set_perf.py` no-ops on our module) |
| Workload | `rr-seq-read.fio`, `bs=128k`, `direct=1`, `psync`, `time_based`; `RR_SIZE=512m`, `RR_RUNTIME=180`; per-second BW log |
| Prep | `rr-prep-write.fio` 512 MiB before every read loop (reads only count on blocks holding valid data) |
| Device node used | **`/dev/nvme1n1`** (15 GiB) |

**Device identification (critical — §2.3 of the prompt).** At 16 GiB our *logical* size (~15 GiB) is
about the same as the other user's ~15 GiB device, and both modules report model `CSL_Virt_MN_01`, so
**size + model can no longer tell them apart**. `rr_run.sh` was changed to pin our node by
**load-order**: snapshot `/dev/nvme*n1` after `rmmod nvmev_rr` and before `insmod`, then take the
**newly-appeared** node as ours, with hard cross-checks that ALL must hold — `/proc/nvmev_rr` present,
`/sys/module/nvmev_rr/parameters/memmap_start == 68719476736` (our 64G region), and the new node is
neither empty, ambiguous (must be exactly one), nor `/dev/nvme0n1` (the 1.8 TB real disk). Every run
this experiment logged `device = /dev/nvme1n1 (newly appeared across insmod; /proc/nvmev_rr present,
memmap_start=64G)`. (During this experiment the other user's `nvmev` was not loaded; the load-order
pin is robust to it loading because their node would be present in the *before* snapshot.)

Tooling changes for this experiment (all in `rr_run.sh`): `MEMMAP_SIZE` 4G→**16G** (env-overridable);
size-based pin **replaced** by the load-order pin above; `RR_SIZE` parameterized (default `512m`);
reclaim count now reports the authoritative cumulative `total_reclaims` counter (dmesg line-count
wraps the ring buffer at high reclaim rates). `rr_sweep.sh` (new) patches RRT in `ssd_config.h` and
calls `rr_run.sh` for each sweep point.

---

## 3. Geometry (dmesg-reported, per FTL instance)

NVMeVirt derives block/page geometry from the **memmap size** (block count is fixed by
`BLKS_PER_PLN`), so enlarging the region enlarges the blocks. From `dmesg` at load:

```
Total Capacity(GiB,MiB)=4,4096 chs=2 luns=4 lines=8192 blk-size(MiB,KiB)=0,128 line-size(MiB,KiB)=0,512
Init FTL instance with 2 channels (1048576 pages)   x4 instances
FTL physical space: 17178820608, logical space: 16054972530 (.../* 100 = 107)   ns 0: size 15311 MiB
```

| Metric | 16 GiB device (this run) | 4 GiB device (0531) |
|---|---|---|
| Per-instance capacity | 4 GiB (4 FTL instances) | 1 GiB |
| **`blk_size`** | **128 KiB** | 32 KiB |
| **`pgs_per_blk`** (÷4 KiB) | **32** | 8 |
| **`flashpgs_per_blk`** (÷32 KiB flash page) | **4** | 1 |
| `line_size` | 512 KiB | — |
| **`pgs_per_line`** (÷4 KiB) | **128** | — |
| **`blks_per_line`** (÷128 KiB) | **4** | — |
| `tt_lines` per instance | 8192 | 8192 |
| OP (physical/logical) | 107% | 107% |

**This is the point of the larger device:** a block now spans **4 flash pages** instead of 1, so a
full sequential pass over a block issues 4 NAND senses and bumps its `read_cnt` by **+4** per pass.

---

## 4. Methodology

**Per-pass increment.** `bs=128k` = exactly one NAND block, and `read_cnt` is incremented once per
**flash-page sense** (`rr_account_read` at both `ssd_advance_nand(NAND_READ)` sites). With
`flashpgs_per_blk = 4`, each pass over the region adds **+4** to every block's counter ⇒ a block
crosses RRT after `RRT/4` passes.

**Pass rate.** Measured at the baseline: ~963 MiB/s over a 512 MiB region ⇒ **~1.87 passes/s**
(~337 passes in 180 s, so `read_cnt` reaches ~1350 if reclaim never fired). This is exactly why
`RR_SIZE` was reduced from 2g to **512m**: at 2g (~2 s/pass, ~88 passes) RRT=256 would fire only
once and **RRT=1024 would never fire** within 180 s. At 512m all four points fire and produce a clean
gradient — and this is reported here rather than silently letting a point not fire.

**Region → reclaim geometry.** 512 MiB over 4 instances = 128 MiB/instance = **256 lines/instance**
(128 MiB ÷ 512 KiB). Because the region is written once and only read, every line is fully valid
(`vpc = pgs_per_line = 128`), so each line-reclaim relocates 128 pages and erases 4 blocks.

**Sweep.** Baseline control `READ_RECLAIM_THRESHOLD = 1000000000` (reclaim effectively OFF), then
RRT ∈ {16, 64, 256, 1024}. For each point: edit `ssd_config.h`, `./rr_run.sh rrt<value>_16g 180`
(rebuild picks up RRT → reload at 16G → pin device → `dmesg -C` → `set_perf_rr max` → prep-write 512m
→ 180 s looped seq-read → report reclaims + BW). Predicted firings/line = passes ÷ (RRT/4):

| RRT | passes to first fire | predicted firings/line in 180 s |
|----:|---:|---:|
| 16   | 4   | many (≈77, with reclaim-induced slowdown) |
| 64   | 16  | ≈20 |
| 256  | 64  | ≈5 |
| 1024 | 256 | ≈1 (≈136 s in) |

---

## 5. Results

`set_perf_rr max`, `RR_SIZE=512m`, `RR_RUNTIME=180`, device `/dev/nvme1n1`. BW from
`rr_results/bw_<label>.log` (179 per-second samples); reclaims = max cumulative
`total_reclaims`/instance from dmesg (× 4 instances = whole-device estimate); WA proxy uses
`pgs_per_line=128` relocated pages and `blks_per_line=4` erases per reclaim.

| RRT | reclaims /inst (≈device) | avg BW | min(dip) BW | max BW | avg vs base | dip vs base | secs<900 | WA: page-copies (≈device) | WA: erases (≈device) | fio err |
|----:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| OFF (1e9) | 0 (0) | 964.1 | 939.6 | 968.1 | — | — | 0/179 | 0 | 0 | 0 |
| 16  | 19,712 (~78,848) | 838.6 | 660.4 | 967.1 | **−13.0%** | −31.5% | 116/179 | ~10.1 M (~38 GiB) | ~315 K | 0 |
| 64  |  5,120 (~20,480) | 930.2 | 654.0 | 967.6 | −3.5% | −32.2% | 29/179 | ~2.62 M (~10 GiB) | ~81.9 K | 0 |
| 256 |  1,280 (~5,120)  | 956.8 | 703.5 | 967.6 | −0.8% | −27.0% | 8/179 | ~655 K (~2.5 GiB) | ~20.5 K | 0 |
| 1024 |   256 (~1,024)  | 963.8 | 743.1 | 968.0 | −0.03% | −22.9% | 2/179 | ~131 K (~512 MiB) | ~4,096 | 0 |

(BW in MiB/s; "vs base" uses the OFF average of 964.1 MiB/s. "secs<900" = per-second samples below
900 MiB/s = a dip-frequency proxy.)

**Reclaim-count fidelity.** The counts are not noisy — they are exactly `256 lines/instance ×
firings`: RRT=1024 → 256×1, RRT=256 → 256×5 = 1280, RRT=64 → 256×20 = 5120, RRT=16 → 256×77 = 19,712.
Each ×4 in RRT divides reclaims by ~4, as predicted by `firings ∝ 1/(RRT/4)`.

**Representative per-second BW excerpts.**

RRT=1024 — flat at line rate with a **single** burst exactly where predicted (256 passes ≈ 136 s):
```
t=130s 966  t=131s 965  t=132s 965  t=133s 965  t=134s 965  t=135s 960
t=136s 743  t=137s 884  t=138s 965  t=139s 965  t=140s 965  t=141s 965
```

RRT=16 — sustained sawtooth: reclaim fires every ~4 passes (~2 s) across all 256 lines, so bandwidth
rarely recovers to the 964 MiB/s line rate (116/179 s below 900):
```
t=1s 952  t=2s 822  t=3s 791  t=4s 940  t=5s 666  t=6s 960  t=7s 798  t=8s 833
t=9s 934  t=10s 708  t=11s 960  t=12s 746  t=13s 879  t=14s 719  t=15s 921  ...
```

---

## 6. Analysis

**RRT vs reclaim frequency vs cost.** The mechanism behaves as a clean knob: reclaim events,
relocated pages, and extra erases all scale as `1/RRT`, and the *average* read-bandwidth penalty
tracks that (−13.0% → −3.5% → −0.8% → −0.03% as RRT goes 16 → 64 → 256 → 1024). The penalty is
entirely a **duty-cycle** effect: every reclaim burst drops bandwidth to roughly the same floor
(~650–740 MiB/s, a ~25–32% dip) regardless of RRT, because a burst is the same line-reclaim work
(copy 128 valid pages + 4 erases, contending with host reads on the single IO worker). Lowering RRT
does not deepen the dips — it makes them **more frequent**, which is what erodes the average and is
visible as the rising `secs<900` count (2 → 8 → 29 → 116).

**How the larger block changes effective RRT scaling.** On the 4 GiB device a block was 1 flash page,
so a pass added +1 and RRT was reads-per-block ≈ passes-to-fire. On the 16 GiB device a block is 4
flash pages, so a pass adds +4 and **passes-to-fire = RRT/4**. The *same numeric RRT therefore fires
4× sooner (and 4× more often)* than it would on the small device — i.e. the effective, geometry-
normalized threshold is `RRT / flashpgs_per_blk`. Any RRT recommendation must be stated together with
the block geometry; "reads per block" is not portable across device sizes. (Real hardware tolerates
~1e4–1e6 reads/block; these low values are deliberate so reclaim fires within a 180 s run.)

**Comparison to the 4 GiB run (0531).** There, RRT=8 on a 2 GiB region gave 45,057 reclaims/instance
and an avg 954 MiB/s with dips to 652 (−36% from a 1003–1006 baseline). The dip *floor* here
(~650–740 MiB/s) and the baseline line rate (~964 MiB/s) are in the same ballpark — expected, since
per-instance channel/lun parallelism (2 ch, 4 lun) and the latency model are unchanged; only the
block/line *size* grew. The new device's value is that it exposes the **+4/pass** behavior and lets
RRT be swept across a regime where reclaim ranges from continuous (RRT=16) to a single late burst
(RRT=1024) — a dynamic range the 1-page-block device could not show.

**Write amplification.** This is **read-induced** WA: at RRT=16 the device performed ~10 M internal
page-copies (~38 GiB relocated) and ~315 K erases over 180 s purely to serve a read-only workload —
~75× the 512 MiB of live data, per minute-ish. At RRT=256 that drops to ~655 K copies (~2.5 GiB) for
a <1% BW cost. The WA/endurance cost of low RRT dwarfs its bandwidth cost and should dominate the
tuning decision on real (erase-limited) NAND.

---

## 7. Conclusions + follow-ups

**Recommended RRT for this 16 GiB geometry.** For verification/observation, **RRT=64** is the sweet
spot: reclaim fires steadily (~20×/line, 5,120 reclaims/instance, clearly visible dips) for only a
−3.5% average BW cost. **RRT=256** is better if the goal is "reclaim demonstrably works with minimal
disturbance" (−0.8% avg, 8 dips). **RRT=16** is only for stress/worst-case (continuous reclaim,
−13% avg, ~38 GiB/run relocation). Normalize by geometry: these equal `RRT/flashpgs_per_blk` =
{4, 16, 64} effective reads-per-block-per-pass-unit; to reproduce a given reclaim cadence on a
different device size, scale RRT by `flashpgs_per_blk`.

**Source left at** `READ_RECLAIM_THRESHOLD = 8` (the repo's pre-experiment default, restored;
the stale "block = 1 flash page" comment was updated to note geometry-dependence and point here),
and the module is **reloaded and left in a known state at RRT=8** on `/dev/nvme1n1`.

**Follow-ups.**
1. **Option B (true per-block reclaim):** reclaim only the crossed block, not its whole 4-block line —
   would cut WA ~4× and shrink each dip; needs a block-level free pool (`read_reclaim_research.md` §6.2).
2. **Export `/proc/nvmev_rr/` counters** for page-copies and erases (and per-instance reclaim totals)
   so WA is measured directly instead of estimated as `reclaims × pgs_per_line`/`× blks_per_line`,
   and so dmesg ring-buffer wrap stops mattering.
3. **Latency metrics:** extend fio output for read mean/p99/p999 to quantify tail-latency impact of
   reclaim bursts (the BW dips suggest significant tail inflation during a burst).
4. **WL-level / STRAW** reclaim policy comparison.
5. **Even larger device** (e.g. `memmap=64G$64G` if free → `pgs_per_blk≈128`, `flashpgs_per_blk≈16`)
   to push toward "hundreds of pages/block" and rescale RRT toward the 1e4–1e6 production range.

Raw logs: `rr_results/{fio_<label>.log, bw_<label>.log}` for labels
`baseline_off_16g, rrt16_16g, rrt64_16g, rrt256_16g, rrt1024_16g`.
