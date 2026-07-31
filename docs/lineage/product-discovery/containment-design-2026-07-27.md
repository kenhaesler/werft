# Werft — containment design for invariant I-1

- Date: 2026-07-27
- Question: *can the agent be prevented from escaping its development environment, given D1/D2
  (Werft keeps the VM; agents get per-project Docker dev environments that can install packages)?*
- Method: two adversarially-verified sweeps — 16 agents on the decision's design consequences,
  23 agents red-teaming I-1 across six escape vectors, each finding refuted by an independent
  skeptic, then a defence synthesis, then an adversarial audit **of the defence** (which broke
  seven of its own mechanisms; the corrected versions are what appear below).
- Status: design proposal. §11 is the decision queue. Nothing here is decided.
- Supersedes: the dangling forward-reference in
  [`agentic-os-gap-analysis-2026-07-27.md`](agentic-os-gap-analysis-2026-07-27.md) §8.0.1.

---

## 0. Verdict

**I-1 is satisfiable with Docker containers on a shared VM** — under six conditions (§3), with three
residual risks that cannot be eliminated and must be written into §13 (§4).

But the verdict comes with a trade the operator has to make knowingly, and it is the first thing in
this document rather than the last:

> **The containment half of I-1 is achievable. The capability half of D1 is being traded away to
> get it.** *"Each one of them can install things if needed"* is delivered for **language packages**
> and withheld for **system packages**. If that trade is unacceptable, §2.4 states what granting
> root actually costs, and it is much more than it looks.

---

## 1. The shape of the problem: data flow, not privilege

Across six independent attack vectors, **every finding that survived adversarial verification had
the same shape, and it was not a kernel shape.** The runtime container hardening holds. What breaks
I-1 is *agent-writable bytes reaching one of three places*:

1. **A container-create body.** §6.3's hardening is applied "on **every** create call — the socket
   proxy cannot enforce it, only manager code can", and §10.1 concedes the proxy "does **not**
   inspect request bodies". If project-supplied config can steer a create body, the manager does not
   even have to be *compromised* — the escape becomes a supported feature.
2. **An image build.** Build steps run as root, with default capabilities, no `no-new-privileges`,
   no `ReadonlyRootfs`, no `--user` — and, sharper than previously noted, **on the daemon's default
   bridge, not `runner_net`.** So §6.7's fail-closed guarantee, which is a *topological* property,
   does not cover build time at all.
3. **A host-side process walking an agent-writable tree.** The artifact copier, the log tailer,
   `git gc` on the mirror. The manager is the process holding every secret; handing it a hostile
   path is cheaper than breaking out.

**So the containment design is a data-flow design.** Three closures, in that order. Everything else
in this document is blast radius or denial-of-service.

Two attacks the red team rated "high" were refuted outright as *live* vectors: build-time escape
(`real=false, already_closed=true` on three separate vectors — §8.1 force-resets the run branch to
`unattended` HEAD on every dispatch, and §6.2 builds images by hand off-VM, so no agent bytes reach
a build today) and the per-project image as a durable implant. Both are **design constraints on
unbuilt work**, not defects in v1.4. That distinction matters: it means this is cheap to get right
now and expensive to retrofit.

---

## 2. The root question, decided

**Recommendation: the agent does not get root, and `ReadonlyRootfs` is not dropped.** D1 is satisfied
by **writable paths**, not by a writable filesystem and not by uid 0.

### 2.1 The case

1. **Root does not fix the blocker.** The gap analysis gives B1's blocking mechanism as the egress
   allowlist — *"No `registry.npmjs.org`, no `pypi.org`, no registry of any ecosystem"* — not
   privilege. Granting root buys **nothing** for dependency installs, which is the actual slice-1
   blocker.
2. **The system-package case is a build-time problem, and D1 already contains its answer.**
   `dnf install tshark` belongs in the pre-built image. The two halves of D1 — "pre-built
   per-project environments" and "can install at runtime" — are largely redundant, and the redundant
   half is the one that costs the invariant.
3. **Language installs need writable paths only.** `uv sync` → `.venv` in `/work` (already a rw
   bind). `npm ci` → `node_modules` in `/work`. Caches → `$HOME`, which §6.3 already makes tmpfs
   *precisely because* "CLIs scribble caches/lockfiles into home". §13 risk #3 already describes the
   runner in present tense as one "that executes agent-authored `npm/pip install`" — the architecture
   already assumes this works.
