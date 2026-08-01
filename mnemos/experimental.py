"""Shared guard for unfinished, explicitly unsupported prototypes."""


class ExperimentalFeatureUnavailable(RuntimeError):
    """Raised when an unfinished prototype is invoked."""


def unavailable(feature: str):
    raise ExperimentalFeatureUnavailable(
        f"{feature} is experimental and unavailable in the supported Mnemos 0.2.x surface."
    )
