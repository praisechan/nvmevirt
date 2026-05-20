# NVMeVirt fio Quickstart

This runs the fio workloads in `nvmev-evaluation/fio` against an NVMeVirt
device. No source-code changes are required, but the host must be booted with a
reserved memory range for NVMeVirt.

## What the fio wrapper does

`fio/do_eval.sh` expects NVMeVirt to already be loaded. It then:

1. calls `common/set_perf.py` to write latency/bandwidth knobs under
   `/proc/nvmev`;
2. starts CPU, disk, and NVMeVirt stat collectors;
3. runs `fio workloads/<workload>.fio`;
4. moves output files into `fio/results/`.

The device is selected by `DEV`, without the `/dev/` prefix. If `DEV` is not
set, `fio/do_eval.sh` uses `nvme2n1`, which is probably wrong on a fresh test
machine.

## Current host notes

On this machine, plain `make` in `/home/juchanlee/nvmevirt` fails with:

```text
main.c:217:64: error: 'E820_TYPE_RESERVED_KERN' undeclared
```

For the installed `6.17.0-14-generic` headers, the module builds without source
edits using a temporary compiler define:

```bash
cd /home/juchanlee/nvmevirt
make KCFLAGS=-DE820_TYPE_RESERVED_KERN=128
```

The host also needs `fio`; `sysstat` is already present here:

```bash
sudo apt install fio sysstat
```

## Reserve memory

NVMeVirt maps its emulated storage into a reserved physical memory range. The fio
workloads in this repo use `filesize=127g`, so reserve and load at least 128 GiB
if you want to run them unmodified.

Edit `/etc/default/grub` and add a memmap reservation. This example reserves
128 GiB starting at 128 GiB and isolates CPUs 7 and 8 for NVMeVirt:

```bash
GRUB_CMDLINE_LINUX="memmap=128G\\\$128G isolcpus=7,8"
```

Then update GRUB and reboot:

```bash
sudo update-grub
sudo reboot
```

After reboot, rebuild NVMeVirt for the running kernel:

```bash
cd /home/juchanlee/nvmevirt
make KCFLAGS=-DE820_TYPE_RESERVED_KERN=128
```

## Load NVMeVirt

Do not use `common/init_nvmev.sh` for the unmodified fio workloads as-is: it
loads only `memmap_size=64G`, but the fio job files use `filesize=127g`.

Load the module manually with a matching 128 GiB range:

```bash
cd /home/juchanlee/nvmevirt
sudo insmod ./nvmev.ko memmap_start=128G memmap_size=128G cpus=7,8
```

Check that the device and `/proc/nvmev` appeared:

```bash
lsblk -d -o NAME,SIZE,MODEL
ls /proc/nvmev
dmesg | tail -50
```

On a machine with no other NVMe devices, the namespace will likely be
`/dev/nvme0n1`, but verify it before running fio. These workloads access the raw
block device directly.

## Run a fio workload

From the fio evaluation directory:

```bash
cd /home/juchanlee/nvmev-evaluation/fio
sudo DEV=nvme0n1 ./do_eval.sh max 0 0 rand-read
```

`max` tells `set_perf.py` to configure minimal delay. The remaining middle
arguments are placeholders in this mode; the fourth argument is the workload
basename under `fio/workloads`.

For a latency/bandwidth-shaped run instead of max mode:

```bash
sudo DEV=nvme0n1 ./do_eval.sh 15 15 3200 rand-read
```

That means target read latency `15 us`, target write latency `15 us`, and target
bandwidth `3200 MB/s`.

Other workload names include:

```text
rand-read
rand-read-aio
rand-write
rand-write-aio
seq-read
seq-read-aio
seq-read-1m
seq-write
seq-write-aio
seq-write-1m
```

Each workload runs for `600` seconds because that value is hard-coded in the fio
job files.

## Results and cleanup

Results are written under `fio/results/`, for example:

```text
result-rand-read-max-0-0
iostat-rand-read-max-0-0
cpustat-rand-read-max-0-0
nvstat-rand-read-max-0-0
```

If you interrupt a run, stop leftover collectors:

```bash
/home/juchanlee/nvmev-evaluation/common/abort_eval.sh
```

Unload NVMeVirt when finished:

```bash
sudo rmmod nvmev
```
