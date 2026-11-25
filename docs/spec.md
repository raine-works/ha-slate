# HA Slate — Firmware Technical Specification & Build Guide

| | |
|---|---|
| **Product** | HA Slate: PoE-powered wall touch panel for Home Assistant |
| **Scope** | Linux firmware image (boot, kiosk UI, voice agent, edge lighting, updates) and the `ha_slate` Home Assistant integration |
| **Build system** | Yocto Project 5.0 "Scarthgap" (LTS, maintained to April 2028), driven by kas |
| **Status** | Draft v0.2: custom HA integration + on-device agent replace the ESPHome-emulation voice design. Not yet run on hardware |

---

## 0. Assumptions and open decisions

The request did not name a SoC or carrier board, so this spec makes the following assumptions. Every
hardware-specific choice is isolated in a BSP glue layer or kas file so you can swap it out.

| # | Assumption | Where it lives | If it's wrong |
|---|---|---|---|
| A1 | SoM: **Raspberry Pi CM4** (BCM2711, 4× A72, eMMC) or **CM5** (BCM2712, 4× A76, eMMC, §3.4) on a custom PoE carrier | `kas/machine/cm4.yml`, `kas/machine/cm5.yml`, `meta-ha-slate/dynamic-layers/raspberrypi/` | Add a new `kas/machine/<board>.yml` and a `dynamic-layers/<bsp>/` dir (§3.2) |
| A2 | Display: MIPI-DSI panel with capacitive touch. CM4: 7" Touch Display 2 class (2-lane). CM5: **Touch Display 2, 10.1"** (1200×1920, 4-lane, 10-point touch) | DT overlay in `RPI_EXTRA_CONFIG`; kernel `slate-display.cfg` / `slate-bcm2712.cfg` | Change the panel/bridge drivers in the fragment |
| A3 | Audio: I2S MEMS mics, or a USB/XMOS mic array, plus an I2S class-D amp (MAX98357A class) | `slate-audio.cfg`, `slate-audio` recipe | Only the PipeWire node names change |
| A4 | Edge bar: **48 × WS2812B/SK6812** on SPI0 MOSI at 5 V; an I2C LP50xx variant is also supported | `lightd.toml`, `slate-leds.cfg` | Pick the `sysfs` backend (§9.2) |
| A5 | Power: IEEE 802.3at (Type 2, 25.5 W) PD; must degrade gracefully on 802.3af (12.95 W) | `[power]` in `lightd.toml` | See the power budget (§3.3) |
| A6 | Home Assistant ≥ 2026.10 is on the same L2 network (mDNS works) or reachable by IP, and the `ha_slate` integration is installed (HACS or `custom_components/`) | `custom_components/ha_slate`, `kiosk.env` | Add the panel by IP in the integration's config flow |

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
| F4 | Shows up in HA automatically (zeroconf) through the `ha_slate` integration as **one device** with a first-class Assist satellite (voice settings, announcements, start/continue conversation, timers) plus every panel control as a normal entity. |
| F4a | **Custom wake words:** any microWakeWord or openWakeWord model placed in HA's `custom_wake_words` folder is pushed to the panel, appears in HA's wake word picker, and can be activated (up to 2 at once) without a firmware change. HA-side wake word engines are an alternative mode. |
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
| N5 | Nothing listens on the network except the agent's TLS WebSocket (TCP 6054, certificate pinned by HA, token-authenticated) and mDNS. No SSH in production |
| N6 | The kiosk browser is memory-capped so a leaking dashboard cannot starve the voice pipeline |
| N7 | Builds are reproducible from pinned layer commits and hash-pinned downloads, with an SPDX SBOM for every release |

---

## 2. System architecture

