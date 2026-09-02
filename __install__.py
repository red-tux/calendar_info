"""Builds the isolated venv for this plugin's backend process.

Run with the same interpreter as the main app (see PluginBase.recreate_venv), so the venv's
Python version always matches what launch_backend()'s health check expects. The backend needs
icalendar / recurring-ical-events, which the shared app environment doesn't (and shouldn't)
ship - see CLAUDE.md, "External dependencies".
"""
import os

from streamcontroller_plugin_tools import create_venv

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))

create_venv(
    os.path.join(PLUGIN_DIR, ".venv"),
    os.path.join(PLUGIN_DIR, "backend_requirements.txt"),
)
