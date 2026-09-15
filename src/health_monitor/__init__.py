"""Health-monitor service.

One of the platform's resident services. Owns three system functions:
memory integrity checking, storage health checking, and inter-process
liveness monitoring over the platform message bus.
"""

__version__ = "2.4.1"

__all__ = ["__version__"]
