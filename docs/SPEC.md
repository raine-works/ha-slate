# HA Slate — Firmware Technical Specification & Build Guide

| | |
|---|---|
| **Product** | HA Slate: PoE-powered wall touch panel for Home Assistant |
| **Scope** | Linux firmware image: boot, kiosk UI, voice satellite, edge lighting, updates |
| **Build system** | Yocto Project 5.0 "Scarthgap" (LTS, maintained to April 2028), driven by kas |
| **Status** | Draft v0.1: architecture and layer scaffold in this repository; not yet run on hardware |

---

## 0. Assumptions and open decisions

The request did not name a SoC or carrier board, so this spec makes the following assumptions. Every
hardware-specific choice is isolated in a BSP glue layer or kas file so you can swap it out.

| # | Assumption | Where it lives | If it's wrong |
|---|---|---|---|
| A1 | Reference SoM: **Raspberry Pi CM4** (BCM2711, 4× A72, 2–4 GB, eMMC) on a custom PoE carrier | `kas/machine/cm4.yml`, `meta-ha-slate/dynamic-layers/raspberrypi/` | Add a new `kas/machine/<board>.yml` and a `dynamic-layers/<bsp>/` dir (§3.2) |
| A2 | Display: 7–10" MIPI-DSI panel, PWM backlight, I2C capacitive touch (Goodix / FocalTech / Ilitek) | DT overlay in `RPI_EXTRA_CONFIG`; kernel `slate-display.cfg` | Change the panel/bridge drivers in the fragment |
| A3 | Audio: I2S MEMS mics, or a USB/XMOS mic array, plus an I2S class-D amp (MAX98357A class) | `slate-audio.cfg`, `slate-audio` recipe | Only the PipeWire node names change |
| A4 | Edge bar: **48 × WS2812B/SK6812** on SPI0 MOSI at 5 V; an I2C LP50xx variant is also supported | `lightd.toml`, `slate-leds.cfg` | Pick the `sysfs` backend (§9.2) |
| A5 | Power: IEEE 802.3at (Type 2, 25.5 W) PD; must degrade gracefully on 802.3af (12.95 W) | `[power]` in `lightd.toml` | See the power budget (§3.3) |
| A6 | Home Assistant is on the same L2 network (mDNS works) or reachable by a configured URL | `kiosk.env`, `voice.env` | Set `KIOSK_URL`; adopt the satellite in HA by IP |

**Open decisions for the hardware team:** (1) confirm the 802.3at PD class and expose the PD's
"Type-2 detected" pin on a GPIO (§3.3); (2) confirm that SPI MOSI idles low, or add a pull-down (§9.2);
(3) decide whether a boot-time light is required before Linux starts. If it is, use the MCU option (§9.2-C).

---

## 1. Requirements

### 1.1 Functional

| ID | Requirement |
|---|---|
| F1 | Power-on goes straight into a full-screen web dashboard. No desktop, no login prompt, and no way for a user to leave the dashboard. |
| F2 | The browser keeps its Home Assistant session (auth tokens in localStorage) across reboots and firmware updates. |
| F3 | Detects a wake word on-device ("Okay Nabu" by default; selectable from HA) and streams the voice pipeline to HA Assist. |
| F4 | Shows up in HA automatically (zeroconf) as a voice satellite with a media player, timers, announcements and mute. |
| F5 | The edge light bar reflects voice state (wake/listen/think/speak/error/muted/timer) and HA can also control it as a regular RGB light entity. |
| F6 | HA can control the display backlight as a dimmable light entity. |
| F7 | Updates are atomic, signed, A/B. A failed update rolls back automatically with no user action. |
| F8 | Site configuration (URL, device name, static IP) survives updates and can be wiped by a factory reset. |

### 1.2 Non-functional

| ID | Target |
|---|---|
| N1 | Cold boot to dashboard pixels in **≤ 15 s** on CM4 eMMC (budget in §11) |
| N2 | Wake-word response (LED reacts) **≤ 300 ms** after the utterance ends |
| N3 | Peak power stays within the PD budget for the negotiated PoE class (§3.3) |
| N4 | Root filesystem is read-only. eMMC writes are limited to `/data` (no persistent journal) |
| N5 | Nothing listens on the network except the encrypted ESPHome API (TCP 6053) and mDNS. No SSH in production |
| N6 | The kiosk browser is memory-capped so a leaking dashboard cannot starve the voice pipeline |
| N7 | Builds are reproducible from pinned layer commits and hash-pinned downloads, with an SPDX SBOM for every release |

---

## 2. System architecture

```
                         ┌──────────────────────── Home Assistant ────────────────────────┐
                         │  Dashboard (HTTP/WS 8123)      ESPHome integration (API 6053)  │
                         └──────────────▲──────────────────────────────▲──────────────────┘
                                        │ HTTPS/WSS                     │ Noise-encrypted
════════════════════════════════════════╪══════════ PoE Ethernet ══════╪═══════════════════
 HA Slate (Linux, systemd, read-only /) │                               │
                                        │                               │
  ┌──────────────┐   wl socket   ┌──────┴───────┐               ┌───────┴────────────┐
  │ weston       │◄─────────────►│ slate-kiosk  │               │ slate-voice (LVA)  │
  │ kiosk-shell  │ /run/wayland-0│ Cog + WPE    │               │ wake word, Assist, │
  │ DRM/KMS      │               │ WebKit       │               │ media, timers      │
  └──────┬───────┘               └──────────────┘               └──┬──────────┬──────┘
         │ KMS / evdev                              pulse protocol │          │ ws://127.0.0.1:6055
  ┌──────┴───────┐                ┌───────────────────────────────┴──┐  ┌────┴────────────┐
  │ DSI panel +  │                │ PipeWire (system) + WirePlumber  │  │ slate-lightd    │
  │ touch        │                │ echo-cancel: aec_mic / aec_sink  │  │ claims → effects│
  └──────────────┘                └──────┬───────────────────┬───────┘  │ power limiter   │
                                         │ ALSA (I2S/USB)    │          └──┬──────────┬───┘
                                    ┌────┴────┐        ┌─────┴────┐   spidev│    sysfs │
                                    │ mics    │        │ amp/spkr │  ┌──────┴──┐ ┌─────┴─────┐
                                    └─────────┘        └──────────┘  │edge bar │ │ backlight │
                                                                     └─────────┘ └───────────┘
  RAUC (A/B) ── U-Boot boot.scr ── hardware watchdog (systemd RuntimeWatchdogSec)
```

### 2.1 Process inventory

