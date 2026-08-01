#!/bin/sh
# The only shell script in the runner (SPEC §4.4).
#
# git calls this with the prompt text as $1 and expects the answer on stdout.
# The token file is read on EVERY invocation, so the manager re-minting the
# per-run installation token by atomic rename is picked up mid-run without
# restarting anything.
case "$1" in
    Username*) printf 'x-access-token\n' ;;
    *) cat /run/secrets/git_token ;;
esac
