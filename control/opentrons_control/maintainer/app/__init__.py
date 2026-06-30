"""Maintenance tooling that runs on the control machine.

This subpackage is not part of the runtime control plane (backend / proxy
/ frontend). It provides operator commands for rolling software out to the
isolated robot fleet:

- :mod:`opentrons_control.maintain.build_wheel` builds the ``opentrons_drivers``
  wheel from the sibling ``drivers/`` project.
- :mod:`opentrons_control.maintain.update_robots` builds that wheel and
  installs it onto every robot listed in the backend config, over SSH.

Both are exposed as console scripts (``ot-build-wheel`` and
``ot-update-robots``) via ``control/pyproject.toml``.
"""