| Unit | User | Purpose | Restart policy |
|---|---|---|---|
| `weston.service` (poky `weston-init`) | `weston` | Wayland compositor, DRM backend, kiosk-shell, PAM session | systemd default |
| `slate-kiosk.service` | `kiosk` | Cog browser, fullscreen | `always`, 2 s, crash-loop guard of 10 per 5 min |
| `pipewire.service` / `.socket` | `pipewire` | System-wide audio graph + AEC | `on-failure` |
| `wireplumber.service` | `pipewire` | Session/policy manager (system mode) | `on-failure` |
| `slate-pipewire-pulse.service` | `pipewire` | Pulse protocol shim for the voice service | `on-failure` |
| `slate-voice.service` | `voice` | Linux Voice Assistant satellite | `always`, 3 s |
| `slate-lightd.service` | root (sandboxed) | Edge bar + backlight, HA light entities | `always`, 1 s |
| `rauc.service`, `rauc-mark-good.service` | root | Update install, slot health commit | — |

### 2.2 Boot sequence

```
ROM → RPi firmware (config.txt, DT overlays) → U-Boot → boot.scr picks slot A/B (§10.2)
  → kernel (from the rootfs slot's /boot) → systemd
     ├─ local-fs: / (ro), /boot (vfat), /data (ext4, growfs) — or tmpfs fallback
     ├─ slate-lightd   (sysinit; boot "scan" animation in < 1 s from userspace start)
     ├─ systemd-networkd + resolved (DHCP, mDNS resolve)
     ├─ weston  → slate-kiosk (graphical.target)
     ├─ pipewire → wireplumber → pipewire-pulse (slate-audio.target)
     └─ slate-voice (after lightd, so HA enumerates the light entities)
          └─ rauc-mark-good (only after kiosk + voice + lightd are healthy for 60 s)
```

---

## 3. Hardware platform

### 3.1 SoC selection criteria

The kiosk browser is the heaviest workload. It needs a GPU with a **mainline Mesa driver** so that
Weston and WebKit get GL ES 3.x without vendor blobs. Voice needs about 1 A-class core of headroom.

| Candidate | GPU (Mesa) | Yocto BSP | Notes |
|---|---|---|---|
| **RPi CM4** (BCM2711) | V3D 4.2 (`v3d`/`vc4`) | `meta-raspberrypi` (very active) | Reference. Mature DSI stack, rpiboot eMMC flashing, huge ecosystem |
| RPi CM5 (BCM2712) | V3D 7.1 | `meta-raspberrypi` (`raspberrypi5`) | About 2–3× CPU. Higher power draw: needs 802.3at |
| Rockchip RK3566/RK3568 | Mali-G52 (`panfrost`) | `meta-rockchip` (mainline kernel) | Cheapest. Mainline DSI/audio quality varies by board |
| NXP i.MX 8M Mini/Plus | GC7000 (`etnaviv`) | `meta-freescale` | Industrial temperature range, long-term supply. 8M Plus adds an NPU |

**Avoid** SoCs that rely only on a vendor GPU blob with no Mesa driver: you would be tied to one
kernel and one compositor version for good.

### 3.2 BSP abstraction

The product layer never refers to a SoC directly. Board specifics live in three places:

1. `kas/machine/<board>.yml`: `MACHINE`, BSP layer pin, boot config, `WKS_FILE`, `IMAGE_FSTYPES`.
2. `meta-ha-slate/dynamic-layers/<bsp-collection>/`: bbappends that are only parsed when that BSP
   layer is present (`BBFILES_DYNAMIC`). Examples are the SoC kernel fragment and the boot script.
3. Runtime facts: `lightd.toml` (LED topology, power), `weston.ini` `[output]` (connector, rotation),
   and the PipeWire AEC node binding.

### 3.3 Power budget

The PD front-end delivers 12.95 W (af) or 25.5 W (at) **at the PD input**. Assume about 85 % DC/DC
efficiency to the 5 V rail.

| Load | Typical | Peak | Notes |
|---|---|---|---|
| CM4 (browser + voice) | 3.0 W | 7.0 W | Peak while WebKit rasterizes and wake-word inference runs at the same time |
| 8" panel backlight | 1.5 W | 3.0 W | Scales with HA-set brightness |
| Audio amp (3 W class-D) | 0.2 W | 3.0 W | TTS peaks only |
| Edge bar, 48 LEDs | 0.3 W | **14.4 W unlimited** → 3.0 W capped | Full white is 60 mA per LED. `slate-lightd` caps it to `max_current_ma` |
| Ethernet PHY, misc | 0.7 W | 1.0 W | |
| **Total at the 5 V rail** | ≈ 5.7 W | ≈ 17 W | |
| **Needed at the PD input (÷0.85)** | ≈ 6.7 W | **≈ 20 W** | Over budget for 802.3af |

**Requirement:** design for 802.3at. Wire the PD controller's Type-2 indication (TPS2372 `TPH/TPL`,
or equivalent) to a GPIO so firmware can drop `max_current_ma` to about 200 mA and cap the backlight
at 70 % when only 802.3af is available. The limiter is implemented (`power.py`). Reading the GPIO and
switching profiles is roadmap item R3.

---

## 4. Build system: Yocto vs Buildroot

| Criterion | Yocto (chosen) | Buildroot |
|---|---|---|
| Kiosk stack | `meta-webkit` (WPE, Cog) maintained by Igalia; Weston + kiosk-shell in OE-core | `wpewebkit`, `cog`, `weston` packages exist |
| Voice deps (Python with native extensions) | Recipes or pinned wheels; `python3-installer` in OE-core | No PEP 517 wheel story. Every native dep needs a hand-written package |
| A/B updates | `meta-rauc` (bundle class, keyring, verity) | RAUC package exists, but bundles are post-build scripts |
| Several carriers/SoCs from one tree | Layers, `BBFILES_DYNAMIC`, MACHINE overrides | One defconfig per board, with duplication |
| Compliance | `create-spdx` SBOM, `archiver`, license manifests | `make legal-info` (no SPDX SBOM) |
| CI cost | sstate cache makes incremental builds minutes | Full rebuild on most config changes |
| Learning curve | Steep | Gentle |
| LTS security maintenance | Scarthgap is LTS until April 2028 | Buildroot LTS is 1 year |

**Decision: Yocto.** The deciding factors are the SBOM requirement, the multi-board roadmap, and the
voice service's native Python dependencies. Buildroot is fine for a single-board prototype. §5.5 sketches
the equivalent `br2-external` tree in case that is ever needed.

