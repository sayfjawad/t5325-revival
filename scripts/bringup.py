#!/usr/bin/env python3
"""
bringup.py — unlock a kiosk-locked HP Compaq t5325 (ARM/Kirkwood thin client) over a
serial console and install passwordless SSH access, without a factory reflash.

See ../README.md for the full story: the hardware, the wiring, and the reasoning
behind every step here. In short, this mechanises:

  1. Catch the device at the U-Boot prompt during power-on (it autoboots after ~1s,
     so we hammer a key the moment the banner appears).
  2. Load the existing kernel from the internal flash (read-only step).
  3. Boot it once with `break=bottom` in the kernel command line. This does NOT wipe
     or reflash anything — it drops into the initramfs's own busybox recovery shell
     *after* the root filesystem is mounted, instead of chaining to the real init.
     (Setting init=/bin/sh directly panics: /dev/console does not exist yet at that
     point in boot, so there's no console to attach a login shell to. `break=bottom`
     is the initramfs's own documented escape hatch and already has a console.)
  4. In that recovery shell, edit the writable overlay layer (commonly named
     something like `writable-cow` on these union-filesystem images) to add an SSH
     key and/or reset the root password.
  5. `exit` the recovery shell. This resumes the *same* boot instead of requiring a
     second power cycle.

Known quirks this script works around (see the README for detail):
  - Cheap/counterfeit USB-serial adapters can replay their last-seen buffer
    indefinitely, padded with NUL bytes, once confused. We open the port with
    O_NONBLOCK + CLOCAL (plain `cat`/`stty` can hang without CLOCAL), strip NULs,
    and drop any chunk identical to the one immediately before it.
  - This era of U-Boot caps commands at 16 space-separated tokens
    ("Too many args (max. 16)") — keep setenv commands short.
  - The on-device OpenSSH on a t5325 this old only speaks ssh-rsa/ssh-dss host and
    pubkey types — use an RSA key, not ed25519, and expect to pass
    HostKeyAlgorithms/PubkeyAcceptedAlgorithms/KexAlgorithms/Ciphers compat flags
    from a modern client.

Usage:
    ./bringup.py --pubkey ~/.ssh/id_rsa_thinclient.pub [--root-password PASSWORD]
                 [--serial /dev/ttyUSB0] [--label unit-1]

What it does NOT do (on purpose, out of caution for unattended runs):
  - It does not discover or print the device's new LAN IP (DHCP; varies per unit).
    Check your router or scan with nmap/arp-scan after boot finishes.
  - It does not disable the factory desktop/kiosk autostart. SSH access works
    alongside it; killing the kiosk is a separate, lower-risk follow-up once you
    have a shell.
  - It never reflashes/repartitions anything. Everything it touches lives in the
    writable overlay layer and is trivially reversible by editing the same files
    back.

Validation status: every individual operation here was run and verified by hand,
interactively, against one real unit. The full catch -> load -> break=bottom -> edit
-> exit sequence as ONE unattended script has not been exhaustively tested across
hardware revisions. Watch the console output on the first run against any unit and
be ready to fall back to the manual steps in the README.
"""
import argparse
import os
import select
import sys
import termios
import time

BAUD = termios.B115200
NEWLINE_WAIT = 0.02  # pacing between characters sent, friendly to flaky adapters


def open_serial(path):
    fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    attrs = termios.tcgetattr(fd)
    attrs[0] = 0  # iflag
    attrs[1] = 0  # oflag
    attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL  # cflag, no HUPCL/modem lines
    attrs[3] = 0  # lflag: raw, no echo/canon
    attrs[4] = attrs[5] = BAUD
    termios.tcsetattr(fd, termios.TCSANOW, attrs)
    termios.tcflush(fd, termios.TCIOFLUSH)
    return fd


def read_dedup(fd, duration):
    """Read for `duration` seconds, stripping NULs and collapsing immediate repeats
    (the counterfeit-adapter replay artifact seen on some cheap USB-serial chips)."""
    out = b""
    last = None
    t0 = time.time()
    while time.time() - t0 < duration:
        r, _, _ = select.select([fd], [], [], 0.05)
        if r:
            try:
                chunk = os.read(fd, 65536)
            except BlockingIOError:
                continue
            chunk = chunk.replace(b"\x00", b"")
            if chunk and chunk != last:
                out += chunk
                last = chunk
    return out


