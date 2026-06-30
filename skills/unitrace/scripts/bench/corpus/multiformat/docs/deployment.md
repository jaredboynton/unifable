# Deployment

## Rollback

To roll back a bad release, repoint the `current` symlink at the previous
versioned directory and restart the service. The previous two versions are
always retained for exactly this reason.

## Health checks

The deploy is only considered landed once the health endpoint returns ready.