**Release policy:** stay on Scarthgap 5.0 LTS and track its point releases (layer pins in
`kas/slate.yml`). Migrate to the next LTS (6.0) as a planned project. That release splits the
monolithic `poky` repository into `bitbake` + `openembedded-core` + `meta-yocto`. Under kas that only
changes the `repos:` block.

---

## 5. Layer structure

### 5.1 Layer stack

| Layer | Collection | Prio | Role |
|---|---|---|---|
| `poky/meta`, `poky/meta-poky` | core, yocto | 5 | Toolchain, systemd, Weston, PipeWire deps |
| `meta-openembedded/meta-oe` | openembedded-layer | 5 | mpv, libubootenv, misc |
| `meta-openembedded/meta-python` | meta-python | 5 | Python packages |
| `meta-openembedded/meta-multimedia` | multimedia-layer | 5 | PipeWire 1.0, WirePlumber 0.5, webrtc-audio-processing |
| `meta-openembedded/meta-networking` | networking-layer | 5 | Network tools for dev images |
| `meta-webkit` | webkit | 7 | WPE WebKit, WPEBackend-fdo, Cog |
| `meta-rauc` *(feature)* | rauc | 6 | RAUC, bundle class |
| `meta-raspberrypi` *(BSP)* | raspberrypi | 9 | CM4 kernel, firmware, U-Boot glue |
| **`meta-ha-slate`** | ha-slate | **20** | Product: distro, images, services, config |

### 5.2 `meta-ha-slate` tree

```
meta-ha-slate/
├── conf/
│   ├── layer.conf                 # deps, BBFILES_DYNAMIC, WKS search path
│   └── distro/slate.conf          # systemd, Wayland-only, ro-rootfs, SPDX, license policy
├── recipes-core/
│   ├── images/
│   │   ├── slate-image.bb         # production
│   │   └── slate-image-dev.bb     # + dropbear, debug tools, root login
│   ├── packagegroups/             # base / kiosk / voice
│   └── slate-base/                # /data layout, networkd, resolved mDNS, watchdog, journald
├── recipes-graphics/wayland/
│   ├── weston-init.bbappend       # kiosk weston.ini
│   └── weston_%.bbappend          # trim backends/shells
├── recipes-kiosk/slate-kiosk/     # Cog launcher + hardened unit
├── recipes-voice/
│   ├── linux-voice-assistant/     # LVA at a pinned commit + lva-wheels.inc (generated)
│   └── slate-audio/               # system PipeWire, pulse shim, AEC, WirePlumber policy
├── recipes-multimedia/mpv/        # build libmpv.so (python-mpv), audio-only, Pulse output
├── recipes-support/slate-lightd/  # edge bar daemon (source: apps/slate-lightd)
├── recipes-kernel/linux/          # config fragments + linux-yocto bbappend
├── wic/slate-rpi-ab.wks.in        # boot | rootfs_a | rootfs_b | data
└── dynamic-layers/
    ├── raspberrypi/               # SoC fragment, A/B boot.scr
    └── rauc/                      # system.conf, fw_env.config, hooks, mark-good gate, bundle
```

Conventions:

* **One concern per recipe.** Each service ships its own unit, config defaults and sandboxing, so
  a reviewer can audit a whole service in one directory.
* **Defaults in `/etc/slate/*.env` (read-only), overrides in `/data/slate/*.env`.** Units read both
  (`EnvironmentFile=-/data/...`). An OTA update can change defaults without wiping site settings.
  `/etc` is deliberately *not* an overlay: overlay upper layers silently shadow updated files.
* **Application source lives outside the layer** (`apps/`), so it can be unit-tested on a host. The
  recipe fetches it with `file://` and `FILESEXTRAPATHS`.
* **No `local.conf` editing.** All configuration lives in versioned kas files.

### 5.3 kas composition

```
kas/slate.yml               base: distro, target, pinned repos, cache dirs
kas/feature/rauc.yml        + meta-rauc, DISTRO_FEATURES rauc, ext4 slot image
kas/machine/qemuarm64.yml   = slate.yml (+ console LED preview)
kas/machine/cm4.yml         = slate.yml + rauc.yml + meta-raspberrypi + boot config
```

### 5.4 Distro policy (`slate.conf`) highlights

* `INIT_MANAGER = "systemd"`. `DISTRO_FEATURES` adds `wayland opengl pam seccomp usrmerge zeroconf`
  and removes `x11 … sysvinit`. No Xwayland anywhere.
* `IMAGE_FEATURES += read-only-rootfs`. `package-management` is removed (updates are images, never packages).
* `create-spdx`, `buildhistory` (committed), and `archiver` for copyleft sources.
* `LICENSE_FLAGS_ACCEPTED += commercial`: this is **needed for ffmpeg → mpv**, which the voice service
  uses for TTS and media playback. ⚠️ Patent-encumbered codecs: get a legal review, or restrict
  `ffmpeg` `PACKAGECONFIG` to the codecs HA actually sends (MP3/FLAC/Opus/WAV).

### 5.5 Buildroot equivalent (reference only)

```
br2-ha-slate/                       # BR2_EXTERNAL
├── external.desc / external.mk / Config.in
├── configs/ha_slate_cm4_defconfig  # BR2_PACKAGE_WESTON, _COG, _WPEWEBKIT, _PIPEWIRE, _RAUC …
├── board/ha-slate/
│   ├── linux.fragment              # same content as recipes-kernel/linux/files/*.cfg
│   ├── genimage.cfg                # same layout as slate-rpi-ab.wks.in
│   ├── post-build.sh               # weston.ini, fstab /data line, unit enablement
│   └── rootfs-overlay/             # /etc/slate, systemd units, pipewire.conf.d
└── package/
    ├── slate-lightd/               # python-package infra, setuptools
    └── linux-voice-assistant/      # would need python-aioesphomeapi, -zeroconf, -numpy 2, … each hand-packaged
```

---

## 6. Kernel configuration essentials

Fragments live in `meta-ha-slate/recipes-kernel/linux/files/` and are applied to `linux-yocto`
(QEMU) and `linux-raspberrypi` (CM4) by bbappends. The kernel-yocto config audit flags any option the
BSP's Kconfig doesn't accept. Treat those warnings as errors in CI.

