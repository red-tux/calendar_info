"""Credential providers: how a calendar source authenticates.

One module per provider (see registry.py). A provider hands a source a `Credential` for an
account id; where that credential comes from - a refresh token this plugin stores, the
desktop's own keyring via signond or GOA, a saved password - is the provider's business alone.
"""