def send_line(fd, text):
    for ch in text.encode() + b"\r":
        os.write(fd, bytes([ch]))
        time.sleep(NEWLINE_WAIT)


def send_cmd(fd, text, duration):
    send_line(fd, text)
    return read_dedup(fd, duration).decode("latin1")


def catch_uboot_prompt(fd, prompt_marker, timeout=180):
    """Spam a space roughly every 100ms until we've seen both the U-Boot banner and
    the device's prompt, i.e. autoboot was successfully interrupted."""
    print(f"[*] Waiting up to {timeout}s for a power cycle. Switch the device off, "
          f"wait 5s, switch it back on now.", file=sys.stderr)
    t0 = time.time()
    next_space = 0
    seen = b""
    banner = False
    while time.time() - t0 < timeout:
        if time.time() > next_space:
            os.write(fd, b" ")
            next_space = time.time() + 0.1
        r, _, _ = select.select([fd], [], [], 0.05)
        if r:
            try:
                chunk = os.read(fd, 65536)
            except BlockingIOError:
                continue
            chunk = chunk.replace(b"\x00", b"")
            if chunk:
                seen = (seen + chunk)[-4000:]
                if b"U-Boot" in seen:
                    banner = True
                if banner and prompt_marker.encode() in seen:
                    print(f"[*] Stopped at the {prompt_marker} prompt.", file=sys.stderr)
                    return True
    return False


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--serial", default="/dev/ttyUSB0",
                    help="serial device (default: /dev/ttyUSB0)")
    ap.add_argument("--pubkey", required=True,
                    help="path to the SSH public key to install for root "
                         "(must be ssh-rsa or ssh-dss if the device's sshd predates "
                         "2014/ed25519 — see the README)")
    ap.add_argument("--root-password", default=None,
                    help="also reset the root password to this value (optional; "
                         "the SSH key alone is enough for passwordless login)")
    ap.add_argument("--label", default=None,
                    help="free-text label for this unit, used only in printed output")
    ap.add_argument("--prompt-marker", default="HP>>",
                    help="the string this device's U-Boot prompt ends with "
                         "(default: 'HP>>', as seen on the t5325; check your own "
                         "device's boot banner if it differs)")
    ap.add_argument("--kernel-load-cmd", default="ext2load ide 0:1 0x200000 /boot/uImage",
                    help="U-Boot command to load this device's existing kernel into "
                         "RAM — read it from `printenv bootcmd`/`printenv thinpro` "
                         "on your own device rather than trusting the default here")
    ap.add_argument("--boot-cmd", default="bootm 0x200000",
                    help="U-Boot command to boot the loaded kernel (must match the "
                         "load address used in --kernel-load-cmd)")
    ap.add_argument("--root-device", default="/dev/sda1",
                    help="kernel root= device for this image (default: /dev/sda1)")
    ap.add_argument("--writable-root", default="/root/writable-cow",
                    help="path, from inside the recovery shell, to the writable "
                         "overlay layer that holds the real /etc, /root, etc. "
                         "(default matches the t5325; inspect your own device's "
                         "layout first if it's a different model/image — see the "
                         "README's 'union/overlay filesystem' section)")
    ap.add_argument("--power-cycle-timeout", type=int, default=180,
                    help="seconds to wait for you to power-cycle the unit (default 180)")
    args = ap.parse_args()

    with open(args.pubkey) as f:
        pubkey = f.read().strip()
    if not (pubkey.startswith("ssh-rsa") or pubkey.startswith("ssh-dss")):
        print("[!] WARNING: this key is not ssh-rsa/ssh-dss. If your device's sshd "
              "predates ed25519 (roughly pre-2014), it will almost certainly reject "
              "anything newer. Continuing anyway, but expect pubkey auth to fail.",
              file=sys.stderr)

    label = f" ({args.label})" if args.label else ""
    print(f"[*] Bringing up device{label} via {args.serial}", file=sys.stderr)

    fd = open_serial(args.serial)
    try:
        if not catch_uboot_prompt(fd, args.prompt_marker, timeout=args.power_cycle_timeout):
            print("[!] Never saw the U-Boot prompt. Check the serial wiring (loose "
                  "jumper wires are a common culprit) and that you actually "
                  "power-cycled the unit, then retry. If your device's prompt isn't "
                  "'HP>>', pass --prompt-marker.", file=sys.stderr)
            sys.exit(1)

        print("[*] Reading printenv for this unit's identity (MAC/serial, if present)...",
              file=sys.stderr)
        env = send_cmd(fd, "printenv", 5)
        for line in env.splitlines():
            if line.startswith(("ethaddr=", "serial=", "product_name=")):
                print(f"    {line}", file=sys.stderr)

        print("[*] Loading the existing kernel (read-only)...", file=sys.stderr)
        out = send_cmd(fd, args.kernel_load_cmd, 12)
        if "bytes read" not in out:
            print(f"[!] Kernel load looked wrong, got:\n{out}", file=sys.stderr)
            sys.exit(1)

        print("[*] Setting one-shot bootargs (break=bottom -> recovery shell, "
              "nothing persisted)...", file=sys.stderr)
        send_cmd(fd,
                 f"setenv bootargs console=ttyS0,115200 root={args.root_device} rw "
                 "init=/init break=bottom", 2)

        print("[*] Booting... waiting for the (initramfs) recovery shell...", file=sys.stderr)
        boot_out = send_cmd(fd, args.boot_cmd, 45)
        if "(initramfs)" not in boot_out:
            print(f"[!] Did not reach the recovery shell. Last output:\n{boot_out[-2000:]}",
                  file=sys.stderr)
            sys.exit(1)
        print("[*] In the recovery shell as root, device's filesystem mounted.",
              file=sys.stderr)

        ssh_dir = f"{args.writable_root}/root/.ssh"
        print("[*] Installing the SSH key for root...", file=sys.stderr)
        send_cmd(fd, f"mkdir -p {ssh_dir}", 2)
        send_cmd(fd, f"chmod 700 {ssh_dir}", 2)
        send_cmd(fd, f"echo '{pubkey}' >> {ssh_dir}/authorized_keys", 2)
        send_cmd(fd, f"chmod 600 {ssh_dir}/authorized_keys", 2)
        verify = send_cmd(fd, f"cat {ssh_dir}/authorized_keys", 2)
        if pubkey not in verify:
            print(f"[!] Key verification failed, file contains:\n{verify}", file=sys.stderr)
            sys.exit(1)
        print("[+] SSH key installed and verified.", file=sys.stderr)

        if args.root_password:
            print("[*] Resetting the root password too...", file=sys.stderr)
            import crypt
            import secrets
            salt = secrets.token_hex(4)[:8]
            new_hash = crypt.crypt(args.root_password, f"$1${salt}$")
            escaped_hash = new_hash.replace("$", r"\$")
            shadow_path = f"{args.writable_root}/etc/shadow"
            send_cmd(fd,
                     "sed -i 's#^root:[^:]*:#root:" + escaped_hash + ":#' " + shadow_path, 2)
            check = send_cmd(fd, f"grep '^root:' {shadow_path}", 2)
            print(f"    {check.strip().splitlines()[-1] if check.strip() else check}",
                  file=sys.stderr)

        print("[*] Syncing and resuming normal boot (no second power cycle needed)...",
              file=sys.stderr)
        send_cmd(fd, "sync", 3)
        send_line(fd, "exit")
        time.sleep(3)
        print("[+] Done. The unit is continuing its normal boot. Give it ~30-60s for "
              "the network to come up, then connect over SSH — see the README's "
              "'SSH that time forgot' section for the client-side compatibility flags "
              "an old sshd usually needs (legacy host key / kex / cipher algorithms, "
              "and IdentitiesOnly if you have other keys loaded).", file=sys.stderr)
    finally:
        os.close(fd)


if __name__ == "__main__":
    main()