| Fragment | Key options | Why |
|---|---|---|
| `slate-system.cfg` | `DEVTMPFS(_MOUNT)`, `CGROUPS`, `CGROUP_BPF`, `MEMCG`, `FHANDLE`, `SECCOMP_FILTER`, `AUTOFS_FS`, `TMPFS_POSIX_ACL` | Hard systemd requirements. `MEMCG` backs `MemoryMax=` on the kiosk; `CGROUP_BPF` backs per-unit firewalling |
| | `EXT4_FS`, `OVERLAY_FS`, `SQUASHFS(_XZ)`, `BLK_DEV_LOOP`, `BLK_DEV_DM`, `DM_VERITY` | Slots, `/data`, and RAUC verity bundles (squashfs + dm-verity, mounted through loop) |
| | `PREEMPT` | The touch UI, the WebKit compositor and 16 kHz capture share 4 cores. Full preemption keeps capture jitter low without PREEMPT_RT |
| | `WATCHDOG`, `WATCHDOG_CORE`, `WATCHDOG_HANDLE_BOOT_ENABLED` | systemd pets `/dev/watchdog`. A hung kernel reboots, which guarantees A/B fallback attempts |
| | `PACKET`, `IPV6`, `VLAN_8021Q` | DHCP, IPv6 RA, optional voice/IoT VLAN |
| `slate-display.cfg` | `DRM`, `DRM_PANEL_SIMPLE`, panel/bridge drivers (`ILI9881C`, `JD9365DA`, `TC358762`, `SN65DSI83`) | KMS panel pipeline. Keep only the panel you ship |
| | `VT=y` | **Required:** poky's `weston.service` has `ConditionPathExists=/dev/tty0` |
| | `BACKLIGHT_PWM`, `BACKLIGHT_GPIO`, `PWM` | `/sys/class/backlight/*` → HA "Display" light |
| | `INPUT_EVDEV`, `TOUCHSCREEN_GOODIX/EDT_FT5X06/ILITEK`, `HID_MULTITOUCH` | Capacitive touch (I2C or USB-HID) |
| | `IIO`, `VCNL4000`, `LTR501` | Proximity/ambient-light sensor for wake-on-approach and auto-brightness (roadmap) |
| `slate-audio.cfg` | `SND_SOC`, `SND_SIMPLE_CARD`, `SND_AUDIO_GRAPH_CARD`, `SND_SOC_DMIC`, `SND_SOC_MAX98357A`, codec, `SND_USB_AUDIO` | I2S/PDM mics and amp via simple-card DT, or a USB/XMOS array |
| `slate-leds.cfg` | `SPI`, `SPI_SPIDEV` | WS2812 bar driven over SPI MOSI (§9) |
| | `LEDS_CLASS(_MULTICOLOR)`, `LEDS_LP50XX`, `LEDS_IS31FL32XX`, `LEDS_PCA963X`, `LEDS_PWM`, `LEDS_TRIGGER_PATTERN` | I2C constant-current alternative. The kernel owns the current limits |
| `slate-hardening.cfg` | `DEVMEM`, `MAGIC_SYSRQ`, `KEXEC`, `BT` **off**; `STRICT_KERNEL_RWX`, `HARDENED_USERCOPY`, `FORTIFY_SOURCE`, `STACKPROTECTOR_STRONG` **on** | Network-facing appliance on a wall |
| `slate-rpi.cfg` (BSP) | `DRM_VC4`, `DRM_V3D`, `BCM2835_WDT`, `SPI_BCM2835`, `PWM_BCM2835`, `SND_BCM2835_SOC_I2S`, `BCMGENET` | SoC drivers for CM4 |

**Command line** (built by `boot.scr`): `root=/dev/mmcblk0p<slot> rootfstype=ext4 rootwait ro
rauc.slot=<A|B> quiet`. Add `spidev.bufsiz=8192` if the bar has more than ~330 LEDs (12 bytes/LED + 128
reset bytes must fit one transfer). Add `consoleblank=0` and drop `quiet` on dev images.

**Device tree must provide:** the DSI panel + backlight (`pwm-backlight`, `default-brightness-level`), the
touch controller with its interrupt/reset GPIOs, an I2S `simple-audio-card` (mics + amp), `spidev` on
the bar's SPI bus (`compatible = "rohm,dh2228fv"` or another listed spidev compatible, *not* `"spidev"`),
and the PD Type-2 GPIO as a `gpio-line-names` entry.

---

## 7. Graphics and kiosk browser

### 7.1 Compositor: Weston kiosk-shell

`weston.ini` (`recipes-graphics/wayland/weston-init/weston.ini`):

* `shell=kiosk-shell.so`: every top-level surface is forced fullscreen. There is no panel, launcher or
  decoration, and no key binding leads to a desktop. This is the main "boot directly into the browser"
  guarantee, and it holds even if the browser misbehaves.
* `backend=drm-backend.so`, `require-input=false` (Weston starts before touch probes), and
  `require-outputs=any` (DSI bridges often defer their probe).
* `idle-time=0`. Blanking is controlled by HA through the Display light entity (`slate-lightd`). If
  you prefer Weston's own DPMS-on-idle (with touch to wake), set `idle-time=300` and verify that the
  waking touch is not delivered to the dashboard as a click.
* Rotation goes in `[output] transform=` (compositor-side, zero cost), never in CSS.

Weston runs through poky's `weston-init`: a system unit (`User=weston`, `PAMName=weston-autologin`,
so logind gives it a seat) and a `weston.socket` at `/run/wayland-0` (group `wayland`, 0775). The
`weston_%.bbappend` drops the IVI/fullscreen shells and the RDP/VNC/X11/Xwayland backends.

### 7.2 Browser: Cog + WPE WebKit (Chromium as fallback)

