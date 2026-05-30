# Read Disturbance & Read Reclaim in SSDs — A Reference for NVMeVirt Implementation

> Purpose: Background research to support implementing a **basic block-level read reclaim**
> mechanism in NVMeVirt (`conv_ftl`). The plan is: the (emulated) SSD controller tracks a
> **per-block read counter**, and when a block's read count reaches a predefined
> **read-reclaim threshold (RRT)**, the controller **reclaims** the block by rewriting its
> valid pages to a fresh block and erasing the old one.

---

## 1. Basic SSD / NAND Flash Structure

### 1.1 Physical hierarchy

A NAND-flash SSD is organized as a strict hierarchy. NVMeVirt models exactly this hierarchy
(see `ssd.h`):

| Level | Unit | Meaning / operation granularity |
|-------|------|---------------------------------|
| **Channel** | `ssd_channel` | Bus between controller and NAND; data-transfer unit |
| **LUN / Die** | `nand_lun` | Independent operation unit (one op at a time per die) |
| **Plane** | `nand_plane` | Sub-unit of a die; enables multi-plane ops |
| **Block** | `nand_block` | **Erase unit** (the smallest erasable unit) |
| **Page** | `nand_page` | **Program (write) / sense (read) unit** |
| **Cell** | — | Stores 1 bit (SLC), 2 bits (MLC), 3 bits (TLC), 4 bits (QLC) |

Key asymmetry that drives all SSD firmware design:

- **Read** and **program (write)** operate at **page** granularity.
- **Erase** operates only at **block** granularity (a block = hundreds to thousands of pages).
- A page **cannot be overwritten in place**; it must be erased first, and erase only works on
  the whole block. → This is why an FTL with out-of-place writes + garbage collection exists.

### 1.2 Cell physics (why disturbance happens at all)

Each NAND cell is a floating-gate (or charge-trap, in 3D NAND) transistor. The amount of
trapped charge sets the cell's **threshold voltage (Vth)**. To read a cell, the controller
applies a **read reference voltage** to its word line and checks whether the cell conducts.
Multi-level cells (MLC/TLC/QLC) pack 2/3/4 bits by dividing the Vth range into 4/8/16 states,
so the margins between states are narrow — small Vth shifts cause bit errors.

A NAND **block** is a 2D grid of cells: cells sharing a **word line (WL)** form a page (or, in
MLC/TLC, several logical pages — LSB/MSB/CSB; NVMeVirt models this with
`CELL_TYPE_LSB/MSB/CSB` and `get_cell()`), and cells sharing a **bit line (BL)** form a
NAND string spanning all WLs in the block. This shared-WL / shared-string structure is the
root cause of read disturbance (Section 3).

### 1.3 In NVMeVirt terms

NVMeVirt's `conv_ftl` adds a logical concept on top of the physical layout:

- **Line** (`struct line`, `conv_ftl.h`): a "superblock" — the set of physical blocks with the
  same block index across **all** channels/LUNs/planes, written together as a stripe.
  `tt_lines` lines total; each line tracks `vpc` (valid page count) and `ipc` (invalid page
  count). GC and the write pointer operate at line granularity.
- **maptbl**: page-level Logical-to-Physical (L2P) mapping table.
- **rmap**: reverse map (PPA → LPN), "stored in OOB."

> **Design decision for this implementation: the read counter and the reclaim unit are the
> physical `nand_block`** (identified by `ch, lun, pl, blk`), *not* the line/superblock. This
> matches real hardware, where read disturb is a property of one physical block. Note that
> `conv_ftl`'s native GC/erase unit is the **line** (all same-index blocks across every die,
> erased together), so per-physical-block reclaim does not map 1:1 onto the stock GC path —
> see Section 6 for how to handle this.

---

## 2. Core SSD Operations

### 2.1 Flash Translation Layer (FTL)

The FTL hides NAND's erase-before-write constraint and presents a normal block device. Its main
jobs: **address mapping** (L2P), **garbage collection**, and **wear leveling**. NVMeVirt's
`conv_ftl` is a page-mapped FTL with out-of-place updates: a write to an already-mapped LPN
invalidates the old page (`mark_page_invalid`) and writes to a new page advanced by the
**write pointer** (`advance_write_pointer`).

### 2.2 Garbage Collection (GC)

Because writes are out-of-place, blocks accumulate **invalid (stale)** pages. GC reclaims free
space by:

