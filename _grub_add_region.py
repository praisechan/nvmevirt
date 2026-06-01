#!/usr/bin/env python3
# §1b helper: append OUR reserved region memmap=4G$64G to GRUB_CMDLINE_LINUX,
# keeping the other user's memmap=16G$96G untouched. Idempotent. Makes a backup.
# Mirrors the existing escaping style (\\\$) so update-grub resolves it correctly.
import shutil, sys, os

p = "/etc/default/grub"
s = open(p).read()

if "memmap=4G" in s:
    print("already present -> no change needed")
    sys.exit(0)

if '96G"' not in s:
    print("ERROR: could not find the 16G$96G token to append after; aborting.")
    sys.exit(1)

shutil.copy2(p, p + ".bak.nvmev_rr")
# raw string -> literal chars: \ \ \ $  (same escaping the working 96G entry uses)
new = s.replace('96G"', r'96G memmap=4G\\\$64G"', 1)
open(p, "w").write(new)
print("appended OUR region. backup at", p + ".bak.nvmev_rr")
print("--- new GRUB_CMDLINE_LINUX line ---")
for line in new.splitlines():
    if line.startswith("GRUB_CMDLINE_LINUX="):
        print(repr(line))