| | Cog / WPE WebKit | Chromium (`meta-browser`, Ozone/Wayland) |
|---|---|---|
| RSS with an HA dashboard | Low (single WebProcess, no browser UI) | Roughly 2× |
| Build cost | Large | Very large (hours, tens of GB of work dir) |
| Kiosk fit | Purpose-built: no UI, fullscreen via kiosk-shell, `--webprocess-failure=restart` | Needs `--kiosk` plus many flags |
| HA compatibility | WebKit is a first-class HA engine (Safari and the iOS companion app's WKWebView) | Reference engine for most custom cards |

**Decision:** ship Cog/WPE. Keep a `kas/feature/chromium.yml` escape hatch (roadmap R5) in case a
required custom card breaks on WebKit.

`slate-kiosk.service`:

* `User=kiosk` (groups `wayland render video`). Uses `WAYLAND_DISPLAY=/run/wayland-0` (an absolute
  socket path) and its own `XDG_RUNTIME_DIR`.
* **Session persistence (F2):** `XDG_DATA_HOME=/data/kiosk/data`. WebKit stores cookies and
  localStorage under `$XDG_DATA_HOME/cog/`, and HA keeps its refresh token there. It survives
  OTA because `/data` is not part of a slot.
* `COG_PLATFORM_WL_VIEW_FULLSCREEN=1` (belt and braces with kiosk-shell).
* `--webprocess-failure=restart` recovers a crashed renderer in place, and `Restart=always` recovers
  Cog itself. `StartLimitBurst=10/300 s` stops a crash loop from hammering the GPU forever.
* Sandboxing: `ProtectSystem=strict`, `ReadWritePaths=/data/kiosk`, `NoNewPrivileges`,
  `MemoryHigh=600M`/`MemoryMax=800M`, and `OOMScoreAdjust=300`, so the browser is killed before voice.

### 7.3 Home Assistant side (no on-screen keyboard needed)

Typing a password on a wall panel is a poor experience, and WPE's on-screen keyboard support under Weston
is limited. Provision the panel instead:

```yaml
# configuration.yaml on the HA server
homeassistant:
  auth_providers:
    - type: trusted_networks
      trusted_networks: [192.168.20.0/24]   # panel VLAN, or the panel's /32
      trusted_users:
        192.168.20.0/24: <id of a non-admin "wall-panel" user>
      allow_bypass_login: true
    - type: homeassistant
```

Then set `KIOSK_URL=http://homeassistant.local:8123/dashboard-wall/0` in `/data/slate/kiosk.env`.
Hide the HA sidebar and header with the dashboard's kiosk options (for example the community
`kiosk-mode` card) and give the panel user a dedicated, non-admin account.

---

## 8. Voice service

### 8.1 Protocol choice

| | **Linux Voice Assistant (chosen)** | Wyoming satellite |
|---|---|---|
| Upstream status | Active (Open Home Foundation) | **Deprecated.** Its README points to LVA |
| Transport | ESPHome native API (TCP 6053), optional Noise encryption | Wyoming JSONL over TCP 10700 |
| HA integration | ESPHome integration: auto-discovered satellite, media player, timers, announce, start/continue conversation, stop word | Wyoming integration, fewer features |
| Wake word | On-device microWakeWord and openWakeWord (TFLite), selectable from HA | Separate wake service |
| Peripheral hooks | WebSocket API (6055): events, plus `register_light` to create HA entities | `--event-uri` |

The satellite only does wake-word detection, audio capture/playback and pipeline control. STT, intent
handling and TTS stay in HA (Assist pipeline), so the panel's CPU budget stays small and constant.

### 8.2 Audio pipeline

```
mics ─ALSA─► PipeWire ─► echo-cancel (webrtc AEC, AGC, NS) ─► aec_mic ─► LVA (16 kHz mono)
                              ▲ reference                                   │
speaker ◄─ALSA◄─ PipeWire ◄── aec_sink ◄────────── mpv (TTS, chimes, media) ◄┘
```

* **System-wide PipeWire**, not per-user. There is no desktop session to hang a user instance off,
  and a system instance starts before any login. OE's PipeWire ships `pipewire.service/.socket`
  (system). `slate-audio` adds the missing **system `pipewire-pulse` unit** (socket at
  `/run/pipewire-pulse/native`, group `pipewire`, `UMask=0007`) and enables WirePlumber's system unit.
* **AEC is mandatory** for a panel with a speaker. Otherwise the wake word is deaf during TTS, timers
  and music. `libpipewire-module-echo-cancel` with `aec/libspa-aec-webrtc` creates `aec_mic`/`aec_sink`.
  All playback **must** go to `aec_sink` (`VOICE_OUTPUT=pulse/aec_sink`) so the canceller sees the
  reference signal. If the hardware uses an XMOS array with on-chip AEC (ReSpeaker Lite, Satellite1),
  remove the module and point `VOICE_INPUT` at the array's processed channel.
* WirePlumber system-mode policy (`50-slate-system.conf`) disables logind seat tracking and device
  reservation, because there is no seat on an appliance.

### 8.3 Packaging strategy

LVA's dependency closure (23 wheels, generated) includes several native extensions: numpy 2,
aioesphomeapi, zeroconf, cryptography, webrtc-noise-gain, netifaces2 (Rust), and pymicro-wakeword /
pyopen-wakeword. The last two **bundle a prebuilt `libtensorflowlite_c.so`**. Their sdists even
contain an x86-64 build of it, so a naive from-source recipe produces a broken image.

**v1 (implemented): hash-pinned upstream wheels in a private prefix.**

* `scripts/gen-lva-wheels.py` resolves the closure for `cp312 / glibc 2.39 / aarch64` (Scarthgap's
  CPython and glibc) with pip's cross-platform resolver. It writes `lva-wheels.inc`, one `SRC_URI`
  entry per wheel with its PyPI content-addressed URL and sha256, and it checks each hash against
  PyPI.
* `linux-voice-assistant_git.bb` installs each wheel with `python3-installer-native` into
  `/opt/lva/lib/python3.12/site-packages`, plus LVA's app tree at a pinned commit. Nothing is compiled.
  It is isolated from system Python, which matters because LVA needs numpy ≥ 2 and Scarthgap ships numpy 1.26.
* Footprint: about **125 MB** installed (numpy + OpenBLAS and the TFLite runtimes dominate).
* Trade-off: these are opaque binaries in the SBOM (listed by name and hash, not by their
  internal components). Acceptable for v1 and tracked as R6.

**v2 (roadmap R6): native recipes.** Build TFLite's C library from source (`tensorflow-lite` CMake,
as in `meta-tensorflow-lite`/`meta-imx-ml`), then patch pymicro-wakeword/pyopen-wakeword to link it.
Write a `python_maturin` recipe for netifaces2 and use `python_setuptools_build_meta` for the rest.
The payoff is CVE tracking per component and a smaller image (shared system OpenSSL and BLAS).

### 8.4 Service

`slate-voice.service` → `/usr/libexec/slate-voice-launch` → `python3 -m linux_voice_assistant`:

| Setting | Value | Reason |
|---|---|---|
| `--name` | `$SLATE_DEVICE_NAME` | HA device name |
| `--preferences-file` | `/data/voice/preferences.json` | Wake word, volume and mute choices survive OTA |
| `--wake-word-root-dir` | `/data/voice/wakewords` | HA can push downloaded models. Internal models are refreshed from the image on every start |
| `--enable-encryption --encryption-key-file` | `/data/voice/encryption.key` | ESPHome API Noise encryption. Paste the key into HA when you adopt the device |
| `--peripheral-host` | `127.0.0.1` | **Security:** the peripheral API has no authentication, so it must never listen on the LAN |
| `--audio-input/output-device` | `aec_mic` / `pulse/aec_sink` | §8.2 |

