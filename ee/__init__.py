"""Owns the "ee" app label for the models that are still pinned to it.

This fork carries no enterprise code. There is deliberately no `apps.py` here, so
`from ee.apps import EnterpriseConfig` still fails and `EE_AVAILABLE` stays False; the
package exists only to give the label a home and to hold the migrations under it.
"""