4. **Staying non-root preserves the `userns-remap` reject instead of forcing its reversal.** Its four
   premises: (a) *the VM is the declared blast-radius boundary* — **withdrawn by I-1**; (b) *runners
   already run as non-root uid 10000* — **preserved only under no-root**; (c) *read-only rootfs* —
   **preserved only under no-root**; (d) *SELinux + CapDrop=ALL + no-new-privileges* — preserved, and
   now carrying more. Granting root leaves **one of four** standing and makes `userns-remap`
   effectively mandatory — a daemon-wide setting that remaps every named volume including the
   **Postgres volume §10.1 already flags as the system's most dangerous footgun**, and that cannot be
   retrofitted onto a live install. That is the single most expensive consequence in this decision,
   and no-root avoids it entirely.

*Correction to an earlier statement in this session: I said D1 had voided the `userns-remap` reject.
That was overstated. It voids one premise; two more fall only if you choose runtime root.*

### 2.2 One empirical test to run before designing anything

**Test `unshare -Ur` inside the current runner.** Whether an unprivileged user namespace can be
created under `CapDrop=ALL` + `no-new-privileges` + Docker's default seccomp profile is an empirical
question with a large payoff either way:

- **If it works**, rootless in-container installs (`bubblewrap`, `proot`, user-namespace overlays,
  nix) may satisfy D1 with **zero relaxation of §6.3** — the cheapest possible answer to the whole
  decision.
- **If it does not**, §2's argument is stronger than stated here.

One afternoon, before any of §5 is built. This is not answerable from the documents and should not
be answered from memory.

### 2.3 What the no-root recommendation costs you

1. **A human round-trip when an agent finds a genuinely new system dependency mid-run.** Mitigation
   is cheap and doctrine-clean: the dispatcher-written `AGENTS.md` states that system packages
   cannot be installed and that the agent must emit `error{code: "needs_system_package", message:
   "<pkg>"}` in `result.json` — **a field §6.3 already defines** — and stop. Add `'needs_environment'`
   to the `runs.parked_reason` CHECK. No prose matching, no new subsystem, and you see exactly what
   to add.
2. **Projects whose test suite needs root in-container** (binding ports < 1024, iptables tests).
   They park. Per-project decision, not a fleet default.
