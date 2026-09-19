#!/bin/sh
set -eu
/usr/bin/ssh-keygen -A
exec "$@"
