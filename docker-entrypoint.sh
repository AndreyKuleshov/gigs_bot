#!/bin/sh
# Run as root: ensure /data is writable by the unprivileged app user, then
# drop privileges and exec the bot. /data may have been seeded by `docker cp`
# from the outside (host uid 1000), which leaves files unreadable for the
# in-container "app" user (uid 999).

set -e

if [ -d /data ]; then
    chown -R app:app /data
fi

# Drop privileges via su (busybox + util-linux both support this form).
exec su -s /bin/sh app -c 'exec python main.py'
