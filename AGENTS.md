# Agent guidance for ha-slate

## Raineworks base rules

This repository follows Raineworks' base rules, in [`.raineworks/base.md`](.raineworks/base.md): read them before
changing anything. They're a copy of `base.md` in `raine-works/agents`, kept current by its sync: change them there,
not here. Where this file is more specific, it wins.

@.raineworks/base.md

## This repository

Firmware and Home Assistant integration for HA Slate, a PoE-powered wall touch panel: a Yocto (Scarthgap LTS) image
driven by kas, two on-device Python daemons, and the `ha_slate` custom integration. The design, the build guide and
the reasons behind both are in [`docs/spec.md`](docs/spec.md); read the section you're changing first.

### Layout

| Path | What |
| --- | --- |
| `kas/` | Build configurations: `slate.yml` (base, never built directly), `machine/{qemuarm64,cm4,cm5}.yml`, `feature/` |
| `meta-ha-slate/` | Product Yocto layer: distro, images, recipes, kernel fragments; BSP-specific parts in `dynamic-layers/<bsp>/` |
| `apps/slate-agent/` | On-device agent (Python 3.12): TLS link to HA, wake words, voice turns |
| `apps/slate-lightd/` | Edge light bar and backlight daemon (Python, no runtime dependencies) |
| `custom_components/ha_slate/` | Home Assistant integration (HACS, HA ≥ 2026.10) |
| `tests/ha_slate/` | Integration tests: the real HA test harness against the real agent over TLS |
| `scripts/` | `gen-dev-keys.sh` (dev RAUC CA into gitignored `keys/`), `gen-wheels.py` (agent's target wheel set) |

### Build and test

The `ci` workflow runs everything below except image builds. On a host (details in SPEC §13.7):

- **lightd:** `pytest -q` in `apps/slate-lightd` (Python 3.12).
- **agent:** `pip install -r requirements-target.txt pytest pytest-asyncio`, then `pytest -q`, in `apps/slate-agent`
  (Python 3.12; `espeak-ng` installed enables the real wake word detection test).
- **integration:** `pytest -q tests/ha_slate` from the root, on Python 3.14 with `pytest-homeassistant-custom-component`
  and the packages `ci.yml` installs.
- **lint:** `shellcheck -s sh` on the shell scripts listed in `ci.yml` (add new ones there), and
  `kas dump kas/machine/cm4.yml` to validate the kas files.
- **image:** `scripts/gen-dev-keys.sh && kas build kas/machine/qemuarm64.yml` (SPEC §13.2). It needs a large host
  and isn't run in CI; say whether you built it.

### Rules

- **Python versions follow their targets:** the agent and lightd run on Scarthgap's CPython 3.12, so they use nothing
  newer; the integration runs on Home Assistant's Python (3.14).
- **Hardware stays behind the BSP split:** board-specific choices go in `kas/machine/<board>.yml` and
  `meta-ha-slate/dynamic-layers/<bsp>/`, never in the common layer (SPEC §3.2).
- **Layer pins:** every kas repository is pinned to an exact commit; bump one on purpose, in its own change.
- **Agent dependencies** are pinned in `apps/slate-agent/requirements-target.txt` and shipped as hash-pinned aarch64
  wheels: after changing a pin, regenerate `slate-agent-wheels.inc` with `scripts/gen-wheels.py` (SPEC §13.6). Don't
  replace the wheels with a from-source recipe: the sdists bundle an x86-64 TFLite library (SPEC §8.9).
- **Protocol:** bump `PROTOCOL_VERSION` on any incompatible Slate Link message change; the integration refuses a panel
  with a different version (SPEC §8.3, §13.6).
- **The image's security posture holds** (SPEC §1.2 N4–N5, §12): read-only root, writes only under `/data`, nothing
  listening but the agent's TLS port and mDNS, no SSH outside `slate-image-dev`, services unprivileged and confined.
  A change that loosens any of it needs a person's go-ahead.
- **No ffmpeg or mpv on the panel:** Home Assistant decodes media and streams raw PCM (SPEC §5.4).
- **Signing keys never enter the repository:** `keys/` is gitignored; dev keys come from `scripts/gen-dev-keys.sh`.
- When a change moves the verification status or roadmap, update SPEC §14–§15 with it.