Ordering: `After=slate-lightd.service`. LVA only publishes peripheral-registered entities if they arrive
within `--peripheral-startup-wait` (2 s), so lightd is up first and reconnects every 250 ms for the
first 30 s.

**Adopting into HA:** Settings → Devices → ESPHome is auto-discovered over zeroconf. Enter the key
from `/data/voice/encryption.key` (shown by `journalctl -u slate-voice` on dev images; on production,
surface it on a provisioning page, roadmap R2). Then assign an Assist pipeline.

---

## 9. Edge lighting bar

### 9.1 Requirements

Linear bar, about 48 RGB pixels. It must show voice states within one frame (20 ms), stay inside the
power budget (§3.3), be controllable from HA as a light entity, reflect system states (boot, OTA,
HA unreachable), and use zero CPU when nothing is changing.

### 9.2 Hardware options

| Option | Interface | Pros | Cons | Use when |
|---|---|---|---|---|
| **A. WS2812B/SK6812 on SPI MOSI** (reference) | SPI DMA generates the 800 kHz waveform (4 SPI bits per data bit @ 3.2 MHz) | Cheapest. Per-pixel colour. No extra MCU. Timing from DMA, not the scheduler | Needs a 3.3→5 V level shifter. MOSI must idle low. Dark until Linux starts. ~1 mA/LED quiescent | Default |
| **B. I2C constant-current driver** (TI LP50xx, ISSI IS31FL32xx) | Kernel LED multicolor class (`leds-lp50xx`) | Current limits in hardware. Mainline drivers. Can be lit early (DT `default-state`) | Fewer, larger segments (LP5024 = 8 RGB). More BOM | Diffused bar with ≤ 12 segments |
| **C. Offload MCU** (RP2040 / STM32G0 over UART or I2C) | MCU renders animations, Linux sends states | Animates from power-on (before U-Boot). Immune to Linux load. Handles the PoE-class GPIO | Second firmware to maintain and update | A "light at power-on" requirement, or > 200 LEDs |

**Why not bit-bang or PWM?** A single-wire 800 kHz protocol needs about 150 ns accuracy, and Linux
userspace jitter is in microseconds. The SPI trick hands timing to DMA. The kernel has no mainline
WS2812 driver usable on every SoC, so a userspace `spidev` writer is the portable choice.

**Option A electrical checklist:** 74AHCT1G125 level shifter at the SoC end. A 330 Ω series resistor at the
first LED. 1000 µF bulk capacitance at the bar's 5 V feed plus 100 nF per LED (often integrated).
Feed 5 V from both ends if the bar is longer than about 30 LEDs. A 100 kΩ pull-down on DIN keeps the
bar dark while the SoC is in reset. Verify on a scope that MOSI idles **low** between transfers.

**Wire timing** (`ws2812_spi.py`, unit-tested): `0 → 1000`, `1 → 1110` at 3.2 MHz, giving T0H 312 ns, T0L
938 ns, T1H 938 ns and T1L 312 ns. This meets both the original WS2812B and the WS2812B-V5/SK6812
tables. A 128-byte zero tail (320 µs) latches the frame. Confirm on a logic analyzer that the SPI
controller really runs at 3.2 MHz: some clock trees round it, and `speed_hz` is configurable.

### 9.3 Software: `slate-lightd`

Source: `apps/slate-lightd/` (Python 3.12 asyncio, stdlib plus `websockets`, 50 unit tests).

```
 LVA events (ws 6055) ─┐                ┌──────────── Engine ─────────────┐
 HA light_command  ────┼─► VoiceBridge ─┤ claims{source → effect, colour, │──► gamma LUT ─► power
 control socket   ─────┘  (lva.py)      │        priority, ttl}           │    (2.2)        limiter
 (RAUC hook, CLI)                       │ render(top claim) @ 50 fps      │                    │
                                        └─────────────────────────────────┘          ws2812-spi │ sysfs │ console
```

**Priority claims instead of a state machine.** Each producer holds at most one claim and the
highest-priority live claim is rendered. Releasing a claim reveals the next one down. This is what makes "error flash
over timer countdown over the user's ambient colour" work without special cases.

| Priority | Source | Trigger (LVA event) | Effect |
|---|---|---|---|
| 90 | `system` | boot (2.5 s), RAUC pre/post-install | scan white / solid green |
| 70 | `error` | `pipeline_error` | 3 red flashes (1.3 s TTL) |
| 60 | `network` | `disconnected`, `snapshot.ha_connected=false` → cleared by `zeroconf: connected` | slow red breathe |
| 55 | `network:lva` | LVA unreachable > 15 s | amber breathe |
| 50 | `alarm` | `timer_ringing` → cleared by `idle` | amber pulse |
| 40 | `voice` | `wake_word_detected` / `listening` / `thinking` / `tts_speaking` → `tts_finished`/`idle` | grow from centre / solid / scanning comet / pulse |
| 35 | `media` | `volume_changed` | volume level bar (1.5 s) |
| 30 | `timer` | `timer_ticking`/`timer_updated` (tracked locally, soonest timer) | draining progress bar |
| 20 | `muted` | `muted` | two end LEDs dim red |
| 10 | `user` | HA `light_command` for **Edge Light** (persisted to `/data/lightd/state.json`) | solid / Breathe / Scan / Pulse |

* **HA integration with no extra infrastructure:** on connect, lightd sends `register_light` for
  `edge_light` (RGB, brightness, effects) and `display` (brightness only, backed by
  `/sys/class/backlight`). LVA turns these into ESPHome light entities on the satellite's HA device.
  No MQTT broker and no custom integration.
* **Power limiter:** estimates current from post-gamma PWM duty (`Σ duty × 20 mA + 1 mA/LED`) and
  scales the frame to stay under `max_current_ma`. Full white on 48 LEDs drops from 2.9 A to 0.6 A.
* **Idle cost:** static frames are not re-sent. The render loop sleeps until a claim changes
  (1 s safety tick), so CPU use is effectively zero at idle.
* **Backends:** `ws2812-spi` (spidev ioctl mode 0 / 8 bit / speed, single `write()` per frame),
  `sysfs` (LED class with `multi_intensity`, one `pwrite` per changed segment), and `console`
  (truecolor preview for QEMU and dev hosts).
* **Control socket** `/run/slate-lightd/control.sock` (0660 root) takes JSON lines. CLI:
  `slate-lightctl claim system progress --value 0.4`, `slate-lightctl release system`, `slate-lightctl status`.

---

