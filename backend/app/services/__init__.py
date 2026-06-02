"""Application service layer.

Services are the *thin* business-logic layer between API routers and the
infra (storage / integrations / scheduler). They:

* Own end-to-end workflows that don't fit any single integration module.
* Translate domain operations into the right combination of storage writes,
  filesystem actions, and integration calls — under the right locks.
* Are imported by API routers and CLI commands.

Phase 4 introduces :mod:`app.services.openclaw_agents`.
"""
