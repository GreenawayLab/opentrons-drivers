# Opentrons

A monorepo of two independently-installable Python packages:

| Directory | Package | Runs on | Purpose |
| --- | --- | --- | --- |
| `drivers/` | `opentrons_drivers` | the **Opentrons robots** | the HTTP agent + protocol/action library that drives the hardware |
| `control/` | `opentrons_control` | the **control machine** | backend / proxy / frontend services that orchestrate the fleet, plus maintenance tooling |

The robots sit on an isolated subnet with no internet access; the control
machine is internet-connected and bridges to that subnet. The two packages
share no code and never import each other, so each is built, versioned, and
deployed on its own.

## Updating software

Each package carries its own version, edited by hand in its `pyproject.toml`:

- **Control software** — bump `version` in `control/pyproject.toml`, then
  rebuild and restart the Docker stack (`make deploy-control`).
- **Robot software** — bump `version` in `drivers/pyproject.toml` (and the
  matching `expected_version` in `control/opentrons_control/maintain/deploy.toml`),
  then run `make update-robots` (`ot-update-robots`) on the control machine.
  This builds the drivers wheel and installs it on every robot in
  `backend.json` over SSH. See
  [`control/opentrons_control/maintain/README.md`](control/opentrons_control/maintain/README.md).

The repo uses [`pip-tools`][pip-tools] for the dev dependency set,
[`pre-commit`][pre-commit] hooks (for [`ruff`][ruff] and [`mypy`][mypy]), and
automated tests using [`pytest`][pytest] and [GitHub Actions].

It was developed by the [Imperial College Research Software Engineering Team].

## Usage

To get started:

1. Activate a git repository (required for `pre-commit` and the package versioning with
`setuptools-scm`):

   ```bash
   git init
   ```

1. Create and activate a [virtual environment]:

   ```bash
   python -m venv .venv
   source .venv/bin/activate # with Powershell on Windows: `.venv\Scripts\Activate.ps1`
   ```

1. Install development requirements and both packages in editable mode:

   ```bash
   pip install -r dev-requirements.txt
   pip install -e ./drivers -e ./control
   ```

1. Install the git hooks:

   ```bash
   pre-commit install
   ```

1. Run the tests:

   ```bash
   pytest
   ```

## Updating Dependencies

To add or remove dependencies:

1. Edit the `dependencies` variables in the `pyproject.toml` file (aim to keep
development tools separate from the project requirements).
1. Update the requirements files:
   - `pip-compile` for `requirements.txt` - the project requirements.
   - `pip-compile --extra dev -o dev-requirements.txt` for the development requirements.
1. Sync the files with your installation (install packages):
   - `pip-sync *requirements.txt`

To upgrade pinned versions, use the `--upgrade` flag with `pip-compile`.

Versions can be restricted from updating within the `pyproject.toml` using standard
python package version specifiers, i.e. `"black<23"` or `"pip-tools!=6.12.2"`

## Customising

All configuration can be customised to your preferences. The key places to make changes
for this are:

- The `pyproject.toml` file, where you can edit:
  - The build system (change from setuptools to other packaging tools like [Hatch] or
[flit]).
  - The python version.
  - The project dependencies. Extra optional dependencies can be added by adding another
list under `[project.optional-dependencies]` (i.e. `doc = ["mkdocs"]`).
  - The `mypy` and `pytest` configurations.
- The `.pre-commit-config.yaml` for pre-commit settings.
- The `.github` directory for all the CI configuration.

[pip-tools]: https://pip-tools.readthedocs.io/en/stable/
[pre-commit]: https://pre-commit.com/
[ruff]: https://pypi.org/project/ruff/
[mypy]: https://mypy.readthedocs.io/en/stable/
[pytest]: https://pytest.org/
[GitHub Actions]: https://github.com/features/actions
[pre-commit.ci]: https://pre-commit.ci
[setuptools-scm]: https://setuptools-scm.readthedocs.io/en/latest/
[latest standards]: https://peps.python.org/pep-0621/
[Imperial College Research Software Engineering Team]: https://www.imperial.ac.uk/admin-services/ict/self-service/research-support/rcs/service-offering/research-software-engineering/
[virtual environment]: https://docs.python.org/3/library/venv.html
[Hatch]: https://hatch.pypa.io/
[flit]: https://flit.pypa.io/
