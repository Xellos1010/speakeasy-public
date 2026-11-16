"""Import every handler module so its @register(...) calls run and populate
speakeasy.gui.bridge.ACTIONS. New handler files must be imported here."""
from speakeasy.gui.handlers import (  # noqa: F401
    home,
    training,
    training_wizard,
    history,
    models,
    mics,
)
