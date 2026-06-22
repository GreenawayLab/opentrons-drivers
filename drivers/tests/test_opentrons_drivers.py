"""Tests for the main module."""

from opentrons_drivers import __version__


def test_version():
    """Check that the version is acceptable."""
    assert isinstance(__version__, str)
