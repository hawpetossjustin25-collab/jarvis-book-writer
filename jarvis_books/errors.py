class JarvisError(Exception):
    """Base error for the app."""


class NotFound(JarvisError):
    pass


class StorageError(JarvisError):
    pass


class ValidationError(JarvisError):
    pass
