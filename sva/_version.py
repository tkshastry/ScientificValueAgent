from warnings import warn

try:
    import dunamai as _dunamai

    __version__ = _dunamai.Version.from_any_vcs().serialize()
    del _dunamai
except ImportError:
    warn(
        "You are using a local development copy of sva,"
        "dunamai is not installed, as such __version__ cannot be"
        "detected and is set to 'dev' by default. For correct local"
        "version tracking, install dunamai locally (pip install dunamai)."
    )
    __version__ = "dev"
except Exception as err:
    warn(f"Unknown exception during versioning: {err}. Version set to 'dev'")
    __version__ = "dev"