```
                         ┌──────────────────────── Home Assistant ────────────────────────┐
                         │  Dashboard (HTTP/WS 8123)   ha_slate integration (§8)          │
                         └──────────────▲──────────────────────────────▲──────────────────┘
                                        │ HTTPS/WSS                     │ wss :6054, pinned TLS
════════════════════════════════════════╪══════════ PoE Ethernet ══════╪═══════════════════
 HA Slate (Linux, systemd, read-only /) │                               │
                                        │                               │
  ┌──────────────┐   wl socket   ┌──────┴───────┐               ┌───────┴────────────┐
  │ weston       │◄─────────────►│ slate-kiosk  │◄─kiosk.env────│ slate-agent        │
  │ kiosk-shell  │ /run/wayland-0│ Cog + WPE    │  (path unit)  │ wake words, Assist │
  │ DRM/KMS      │               │ WebKit       │               │ turns, controls    │
  └──────┬───────┘               └──────────────┘               └──┬──────────┬──────┘
         │ KMS / evdev                                      pw-cat │          │ /run/slate-lightd/control.sock
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
| `slate-agent.service` | `slate` | HA session (TLS), wake words, voice turns, settings | `always`, 2 s |
| `slate-kiosk-reload.path`, `slate-reboot.path` | root | Privileged actions requested by the agent via trigger files | — |
| `slate-lightd.service` | root (sandboxed, no network) | Edge bar + backlight. Takes voice events and light commands from the agent | `always`, 1 s |
| `rauc.service`, `rauc-mark-good.service` | root | Update install, slot health commit | — |

### 2.2 Boot sequence

```
ROM → RPi firmware (config.txt, DT overlays) → U-Boot → boot.scr picks slot A/B (§10.2)
  → kernel (from the rootfs slot's /boot) → systemd
     ├─ local-fs: / (ro), /boot (vfat), /data (ext4, growfs) — or tmpfs fallback
     ├─ slate-lightd   (sysinit; boot "scan" animation in < 1 s from userspace start)
     ├─ systemd-networkd + resolved (DHCP, mDNS resolve)
     ├─ weston  → slate-kiosk (graphical.target)
     ├─ pipewire → wireplumber (slate-audio.target)
     └─ slate-agent (after lightd; advertises _ha-slate._tcp, waits for HA)
          └─ rauc-mark-good (only after kiosk + agent + lightd are healthy for 60 s)
```

---

## 3. Hardware platform

### 3.1 SoC selection criteria

The kiosk browser is the heaviest workload. It needs a GPU with a **mainline Mesa driver** so that
Weston and WebKit get GL ES 3.x without vendor blobs. Voice needs about 1 A-class core of headroom.

| Candidate | GPU (Mesa) | Yocto BSP | Notes |
|---|---|---|---|
| **RPi CM4** (BCM2711) | V3D 4.2 (`v3d`/`vc4`) | `meta-raspberrypi` (very active) | Reference. Mature DSI stack, rpiboot eMMC flashing, huge ecosystem |
| **RPi CM5** (BCM2712) | V3D 7.1 | `meta-raspberrypi` (`raspberrypi-cm5-io-board`) | **Supported** (`kas/machine/cm5.yml`, §3.4). About 2–3× CPU and 4-lane DSI for the 10.1" panel. Higher power draw |
| Rockchip RK3566/RK3568 | Mali-G52 (`panfrost`) | `meta-rockchip` (mainline kernel) | Cheapest. Mainline DSI/audio quality varies by board |
| NXP i.MX 8M Mini/Plus | GC7000 (`etnaviv`) | `meta-freescale` | Industrial temperature range, long-term supply. 8M Plus adds an NPU |

**Avoid** SoCs that rely only on a vendor GPU blob with no Mesa driver: you would be tied to one
kernel and one compositor version for good.

### 3.2 BSP abstraction

The product layer never refers to a SoC directly. Board specifics live in three places:

1. `kas/machine/<board>.yml`: `MACHINE`, BSP layer pin, boot config, `WKS_FILE`, `IMAGE_FSTYPES`.
2. `meta-ha-slate/dynamic-layers/<bsp-collection>/`: bbappends that are only parsed when that BSP
   layer is present (`BBFILES_DYNAMIC`). Examples are the SoC kernel fragment and the boot script.
3. Runtime facts: `lightd.toml` (LED topology, power), the display connector and rotation
   (`SLATE_DISPLAY_OUTPUT` / `SLATE_DISPLAY_TRANSFORM`, which append a `weston.ini` `[output]`), the
   RAUC compatible string (`SLATE_RAUC_COMPATIBLE`, so a bundle for one board is refused by another),
   and the PipeWire AEC node binding.

SoC-specific kernel options are split per BSP machine override: `slate-bcm2711.cfg` for
`raspberrypi4-64` (CM4) and `slate-bcm2712.cfg` for `raspberrypi5` (CM5).

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

### 3.4 Compute Module 5 + Touch Display 2 (10.1")

Build with `kas build kas/machine/cm5.yml`. The product software (agent, integration, lightd, kiosk)
is identical across boards; only the following differ.

| Topic | CM5 specifics | Verified how |
|---|---|---|
| Panel | Touch Display 2 10.1": 1200×1920 portrait, **4-lane DSI** (not usable on CM4/Pi 4), 10-point capacitive touch | Raspberry Pi product brief and docs |
| Kernel | **linux-raspberrypi 6.12** (`PREFERRED_VERSION_linux-raspberrypi = "6.12.%"`). The BSP default 6.6 lacks the panel driver | The 6.12.93 commit pinned by meta-raspberrypi contains `panel-ilitek-ili79600a.c` and the overlay; rpi-6.6.y has neither |
| Drivers | `DRM_PANEL_ILITEK_IL79600A` (sic), `TOUCHSCREEN_ILITEK_V3`, `REGULATOR_RASPBERRYPI_TOUCHSCREEN_V2`, plus RP1 (`MFD_RP1`, `MACB`, `SPI_DW_MMIO`, `PWM_RP1`) in `slate-bcm2712.cfg` | All present and enabled in the pinned `bcm2712_defconfig` |
| Boot config | `dtoverlay=vc4-kms-dsi-ili79600-10-1inch` (port DSI1; append `,dsi0` for port 0). Compute Modules don't auto-detect the panel | Overlay README in the pinned kernel |
| Rotation | In the compositor only (`SLATE_DISPLAY_TRANSFORM`, default `rotate-90` for landscape). Weston maps touch through the same transform. Do **not** also use the overlay's `rotation=`, or touch is rotated twice | Weston behaviour |
| Multi-touch | Kernel MT → libinput → Weston → Cog (tracks 10 touch points) → WebKit touch events → dashboard | Cog 0.18 Wayland backend source |
| U-Boot | **v2026.10** recipe in `dynamic-layers/raspberrypi/recipes-bsp/u-boot/` (`DEFAULT_PREFERENCE = -1`, selected only by `cm5.yml`). poky's 2024.01 has no BCM2712 support (added in v2024.04). meta-raspberrypi's `fdt_addr` patch is already upstream in v2026.10 and is removed from `SRC_URI`. Fetched from branch **`main`**: U-Boot froze `master` at v2026.07 | Searched U-Boot sources at both tags. The patch reverse-applies on v2026.10. BitBake fetch + unpack of v2026.10 succeeds |
| Page size | The Pi 5 kernel uses **16K pages**. All 166 loadable segments in slate-agent's prebuilt libraries (TFLite, numpy/OpenBLAS, feature extractor) are 64K-aligned, so they load | `readelf` on every `.so` in the wheel set |
| Edge bar SPI | SPI is on RP1 (DesignWare). 3.2 MHz may round to about 3.1 MHz, which is still inside WS2812B timing. Confirm on a scope | Bring-up checklist |
| Power | CM5 under load plus a 10" backlight puts the peak close to the 802.3at limit (§3.3). Measure early. If needed, cap the bar and backlight harder, or design the carrier for 802.3bt | Estimate, needs measurement |
| RAUC | `compatible=ha-slate-cm5`. CM4 and CM5 bundles are mutually exclusive | `bitbake -e` |

**Production boot option:** the Raspberry Pi bootloader has native A/B "tryboot" (`autoboot.txt`). On CM5
it can replace U-Boot entirely: faster boot, one less component to maintain. It needs two boot partitions
and a small RAUC `custom` bootloader backend script (roadmap R14).

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
| `meta-openembedded/meta-oe` | openembedded-layer | 5 | libubootenv, misc |
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
│   ├── slate-agent/               # agent (source: apps/slate-agent) + slate-agent-wheels.inc
│   │                              # + kiosk-reload / reboot path units
│   └── slate-audio/               # system PipeWire, AEC, WirePlumber policy
├── recipes-support/slate-lightd/  # edge bar daemon (source: apps/slate-lightd)
├── recipes-kernel/linux/          # config fragments + linux-yocto bbappend
├── wic/slate-rpi-ab.wks.in        # boot | rootfs_a | rootfs_b | data
└── dynamic-layers/
    ├── raspberrypi/               # SoC fragments (bcm2711/bcm2712), A/B boot.scr,
    │                              # U-Boot 2026.10 for CM5
    └── rauc/                      # system.conf, fw_env.config, hooks, mark-good gate, bundle
```

Outside the layer, the repository also holds `apps/slate-agent` and `apps/slate-lightd` (application
sources, tested on a host), `custom_components/ha_slate` (the HA integration, HACS-installable via the
root `hacs.json`), and `tests/ha_slate` (integration tests against a real agent).

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
kas/machine/cm5.yml         = same, CM5 machine, kernel 6.12, U-Boot 2026.10, 10.1" panel
```

### 5.4 Distro policy (`slate.conf`) highlights

* `INIT_MANAGER = "systemd"`. `DISTRO_FEATURES` adds `wayland opengl pam seccomp usrmerge zeroconf`
  and removes `x11 … sysvinit`. No Xwayland anywhere.
* `IMAGE_FEATURES += read-only-rootfs`. `package-management` is removed (updates are images, never packages).
* `create-spdx`, `buildhistory` (committed), and `archiver` for copyleft sources.
* **No ffmpeg or mpv on the panel**, so no `commercial` license flag. Home Assistant decodes TTS and
  announcement media (it already ships ffmpeg) and streams raw PCM to the panel.

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
| `slate-bcm2711.cfg` (CM4) | `DRM_VC4`, `DRM_V3D`, `BCM2835_WDT`, `SPI_BCM2835`, `PWM_BCM2835`, `SND_BCM2835_SOC_I2S`, `BCMGENET` | SoC drivers for CM4 |
| `slate-bcm2712.cfg` (CM5) | `DRM_PANEL_ILITEK_IL79600A`, `TOUCHSCREEN_ILITEK_V3`, `REGULATOR_RASPBERRYPI_TOUCHSCREEN_V2`, `MFD_RP1`, `MACB`, `SPI_DW_MMIO`, `PWM_RP1`, `SND_DESIGNWARE_I2S`, `ARM64_16K_PAGES` | SoC and 10.1" panel drivers for CM5 (§3.4) |

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
* Rotation goes in `[output] transform=` (compositor-side, zero cost), never in CSS. It is set per board
  through `SLATE_DISPLAY_OUTPUT` / `SLATE_DISPLAY_TRANSFORM`.

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

## 8. Home Assistant integration and voice

### 8.1 Decision: custom integration + on-device agent (not MQTT, not ESPHome emulation)

| Option | Voice as a first-class Assist satellite? | Custom wake words | Panel controls | Infrastructure | Verdict |
|---|---|---|---|---|---|
| **MQTT discovery** | **No.** HA's MQTT integration has no `assist_satellite` platform and no audio transport | No path into HA's wake word UI | Yes (light, select, switch, number, text, button) | A broker that must be up before the panel works | Rejected for voice. Possible later as an *extra* bridge for non-HA consumers |
| **Emulate an ESPHome device** (Linux Voice Assistant, the previous revision of this spec) | Yes, through the built-in ESPHome integration | Only what the ESPHome API exposes (micro models via URL) | Only through LVA's peripheral side channel (lights and buttons only) | None | Rejected: protocol owned upstream (LVA is "alpha"), panel features shoehorned, 23 native wheels |
| **Custom integration `ha_slate` + `slate-agent`** (chosen) | **Yes.** A real `AssistSatelliteEntity`: voice settings UI, announce, start/continue conversation, timers, pipeline and VAD selects | microWakeWord + openWakeWord, pushed from HA's `custom_wake_words` folder, selectable in HA's wake word picker, up to 2 active. HA-side wake word engines as an alternative | Every panel capability is a normal entity on one device | None (zeroconf discovery) | Chosen |

The integration subclasses the same base classes HA's own Wyoming and ESPHome integrations use
(`AssistSatelliteEntity`, `AssistPipelineSelect`, `VadSensitivitySelect`, `intent.async_register_timer_handler`).
That is what makes the panel indistinguishable from a built-in voice device in HA's UI.

### 8.2 Components

```
Home Assistant                                         Panel
┌──────────────────────────────────────┐              ┌──────────────────────────────────────────┐
│ custom_components/ha_slate           │  wss://:6054 │ slate-agent (apps/slate-agent)           │
│  config_flow   zeroconf + pairing    │◄────────────►│  TLS server, pairing, settings           │
│  device        session, reconnect,   │  Slate Link  │  wake word store + engine (TFLite)       │
│                wake word sync        │  protocol v1 │  voice state machine                     │
│  assist_satellite  pipeline, TTS,    │              │  pw-cat capture (aec_mic) / playback     │
│                announce, timers      │              │  (aec_sink)                              │
│  select / switch / number / light /  │              │  → slate-lightd (voice events, lights)   │
│  text / button / event               │              │  → kiosk.env / reboot trigger files      │
└──────────────────────────────────────┘              └──────────────────────────────────────────┘
```

* **`slate-agent`** (Python 3.12, about 1,000 lines, 28 tests) is the panel's only network-facing process.
  It has no codecs and no media player: HA sends raw PCM, and `pw-cat` plays it into the echo-cancel
  sink. So the device image no longer contains ffmpeg or mpv, and needs no `commercial` license flag.
* **`ha_slate`** (installable through HACS, needs HA ≥ 2026.10) owns everything user-facing. It is
  tested against HA 2026.10.4 with the real agent running in-process over TLS (19 tests).

### 8.3 Slate Link protocol v1

Reference: `apps/slate-agent/slate_agent/protocol.py`. Transport: WebSocket over TLS on TCP **6054**,
JSON text frames plus binary frames `<channel byte><payload>` (`0x01` mic PCM, `0x02` speaker PCM,
`0x03` model upload).

| Direction | Message | Purpose |
|---|---|---|
| panel → HA | `hello {device_id, name, model, sw_version, paired, nonce}` | First frame of every session |
| HA → panel | `auth {proof = HMAC-SHA256(token, nonce)}` · `pair {code?}` | Authenticate (the token never crosses the wire) or pair |
| panel → HA | `auth-ok {token?}` · `auth-failed {reason}` | `reason`: `invalid_auth`, `not_paired`, `invalid_code`, `pairing_window_closed`, `already_paired`, `locked` |
| panel → HA | `state {settings, wake_words[], max_active_wake_words, lights, voice_state}` | Full state on connect and after every change |
| panel → HA | `wake-word-detected {wake_word_id, wake_word_phrase, probability}` | Feeds the wake word event entity and `ha_slate_wake_word_detected` |
| panel → HA | `run-pipeline {run_id, start_stage: stt\|wake_word, wake_word_phrase?, conversation?}` + `0x01` frames, then `audio-end` | One Assist turn |
| HA → panel | `pipeline-event {run_id, event, data}` | `stt-vad-end` stops the mic; `intent-start`, `tts-end`, `run-end` and `error` drive state and LEDs |
| HA → panel | `audio-start {purpose: tts\|announce, rate, width, channels}` + `0x02` frames + `audio-stop` | TTS and announcements as PCM |
| panel → HA | `played {purpose, run_id}` | Playback drained. HA returns the satellite to idle and may reopen the mic (continue conversation) |
| HA → panel | `start-listening {conversation}` | `start_conversation` and follow-up questions: listen without a wake word |
| HA → panel | `set-config {settings}` · `light {target, …}` · `timer {event, …}` · `reboot` · `reload-kiosk` · `unpair` | Controls (request/response with `id`) |
| HA → panel | `wake-word-sync {models[{id, sha256}]}` → `{missing, removed}`; `wake-word-upload {id, sha256, size, config}` + `0x03` frames + `wake-word-upload-end` | Custom model management |

### 8.4 Pairing and transport security

* **TLS with fingerprint pinning (TOFU).** On first boot the agent generates a self-signed P-256 certificate
  in `/data/agent`. The config flow reads its SHA-256 fingerprint, shows the first 16 hex digits on the
  pairing screen, and stores it. Every later connection pins it (`aiohttp.Fingerprint`). A changed
  certificate, from a factory reset, a swapped device or a man-in-the-middle, fails closed and starts
  HA's re-authentication flow.
* **Pairing gate.** An unpaired panel accepts a `pair` request only (a) with the **factory pairing code**
  printed on the device label (`/boot/slate-pairing-code`, which survives factory reset), or (b) if no code
  is provisioned, within **15 minutes of power-on**. Five wrong codes lock pairing for 60 s. The panel
  then holds exactly one token (0600, `/data/agent/token`) and refuses further pairing until it is
  unpaired (removing the HA config entry sends `unpair`) or factory reset.
* **Session.** Challenge–response HMAC per connection. A newer authenticated session replaces the older
  one (HA restart). The agent drops a client that hasn't authenticated within 10 s.
* **Least privilege.** The agent runs as user `slate` with `ProtectSystem=strict` and writes only to
  `/data/agent`. Restarting the kiosk and rebooting go through `systemd` path units that watch trigger
  files (`slate-kiosk-reload.path`, `slate-reboot.path`), so the agent holds no privilege at all.

### 8.5 A voice turn

```
panel: wake word "Okay Nabu" (p=0.99) ─► wake-word-detected, chime, run-pipeline{stt, "Okay Nabu"}
panel: 0x01 PCM ──────────────────────► HA: async_accept_pipeline_from_satellite(stream, STT, phrase)
HA:    pipeline-event stt-vad-end ─────► panel: stop mic, audio-end, LEDs "thinking"
HA:    intent-start … tts-end ─────────► panel: expects TTS
HA:    audio-start + 0x02 PCM + stop ──► panel: pw-cat → aec_sink, LEDs "speaking"
panel: played{tts} ───────────────────► HA: tts_response_finished(); if continue_conversation:
HA:    start-listening ────────────────► panel: run-pipeline{stt} without a wake word
```

Safety nets: a 30 s cap per utterance, a 20 s wait for HA after speech ends, and an HA-side deadline
(TTS duration + 3 s) in case `played` never arrives. Errors such as `stt-no-text-recognized` play an
error earcon and flash the bar red. If the wake word fires while HA is disconnected, the panel gives
local feedback (red flash) instead of failing silently.

### 8.6 Custom wake words (full support)

**Formats.** One file pair per wake word, `<id>.json` + `<id>.tflite`. This follows HA's own
convention, so the same files serve ESPHome satellites too:

```json
{ "type": "micro", "wake_word": "Hey Slate", "trained_languages": ["en"], "version": 2,
  "micro": { "probability_cutoff": 0.97, "sliding_window_size": 5 } }
```
```json
{ "type": "openWakeWord", "wake_word": "Hey Slate", "trained_languages": ["en"], "probability_cutoff": 0.5 }
```

microWakeWord is preferred on the panel: one shared 10 ms feature front-end, with small int8 models
costing well under a millisecond per inference. openWakeWord models (the format produced by the
openWakeWord training notebook) share one mel-spectrogram + embedding front-end and cost more CPU.
Both can be mixed (max 2 active).

**Lifecycle.**

1. Drop the files into `<HA config>/custom_wake_words/`. This is the folder HA's ESPHome integration
   already reads.
2. The integration validates them (id `[a-z0-9_-]{1,64}`, supported type, `wake_word` present,
   TFLite flatbuffer, ≤ 8 MB) and syncs them to every panel on connect, or on demand with the
   `ha_slate.sync_wake_words` action (which returns what was uploaded and removed). The panel
   re-verifies the sha256 and the flatbuffer header and writes atomically (model first, then config).
3. The wake word shows up in HA's standard voice assistant settings for the device. HA calls
   `async_set_configuration`, and the agent rebuilds its engine at once (no restart).
4. Deleting the files from HA's folder removes them from the panels at the next sync. HA's folder is the
   single source of truth for custom models.

**Tuning per panel.** The **Wake word sensitivity** select shifts each model's own cutoff: `more_sensitive`
lowers it (by `min(0.1, 1−cutoff)`), `less_sensitive` halves the remaining margin. There is a 2 s
refractory period after each detection.

**Wake word in Home Assistant (alternative).** Set **Wake word detection** to *In Home Assistant*. The panel
then streams continuously with `start_stage=wake_word`, and the pipeline's wake word engine decides. That
can be the openWakeWord add-on with any custom model, or any other wake word provider. It costs about
32 kB/s of network per panel and adds latency, but lets you use engines or models that can't run on the
panel.

**Built-in models.** From the wheels: microWakeWord `okay_nabu`, `hey_jarvis`, `hey_mycroft`, `alexa`;
openWakeWord `*_oww` variants plus `hey_rhasspy_oww`.

**Measured on the host (real TFLite runtime, espeak-ng synthetic speech, 4 voices):** microWakeWord
`okay_nabu` detected "okay nabu" in 4 of 4 voices (p = 0.97–1.0), and "turn on the kitchen lights" produced
no detection (max p = 0.04). This regression test runs in CI when espeak-ng is installed. Synthetic speech
is not a substitute for a far-field test with real voices; that is on the bring-up checklist.

### 8.7 Entities (one HA device per panel)

| Entity | Type | Notes |
|---|---|---|
| *Panel name* | `assist_satellite` | States idle/listening/processing/responding. Announce, start conversation, ask question, timers |
| Assistant | `select` | HA's own pipeline select (`AssistPipelineSelect`) |
| Finished speaking detection | `select` | HA's own VAD sensitivity select |
| Wake word sensitivity | `select` | default / more sensitive / fewer false wakes |
| Wake word detection | `select` | On the panel / In Home Assistant |
| Mute microphone | `switch` | Stops the capture process itself, not just the stream. Edge bar shows red ends |
| Wake sound | `switch` | Chime on detection |
| Volume | `number` | 0–100 %, applied per stream (`pw-cat --volume`) |
| Edge light | `light` | RGB + brightness + effects None/Breathe/Scan/Pulse. Voice, timer and alert states temporarily take precedence |
| Display | `light` | Backlight brightness. Off = dark screen, kiosk keeps running |
| Dashboard URL | `text` | Rewrites `/data/agent/kiosk.env`. The browser restarts on the new URL |
| Reboot, Reload dashboard | `button` | Via the path-unit trigger files |
| Wake word | `event` | `detected` with `wake_word_id`, `wake_word_phrase`, `probability`. Also fired as bus event `ha_slate_wake_word_detected` |

### 8.8 Audio pipeline

```
mics ─ALSA─► PipeWire ─► echo-cancel (webrtc AEC, AGC, NS) ─► aec_mic ─► pw-cat --record ─► slate-agent
                              ▲ reference
speaker ◄─ALSA◄─ PipeWire ◄── aec_sink ◄── pw-cat --playback ◄── PCM from HA (TTS, announcements, chimes)
```

* **System-wide PipeWire + WirePlumber** (no user session on an appliance). The agent talks native
  PipeWire through `pw-cat`, so there is no PulseAudio shim, no `libpulse`, and no Python audio bindings.
* **AEC is mandatory** for a panel with a speaker: all playback goes to `aec_sink`. Panels that use an
  XMOS array with on-chip AEC drop the module and point `SLATE_CAPTURE_TARGET` at the array.
* **WirePlumber system-mode policy** (`50-slate-system.conf`) disables logind seat tracking and device
  reservation.

### 8.9 Packaging

* `slate-agent_0.1.0.bb` installs the agent from `apps/slate-agent` plus a **7-wheel** pinned set
  (`slate-agent-wheels.inc`, generated by `scripts/gen-wheels.py` from `requirements-target.txt`):
  pymicro-wakeword, pyopen-wakeword, pymicro-features, numpy 2, zeroconf, ifaddr, websockets. They go
  into `/opt/slate-agent` with `python3-installer-native`. Nothing is compiled, and every file is
  sha256-checked by the BitBake fetcher.
* **Why wheels:** both wake word packages ship a prebuilt `libtensorflowlite_c.so`, and their sdists
  contain an **x86-64** build of it, so a from-source recipe silently produces a broken image. Roadmap R6
  builds TFLite's C library from source and replaces the bundled copies.
* **Unit:** `slate-agent.service`, `Type=notify`, user `slate` (groups `audio`, `pipewire`),
  `MemoryMax=300M`, `Nice=-10`, writes only to `/data/agent`.

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

Source: `apps/slate-lightd/` (Python 3.12 asyncio, stdlib only, no network access, 52 unit tests).

```
 slate-agent ── voice_event ──┐  control socket       ┌──────────── Engine ─────────────┐
 slate-agent ── light ────────┼─► LightController ───►│ claims{source → effect, colour, │──► gamma ─► power
 RAUC hook / CLI ── claim ────┘   + VoiceBridge        │        priority, ttl}           │    LUT      limiter
                                  (persisted state)    │ render(top claim) @ 50 fps      │              │
                                                       └─────────────────────────────────┘  ws2812-spi │ sysfs │ console
```

**Priority claims instead of a state machine.** Each producer holds at most one claim and the
highest-priority live claim is rendered. Releasing a claim reveals the next one down. This is what makes "error flash
over timer countdown over the user's ambient colour" work without special cases.

| Priority | Source | Trigger (agent voice event) | Effect |
|---|---|---|---|
| 90 | `system` | boot (2.5 s), RAUC pre/post-install | scan white / solid green |
| 70 | `error` | `pipeline_error` | 3 red flashes (1.3 s TTL) |
| 60 | `network` | `disconnected` (no HA session; also shown from lightd start until the agent connects) → cleared by `zeroconf: connected` | slow red breathe |
| 50 | `alarm` | `timer_ringing` → cleared by `idle` | amber pulse |
| 40 | `voice` | `wake_word_detected` / `listening` / `thinking` / `tts_speaking` → `tts_finished`/`idle` | grow from centre / solid / scanning comet / pulse |
| 35 | `media` | `volume_changed` | volume level bar (1.5 s) |
| 30 | `timer` | `timer_ticking`/`timer_updated` (tracked locally, soonest timer) | draining progress bar |
| 20 | `muted` | `muted` | two end LEDs dim red |
| 10 | `user` | HA **Edge light** entity (persisted to `/data/lightd/state.json`) | solid / Breathe / Scan / Pulse |

* **HA control path:** the integration's **Edge light** and **Display** entities send `light` requests
  to the agent, which forwards them over the control socket. lightd owns and persists the state
  (`/data/lightd/state.json`) and reports it back in `status`, so HA sees the true state after a reboot.
* **Power limiter:** estimates current from post-gamma PWM duty (`Σ duty × 20 mA + 1 mA/LED`) and
  scales the frame to stay under `max_current_ma`. Full white on 48 LEDs drops from 2.9 A to 0.6 A.
* **Idle cost:** static frames are not re-sent. The render loop sleeps until a claim changes
  (1 s safety tick), so CPU use is effectively zero at idle.
* **Backends:** `ws2812-spi` (spidev ioctl mode 0 / 8 bit / speed, single `write()` per frame),
  `sysfs` (LED class with `multi_intensity`, one `pwrite` per changed segment), and `console`
  (truecolor preview for QEMU and dev hosts).
* **Control socket** `/run/slate-lightd/control.sock` (0660 root:slate, so only the agent and root
  services can use it) takes JSON lines: `voice_event`, `light`, `claim`, `release`, `status`. CLI:
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
   `slate-agent` and `slate-lightd` have been active for 60 s, and only then marks the slot good.
4. If the new slot crashes, hangs (the hardware watchdog forces a reboot) or never becomes healthy,
   its counter reaches 0 and U-Boot falls back to the old slot (F7).

The same `boot.scr` serves CM4 (U-Boot 2024.01) and CM5 (U-Boot 2026.10). Both keep the environment
in `/boot/uboot.env` (16 KiB), which matches the BSP's `fw_env.config`.

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
| Agent (TCP 6054) | TLS 1.2+, self-signed P-256 cert pinned by HA at pairing. HMAC challenge–response per session. Pairing gated by factory code or a 15-min power-on window, with lockout after 5 wrong codes (§8.4) |
| Local IPC | lightd control socket 0660 `root:slate`. lightd has `PrivateNetwork=yes` |
| mDNS | The agent's zeroconf responder (`_ha-slate._tcp`). `systemd-resolved` in `resolve`-only mode so the two don't fight over 5353 |
| SSH | Absent from `slate-image`. Dropbear only in `slate-image-dev` |
| Browser | Unprivileged `kiosk` user, `ProtectSystem=strict`, memory-capped, writes only to `/data/kiosk`. Non-admin HA user (§7.3) |
| Agent | Unprivileged `slate` user, `ProtectSystem=strict`, `PrivateDevices=yes`, writes only to `/data/agent`, `MemoryMax=300M`. Privileged actions only through two path units. Mic mute stops the capture process |
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

# CM5 + 10.1" Touch Display 2 (§3.4)
kas build kas/machine/cm5.yml                 # → slate-image-raspberrypi-cm5-io-board.rootfs.wic.bz2
kas build kas/machine/cm5.yml --target slate-bundle
```

Artifacts: `build/tmp/deploy/images/<machine>/`. The SBOM is in `build/tmp/deploy/spdx/`.

### 13.3 Flash a CM4 / CM5 (eMMC)

```sh
# Carrier: set the nRPIBOOT jumper (CM5 IO Board: "disable eMMC boot"), connect the USB-C port
sudo rpiboot                                  # CM4; from raspberrypi/usbboot. eMMC appears as /dev/sdX
sudo rpiboot -d mass-storage-gadget64         # CM5 (see Raspberry Pi's CM5 flashing docs)
sudo bmaptool copy build/tmp/deploy/images/<machine>/slate-image-<machine>.rootfs.wic.bz2 /dev/sdX
# remove jumper, power over PoE
```

### 13.4 First boot and provisioning

1. The bar shows a blue scan (lightd up), then the dashboard appears.
2. Install the integration in HA: add this repository as a HACS custom repository ("Integration"),
   or copy `custom_components/ha_slate` into `<config>/custom_components/`. Then restart HA.
3. HA shows **HA Slate discovered** (zeroconf). Click **Configure** and pair. Enter the label's pairing
   code if the panel has one; otherwise do it within 15 minutes of powering the panel. Check that the
   fingerprint prefix shown matches the one in the panel's log on dev images.
4. On the device page, pick the **Assistant** (pipeline) and wake word (*Settings → Voice assistants →
   the panel → Wake word*). Set the **Dashboard URL**. Use **Edge light**, **Display** and the
   **Wake word** event in automations (for example dim at night, or colour the bar on alarm).
5. Site settings that don't belong in HA (static IP) go in `/data/slate/network/10-static.network`.
   On dev images, use SSH; on production, use the provisioning flow (R2).

### 13.4a Add a custom wake word

```sh
# 1. Train: microWakeWord (recommended on the panel) or openWakeWord (training notebook).
# 2. Install into Home Assistant's config folder (same folder ESPHome devices use):
#      <config>/custom_wake_words/hey_slate.tflite
#      <config>/custom_wake_words/hey_slate.json   (format: §8.6)
# 3. Push it to all panels (also happens automatically on every reconnect):
#      Developer tools → Actions → "HA Slate: Sync wake words" (ha_slate.sync_wake_words)
# 4. Select "Hey Slate" in the panel's voice assistant settings; adjust "Wake word sensitivity".
```

### 13.5 OTA update

```sh
scp slate-bundle-raspberrypi4-64.raucb root@panel:/data/   # dev image
ssh root@panel rauc install /data/slate-bundle-raspberrypi4-64.raucb && reboot
rauc status                                                 # after reboot: booted slot, "good"
```

Production: pull from your update server (hawkBit through `rauc-hawkbit-updater` in `meta-rauc`, roadmap R1).

### 13.6 Upgrading the agent's dependencies

```sh
# 1. edit apps/slate-agent/requirements-target.txt (pins), the same pins in its pyproject.toml,
#    and recompile requirements-test.txt (the command is in its header)
# 2. regenerate the hash-pinned wheel set for the target (cp312 / glibc 2.39 / aarch64):
scripts/gen-wheels.py --requirements apps/slate-agent/requirements-target.txt \
    --out meta-ha-slate/recipes-voice/slate-agent/slate-agent-wheels.inc
# 3. run the agent + integration tests, build, run the voice acceptance tests on hardware
```

The protocol version (`PROTOCOL_VERSION`) must be bumped on any incompatible message change. The
integration refuses to talk to a panel with a different version, and HA shows it as unavailable.

### 13.7 Developing on a host

```sh
# Agent (real TFLite engines; espeak-ng enables the synthetic-speech detection test)
cd apps/slate-agent && python3.12 -m venv .venv && .venv/bin/pip install -r requirements-test.txt
.venv/bin/pytest -q
.venv/bin/python -m slate_agent --state-dir /tmp/agent --no-audio --no-zeroconf --debug   # pair from a dev HA

# lightd
cd apps/slate-lightd && python3.12 -m venv .venv && .venv/bin/pip install -r requirements-test.txt && .venv/bin/pytest -q
.venv/bin/python -m slate_lightd --backend console -c lightd.toml.example   # bar preview in your terminal

# HA integration (Python 3.14; runs the real agent in-process over TLS)
uv venv -p 3.14.10 .ha && uv pip install -p .ha/bin/python -r requirements-test.txt
.ha/bin/python -m pytest -q tests/ha_slate
```

---

## 14. Verification status and bring-up checklist

**Already checked in this repository:**

* **`ha_slate` integration: 19 tests on HA 2026.10.4 (Python 3.14)**, each against the *real* agent
  running in-process over TLS:
  * Config flow: manual, zeroconf, a wrong-then-right pairing code, and an unreachable host.
  * All 14 entities created and available.
  * Custom wake words: synced from `custom_wake_words/`, offered by `async_get_configuration`, and
    activated through `async_set_configuration`. Removal is propagated by the `sync_wake_words` action.
  * Every control reaches the panel (mute, volume, wake location, edge light RGB/effect, kiosk URL,
    reboot trigger).
  * A full voice turn: wake word → `async_accept_pipeline_from_satellite` receives the exact mic bytes and
    phrase → TTS wav streamed and played byte-exact → state back to idle → wake word event entity fires.
  * Continue-conversation reopens the mic without a wake word; HA-side wake word mode
    (`start_stage=wake_word`).
  * Announcement through real ffmpeg conversion, played byte-exact.
  * Timer forwarding, reconnect after the panel drops the session, and reauth after a factory reset.
  * Refusals: a certificate that doesn't match the pinned fingerprint (no session; setup starts reauth),
    and a panel speaking another protocol version (no session, setup retries, pairing refused).
* **`slate-agent`: 28 tests.** HMAC auth, pairing window/code/lockout, certificate stability, settings
  validation, the wake word store (install / reject bad id, hash, non-TFLite, missing cutoff, unknown type /
  remove), engine thresholds, sensitivity presets, refractory period and the 2-model limit. TLS sessions
  cover pairing and auth, a full voice turn, start-listening, errors, HA-side wake mode (including the
  error back-off), config, lights and kiosk, and custom model upload / sync / activate / corrupt-upload
  rejection.
* **Real wake word engines on synthetic speech** (TFLite runtime, espeak-ng): microWakeWord
  `okay_nabu` fires in 4 of 4 voices, and the negative phrase stays ≤ 0.04 probability (§8.6).
* **`slate-lightd`: 52 tests** (WS2812 encoding, power limiter, effects, claims, voice-event mapping,
  light controller persistence, sysfs backend, control socket).
* Upstream facts the design depends on, read from source: HA's `AssistSatelliteEntity`, the Wyoming and
  ESPHome satellite implementations, the `custom_wake_words` convention, and timer handlers; also
  `weston-init`, Cog 0.18 options and paths, PipeWire 1.0.9 system units and `pw-cat` raw stdio, and the
  pymicro/pyopen-wakeword APIs.
* BitBake against the pinned Scarthgap layers (this v0.2 layer):
  * `qemuarm64`: 2835 recipes parse with 0 errors. `bitbake -n slate-image slate-image-dev` resolves
    the full task graph (7,721 tasks), so every `RDEPENDS` has a provider.
  * `cm4` (+ meta-raspberrypi + meta-rauc): 2899 recipes parse, and `bitbake -n slate-image slate-bundle`
    resolves (7,644 tasks).
  * `bitbake -c unpack` of every product recipe, with the BitBake fetcher downloading and
    sha256-checking all 7 agent wheels.
  * The only error in these runs is the sandbox blocking the `uninative` tarball download, not the
    configuration. An earlier dry run caught one real bug (mpv not building `libmpv`), which the
    move to slate-agent has since made moot.
* **CM5 (`kas/machine/cm5.yml`):** 2900 recipes parse, and `bitbake -n slate-image slate-bundle`
  resolves (7,644 tasks). `bitbake -e` confirms kernel `6.12.93`, U-Boot `2026.10`, RAUC compatible
  `ha-slate-cm5`, and the Weston `[output]` (`DSI-1`, `rotate-90`). `bitbake -c unpack u-boot` succeeds
  with only `fw_env.config` left in `SRC_URI`. CM4 is unchanged (U-Boot 2024.01, `ha-slate-cm4`, graph
  resolves). Validation caught two real bugs, both fixed: U-Boot v2026.10 is only on branch `main`, and
  a duplicate `/etc/fw_env.config` would have collided with the BSP's `u-boot-env`.
* Shell scripts pass `shellcheck`. Unit files pass `systemd-analyze verify` with no unknown keys.

**Not yet run.** No full image compile (it needs a build host, §13.1), so compile-time and
`do_package_qa` issues are still possible. The first suspects are the `INSANE_SKIP` list on the
wheel-based `slate-agent` recipe and compiling U-Boot 2026.10 with scarthgap's toolchain and host
tools (`do_patch`/`do_compile` couldn't run in the validation sandbox, which blocks GNU mirrors). Nothing has run on panel hardware.

**Bring-up checklist (in order):**

- [ ] Full build for `qemuarm64`. Boot with `runqemu`, confirm Weston + Cog render HA, the lightd console
  preview, and the `/data` tmpfs fallback
- [ ] CM4 build + flash. Confirm the A/B `boot.scr` picks slot A, `rauc status` is sane, and `fw_printenv` works
- [ ] CM5: U-Boot 2026.10 boots from eMMC as `mmc 0`, Linux sees it as `mmcblk0` (`boot.scr` assumes both). Weston drives the panel on `DSI-1` (else set `SLATE_DISPLAY_OUTPUT`). Touch follows the rotation, all 10 points register (`evtest`), pinch works in the dashboard
- [ ] Panel: DSI overlay, backlight under `/sys/class/backlight`, touch mapped to the output, rotation
- [ ] Weston 13 accepts `require-outputs=any` with a late DSI bridge
- [ ] WirePlumber system mode: the ALSA nodes appear with `support.logind = disabled` (`pw-cli ls Node`)
- [ ] Echo cancel: bind `capture.props`/`playback.props` to the real nodes. Wake word triggers during TTS at 70 % volume
- [ ] Agent: discovered by HA, pairing, wake word → pipeline → TTS through `aec_sink`, follow-up question
- [ ] Wake words with real voices at 1 m / 3 m / 5 m, with and without TTS playing. Tune the per-model cutoffs. False wakes per 24 h of TV audio
- [ ] Edge bar: scope MOSI idle level and the actual SPI clock. Colour order. 48 LEDs at full white stay ≤ 600 mA measured
- [ ] Mute switch: capture process stops (`pw-top` shows no stream). Edge bar shows the muted indicator
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
| R6 | Native recipes for the agent's dependencies, with TFLite built from source |
| R7 | Hardware-in-the-loop CI: flash, boot-time measurement, wake-word replay test |
| R8 | Proximity sensor: wake the screen on approach, auto-brightness from ambient light |
| R9 | Barge-in + "stop" word: keep wake word detection running during TTS (a micro `stop` model) to interrupt responses and timers |
| R10 | `media_player` entity (music / Music Assistant) over the same PCM path, with ducking under TTS |
| R11 | `update` entity: firmware version and one-click install via RAUC, driven by the integration |
| R12 | Wake word training helper: record positive samples on the panel itself to improve a custom model for the room |
| R13 | Optional MQTT bridge for non-HA consumers (light bar, sensors). Never for voice |
| R14 | CM5: replace U-Boot with the Raspberry Pi bootloader's `tryboot` A/B (two boot partitions, RAUC `custom` backend) |
