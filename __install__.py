"""Builds the isolated venv for this plugin's backend process.

Run with the same interpreter as the main app (see PluginBase.recreate_venv), so the venv's
Python version always matches what launch_backend()'s health check expects. The backend needs
icalendar / recurring-ical-events, which the shared app environment doesn't (and shouldn't)
ship - see CLAUDE.md, "External dependencies".

The one thing the backend borrows from the app is PyGObject (`gi`, for GDBus): it has no
PyPI wheels and the Flatpak runtime has no compiler, so it can't be pip-installed here. The
.pth below puts the app interpreter's site-packages *after* the venv's own on sys.path, so
the venv's pinned packages still win and only what it lacks falls through.
"""
import os
import sys

from streamcontroller_plugin_tools import create_venv

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
VENV = os.path.join(PLUGIN_DIR, ".venv")

create_venv(VENV, os.path.join(PLUGIN_DIR, "backend_requirements.txt"))

import gi  # noqa: E402 - the app interpreter always has it; this is the dir we want to borrow

app_site_packages = os.path.dirname(os.path.dirname(os.path.abspath(gi.__file__)))
venv_site_packages = os.path.join(
    VENV, "lib", f"python{sys.version_info.major}.{sys.version_info.minor}", "site-packages")
with open(os.path.join(venv_site_packages, "calendar-info-app-gi.pth"), "w", encoding="utf-8") as f:
    f.write(app_site_packages + "\n")
