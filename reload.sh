#!/usr/bin/env bash
# reload.sh — rebuild and reload the NVMeVirt kernel module for one iteration.
#
# Prereqs (see experiment.md):
#   1. Physical memory reserved via grub (memmap=...) and rebooted.
#   2. Kbuild set to CONFIG_NVMEVIRT_SSD := y  (read reclaim lives in conv_ftl.c).
#   3. sudo credentials cached:  sudo -v   (timeout extended via /etc/sudoers.d/nvmev-timeout).
#
# Usage:
#   ./reload.sh                       # uses defaults below
#   MEMMAP_START=128G MEMMAP_SIZE=64G CPUS=7,8 ./reload.sh
#
# IMPORTANT: MEMMAP_START / MEMMAP_SIZE MUST match the memmap=<size>$<start>
# reservation in /etc/default/grub, or insmod will fail / corrupt memory.

set -euo pipefail

# --- Config (override via environment) ---
# Defaults sized for THIS host (125 GiB RAM, 24 cores): small 4 GiB device for
# fast read-reclaim testing, NVMeVirt threads on isolated high cores.
MEMMAP_START="${MEMMAP_START:-64G}"    # offset of reserved memory  (memmap=4G$64G -> 64G)
MEMMAP_SIZE="${MEMMAP_SIZE:-4G}"       # size of reserved memory    (memmap=4G$64G -> 4G)
CPUS="${CPUS:-22,23}"                  # >=2 cores: 1 dispatcher + >=1 worker
MODNAME=nvmev

# Kernel 6.8.0-111 (current host) DEFINES E820_TYPE_RESERVED_KERN in its headers
# (arch/x86/include/asm/e820/types.h), so we must NOT force-define it — doing so
# collides with the enum ("expected identifier before numeric constant"). Build
# clean. (On the older 6.5.0-35 kernel the symbol was absent and needed
# -DE820_TYPE_RESERVED_KERN=128; override via KCFLAGS env if you go back to it.)
# Requires gcc-12 (the compiler the kernel was built with).
KCFLAGS="${KCFLAGS:-}"

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

echo "==> [1/4] Building module (make KCFLAGS=$KCFLAGS)..."
make KCFLAGS="$KCFLAGS"

# Secure Boot is enabled on this host (sig_enforce=Y under EFI lockdown), so the
# kernel rejects unsigned modules ("Key was rejected by service"). Sign the freshly
# built module with the enrolled MOK before loading. One-time enroll: see QUICKSTART §1.
MOK_KEY="${MOK_KEY:-/var/lib/shim-signed/mok/MOK.key}"
MOK_DER="${MOK_DER:-/var/lib/shim-signed/mok/MOK.der}"
SIGN_FILE="/usr/src/linux-headers-$(uname -r)/scripts/sign-file"
if [ -f "$MOK_KEY" ] && [ -f "$MOK_DER" ] && [ -x "$SIGN_FILE" ]; then
    echo "==> [1b] Signing module with MOK ($MOK_DER)..."
    sudo "$SIGN_FILE" sha256 "$MOK_KEY" "$MOK_DER" "./${MODNAME}.ko"
else
    echo "==> [1b] WARNING: MOK signing skipped (key or sign-file missing) — insmod will"
    echo "         fail under Secure Boot. See QUICKSTART §1 (module signing)."
fi

echo "==> [2/4] Unloading old module if present..."
if lsmod | grep -q "^${MODNAME}\b"; then
    sudo rmmod "$MODNAME"
else
    echo "    (not loaded)"
fi

echo "==> [3/4] Loading: memmap_start=$MEMMAP_START memmap_size=$MEMMAP_SIZE cpus=$CPUS"
sudo insmod "./${MODNAME}.ko" \
    memmap_start="$MEMMAP_START" \
    memmap_size="$MEMMAP_SIZE" \
    cpus="$CPUS"

echo "==> [4/4] Recent kernel log:"
sudo dmesg | tail -n 25

echo "==> Device nodes:"
ls -l /dev/nvme* 2>/dev/null || echo "    (no /dev/nvme* found — check dmesg above)"
