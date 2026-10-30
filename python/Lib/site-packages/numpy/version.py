
"""
Module to expose more detailed version info for the installed `numpy`
"""
version = "2.5.4"
__version__ = version
full_version = version

git_revision = "a3d0ef1df045b858e68115001fdabc930bb3b68c"
release = 'dev' not in version and '+' not in version
short_version = version.split("+")[0]
