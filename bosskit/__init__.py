from packaging import version

__version__ = "0.1.0"
safe_version = __version__

try:
    from bosskit._version import __version__
except ImportError:
    pass

if type(__version__) is not str:
    __version__ = safe_version + "+type"
else:
    try:
        if version.parse(__version__) < version.parse(safe_version):
            __version__ = safe_version + "+less"
    except Exception:
        __version__ = safe_version + "+parse"

__all__ = [__version__]