## 10. Storage, persistence and updates

### 10.1 Partition layout (`wic/slate-rpi-ab.wks.in`, MBR for the RPi ROM)

| # | Label | FS | Size | Mount | Contents |
|---|---|---|---|---|---|
| 1 | `boot` | vfat | 128 MB | `/boot` | RPi firmware, `config.txt`, DT overlays, U-Boot, `boot.scr`, `uboot.env` |
| 2 | `rootfs_a` | ext4 | 1.5 GB | `/` (ro) | Slot A, including `/boot/Image` |
| 3 | `rootfs_b` | ext4 | 1.5 GB | — | Slot B |
| 4 | `slate-data` | ext4 | 512 MB → grows | `/data` | Browser profile, voice prefs and keys, lightd state, site `.env`, RAUC status |

`/data` is mounted by label with `x-systemd.growfs` and `nofail`. If it is missing (QEMU, or a
corrupted partition), `slate-data-fallback.service` mounts a tmpfs so the product still boots, without
persistence. Directory ownership comes from `tmpfiles.d/slate.conf`. **Factory reset** = `mkfs.ext4 -L
slate-data` on p4 and reboot (roadmap R2: long-press gesture).

The kernel lives inside each rootfs slot, so it updates atomically with userspace. The DTB and
overlays stay on `boot` (the RPi firmware merges them before U-Boot runs). That is acceptable because
they change rarely, and a future RAUC `boot` slot of type `vfat` can cover them.

### 10.2 A/B with RAUC + U-Boot

1. `rauc install bundle.raucb`. The bundle is `verity` format, signed by the release CA, and checked
   against `/etc/rauc/slate-ca.cert.pem`. RAUC writes the inactive slot and sets `BOOT_ORDER` to try
   it first with `BOOT_x_LEFT=3`.
2. Reboot. `boot.scr` decrements `BOOT_x_LEFT` and boots that slot.
3. `rauc-mark-good.service` is gated by `10-slate-mark-good.conf`: it waits until `slate-kiosk`,
   `slate-voice` and `slate-lightd` have been active for 60 s, and only then marks the slot good.
4. If the new slot crashes, hangs (the hardware watchdog forces a reboot) or never becomes healthy,
   its counter reaches 0 and U-Boot falls back to the old slot (F7).

The edge bar shows "updating" (white scan) and "installed" (green) through the RAUC handlers.

### 10.3 Signing and PKI

* Development: `scripts/gen-dev-keys.sh` writes a throw-away CA to `keys/dev/` (gitignored). Builds
  find it through `SLATE_KEYS_DIR`.
* Production: an offline root CA signs an online **release signing certificate**, and the private key
  lives in an HSM, used through RAUC's PKCS#11 support. CI sets `SLATE_KEYS_DIR` to the release CA
  cert and `RAUC_KEY_FILE` to a `pkcs11:` URI. Devices trust the root, so the signing key can rotate
  without a field update.
* Roadmap R4: secure boot. On CM4, that means a signed `boot.img` via the bootloader EEPROM and U-Boot
  FIT verified boot. dm-verity on the rootfs slots would then extend the chain of trust into userspace.

---

## 11. Boot-time budget (target ≤ 15 s to dashboard)

| Stage | Budget | Techniques |
|---|---|---|
| ROM + RPi firmware | 2.5 s | `disable_splash=1`, `boot_delay=0`, no HDMI probe (`disable_fw_kms_setup=1` once the panel works) |
| U-Boot + boot.scr | 1.0 s | `bootdelay=0`, no network/USB in U-Boot |
| Kernel to `/sbin/init` | 2.0 s | `quiet`, panel/touch/audio as modules loaded in parallel by udev, no initramfs |
| systemd to `weston` ready | 2.5 s | Read-only root (no fsck of /), volatile journal, mask unused units |
| Cog + WebKit + first paint | 4–6 s | Data on eMMC, HTTP cache in `/data/kiosk/cache`, avoid heavy custom cards on the first view |

Measure with `systemd-analyze critical-chain slate-kiosk.service` and a photodiode on the panel for
first light. Track it in CI on a hardware runner (R7).

---

## 12. Security

| Surface | Control |
|---|---|
| ESPHome API (TCP 6053) | Noise encryption on by default (`VOICE_ENCRYPTION=1`), per-device key on `/data` |
| LVA peripheral API (6055) | Bound to `127.0.0.1` only (it has no authentication) |
| mDNS | LVA's zeroconf responder. `systemd-resolved` in `resolve`-only mode so the two don't fight over 5353 |
| SSH | Absent from `slate-image`. Dropbear only in `slate-image-dev` |
| Browser | Unprivileged `kiosk` user, `ProtectSystem=strict`, memory-capped, writes only to `/data/kiosk`. Non-admin HA user (§7.3) |
| Voice | Unprivileged `voice` user, writes only to `/data/voice`, `MemoryMax=400M` |
| Root FS | Read-only. Updates only via signed RAUC bundles |
| Kernel | Hardening fragment (§6). No `/dev/mem`, no SysRq, no kexec |
| Supply chain | All layers commit-pinned. Wheels sha256-pinned. SPDX SBOM + buildhistory per release |

---

## 13. Build guide

### 13.1 Host requirements

Ubuntu 22.04/24.04 or Debian 12. 8+ cores (16+ recommended), 32 GB RAM, 300 GB free SSD. The first
full build is several hours (WPE WebKit dominates). Rebuilds from sstate take minutes.

```sh
sudo apt install gawk wget git diffstat unzip texinfo gcc build-essential chrpath socat cpio \
    python3 python3-pip python3-pexpect xz-utils debianutils iputils-ping python3-git \
    python3-jinja2 python3-subunit zstd liblz4-tool file locales libacl1 bmap-tools
sudo locale-gen en_US.UTF-8
pip install --user kas==5.5          # or use kas-container (no host deps)
```

### 13.2 Build

```sh
git clone <this repo> ha-slate && cd ha-slate
scripts/gen-dev-keys.sh                       # dev RAUC CA → keys/dev (gitignored)

# QEMU (no hardware, fastest path to see the kiosk + voice + LED preview)
kas build kas/machine/qemuarm64.yml
kas shell kas/machine/qemuarm64.yml -c "runqemu slirp gl sdl"

# Reference hardware (CM4)
kas build kas/machine/cm4.yml                 # → slate-image-raspberrypi4-64.rootfs.wic.bz2
kas build kas/machine/cm4.yml --target slate-image-dev
kas build kas/machine/cm4.yml --target slate-bundle      # → .raucb update bundle
```

