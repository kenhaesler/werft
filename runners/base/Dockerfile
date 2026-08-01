# Werft runner base image — the capable dev box (SPEC §4.1).
#
# INVARIANT [CD M4], and it is the reason this file is here and not in a managed
# repository: **no agent-authored bytes ever enter an image build.** Every input
# is Werft-owned and human-authored. Agents mutate the running container; they
# never influence the image. The build context is this directory only.
#
# Built on the VM by Werft (`docker build -t werft-runner-base:<date> runners/base`),
# never pulled from a registry, and referenced by containers **by digest only**
# (the create-body rejects tags — see werft/runner/create_body.py).
#
# Base digest re-resolved 2026-08-01. The Rocky-org repo is the maintained one;
# the docker-library `rockylinux` images are not updated.
FROM rockylinux/rockylinux@sha256:827d37bc128288ccf160ee318bb3cb92d591164cb217e92f8bc61e3982ae1834

# --- Dependency cooldown (SPEC §2) -------------------------------------------
# "under capable boxes this cooldown is the only supply-chain damper on
# agent-initiated installs, so it ships in the base image, not as advice."
#
# uv reads /etc/uv/uv.toml as system configuration — verified in-image, along
# with `exclude-newer` accepting a relative duration ("7 days"), by feeding it a
# deliberately invalid value and watching it reject it from that exact file.
COPY uv.toml /etc/uv/uv.toml

# --- Toolchain ----------------------------------------------------------------
RUN dnf install -y --setopt=install_weak_deps=False \
        git tar gzip which procps-ng ca-certificates \
        epel-release dnf-plugins-core \
 && dnf config-manager --set-enabled crb \
 && dnf install -y --setopt=install_weak_deps=False chromium-headless \
 && dnf clean all

# Node 24 LTS. Rocky 10 ships Node as a plain AppStream package (default v22) and
# has no `nodejs:24` module stream, so NodeSource is the pinned route.
RUN curl -fsSL https://rpm.nodesource.com/setup_24.x | bash - \
 && dnf install -y --setopt=install_weak_deps=False nodejs \
 && dnf clean all \
 && node --version

# npm's cooldown, and the reason it is set this way: npm's global config is NOT
# /etc/npmrc — it is $PREFIX/etc/npmrc (here /usr/etc/npmrc), so a file dropped
# at /etc/npmrc is read by nothing and the cooldown would be silently inert
# (observed: `npm config get min-release-age` returned null). Letting npm write
# its own config cannot drift if the prefix changes, and the assertion below
# fails the BUILD rather than the runs if the key name ever changes.
RUN npm config set min-release-age 7 --location=global \
 && npm config set fund false --location=global \
 && npm config set audit false --location=global \
 && test "$(npm config get min-release-age)" = "7"

# uv, for Python projects.
ENV UV_NO_MODIFY_PATH=1
RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh \
 && uv --version

# The provider CLI (SPEC §5). Pinned; bumped by hand, never by a run.
ARG CLAUDE_CODE_VERSION=2.1.220
RUN npm install -g --no-fund --no-audit "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
 && claude --version

# --- The adapter ---------------------------------------------------------------
# Root-owned and outside every writable prefix: a run must not be able to reach
# the adapter's own interpreter or site-packages mid-run [CD "dropped" ledger —
# /usr/local as the writable prefix was rejected for exactly this reason].
RUN dnf install -y --setopt=install_weak_deps=False python3.12 \
 && dnf clean all \
 && python3.12 -m venv /opt/werft/adapter \
 && chmod -R a-w /opt/werft
COPY --chmod=0555 git-askpass.sh /opt/werft/git-askpass.sh

# GIT_ASKPASS reads /run/secrets/git_token on every git invocation, so the
# manager's re-mint-by-rename before expiry is picked up without restarting
# anything (SPEC §4.4).
ENV GIT_ASKPASS=/opt/werft/git-askpass.sh \
    GIT_TERMINAL_PROMPT=0 \
    NPM_CONFIG_FUND=false \
    NPM_CONFIG_AUDIT=false \
    CHROME_BIN=/usr/lib64/chromium-browser/headless_shell

WORKDIR /work
