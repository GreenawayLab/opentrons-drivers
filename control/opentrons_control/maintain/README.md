# Fleet maintenance tooling

Operator commands that run **on the control machine** to update software.
They are not part of the runtime control plane (backend / proxy / frontend).

## Two version numbers, two update paths

| You want to update… | Edit this version | Then run |
| --- | --- | --- |
| Control software (backend/proxy/frontend) | `control/pyproject.toml` `version` | `make deploy-control` (rebuilds & restarts the Docker stack) |
| Robot software (the drivers on every Opentrons) | `drivers/pyproject.toml` `version` (and the `expected_version` guard in `deploy.toml`) | `make update-robots` (a.k.a. `ot-update-robots`) |

## Updating the robots

```bash
# from a checkout of this repo on the control machine
pip install -e ./control          # once, installs the ot-* console scripts
ot-update-robots --dry-run        # build the wheel + show what would change
ot-update-robots                  # build, push, and install on every robot
```

Useful flags:

- `--robot <id>` — limit to specific robot id(s) from `backend.json` (repeatable).
- `--force` — reinstall even if a robot already reports the target version.
- `--no-version-check` — skip the `expected_version` guard.
- `--config <path>` — use a different `deploy.toml`.

`ot-build-wheel` builds the wheel only (into `./dist/wheels`) without deploying.

## How the robot install works

The robots run a network-isolated, non-standard Opentrons Linux image with
no usable package index. A wheel is just a zip archive, so the install needs
nothing but the robot's stdlib:

1. Build the `opentrons_drivers` wheel from `drivers/`.
2. Guard: assert the built version equals `deploy.toml`'s `expected_version`.
3. For each robot in `backend.json`: SCP the wheel to `staging_dir`, delete
   the old package tree + `.dist-info` from the site-packages overlay, then
   `zipfile.extractall` the wheel into that overlay, and verify the version.

`opentrons` (the only dependency) ships in the Opentrons system image, so no
dependency resolution happens on the robot.

## Configuration

`deploy.toml` (alongside this file) holds the operational settings: the
expected driver version, where the drivers project lives, the path to
`backend.json` (which defines the fleet), the SSH key directory, and the
on-robot site-packages / staging paths. Relative paths are resolved against
`deploy.toml`'s own directory.

`deploy.toml` is **gitignored** — it is per-deployment config you hand-edit on
the control machine, so keeping it untracked means editing it never conflicts
with a `git pull`. Create it once from the committed template:

```bash
cp control/opentrons_control/maintain/deploy.example.toml \
   control/opentrons_control/maintain/deploy.toml
```

Note that the *driver version actually deployed* comes from
`drivers/pyproject.toml` (a tracked, committed value) — bump that via a normal
commit and `git pull` it onto the control machine. `expected_version` in
`deploy.toml` is only a local guard that must match it.

`backend.json` is runtime config, not source — it is gitignored and lives in
the Docker `data/` volume. Copy the committed template to create it:

```bash
cp control/opentrons_control/backend.example.json control/opentrons_control/data/backend.json
```

Note: the backend fetches each robot's `key_name` from the artifact store and
caches it, whereas `ot-update-robots` expects the SSH private keys in a local
directory (`ssh_key_dir` in `deploy.toml`). Point `ssh_key_dir` at wherever
the keys actually live on the control machine.
