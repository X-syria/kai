"""Observability primitives: metrics and tracing.

Production requirement: "no metrics/tracing export" was the blocker; this module
provides minimal, deterministic, production-safe instrumentation that never
leaks sensitive identifiers into metric labels.
""" 

__all__ = ["metrics", "tracing"]