1. **Victim selection** — pick a block/line to clean. The classic policy is **greedy**:
   choose the victim with the fewest valid pages (least copy cost). NVMeVirt uses a priority
   queue keyed on valid-page count (`select_victim_line`, `victim_line_pq`,
   `victim_line_cmp_pri`).
2. **Valid-page migration** — read each still-valid page and rewrite it to a fresh block,
   updating the mapping (`clean_one_flashpg` → `gc_read_page` + `gc_write_page`).
3. **Erase** — erase the now fully-invalid victim block (`mark_block_free` + `NAND_ERASE`),
   returning it to the free list (`mark_line_free`).

GC is triggered when free space runs low. NVMeVirt: `should_gc()` /
`should_gc_high()` compare `free_line_cnt` against `gc_thres_lines`; `foreground_gc()` loops
`do_gc()` until enough free lines exist.

**Write amplification (WA):** the migrated valid pages are extra NAND writes the host never
asked for. WA = (NAND writes) / (host writes). GC is the dominant source of WA — and, crucially,
**read reclaim is a *second* source of host-invisible writes** (Section 4.3).

> **Read reclaim reuses the GC machinery — but at a finer grain.** A reclaim is essentially "GC
> this block *now*, regardless of how many invalid pages it has, because it has been read too
> many times." NVMeVirt's `clean_one_flashpg` + `mark_block_free` + `NAND_ERASE` are the right
> primitives, but stock `do_gc` drives them across a **whole line**. Per-physical-block reclaim
> must invoke them on **one** `nand_block` (one `ch,lun,pl,blk`) — see Section 6.

### 2.3 Wear Leveling (WL)

NAND blocks wear out: each **program/erase (P/E) cycle** degrades the oxide, and a block dies
after an endurance limit (e.g., ~1K–3K P/E cycles for TLC, ~100K for SLC). **Wear leveling**
spreads P/E cycles evenly so no block dies prematurely:

- **Dynamic WL**: choose erased blocks with the lowest erase count for new writes.
- **Static WL**: periodically migrate *cold* (rarely updated) data off low-wear blocks so those
  blocks can take their share of writes.

NVMeVirt tracks `erase_cnt` per `nand_block` (in `ssd.h`), which is the hook a wear-leveling
policy would use. (The stock `conv_ftl` does not implement an explicit WL policy; striping
across all dies already distributes wear fairly evenly.)

**WL interaction with read reclaim:** read reclaim *adds* P/E cycles (every reclaim erases a
block). A read-heavy, write-light workload can therefore wear out the device through reclaim
alone — which is exactly why minimizing unnecessary reclaims matters (Section 4.3, 5).

---

## 3. Read Disturbance

### 3.1 Mechanism

To read one page, the controller must turn the *entire NAND string* into a conductor so that
current can flow through the one cell being sensed. It does this by applying a high
**pass-through voltage `Vpass`** (a.k.a. `Vread`/`Vpassread`, e.g. **> ~6 V**) to **all the
*other* (unselected) word lines in the block**, forcing those cells to conduct regardless of
their stored value.

This high `Vpass` causes **weak, unintended programming** of the unselected cells: a small
amount of charge tunnels onto their floating gates (Fowler–Nordheim / hot-carrier injection),
**shifting their Vth upward**. One read barely moves anything, but the effect **accumulates over
many reads** to the *same block*. Eventually a disturbed cell's Vth crosses a state boundary and
the stored bit flips.

Key characteristics from the literature:

- **It is the *unread* pages in a *read* block that get corrupted**, not the page being read.
  Read disturb is a *block-local, read-count-driven* error mode.
- **Direction:** Vth shifts **upward**. Cells in the **erased / lowest-Vth state are the most
  vulnerable** (they have the most room to shift and the read-reference margin is tightest).
- **Adjacency (3D NAND):** disturbance is **highly non-uniform** across WLs. Reading a WL
  disturbs its **immediate neighbor WLs far more** than distant ones — the STRAW study measured
  **~8.4× more disturbance to adjacent WLs** than non-adjacent WLs, partly because controllers
  apply a higher `VpassH` (~0.4 V higher) to the two WLs adjacent to the target.

### 3.2 Factors that worsen read disturb

| Factor | Effect |
|--------|--------|
| **Read count** (to the block) | RBER rises **~linearly** with the number of reads since last erase/program. This is the primary driver, and the basis for read-count thresholding. |
| **P/E cycle count (wear)** | More-worn blocks have a damaged oxide → disturb errors appear sooner and grow faster. Disturb tolerance drops as the device ages. |
| **Retention age** | Time since programming; combines with disturb to push RBER past ECC. |
| **Cell density** | MLC < TLC < QLC tolerance; more states = thinner margins = fewer reads tolerated. 3D scaling adds per-WL variation. |
| **Per-WL process variation** | Even within one block, tolerance varies widely WL-to-WL (STRAW: best WL endured **+559K** reads vs worst WL **403K** before corruption). |

