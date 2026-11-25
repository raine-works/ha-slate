# HA Slate

Firmware and Home Assistant integration for a PoE-powered wall touch panel. The panel runs a Yocto
(Scarthgap LTS) image that boots straight into a full-screen dashboard (Weston kiosk-shell + Cog/WPE
WebKit), runs an on-device voice agent with custom wake word support, and drives an integrated edge
light bar. The `ha_slate` custom integration makes the panel a first-class Home Assistant device:
a native Assist satellite (HA's wake word picker, announcements, conversations, timers) plus lights,
volume, mute, dashboard URL and more. Updates are A/B with RAUC.

**Read first: [`docs/SPEC.md`](docs/SPEC.md)**. It covers architecture, layer structure, kernel
configuration, the voice and lighting design, partitioning and updates, and the build guide.

## Repository layout

| Path | What |
|---|---|
| `docs/SPEC.md` | Technical specification and build guide |
| `kas/` | Build configurations: `machine/qemuarm64.yml` (dev/CI), `machine/cm4.yml`, `machine/cm5.yml` (CM5 + 10.1" Touch Display 2) |
| `meta-ha-slate/` | Product Yocto layer: distro, images, services, kernel fragments, A/B boot |
| `custom_components/ha_slate/` | Home Assistant integration (HACS-installable; HA ≥ 2026.10) |
| `apps/slate-agent/` | On-device agent: TLS link to HA, wake word engine + custom models, voice turns |
| `apps/slate-lightd/` | Edge light bar + backlight daemon (Python, unit-tested on the host) |
| `tests/ha_slate/` | Integration tests: real HA test harness against a real agent over TLS |
| `scripts/gen-wheels.py` | Regenerates the hash-pinned target wheel set for the agent |
| `scripts/gen-dev-keys.sh` | Creates a throw-away RAUC signing CA in `keys/dev/` (gitignored) |

## Quick start

```sh
pip install kas==5.5
scripts/gen-dev-keys.sh
kas build kas/machine/qemuarm64.yml          # or kas/machine/cm4.yml
kas shell kas/machine/qemuarm64.yml -c "runqemu slirp gl sdl"
```

Then install the integration (HACS custom repository, or copy `custom_components/ha_slate`), and the
panel is discovered automatically. Custom wake words: put `<id>.json` + `<id>.tflite` in HA's
`config/custom_wake_words/` folder (SPEC §8.6).

Host requirements, flashing, and test instructions are in `docs/SPEC.md` §13.
