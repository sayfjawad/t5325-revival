# Reviving the HP Compaq t5325: from locked kiosk to your own Linux box over a $5 cable

A step-by-step account — and a working script — for unlocking an ex-corporate **HP
Compaq t5325** thin client with an unknown admin password, no reset button, and every
remote door closed. If you've got a pile of these (or anything similar: old ARM thin
clients with a U-Boot bootloader instead of a BIOS) heading for the scrap pile, this is
how to get a root shell on one without a factory reflash — and the decision tree for
what to put on it afterwards.

This isn't a product or a maintained appliance image. It's a field report from actually
doing it, kept close to what happened, so the dead ends are as visible as the fix.

## The box, and why it's stuck

The t5325 is a thin client: no hard disk to speak of, a small flash chip, and just enough
Linux to run a remote-desktop client full-screen. Ours were ex-corporate units, pulled
from service, still configured to auto-launch a connection to a server that no longer
exists. That's normal for these — they get thrown out *because* nobody remembers the
admin password, not because they're broken.

**What a normal person tries first, and why it doesn't work here:**

- *Press the magic key for a boot menu.* There isn't one. This is ARM, not x86 — there's
  a bootloader called **U-Boot** instead of a BIOS, and no USB-boot hotkey.
- *SSH or VNC in.* Both are listening, both ask for credentials you don't have.
  `root`/`root` — the old folk-wisdom default for this vendor's kiosk mode — was
  changed by whoever owned it.
- *Use the browser to get around it.* No address bar. It's locked to one destination.
- *Hold a reset button.* There isn't one.

Every soft route is closed on purpose. The hardware opens one door that software can't
lock: a serial console into the bootloader, present before any of the kiosk software has
had a chance to start.

## Opening the hardware door

Inside, on the motherboard, there's a small 4-pin header (ours was labelled **COMA**):
`3V3 / TXD / RXD / GND`, a micro-JST 1.25mm connector. That's a UART — a dumb,
decades-old serial port, 115200 baud, 3.3V logic. A $5 USB-to-serial adapter (CP2102 or
FTDI chipsets are the common, reliable ones) plus a matching 4-pin pigtail cable is all
you need. Wire it crossed:

| Thin client header | → | USB-serial adapter |
|---|---|---|
| GND | → | GND |
| TXD | → | RXD |
| RXD | → | TXD |
| 3V3 | → | **leave disconnected** — don't power the board from the USB adapter |

Plug it into any Linux (or Mac/Windows with the right driver) machine, open a terminal
emulator at 115200 8N1 (`picocom`, `minicom`, `screen /dev/ttyUSB0 115200`), and power the
unit on. You should see the bootloader's banner scroll past.

**A cheap-adapter gotcha that cost real debugging time:** a counterfeit PL2303 adapter
(common on marketplaces; check `dmesg` for `pl2303_get_line_request - failed: -32` /
`error sending break = -32` as tells) will, once it gets slightly confused, **replay its
last-received buffer indefinitely**, padded with null bytes, instead of going quiet. If
you're scripting the serial link rather than watching it by eye: open the port with
`O_NONBLOCK` and `CLOCAL` set (plain `cat`/`stty` on the raw device can simply hang
without `CLOCAL`), strip null bytes, and discard any chunk that's byte-identical to the
one immediately before it — otherwise you'll read megabytes of stale text and never
notice when something new actually arrives. The fragile little jumper wires on a pigtail
cable also come loose easily; if the serial link suddenly goes *completely* silent after
working fine, check the wiring before you suspect anything else.

## The bootloader prompt, and its one real limit

U-Boot (ours: version 1.1.4, from 2010) autoboots about a second after its banner unless
you mash a key first. Catch that window and you land at a `>>` prompt where you can run
commands directly — load files from the internal storage, inspect and change boot
variables, and so on.

One easy-to-hit limit: this old a U-Boot caps commands at **16 space-separated tokens**
(`** Too many args (max. 16) **` if you go over). Keep `setenv` commands short, or build
them up across a couple of commands.

