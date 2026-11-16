"""Bridge handlers for the Models & System screen -- real data, same
live engine-swap logic the menu-bar "Models" item already uses
(speakeasy/__main__.py::_do_switch_engine, exposed here as ctx.switch_engine).

Actions:
  models_get_state -> {chip, ram_gb, active_engine, recommended_engine,
                        engines: [{key, label, detail, is_active,
                                   is_recommended}, ...]}
  models_switch_engine -> payload {engine: "parakeet"|"whisper"}
      Passthrough to ctx.switch_engine(name); returns its result directly:
      {"started": True} once the background swap has kicked off, or
      {"already_active": True} if `engine` is already the active one.
      Completion (success/failure) is reported later via the
      "engine_switch_result" push event, not by this handler's return value.
"""
from speakeasy.gui.bridge import register
from speakeasy.sysinfo import ENGINE_INFO, recommend_engine


@register("models_get_state")
def models_get_state(_payload, ctx):
    specs = ctx.detect_specs()
    recommended = recommend_engine(specs)
    active = ctx.settings.engine

    engines = [
        {
            "key": key,
            "label": info["label"],
            "detail": info["detail"],
            "is_active": key == active,
            "is_recommended": key == recommended,
        }
        for key, info in ENGINE_INFO.items()
    ]

    return {
        "chip": specs.chip,
        "ram_gb": specs.ram_gb,
        "active_engine": active,
        "recommended_engine": recommended,
        "engines": engines,
    }


@register("models_switch_engine")
def models_switch_engine(payload, ctx):
    return ctx.switch_engine(payload["engine"])
