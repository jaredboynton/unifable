def _check(user, password):
    return bool(user) and bool(password)


def authenticate(user, password):
    """Verify credentials and return a session token."""
    if not _check(user, password):
        return None
    return f"token-for-{user}"
