# HA Slate

Firmware for a PoE-powered wall touch panel for Home Assistant. It is a Yocto (Scarthgap LTS) image that
boots straight into a full-screen dashboard (Weston kiosk-shell + Cog/WPE WebKit), runs an on-device
wake-word voice satellite (Linux Voice Assistant, ESPHome protocol), and drives an integrated edge light
bar that shows voice state and appears in HA as a light entity. Updates are A/B with RAUC.

**Read first: [`docs/SPEC.md`](docs/SPEC.md)**. It covers architecture, layer structure, kernel
configuration, the voice and lighting design, partitioning and updates, and the build guide.

## Repository layout

| Path | What |
|---|---|
| `docs/SPEC.md` | Technical specification and build guide |
| `kas/` | Build configurations: `machine/qemuarm64.yml` (dev/CI), `machine/cm4.yml` (reference HW) |
| `meta-ha-slate/` | Product Yocto layer: distro, images, services, kernel fragments, A/B boot |
| `apps/slate-lightd/` | Edge light bar + backlight daemon (Python, unit-tested on the host) |
| `scripts/gen-lva-wheels.py` | Regenerates the hash-pinned wheel set for the voice service |
| `scripts/gen-dev-keys.sh` | Creates a throw-away RAUC signing CA in `keys/dev/` (gitignored) |

## Quick start

```sh
pip install kas==5.5
scripts/gen-dev-keys.sh
kas build kas/machine/qemuarm64.yml          # or kas/machine/cm4.yml
kas shell kas/machine/qemuarm64.yml -c "runqemu slirp gl sdl"
```

Host requirements and flashing instructions are in `docs/SPEC.md` §13.
