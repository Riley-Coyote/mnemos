"""Self-audit of generated text, for the advanced server and the substrate.

Modules:
- introspection: heuristic self-audit (the advanced server's
  ``mnemos_introspect``, and the substrate's introspection pass)
- introspection_api: the same audit from token logprobs, when a provider
  returns them (the substrate's introspection pass)
"""

__all__ = [
    "introspection",
    "introspection_api",
]