### 3.3 Representative numbers (order of magnitude)

These vary enormously by node/vendor; treat as illustrative for picking an emulation threshold:

- Older work treated read-disturb as a ~**1,000,000-read** problem per block.
- Modern dense MLC can show uncorrectable errors after **as few as ~20,000–50,000 reads** to a
  block (Cai et al.: "as few as 20,000 reads"; STRAW pattern PA: uncorrectable after **54,560**
  reads to one hot WL). Sequentially spread reads tolerate far more (STRAW pattern PB:
  **518,420** reliable reads).
- RBER grows roughly linearly with read count for a fixed P/E state; lowering `Vpass` by ~2% can
  cut RBER by up to ~50% (Cai et al.).

> **Takeaway for NVMeVirt:** the *defensible model* is "a block can absorb `RRT` reads since its
> last erase before it must be reclaimed," with `RRT` a configurable constant (e.g. tens of
> thousands to ~1M). The exact value is a tunable, not a law of physics.

---

## 4. Read Reclaim

### 4.1 What it is

**Read reclaim (RR)** — also called read-disturb-driven data migration or "read refresh" — is
the firmware countermeasure to read disturb: **before** accumulated read disturbance pushes a
block's RBER beyond what ECC can correct, the controller **relocates the block's valid data to a
fresh block and erases the old one**. Erasing resets every cell to the erased state, wiping out
the accumulated Vth drift; the migrated data lands in a clean block with a fresh read budget.

### 4.2 Block-level (read-count) read reclaim — the basic mechanism

This is the mechanism to implement. It is the simplest and most common production approach:

1. **Per-block read counter `RC`.** The controller maintains a counter for each block
   (reset to 0 on erase). Every page read to the block increments `RC`.
2. **Threshold `RRT` (a.k.a. `RC_MAX` / `RD_THRESHOLD`).** A single predefined upper bound on
   reads-per-block, **set conservatively** so that even the worst-case access pattern (all reads
   hammering the single most-vulnerable WL) stays within ECC capability.
3. **Trigger.** When `RC ≥ RRT`, the block is flagged for reclaim.
4. **Reclaim action.** Copy all **valid** pages of the block to free pages (out-of-place,
   updating L2P), then **erase** the old block and return it to the free pool with `RC = 0`.
   This is functionally identical to a GC pass on that block.

Pseudo-flow:

```
on read(page P in block B):
    perform NAND sense for P
    B.read_cnt += 1
    if B.read_cnt >= RRT:
        enqueue B for read-reclaim     # or reclaim inline

read_reclaim(B):
    for each valid page p in B:
        read p; write p to new block (update maptbl)   # like gc_write_page
    erase B; B.read_cnt = 0; return B to free list
```

### 4.3 Costs (why RRT must not be too low)

Read reclaim is **not free** — it converts reads into hidden background writes + erases:

- **Extra NAND writes** → contributes to **write amplification** (on top of GC).
- **Extra P/E cycles** → consumes **endurance**; a pathological read-only workload can wear the
  device out purely via reclaim.
- **Performance / tail latency** → reclaim contends with host I/O for die time, inflating p99/p999
  read latency (STRAW reports block-level RR can dominate tail latency under read-heavy loads).

Hence the central tension: **`RRT` too high → data corruption (uncorrectable errors); `RRT` too
low → excessive WA, wasted endurance, and latency spikes.** Conservative single-threshold
block-level RR is safe but over-reclaims, because it sizes the whole block by its single weakest
WL while most WLs and most read patterns are far below the limit.

### 4.4 More advanced variants (context / future work — *not* required for the basic version)

| Technique | Idea |
|-----------|------|
| **Word-line-level RR (e.g. STRAW)** | Track disturbance per WL, not per block; reclaim only the heavily-disturbed WLs. Exploits the ~8.4× adjacency asymmetry and per-WL variation. STRAW reports ~**83.6%** fewer RR-induced page writes and ~**70.4%** lower p99.9 read latency vs block-level. |
| **Adaptive / error-based thresholding** | Instead of a fixed read count, periodically *test-read* a block and measure actual bit errors; trigger reclaim when RBER nears the ECC limit. (Used in 3D NAND controllers.) |
| **Vpass tuning** | Dynamically lower the pass-through voltage per block to slow disturb accumulation (Cai et al.: ~21% endurance gain). |
| **Read-disturb-aware scheduling / hot-data segregation** | Place frequently-read ("read-hot") data so disturbance is spread or isolated, reducing reclaim frequency. |
| **Read Disturb Recovery (RDR)** | A recovery (not prevention) step that re-reads with adjusted references to recover data from an already-disturbed block (Cai et al.: ~36% RBER reduction at 1M reads). |