Artifacts: `build/tmp/deploy/images/<machine>/`. The SBOM is in `build/tmp/deploy/spdx/`.

### 13.3 Flash a CM4 (eMMC)

```sh
# Carrier: set nRPIBOOT jumper, connect USB-C slave port
sudo rpiboot                                  # from raspberrypi/usbboot; eMMC appears as /dev/sdX
sudo bmaptool copy build/tmp/deploy/images/raspberrypi4-64/slate-image-raspberrypi4-64.rootfs.wic.bz2 /dev/sdX
# remove jumper, power over PoE
```

### 13.4 First boot and provisioning

1. The bar shows a blue scan (lightd up), then the dashboard appears.
2. Site settings (optional): put `kiosk.env`, `slate.env` or `voice.env` overrides into `/data/slate/`.
   A static IP goes in `/data/slate/network/10-static.network`. On dev images, use SSH; on production,
   use the provisioning flow (R2).
3. In HA: adopt the discovered ESPHome device (§8.4), choose a wake word and pipeline, and expose
   **Edge Light** and **Display** to your automations (for example dim at night, or colour the bar on alarm).

### 13.5 OTA update

```sh
scp slate-bundle-raspberrypi4-64.raucb root@panel:/data/   # dev image
ssh root@panel rauc install /data/slate-bundle-raspberrypi4-64.raucb && reboot
rauc status                                                 # after reboot: booted slot, "good"
```

Production: pull from your update server (hawkBit through `rauc-hawkbit-updater` in `meta-rauc`, roadmap R1).

### 13.6 Upgrading the voice service

```sh
# 1. bump SRCREV_lva in linux-voice-assistant_git.bb
# 2. regenerate the pinned wheel set from that commit's pyproject.toml:
git -C ../linux-voice-assistant checkout <commit>
scripts/gen-lva-wheels.py --lva-src ../linux-voice-assistant --python 3.12 --glibc 2.39
# 3. update LIC_FILES_CHKSUM if LICENSE.md changed; build; run the voice acceptance tests
```

### 13.7 Developing lightd on a host

```sh
cd apps/slate-lightd && python3 -m venv .venv && .venv/bin/pip install websockets pytest
.venv/bin/pytest -q
.venv/bin/python -m slate_lightd --backend console -c lightd.toml.example   # bar preview in your terminal
```

---

## 14. Verification status and bring-up checklist

**Already checked in this repository:**

* `slate-lightd`: 50 unit tests cover the WS2812 bit encoding and reset length, the power limiter, every
  effect at several LED counts, claim priority and expiry, LVA event mapping (pipeline, mute,
  disconnect, timers), the sysfs multicolor backend against a fake LED class tree, the control socket,
  and an end-to-end WebSocket session against a fake LVA.
* LVA dependency closure: resolved for cp312/aarch64/glibc 2.39, every wheel hash checked against PyPI,
  and the recipe's install step rehearsed on a host (all 23 wheels install into the `/opt/lva` layout).
* Upstream facts the design depends on, checked against source at the pinned commits: `weston-init`
  unit and socket, Cog 0.18 options and storage paths, PipeWire 1.0.9 system units (no pulse unit, hence
  ours), LVA arguments and peripheral API, and Wyoming satellite's deprecation.
* BitBake against the pinned Scarthgap layers:
  * `qemuarm64`: all 2835 recipes parse with 0 errors, and `bitbake -n slate-image slate-image-dev`
    resolves the full task graph (7,944 tasks), so every `RDEPENDS` has a provider.
  * `cm4` (+ meta-raspberrypi + meta-rauc): 2899 recipes parse, and `bitbake -n slate-image slate-bundle`
    resolves (7,952 tasks).
  * `bitbake -c unpack` of every product recipe, including the BitBake fetcher downloading and
    sha256-checking all 23 wheels plus the pinned LVA commit.
  * The dry run caught one real bug, now fixed: meta-oe's mpv does not build `libmpv`, which
    python-mpv needs (`recipes-multimedia/mpv/mpv_%.bbappend`).
* Shell scripts pass `shellcheck`. Unit files pass `systemd-analyze verify` with no unknown keys.

**Not yet run.** No full compile has been done (it needs a build host, §13.1), so compile-time and
`do_package_qa` issues are still possible. The first suspects are the `INSANE_SKIP` list on the
wheel-based voice recipe and the mpv `--enable-libmpv-shared`/`--enable-pulse` options.

**Bring-up checklist (in order):**

- [ ] Full build for `qemuarm64`. Boot with `runqemu`, confirm Weston + Cog render HA, the lightd console
  preview, and the `/data` tmpfs fallback
- [ ] CM4 build + flash. Confirm the A/B `boot.scr` picks slot A, `rauc status` is sane, and `fw_printenv` works
- [ ] Panel: DSI overlay, backlight under `/sys/class/backlight`, touch mapped to the output, rotation
- [ ] Weston 13 accepts `require-outputs=any` with a late DSI bridge
- [ ] WirePlumber system mode: the ALSA nodes appear with `support.logind = disabled` (`pw-cli ls Node`)
- [ ] Echo cancel: bind `capture.props`/`playback.props` to the real nodes. Wake word triggers during TTS at 70 % volume
- [ ] LVA: discovered by HA, encryption key adoption, wake word → pipeline → TTS through `aec_sink`
- [ ] Edge bar: scope MOSI idle level and the actual SPI clock. Colour order. 48 LEDs at full white stay ≤ 600 mA measured
- [ ] HA entities: Edge Light and Display appear on the device page (startup-wait ordering)
- [ ] OTA: install bundle → reboot → mark-good after 60 s. Then a deliberately broken bundle → automatic fallback
- [ ] Power: measure peak at the PD input on 802.3at and 802.3af switches
- [ ] Boot time to first dashboard paint ≤ 15 s

---

## 15. Roadmap

| ID | Item |
|---|---|
| R1 | Update server: hawkBit + `rauc-hawkbit-updater`, staged rollouts |
| R2 | On-device provisioning page (served on first boot): URL, name, static IP, show the API key. Factory-reset gesture |
| R3 | PoE class detection GPIO → automatic power profile (LED current, backlight cap) |
| R4 | Secure boot (signed boot.img / FIT) + dm-verity rootfs |
| R5 | `kas/feature/chromium.yml` fallback browser |
| R6 | Native recipes for the voice dependencies, with TFLite built from source |
| R7 | Hardware-in-the-loop CI: flash, boot-time measurement, wake-word replay test |
| R8 | Proximity sensor: wake the screen on approach, auto-brightness from ambient light |