## The wall: `init=/bin/sh` panics

The textbook trick for getting a root shell on a locked Linux box is to tell the kernel to
run a shell instead of the normal init process (`init=/bin/sh` on the kernel command
line). On this box, that **panics**:

```
Warning: unable to open an initial console.
Kernel panic - not syncing: Attempted to kill init!
```

The reason is a timing problem, not a security feature: this kernel boots through an
**initramfs** (a tiny initial filesystem whose job is to set things up before handing off
to the real one), and `/dev/console` — the thing a shell needs to read your keystrokes
and print output — doesn't exist yet at the point `init=` takes over. Replacing init
entirely skips the initramfs script that would have created it, so the shell has nothing
to attach to and dies instantly.

**The initramfs's own scripts already have a better door**, meant for exactly this kind
of debugging: a `break=<stage>` kernel argument (stages like `top`, `premount`, `mount`,
`bottom`...). `break=bottom` drops you into a real shell — with a working console —
*after* the real root filesystem is already mounted, just before the final hand-off to
normal init:

```
setenv bootargs console=ttyS0,115200 root=/dev/sda1 rw init=/init break=bottom
bootm 0x200000     # after loading the kernel into memory first — see the script
```

(The exact root device and kernel load address are specific to how this image's boot was
set up — `printenv` at the bootloader prompt will show you what this unit already uses to
boot normally; copy that and just add `break=bottom`.)

**A pleasant discovery:** you don't need to power-cycle the unit to get back to a normal
boot afterwards. Typing `exit` out of that recovery shell doesn't reboot — the script that
dropped you in is designed to simply *carry on* to the normal boot once you leave the
shell. Whatever you changed while you were in there takes effect on the very same boot.

## What you can actually do once you're in that shell

You're root, and the real disk is mounted (commonly at `/root` from the initramfs's point
of view, since that's its own temporary mountpoint convention — not to be confused with
the `root` *user's home directory*, which lives somewhere underneath it). From there,
normal Unix tools work: find `/etc/shadow` and edit the password hash, or add an SSH
public key to `root`'s `~/.ssh/authorized_keys`. Many of these embedded Linux images use
a **union/overlay filesystem** — a read-only base layer plus a writable layer on top, so
the vendor can ship a read-only "factory" filesystem that still allows configuration
changes. If `/etc` looks suspiciously empty when you `ls` it, look for a sibling
directory (ours was named `writable-cow` — "cow" as in *copy-on-write*) that holds the
real, editable files; that's what you should edit, and it'll be what the fully-booted
system actually reads.

**Resetting a password**, generically:

```sh
openssl passwd -1 -salt <8-hex-chars> '<new-password>'   # prints an MD5-crypt hash
# then, in the recovery shell:
sed -i 's#^root:[^:]*:#root:<new-hash>:#' /path/to/writable/etc/shadow
```

**Installing an SSH key** instead (so you never need a password again):

```sh
mkdir -p /path/to/writable/root/.ssh
chmod 700 /path/to/writable/root/.ssh
echo '<your-public-key>' >> /path/to/writable/root/.ssh/authorized_keys
chmod 600 /path/to/writable/root/.ssh/authorized_keys
```

## SSH that time forgot

If the device's SSH server is as old as ours (this one dated to roughly 2008/2009), two
things will trip up a modern client:

1. **It won't understand an `ed25519` key.** That key type didn't exist until 2014.
   Generate an RSA key for devices like this instead: `ssh-keygen -t rsa -b 2048 -f
   ~/.ssh/id_rsa_thinclient`.