For the NVMeVirt task, **block-level read-count RR (4.2) is the target**; the rest is useful
framing for evaluation and future extensions.

---

## 5. Choosing the Threshold (practical guidance for the emulator)

- Make `RRT` a **compile-time constant or config** (alongside the GC thresholds in
  `conv_ftl.c` `conv_init_params`, or in `ssd_config.h`). `RRT` is **reads per physical block**.
  Typical experimental values to sweep: **50K, 100K, 500K, 1M reads/block**.
- Optionally scale `RRT` with **cell type** (`cell_mode` / `MAX_CELL_TYPES` in `ssd.h`): lower for
  TLC/QLC, higher for SLC/MLC.
- Optionally couple to **wear**: shrink the effective `RRT` as `erase_cnt` grows (disturb worsens
  with P/E cycles) — a cheap way to model Section 3.2.
- For evaluation, the interesting outputs are **read-reclaim count**, the resulting **write
  amplification**, **extra erases**, and **read tail latency** — exactly the costs in Section 4.3.

---

## 6. Mapping to NVMeVirt (`conv_ftl`) — implementation hooks

This is orientation only (the literature summary is the deliverable), but it grounds Section 4.2
in the actual code. **Granularity = physical `nand_block`** for both counting and reclaim.

### 6.1 Counting (straightforward)

1. **Add a per-physical-block counter.** Add `int read_cnt;` to **`struct nand_block`**
   (`ssd.h`) — *not* `struct line`. This gives one counter per `(ch, lun, pl, blk)`.
2. **Increment on read.** In `conv_read()` (`conv_ftl.c:833`), for each page actually sensed,
   resolve its physical block with `get_blk(conv_ftl->ssd, &ppa)` and do `blk->read_cnt++`.
   ⚠️ The stock read path **aggregates** consecutive reads that fall in the same flash page (for
   latency modeling). Disturb accumulates **per read operation (per WL sense)**, so increment for
   each page/flash-page actually sensed, not once per host command — audit the aggregation loop so
   the count reflects the sensing semantics you intend.
3. **Reset on erase.** Set `read_cnt = 0` in `mark_block_free()` (`conv_ftl.c:558`), which already
   runs for every physical block when its line is reclaimed/erased. (Erase wipes the disturb, so
   the counter must reset there.)

### 6.2 Reclaim (the part that needs care)

The catch: `do_gc()` erases an **entire line** at once (it loops over every `ch`/`lun` and erases
all same-index blocks together; the write pointer stripes new data across the whole line). A
single physical block is therefore **not** independently free-listed or erasable in stock
`conv_ftl`. Two ways to honor physical-block granularity:

- **Option A — block-granular monitor, line-granular reclaim (recommended first cut).**
  Keep the counter per physical block (true to hardware), but when **any** block in a line reaches
  `RRT`, reclaim the **whole line** via the existing `do_gc()`-style path (relocate all valid pages,
  erase every block in the line, reset every block's `read_cnt`). This needs almost no change to
  the free-space/erase model — you only add the trigger and a victim-selection-by-read-count path.
  It slightly over-reclaims (drags along the line's cooler blocks) but is simple and correct.

- **Option B — true per-physical-block reclaim (faithful, more invasive).** Reclaim and erase
  exactly the one over-threshold `nand_block`. This requires the FTL to track free space and a
  write target at **block** granularity rather than line granularity: relocate just that block's
  valid pages (`clean_one_flashpg` over its flash pages), `NAND_ERASE` that single block, and make
  it independently allocatable again. You must handle a line that is now **partially erased**
  (mixed free/used blocks), which the stock line abstraction (`free_line_list`,
  `mark_line_free`, the line write pointer) does not support as-is. Plan for a block-level free
  pool / allocator if you go this route.

In both options, reclaim selection differs from GC: GC's `select_victim_line` is **greedy by
valid-page count**, whereas read reclaim selects by **`read_cnt ≥ RRT` regardless of valid-page
count**. Implement it as a separate check (a scan, a flag set at increment time, or a dedicated
reclaim queue) rather than reusing the `victim_line_pq` priority.

### 6.3 Cost modeling & safety (both options)

4. **Reuse the NAND primitives.** Route copy-reads/copy-writes through `gc_read_page` /
   `gc_write_page` and the erase through a `NAND_ERASE` `nand_cmd` — all go through
   `ssd_advance_nand()`, so the extra latency, die-busy time, and `erase_cnt` bump are already
   modeled. Decide whether reclaim runs **inline** with the triggering read or as **background**
   work, since that drives the read-tail-latency results you measure.
5. **Free-space safety.** Reclaim consumes free pages just like GC; make sure it cooperates with
   `should_gc()` / the free-line (or free-block) accounting so the device can't run out of space
   mid-reclaim.
6. **Suggested metrics to export.** reclaim count, reclaim-induced page copies, extra erases
   (→ write amplification), and read p99/p999 latency — the costs from Section 4.3.

---

## 7. Annotated Sources

**Foundational characterization & mitigation**
- Yu Cai, Yixin Luo, Saugata Ghose, Onur Mutlu, *"Read Disturb Errors in MLC NAND Flash Memory:
  Characterization, Mitigation, and Recovery,"* IEEE/IFIP **DSN 2015**. The canonical
  experimental study of read disturb: pass-through-voltage mechanism, RBER vs. read count / P/E
  cycles, `Vpass` tuning (~21% endurance gain), and Read Disturb Recovery (~36% RBER reduction).
  PDF: <https://arxiv.org/pdf/1805.03283> · IEEE: <https://ieeexplore.ieee.org/document/7266871>
- Onur Mutlu et al., *"Error Characterization, Mitigation, and Recovery in Flash-Memory-Based
  SSDs"* (Proc. IEEE survey) — broader context: P/E cycling, retention, read disturb, cell-to-cell
  interference, and the full menu of FTL countermeasures.

**Read reclaim techniques**
- *"STRAW: A Stress-Aware WL-Based Read Reclaim Technique for High-Density NAND Flash-Based
  SSDs,"* **2025**. Clearest modern description of conventional block-level RR (`RC` counter,
  `RCMAX` threshold, copy-all-valid-then-reclaim) and its inefficiency vs. per-WL disturbance
  (~8.4× adjacency asymmetry; per-WL tolerance variation; ~83.6% write reduction, ~70.4% p99.9
  latency reduction). Best single reference for the "block vs. WL" framing.
  PDF/HTML: <https://arxiv.org/pdf/2501.02517> · <https://arxiv.org/html/2501.02517>
- *"A read-disturb management technique for high-density NAND flash memory."* Early read-reclaim /
  read-count-threshold management proposal.
  <https://www.researchgate.net/publication/262411470_A_read-disturb_management_technique_for_high-density_NAND_flash_memory>
- J. Li et al., *"Mitigating Negative Impacts of Read Disturb in SSDs,"* **ACM TODAES / DAES 2020**.
  Read-disturb-aware data management to cut reclaim overhead.
  <https://bgerofi.github.io/papers/jli-DAES20.pdf>
- *"Read Disturb-aware Write Scheduling and Data Reallocation in SSDs."* Scheduling/placement to
  reduce read-disturb-induced migrations.
  <https://www.researchgate.net/publication/340107342>
- *"Page Type-Aware Data Migration Technique for Read Disturb Management of NAND Flash Memory,"*
  IEEE. LSB/MSB/CSB-aware reclaim granularity (relevant to NVMeVirt's `CELL_TYPE_*`).
  <https://ieeexplore.ieee.org/document/10040559/>

**Industry / patents (adaptive thresholding)**
- SK hynix, *"Read disturb detection and recovery with adaptive thresholding for 3-D NAND
  storage,"* US Patent. Error-based (test-read) reclaim triggering instead of fixed read counts.
  <https://image-ppubs.uspto.gov/dirsearch-public/print/downloadPdf/10714195>

**NVMeVirt codebase references used**
- `ssd.h` — physical hierarchy (`nand_block.erase_cnt`, `cell_mode`, `get_blk`/`get_line`).
- `conv_ftl.h` / `conv_ftl.c` — `struct line`, GC (`do_gc`, `select_victim_line`,
  `clean_one_flashpg`, `mark_block_free`, `mark_line_free`), read path (`conv_read`),
  thresholds (`should_gc`, `conv_init_params`).

---

*Sources accessed May 2026. Specific read-count and overhead figures are device- and study-
specific; use them to choose a configurable `RRT` for emulation, not as absolute hardware limits.*
