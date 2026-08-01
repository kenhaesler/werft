# Runner images

The capable dev box the agent works in (SPEC §4).

## The invariant

**No agent-authored bytes ever enter an image build** [CD M4]. Every input under
this directory is Werft-owned and human-authored; the build context is the image
directory itself. Agents mutate the *running container* — which is destroyed
after the run — and never influence the image.

This is why the Dockerfile lives in Werft's own repository and not in a managed
project repository: Werft is never an onboarded project of itself, so nothing an
agent can write is ever a build input.

## Building

Built **on the VM by Werft**, never pulled from a registry (SPEC §4.1 —
this is what resolves [BP§14] defect #1: no registry delivery path is needed):

```sh
docker build -t werft-runner-base:$(date +%F) runners/base
docker image inspect werft-runner-base:$(date +%F) --format '{{index .RepoDigests 0}}'
```

Containers are created **by digest only**. `werft.runner.create_body` rejects a
tag outright, which closes the tag-hijack path; the digest is recorded on each
run so the outcome record doctrine #4 depends on is never confounded by a
silently-changed image.

## What is in it, and why

| Ingredient | Reason |
|---|---|
| Rocky Linux 10 (digest-pinned) | matches the host distro (SPEC §2) |
| git, chromium-headless, Node 24, uv, Python | the "capable" half of a capable dev box |
| `@anthropic-ai/claude-code` (pinned) | the provider CLI (SPEC §5) |
| npm `min-release-age=7` (global config) and `/etc/uv/uv.toml` `exclude-newer = "7 days"` | dependency cooldown — SPEC §2 requires it **baked into the image, not offered as advice**, because under capable boxes it is the only supply-chain damper on agent-initiated installs |
| adapter venv at `/opt/werft/adapter`, read-only | outside every writable prefix, so a run cannot reach the adapter's own interpreter mid-run |
| `git-askpass.sh` | reads `/run/secrets/git_token` per invocation, so the manager's token re-mint by rename is picked up mid-run (SPEC §4.4) |

## What is *not* in it

No credentials, ever — **no credential may enter an image layer**; both the
GitHub token and the provider credential arrive as read-only runtime mounts
(SPEC §4.4). No Docker socket, no Werft API token, no database access.

### A trap worth recording

npm's global config is **`$PREFIX/etc/npmrc`** (`/usr/etc/npmrc` here), *not*
`/etc/npmrc`. A cooldown file written to `/etc/npmrc` is read by nothing —
`npm config get min-release-age` returns `null` and every install proceeds with
no cooldown at all, silently. The Dockerfile therefore has npm write its own
config and then asserts the value back, so a wrong key or a moved prefix fails
the **build** instead of quietly disarming the only supply-chain control the
capable-box posture has. uv's `/etc/uv/uv.toml` was verified the same way (by
planting an invalid value and confirming uv rejects it from that exact path).

## Hardening

The image is only half the story; the create-body is the other half and lives in
`manager/werft/runner/create_body.py`, where `BASE_HOST_CONFIG` is asserted
byte-for-byte by `tests/unit/test_create_body.py`. The empirically-settled
capability floor is documented and re-runnable in `scripts/capfloor.sh`.