3. **It does not deliver B3** (running the project's own `docker-compose` stack) — but **neither
   does root-in-container.** That needs a Docker socket or a nested runtime; both are separate §12
   rejects and a separate decision.

### 2.4 If root is granted anyway

In this order, never partially: (i) a per-project operator-acknowledged grant using the §3.4
pattern, never a global mode; (ii) uid 0 **plus `CapDrop=ALL`** — in a per-project image everything
under `/` is root-owned, so DAC is satisfied by ownership and `dnf` may need no capabilities at all
(**one afternoon of testing; it may make root cheap**); (iii) paired with a user namespace, set in
`install.sh` **at bootstrap before the first `docker compose up`, never retrofitted**, with
`userns_mode: host` declared for postgres, manager, socket-proxy, egress-proxy, dns-guard and
local-inference.

> **Verify before building:** whether the pinned Docker CE offers *per-container* user-namespace
> mapping or only daemon-wide `userns-remap` with a `--userns=host` opt-out. Understanding is the
> latter, with per-container being a Podman capability. This materially changes the design.

---

## 3. The six conditions

| # | Condition | Why it is load-bearing |
|---|---|---|
| **C1** | The container-create body is a pure function of **enumerated** Werft-owned state. No byte originating in a managed repository or run workspace reaches it. | Manager code is the *only* enforcement point (§10.1). Note the enumeration: "the `runs` row" is **not** safe — it carries `result JSONB` (agent-written) and reaches `backlog_items.body` (issue text, the first-named injection vector) by FK. Permit **columns**, not rows. |
| **C2** | No agent-authored bytes enter an image build, wherever the build runs. | Build time is strictly weaker than run time on every axis, including network topology. |
| **C3** | Host-side readers of agent-writable trees never traverse non-regular files. | The manager holds every secret. `shutil.copytree` over an agent-controlled artifact dir is a textbook confused deputy. |
| **C4** | Cross-run separation is **doubled**: per-run network *and* per-run uid, not SELinux MCS alone. | §6.3 chowns every run dir to the same `10000:10000`, so MCS is currently the *sole* mechanism implementing I-1's "no control over any other run's workspace" — and `runner_net` is shared, so the clause also fails at layer 3. |
| **C5** | `CapDrop=ALL`, `no-new-privileges`, `ReadonlyRootfs` and non-root are never relaxed; writable **paths** replace a writable **filesystem**. | Docker's default seccomp profile is capability-conditioned — `mount`, `unshare`, `setns`, `pivot_root` are gated on `CAP_SYS_ADMIN`. Restore the default 14 caps and `/proc`, `/sys` and the mount namespace reopen. |
| **C6** | `local-inference` authenticates, per run. | §10.1 puts it on `runner_net` with no stated auth, and §6.6 option 2 ("strongest") has it holding the real provider secret. Unauthenticated, it falsifies I-1's credential clause by itself. |

---

## 4. Residual risks — for §13, not for hiding

**R-A · A runc/kernel escape is now unbounded by a second wall.** I-1 removed the VM as the declared
boundary. §12 carries the revisit trigger ("a runc-escape CVE lands with no cheaper mitigation") on
two entries — but **nothing monitors it**: no alert in §9.5, no runbook row in §10.7. Irreducible
without a second runtime, which §12 rejects and D2 settled.

**R-B · I-1's credential clause is already false in the shipped spec, independent of this decision.**
§6.6 trust model 3 mounts one `werft-creds-<provider>` named volume into **every** runner of that
provider. There is no per-run provider credential to isolate. Either §6.6's preference order becomes
binding, or I-1 must be amended to say *"…the credentials of any other run, except a shared
whole-account provider session where one exists."* **This is an edit to an operator-declared hard
requirement and is therefore a decision for you, not a risk-list entry** — it appears in §11.

**R-C · The manager is the whole of I-1's enforcement.** §10.1 says it plainly. The honest statement
is not "the correctness of one Python module" but "**the manager is never compromised *and* its
create-body code is correct**" — a conjunction. See §5's G2 for the one control that sits below it.

**What is explicitly not claimed:** with C1–C6 the agent has **no reachable path** to the VM that
does not require a kernel or runtime 0-day. It does not have *provable* impossibility. Say that in
§13 rather than implying a stronger property than SELinux + seccomp + capabilities + namespaces can
deliver.

---

## 5. The control set

Corrected against the adversarial audit — seven of the defence's original mechanisms were broken and
are replaced here.

### MUST — I-1 fails without these

**M1 · Create-body input purity.** New normative paragraph in §6.3: *"Every field of a
container-create call derives exclusively from Werft-owned state — an enumerated column set on
`projects`, `/opt/werft/config/`, and an enumerated column set on `runs`. No byte originating in a
managed repository or a run workspace is ever an input, directly or by transformation."* Grant
storage follows the §8.6 step-4 precedent (test-path globs already register via
`onboard-register`) and the §5.5 `routing.yaml` precedent. *Cost: a schema decision made once.*

**M2 · Un-grantable set — absent from the vocabulary, not operator-gated.** The gap analysis's own
P4 example lists `runtime: {nested_containers, root_in_namespace}` as grantable; I-1 says the agent
has **no** control over the container runtime. **These conflict and I-1 wins.** Not expressible at
any privilege level: `Privileged`, any `CapAdd`, `Devices`, `DeviceCgroupRules`, `VolumesFrom`,
`Runtime`, `CgroupParent`, `Sysctls`, `{Pid,Network,Ipc,Userns,Cgroup}Mode=host`, `SecurityOpt ∈
{seccomp=unconfined, apparmor=unconfined, label=disable}`, published ports, `ExtraHosts`, extra
network endpoints, any bind resolving outside this run's directory. *Cost: one sentence — a smaller
vocabulary is cheaper than a larger one.*

**M3 · Whole-body equality assertion — plus explicit negative assertions.** §6.3's test changes from
*"the dict is present"* to *`create_body == BASE | delta`*, `delta.keys() ⊆ CLOSED_DELTA_KEYS`, every
value in range, **any unknown key is a failure**. *Audit correction:* equality alone passes trivially
if a maintainer adds `Privileged: true` to `BASE`, so it must be paired with direct negative
assertions on the final body (`"Privileged" not in body`, `CapAdd == []`, no `*Mode == "host"`, no
`SecurityOpt` containing `unconfined`/`label=disable`). Plus a property test over the whole grant
space, and an import-linter contract that `runner/` cannot import anything that reads the workspace.

**M4 · No agent-authored bytes in any image build.** §6.2 gains: *"Build inputs come only from
Werft-owned paths. No path any run can write ever enters a build, wherever the build runs."* Note
the load-bearing rule is **not** "keep `BUILD=0`" — that governs only the manager→proxy path, and an
operator obeying it perfectly can still hand-build from a repo-resident Dockerfile. Keep `BUILD=0`
as the second wall, not the wall.

**M5 · Resolve §6.2's three-way contradiction.** It asserts all three of *"Rebuilt **only** by
hand"*, *"Images are built by Werft's own CI"*, and *"No private registry — one VM, local daemon
cache"*. CI-built images with no registry have **no delivery path to the VM**. §10.5 already ships
`ghcr.io/…/werft-manager`, so "no private registry" must mean *no self-hosted registry service*.
Pick one and say so; amend the §12 entry.

**M6 · Per-run network, on static per-slot subnets.** `docker network create werft-run-<slot>`
(`internal: true`), runner plus egress-proxy, dns-guard and local-inference attached, removed on
cleanup. `NETWORKS=1 POST=1 DELETE=1` are already enabled and §6.1 already does an explicit
create/rm lifecycle. *Audit corrections:* dynamic subnets would break §6.7's **static** squid config
(`acl src <subnet>` cannot be templated per run without making the manager a config generator for a
pipeline-wide SPOF), **and** would falsify the "BASE never varies" claim because `Dns` would change
per run. Fix both with static per-slot subnets (`10.90.<slot>.0/24`) and static per-slot IPs for
dns-guard and egress-proxy. Confirm dnsmasq is not started with `bind-interfaces` or it will not
answer on hot-attached interfaces.

**M7 · Per-run uid.** `--user 10000+slot`, run dir chowned to match, so DAC provides a second wall
beside MCS. *Audit correction: this is not "one arithmetic change".* No slot concept exists —
§10.4 enforces `MAX_CONCURRENT_RUNS` against the DB, not against containers. Correct cheap form:
`runs.slot SMALLINT` with a partial unique index `WHERE status IN ('claimed','running')` — a direct
mirror of `ux_runs_one_active_per_item` — allocated **inside the existing claim CAS transaction**.
Plus four pre-created users in `runners/base`, or `getpwuid`-dependent tooling and `$HOME`
resolution break.

**M8 · Host-side readers never follow links.** `os.walk(followlinks=False)` + `lstat`; copy only
regular files and directories; everything else skipped with the truncation event §3.6.3 already
defines; the artifact cap enforced as a **running total during** the walk (this is what turns
"symlink a directory to `/`" from a disk-fill into a bounded no-op); modes stripped; the artifact
path validated once at onboarding. Explicit prohibition in the text: **not `cp -r`, not `rsync -a`,
not `tar -C`** — the shortcut a solo operator reaches for under pressure. *Dropped as
over-engineering:* `openat2(RESOLVE_BENEATH)`, per-component `O_NOFOLLOW`, `st_nlink` checks — no
TOCTOU racer exists (§6.1 orders die → inspect → read → `rm`, so the copy happens after the
container is dead), Python 3.14 has no stdlib `openat2`, and hardlinks cannot cross a mount
boundary. **Also resolve here:** §3.6.3 as written *cannot work* — `workspace/` is mounted `:Z`
("private label, single container"), so the manager gets EACCES reading the artifact directory
inside it. Decide deliberately; do **not** "fix" it by downgrading to `:z`.

**M9 · `local-inference` authenticates, per run.** Manager mints a key at dispatch, written beside
`secrets/git_token` with the same 0400/chown/shred discipline; vLLM `--api-key` or a LiteLLM virtual
key with a spend cap; reject client-supplied `api_base`. *Unresolved contradiction to settle in the
same edit:* §10.1 gives `local-inference` `nets: runner_net` only, while §6.6 option 2 requires it to
reach a real provider API — **the strongest credential posture in the architecture is not buildable
on the stated topology**, and the obvious fix (dual-homing it) is the worst possible change for
containment. *Audit correction:* the accompanying "no `runner_net` member is ever dual-homed" rule
is self-refuting as stated — §10.1 gives `egress-proxy: nets: runner_net, mgr_egress, internet`. It
**is** dual-homed, and is today the single L3 pivot between the runner network and the manager's.
Restate it as a named, accepted exception and put the pivot in §13.

**M10 · CODEOWNERS on instruction files only.** Extend §8.6 step 2's existing CODEOWNERS block to
`/.werft/**`, `/AGENTS.md`, `/CLAUDE.md`, `/.mcp.json`, `/.aider.conf.yml`. A run PR touching them
parks with `parked_reason='merge_blocked'` — a value §4.3 already defines and already classifies as
"the protection mechanism firing **BY DESIGN**". *Audit corrections, both important:* **drop
`/Dockerfile*` and `/.dockerignore`** — they are ordinary project source (the pilot ships a
`docker-compose.yml`), so §8.2's own reason for rejecting CODEOWNERS on test paths ("good PRs touch
tests") applies to them and every legitimate PR would park. And **do not have the dispatcher delete
a repo-resident `AGENTS.md`** — on a project with one committed (common in 2026), every run's diff
then deletes a CODEOWNERS-protected file and every run parks. Write the canonical context outside
the working copy and pass it by flag.

### SHOULD — defer into §12 with revisit triggers, not ship as a checklist

The audit's strongest process point: 6 MUST + 16 SHOULD is *a subsystem*, and §12's ledger exists
because that is how v1 accreted. These are ranked by value-per-line; the top four are worth doing
immediately.

| # | Control | Closes |
|---|---|---|
| **S2** | **Revoke the installation token on teardown and at the 45-min re-mint** — one `DELETE /installation/token` in the handler that already removes the container | The per-run token outlives its container by up to ~60 min. **Highest value-per-line in the report.** |
| **S3** | **Attenuate the per-run token to `contents: write`** — GitHub's mint endpoint accepts a permissions subset; the manager already opens PRs and comments itself | Cross-run branch poisoning, agent-authored issue comments |
| **S16** | **Pin `EXEC=0 VOLUMES=0 BUILD=0 SYSTEM=0` in `tests/architecture_spec.test.mjs`** | Verified: the suite contains **no** `EXEC`/`BUILD`/`Privileged`/`CapDrop` assertion. The line holding the build plane closed is unpinned, right as it is being designed against. *~2 minutes.* |
| **G2** | **`"no-new-privileges": true` in `/etc/docker/daemon.json`** — `install.sh` already writes and asserts that file for `{"selinux-enabled": true}` | The **only** below-the-manager control available at zero cost. Partial answer to R-C, which otherwise claims no such control exists. |
| S1 | Split squid ACLs by source network; deny private destinations ahead of the allowlist | `ntfy.sh`/`api.telegram.org` are on the *shared* allowlist — a credential-less exfil sink for every runner |
| S6 | Persistence/cache design (answers D7): per-project, `:ro` in the runner, content-addressed stores only | A poisoned cache survives every `docker rm`. *Audit caveat: "written only by the manager" is probably not implementable as stated — it would require the manager to run `npm`/`uv` against repo-resident lockfiles, i.e. agent-authored bytes driving the process holding every secret. Verify empirically first.* |
| S7/F2 | No `{@html}` for any run-derived string; `Content-Disposition: attachment` + `nosniff` on artifacts | Stored XSS against the origin that hosts the **Promote** button — the single human gate. `log.jsonl` is the larger surface and exists on *every* run |
| S8 | Pin the mount topology: `task.json` and `secrets/` as separate **read-only directory** mounts | A single-file bind does not follow a host-side rename, so the 45-min token re-mint silently fails and every run past 60 min 401s at push |
| S14 | Make §6.5's "prefers" and §6.6's "prefer" **binding** | Both are load-bearing in §13 #3's containment list while being written as preferences |
| S15 | Fix §6.5's self-contradiction on `CLAUDE.md` | It runs claude with `--bare` *to skip `CLAUDE.md` discovery* while writing a `CLAUDE.md`. Both are asserted; one must go |
| S11/S12 | CVE-watch appended to Appendix A's existing monthly item; one concurrency assertion added to §11's existing rehearsal | R-A has a trigger nobody watches; `:Z` isolation is untested and now carries I-1's cross-run clause |

### Dropped from the defence as broken or disproportionate

- **Named volumes for the install prefix** — auto-created on container create, but removal needs
  `DELETE /volumes/{name}`, which `VOLUMES=0` blocks. One orphaned volume per run, unremovable
  through the sanctioned control plane. **Use a bind mount from the run directory instead.**
- **`/usr/local` as the writable prefix** — the adapter is **PID 1** (§6.5) and enforces the 90-min
  ceiling, the tree-kill, and the §6.6 token redaction. A writable `/usr/local` hands the agent the
  adapter's own `site-packages` and `bin` mid-run. **The defence picked the one prefix that
  maximises damage.** Adapter goes in a root-owned venv outside every writable prefix; the install
  prefix points into the run directory.
- **`StorageOpt.size`** — bounds the container's *writable layer*, which `ReadonlyRootfs` makes
  nearly unused; everything fillable is tmpfs (already sized) or a host bind. Use `statvfs`/`du` in
  the log reader the manager already runs every 500 ms.
- **`runs.touches_env`** — redundant once M10 lands; the column would always read false.
- **Denylist of forbidden flags** — replaced by M2 (smaller vocabulary) + M3 (equality + negatives).
- **A pull-through registry mirror justified as a cache-poisoning control** — its real justification
  is the exfiltration case (D3); stacking it here borrows weight it has not earned.
- **Bit-reproducible builds**, **a custom seccomp profile**, **the `exhausted_until` 24 h clamp**
  (contradicts `weekly_cap_runs` aggregating over 168 h).

---

## 6. What §6.3's hardening dict becomes

```
BASE (never varies, never grantable, asserted by equality + explicit negatives):
  CapDrop         = ["ALL"]                        # unchanged
  CapAdd          = []                             # empty, permanently
  SecurityOpt     = ["no-new-privileges:true"]     # label=disable never present
  ReadonlyRootfs  = true                           # KEPT — writable PATHS, not a writable /
  User            = "1000<slot>:1000<slot>"        # NEW (M7)
  NetworkMode     = "werft-run-<slot>"             # NEW, static per-slot subnet (M6)
  Dns             = [<dns-guard @ static slot IP>] # static, so BASE really is invariant
  PortBindings    = {}
  Privileged      = false
  Devices, DeviceCgroupRules, VolumesFrom, Runtime,
  CgroupParent, Sysctls, ExtraHosts,
  {Pid,Ipc,Userns,Cgroup}Mode                      = ABSENT (not false — absent)
  Labels          = {"werft.run_id": <id>}         # the §6.1 events filter

WRITABLE SURFACE (the D1 delta — paths, not a filesystem):
  bind  runs/<id>/workspace  → /work         rw, :Z
  bind  runs/<id>/task.json  → /task.json    ro     # separate mount (S8)
  bind  runs/<id>/secrets    → /run/secrets  ro     # DIRECTORY, so re-mint by rename works (S8)
  bind  runs/<id>/prefix     → <install prefix> rw  # NOT /usr/local, NOT a named volume
  tmpfs /tmp                 size=1g, nosuid, nodev
  tmpfs $HOME                size=512m, nosuid, nodev   # now SIZED — it is charged to the
                                                        # memory cgroup and OOMs a big npm ci
  volume werft-cache-<slug>  → <cache paths>  ro    # optional, per-project (S6)
  env: NPM_CONFIG_PREFIX, PYTHONUSERBASE, PIP_PREFIX, CARGO_HOME, UV_CACHE_DIR → writable paths

CLOSED TYPED DELTA (the only per-project variance; ranges, not free values):
  Image      digest form only, "…@sha256:…"; tags rejected
  NanoCpus / Memory / PidsLimit / Mounts ⊆ generated writable-path set
```

Two corrections carried from the audit into the ranges:

- **`PidsLimit` must rise from 256** — package managers fork heavily, and the failure is invisible
  to every classification mechanism Werft has.
- **The ranges need admission control or they are a DoS vector.** §10.4 sizes the VM at 16 vCPU /
  64 GB with a strict sum of ~12/~23 at four runners. Four grants at a naive top-of-range
  (8 vCPU / 16 GB) is 32 vCPU / 64 GB, and neither Postgres nor the manager has a *reservation* —
  only a limit. P1/D8 (a scheduler with admission control) is **undecided**, so either the validator
  caps a grant at `host_budget / MAX_CONCURRENT_RUNS`, or the range stays at 2/4 until D8 is
  answered.

**What the unit test must assert:** equality against `BASE | delta`; every delta key in the closed
set and every value in range; **explicit negative assertions on the final body**; a property test
over the entire grant space; mount-source containment after `realpath`; and document locks in
`tests/architecture_spec.test.mjs`.

---

## 7. The build-declaration design

*"A human can change a project's environment easily; an agent cannot."* No new services, no new
daemons, no new runtime.

**7.1 · The declaration is a manifest, not a Dockerfile, and it lives in Werft-owned state.**
`/opt/werft/config/environments/<slug>.yaml`, bind-mounted read-only into the manager exactly like
`routing.yaml`, version-controlled in Werft's own repo — which §10.5 keeps structurally out of agent
reach (*"Werft is not an onboarded project of itself — ever"*). Allowlisted keys only: distro package
names, toolchain versions, service images by digest, environment variable **names** (never values),
the writable-path list, a resource request inside the closed range. **No `FROM`. No `RUN`. No `COPY`.
No build args. No create flags.** Pydantic with `extra='forbid'`.

This is the load-bearing structural bit: **approval can never mean "approve arbitrary root code",
only "approve a package name."** One schema decision closes the agent-authored-`RUN` case, the
hostile-`FROM` case and the baked-credential case, with zero runtime machinery.

**7.2 · The project may *request*, never *declare*.** P3's requirement is real. Satisfy it without
giving the repo write access to a build input: the project may carry `.werft/env-request.yaml`, read
**for display only, never as a build input** — the same firewall §6.4 applies to logs. The poller
notices a change → dashboard banner + alert. You run `werftctl env approve <slug> --sha <hash>`,
which renders the compiled Dockerfile for you to read, records `projects.env_manifest_sha256`, and
builds. This is the discovery spec §3.4 acknowledgement pattern exactly: *warn, accept once, store
with the configuration, invalidate on change.*

*Audit correction:* the spec must say **which ref** the poller reads the request from. If it is the
run branch, an agent can manufacture operator attention on demand — not doctrine #5 by the letter,
but squarely against its rationale. Read from `unattended` post-merge, where M10's glob applies.

**7.3 · Werft compiles; the operator builds; the manager never holds the build endpoint.** Compile
the manifest into a Dockerfile against a pinned `FROM werft-runner-base@sha256:…`. Run
`make build-env <slug>` — the same verb §6.2 already mandates, with an argument. **`BUILD=0` never
moves**, so no manager bug or compromise can build agent-authored content: the capability is not on
the manager's side of the proxy at all. The build context root is a Werft-side directory containing
only the compiled Dockerfile.

*Optional and better for dev↔oracle parity:* build in Werft's own Actions CI → ghcr digest → daemon
pull. This uses §8.2's own decisive reasoning (keep agent-adjacent execution off the VM) and pairs
with the pilot's need for a pinned tshark in **both** environments. Requires M5, and one addition:
**extend §8.6 3a's workflow hardening to Werft's own image-build workflow** — today the spec hardens
the oracle it does not own while leaving unstated the build it does.

**7.4 · Verification.** Manifest schema `extra='forbid'`; rendered Dockerfile shown at approval;
digest recorded in `projects.env_image_digest` and **containers created by digest only, never by
tag**; image digest journalled per run (same shape as `provider_chain` and `routing_rules_hash`);
`gitleaks` over the rendered Dockerfile; and **§6.6 gains one sentence — *"No credential may enter
an image layer; if a build ever needs one, BuildKit `--mount=type=secret` only"*** — §6.6 defines
exactly two trust models, both runtime mounts, and an image layer is an unruled third.

**7.5 · The invariant to write down, because absence is not a control.** Into §6.2 and locked in the
test suite: *"The image an agent runs in is always fixed before the container starts, is never
rebuilt from anything the current run produced, and no build input ever originates from a managed
repository."* Today this is guaranteed by §8.1's force-reset and §6.2's hand-built images — that is
*incidental* closure, and a product-discovery document is not inside the structural lock README
line 42 advertises. Move the sentence into the architecture.

---

## 8. Reject-ledger impact (§12) — every entry checked

| Entry | Premise changed? | Action |
|---|---|---|
| **`userns-remap`** | **Yes — 1 of 4 withdrawn; 3 of 4 if root is granted** | **Re-argue, keep rejected.** New reason: *"the container is now the only wall, but the agent stays non-root with `ReadonlyRootfs` intact, so the two premises that mattered survive."* New revisit trigger: **"the operator grants container-root to any project"** — not just a CVE |
| **gVisor / Kata / microVM** | **Yes — 1 of 4 withdrawn.** *"A multi-tenant threat model Werft doesn't have"* is no longer true: I-1 names "any other run's workspace" and "the credentials of any other run", which **is** a multi-tenancy requirement between concurrent runs | **Re-argue, keep rejected** on the surviving three. Note its trigger is now the only external one covering R-A — pair it with the CVE-watch |
| **Custom seccomp profiles** | Sharpened, not withdrawn | Keep. Restate: *"the default profile plus a strict capability allowlist is stronger and cheaper than a hand-authored one."* Add `seccomp=unconfined` to M2's un-grantable set — the cheapest one-line way to reopen `mount`/`unshare` |
| **Private registry** | **Yes** — contradicts §6.2's own "built by Werft's CI", and §10.5 already ships via ghcr | **Amend** to "no *self-hosted* registry service" (M5) |
| **Registry-proxy / cooldown** | **Yes** — argued on the inbound half only | **Re-open on the exfil case.** New trigger: *"fires the moment any package registry appears on the §6.7 allowlist."* State honestly that a GET/HEAD-only mirror is a bandwidth *reduction*, not a closure, and that packument/simple-index responses carry absolute upstream URLs, so a plain reverse proxy needs response rewriting |
| **docker-in-docker** | Unchanged; **pressure increases** (the pilot ships a compose file) | Keep, and state that no capability grant may enable it — under I-1 a nested runtime **is** control over the container runtime |
| **Warm container pools** | **Strengthened on new grounds** | Keep, add the security reason: a warm pool is now also a cross-run persistence channel |
| **Self-hosted CI runners** | **Unchanged and reinforced** | Keep — §8.2's *"self-hosting that execution on the same VM as the manager and its database is a lateral-movement gift"* is the same argument as M4. Cite it as precedent |
| **Auto-updating CLI versions** | Unchanged, reinforced | Keep; it governs image rebuild cadence |
| **NEW** | — | Add: *"Agent-triggered or run-branch-sourced image rebuild. Revisit trigger: never. Parking is the answer."* |
| All others (brokers, K8s, LLM routing, LLM merge logic, agent backlog, webhooks, OTel, outbox, pg_cron, RBAC, PITR, log search, diff viewers, blue/green, HA Postgres, per-token accounting, Werft managing itself) | **No premise change** | Keep, unchanged |

---

## 9. Bugs found in `ARCHITECTURE.md` v1.4

Independent of D1/D2. Each was verified against the shipped text.

1. **§6.2 asserts three incompatible things in one paragraph** — hand-built only / built by Werft's
   CI / no registry. CI-built images have no delivery path to the VM's daemon cache.
2. **§2's containers row still cites the withdrawn premise.** It rejects rootless/Podman/`userns-remap`
   because *"the VM is the declared blast-radius boundary"* — exactly what I-1 withdraws — in the
   document's most-read summary table.
3. **§4.3 vs §6.4 on `result.json` authority.** §4.3 calls `result` *display-only* and
   `error_message` *never branched on*; §6.4 has `result.json.status` → `quota_exhausted` drive chain
   fallthrough. Status already drives control flow while the column is documented as display-only.
4. **§9.1 vs §6.3 on `:Z` scope.** §6.3 puts `:Z` on `workspace/`; §9.1 says *the run directory* is
   mounted `:Z`. Read literally, §9.1 means the manager cannot read `result.json` or `log.jsonl` and
   **the entire completion path fails.** This is also the root cause of the artifact-copy problem.
5. **§6.5 self-contradiction:** runs claude with `--bare` *to skip `CLAUDE.md` auto-discovery* while
   writing a `CLAUDE.md` that imports `AGENTS.md`. Either the file is dead — and "one source of
   truth, all adapters" is false for the primary provider — or discovery is on.
6. **§10.1 vs §6.6:** `local-inference` is on `runner_net` (`internal: true`) only, while §6.6
   option 2 ("strongest") requires it to reach a real provider API. The strongest credential posture
   in the architecture is not buildable on the stated topology.
7. **§6.3 vs §6.4 on the mirror:** exit code `3 = clone failure` is in the *runner's* tier, implying
   the mirror is mounted into the container, but §6.3's mount table never lists it. If it is
   mounted, it is a cross-run persistence channel; if it is not, the exit-code tier is wrong.
8. **`tests/architecture_spec.test.mjs:30` pins `v1.4 (2026-07-25)`** — every change in this
   document bumps that version, so **the suite fails on the first edit made.** Handle deliberately in
   the same PR, as the version lock it is. Note also `:263` pins *"whole-account session is the last
   resort"*, which S14's rewrite must preserve.

---

## 10. If only six things get done

In dependency order, all reusing mechanisms the architecture already has:

1. **Run the `unshare -Ur` test** (§2.2) — one afternoon, and it may collapse the entire decision
   into "change nothing".
2. **Decide no-root and writable-paths** (§2) — free, and it preserves the `userns-remap` reject.
3. **Manifest is data, in Werft-owned state, compiled by Werft, built by hand** (§7) — free,
   decided once, before code.
4. **Equality assertion + explicit negatives + un-grantable set** (M2/M3) — hours; it is the only
   enforcement point §10.1 admits exists.
5. **Per-run network on static slot subnets + per-run uid via `runs.slot`** (M6/M7) — closes I-1's
   cross-run clause at both layers.
6. **Pin `EXEC=0 VOLUMES=0 BUILD=0 SYSTEM=0` in the test suite** (S16) — two minutes, and it is
   currently the only unpinned line holding the build plane closed.

Plus two that cost almost nothing and were missed entirely: **S2** (revoke the installation token on
teardown — it currently outlives its container by up to an hour) and **G2** (`no-new-privileges` in
`daemon.json` — the only control that sits *below* the manager).

Every one passes §14. None moves merge authority, none adds a service, none requires you to operate
anything new.

---

## 11. Decisions for the operator

| # | Decision | Why it needs you |
|---|---|---|
| **D-a** | **Accept the no-root trade?** D1's "can install things" becomes language packages only; system packages go through a manifest + rebuild with a human round-trip. | This is the capability half of your own decision being traded for the containment half of your own requirement. Only you can make that call. |
| **D-b** | **Amend I-1, or make §6.6's credential order binding?** One `werft-creds-<provider>` volume is mounted into every runner of that provider, so I-1's "credentials of any other run" is already false where trust model 3 applies. | I-1 is your stated hard requirement. Editing it is not something a risk list should do quietly. Recommendation: both — make the order binding, and state the exception until Kimi offers a scoped credential. |
| **D-c** | **Where does the environment manifest live** — Werft-side (`/opt/werft/config/`, out of agent reach, but the project no longer describes itself and every onboarding needs a hand-written file) or repo-side with a request/approve flow (§7.2)? | Recommendation is §7.2's hybrid: Werft-side truth, repo-side *request*. |
| **D-d** | **Resource ranges now, or 2 vCPU / 4 GB until D8?** The closed delta as drawn can oversubscribe the host against Postgres, which has a limit but no reservation. | Recommendation: hold at 2/4 until the scheduler question (P1/D8) is answered. |
| **D-e** | **Ship the SHOULD list, or defer it into §12 with triggers?** 6 MUST + 16 SHOULD is a subsystem, and §12 exists because that is how v1 accreted. | Recommendation: do the six in §10 plus S2 and G2; give the rest revisit triggers. |