2. **A modern OpenSSH client refuses to even negotiate** with a server that only offers
   `ssh-rsa`/`ssh-dss` host keys — `no matching host key type found` — because those are
   disabled by default now for security. You need to explicitly re-allow them for this
   one connection via `~/.ssh/config`:

   ```
   Host my-thin-client
       HostName <its-ip>
       User root
       IdentityFile ~/.ssh/id_rsa_thinclient
       IdentitiesOnly yes
       HostKeyAlgorithms +ssh-rsa,ssh-dss
       PubkeyAcceptedAlgorithms +ssh-rsa,ssh-dss
       KexAlgorithms +diffie-hellman-group1-sha1,diffie-hellman-group14-sha1
       Ciphers +aes128-cbc,3des-cbc
   ```

   `IdentitiesOnly yes` matters if you have other SSH keys loaded: without it, a client
   offers *all* of them, and an ancient server will disconnect with `Too many
   authentication failures` before ever trying the right one.

## The script

[`scripts/bringup.py`](scripts/bringup.py) automates the whole sequence above: catch the
bootloader prompt during power-on, load the kernel, drop into the recovery shell via
`break=bottom`, install an SSH key (optionally reset the root password too), and resume
the normal boot — no reflashing, no second power cycle.

```sh
./scripts/bringup.py --pubkey ~/.ssh/id_rsa_thinclient.pub [--root-password '<temp-pw>'] \
    [--serial /dev/ttyUSB0] [--label unit-1]
```

It's written for the t5325 specifically (the kernel load address, root device, and recovery
path it uses come from this model's `printenv`), but the *technique* — serial console →
interrupt autoboot → `break=bottom` → edit the writable overlay → `exit` to resume — should
transfer to plenty of other U-Boot-based embedded Linux devices with minimal changes. If you
adapt it for a different model, a PR adding that model's specifics is very welcome.

**Honesty check:** every individual step here was run and verified by hand against one real
unit. The script assembles those steps into one automated run; treat the first run against
any given device as a dry run you watch closely, not a "set and forget."

## So what do you actually put on it?

Getting in is the easy 20%. These things are genuinely weak hardware — ours: a 1.2GHz
single-core ARMv5 (no hardware floating point even), 512MB RAM, and well under 512MB of
usable flash storage. That rules out more than it allows. Realistic, useful jobs for a box
like this: DNS-based ad-blocking for a home network, a small MQTT broker, a print spooler,
or any lightweight always-on script.

What we learned sizing up the OS options:

- **"Just install current Debian"** is usually a dead end on hardware this old. Debian
  has quietly retired support for the architecture these chips use (ARMv5 soft-float,
  called `armel`) from its current releases; whatever community how-to exists for your
  exact model was probably written a decade ago against a Debian release that's long
  past end-of-life, and may say outright that the result doesn't fully boot. Don't assume
  "Debian" means "modern and maintained" here — check the actual architecture support
  before investing time.
- **Cleaning up the factory image in place** (kill the auto-launching kiosk app, keep
  SSH, maybe free up space) is the fastest, lowest-risk option, and everything above gets
  you there. The catch: you're still running a decade-plus-old userland with dead package
  repositories. Fine as a quick stopgap; not something to build new software on.
- **A purpose-built embedded Linux distribution for exactly this resource class** —
  OpenWrt is the standout example — is the better long-term target if your chip family
  has an actively maintained target for it. These distributions are *designed* for
  512MB-and-under boxes, ship current, patched packages via a lightweight package
  manager, and often already support the exact reference silicon your device is built on
  under the hood (worth checking what board ID shows up in your boot log — ours turned
  out to be built on a basically-stock reference design from its chip vendor, which is a
  good sign for driver support in a generic image for that chip family). It's extra setup
  work up front — there's rarely a ready-made image for your *exact* thin client model —
  but it's the difference between a box you can keep securely for years and one you're
  quietly glad nobody ever points a port scanner at.

Whichever you pick, you only need this serial unlock once per unit, regardless of what you
put on it afterwards — it's how you get in, not what you end up running.

## Contributing

Got a different thin client model (or any other U-Boot-based embedded box) with a similar
story? PRs adding a model-specific note, a wiring diagram, or boot quirks you hit are
welcome — the goal is a growing reference for "I found an old thin client, now what,"
not a single-vendor how-to.

## License

MIT — see [LICENSE](LICENSE). Do what you like with it; no warranty, especially regarding
anyone else's leftover corporate hardware you happen to find.
