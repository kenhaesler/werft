# Werft — agentic-OS gap analysis

- Date: 2026-07-27
- Question put to the analysis: *what gaps remain, and what must still be defined, before Werft can
  be a real agentic OS in which it has its own operating system with full access?*
- Subjects: `ARCHITECTURE.md` v1.4, `README.md` doctrine,
  `docs/product-discovery/core-loop-proof-2026-07-26.md` v2, the pilot repository
- Status: analysis and decision list. **D1 and D2 were decided by the operator on 2026-07-27 —
  see §8.0.** The goal is **R1 (capable runner)**: Werft keeps full VM access, agents get
  per-project pre-built Docker development environments that may install packages. §1 and §3 below
  record the analysis that produced that choice and are retained as reasoning, not as open options.

---

## 0. The one-paragraph answer

The architecture describes a **narrow, hardened runner**: non-root uid 10000, `CapDrop=ALL`,
read-only rootfs, 2 vCPU / 4 GB, no Docker, no published ports, and an egress allowlist containing
GitHub, the provider APIs and two alerting hosts — **and no package registry of any kind**. "Werft
gets its own full operating system with full access" is not an increment on that; it is the
opposite posture. The good news is structural and was not obvious until this pass: **because the
oracle and branch protection live on GitHub rather than on the VM, doctrine #1 survives full agent
access to the machine** — the merge gate is not on the machine the agent would own. What does *not*
survive is everything else co-located on that VM: Postgres (the source of truth), the GitHub App
private key (fleet-wide), every provider credential at once, the audit journal, the backup
credentials, and the other in-flight runs. So the reconciling design is not "give the agent the
VM"; it is **give the agent full root on a disposable machine that holds nothing** — which is a
per-run VM, and which the architecture currently rejects on a hardware premise (§12: Kata "needs
nested virt a guest VM can't provide"). That premise, not the security model, is the real blocker,
and it is a deployment decision. Meanwhile, three gaps bite **before** any of this: the runner
cannot install project dependencies, projects cannot declare system requirements, and nothing
limits concurrent runs per project.

---

## 1. "Full access" — three readings, and which one is meant

The phrase has three distinct readings with very different consequences. The first decision is which
one is the goal.

| Reading | Meaning | Consequence |
|---|---|---|
| **R1 — Capable runner** | The agent stays in a container but gets what real work needs: root inside its own namespace, package installs, a nested container runtime, wider egress, persistent caches | Incremental. Most of the architecture survives. Roughly a slice of work |
| **R2 — Disposable machine per run** | The agent gets genuine root on a full OS that exists for one run and is destroyed after | This is what "full access" honestly means. Requires a hypervisor decision and changes the deployment target |
| **R3 — Werft's VM is the agent's VM** | Agents have full access to the machine Werft itself runs on | **Not survivable.** §4 audits exactly what breaks. This reading should be discarded explicitly rather than drifted into |

R3 is worth naming because it is what "Werft gets its own full OS with full access" reads like on
first pass, and because the README's own phrasing — *"the agents' blast radius ends at the VM
boundary"* — can be misread as already granting it. It does not: today the agent is contained by a
container *inside* the VM, and the VM is a second wall. R3 removes the first wall while keeping all
the valuables on the far side of it.

**Recommendation: R1 now, R2 as the real target.** R1 unblocks the work that is currently
impossible (§2) and is buildable inside the existing topology. R2 is the design that actually
delivers the ambition, and §3 is the fork it requires. R3 is a trap.

---

## 2. What is blocked today — the requirements list, with evidence

This is the honest list of what an agent cannot do in a Werft runner as specified. Each row is
verified against `ARCHITECTURE.md`, not assumed.

| # | Blocked capability | Mechanism blocking it | Bites |
|---|---|---|---|
| **B1** | **Install project dependencies** | §6.7's egress allowlist is `github.com`, `api.github.com`, `codeload.github.com`, `objects.githubusercontent.com`, provider API hosts, `ntfy.sh`, `api.telegram.org`. **No `registry.npmjs.org`, no `pypi.org`, no registry of any ecosystem.** Meanwhile §6.5 *requires* `npm ci` / `pip install --require-hashes` / `uv sync --frozen` | **Slice 1.** The pilot needs both `uv sync` and `npm ci` |
| **B2** | **Install system packages** | §6.3: `--user 10000:10000`, `CapDrop=ALL`, `ReadonlyRootfs`. No package manager can write | **Slice 1.** The pilot needs **tshark** (`pyshark`) |
| **B3** | **Run the project's own stack** | No Docker socket in the runner (§6.7: "Runners never see the Docker socket and can never spawn siblings"); docker-in-docker is a §12 reject | Any project with integration tests. The pilot ships a `docker-compose.yml` |
| **B4** | **Run and exercise a service it just built** | No published ports (§6.3). Loopback inside the container works; nothing else can reach it | Frontend/backend work, smoke tests |
| **B5** | **Drive a browser** | No browser in `runners/base` (§6.2); Playwright's browser download hosts are not on the allowlist | The evidence feature in the discovery spec (§3.6.3) |
| **B6** | **Read documentation or reach any API while working** | Allowlist again. An agent cannot look anything up | Quality of work on unfamiliar code |
| **B7** | **Remember anything about a project between runs** | **No persistent project context store exists anywhere in the architecture.** Every run is a fresh `--reference` clone with a dispatcher-written `AGENTS.md` built from `task.json` fields the manager already holds | Token cost (a stated top priority), and work quality |
| **B8** | **Use a GPU** | Not in the runner resource spec; §10.4 budgets a GPU only for the local-inference *service* | Local-tier viability, any ML project |

**B1 and B2 are not future concerns — they block slice 1 of the core-loop proof.** A run against
`pcapng-inspector` cannot install its own dependencies, and therefore cannot run its own tests
locally, and therefore the agent works blind until GitHub Actions tells it what broke. That is
survivable (the oracle is the gate, not the agent's local run) but it is a much weaker loop than
intended, it burns CI minutes as the only feedback channel, and it multiplies retries.

### 2.1 The registry problem is sharper than "add two hosts to the allowlist"

§6.7 anticipates extension — *"maintaining it per new ecosystem is accepted manual toil"* — and §13
#6 accepts the toil. But the toil is not the interesting part. Three things are undefined:

1. **Mechanism.** Who edits the squid config, is it per-project or global, does it require a
   container restart, is it version-controlled, does `werftctl onboard` touch it? None specified.
2. **Scope.** The allowlist is global across `runner_net`. Project A's ecosystem needs become
   project B's reachable hosts. There is no per-project egress scope, and adding one means either
   per-project networks or a proxy that authenticates the run.
3. **The exfil consequence, which §12 did not consider.** §6.7 spends considerable design effort
   closing both the IP route *and* the DNS side-channel so that *"an injected agent has neither an
   IP route nor a DNS channel out."* **A package registry is a bidirectional channel.** It accepts
   uploads and arbitrary metadata; `npm publish` of a package whose name or content encodes stolen
   data is an exfiltration path that squid's hostname allowlist cannot distinguish from `npm ci`.

   §12 rejects *"registry-proxy / cooldown / min-release-age infrastructure"* with the reason
   *"per-ecosystem services; superseded by an executed dependency-audit gate in the oracle."* That
   reasoning addresses the **inbound** threat (a malicious package arriving). It does not address
   the **outbound** one. **The reject is argued on half the threat model** and should be re-opened
   on the exfil case: a pull-through, read-only registry mirror is the control that keeps §6.7's
   fail-closed claim true once registries are reachable.

This is the sharpest single finding in this pass, because it means B1 cannot be fixed by editing a
list — fixing it properly reopens a rejected subsystem.

---

## 3. The fork: where does the agent's machine live?

R2 ("full root on a disposable machine") requires deciding where that machine comes from. The
architecture's current answer is "nowhere, by design", and its stated reason is a hardware premise:

> §12: *"Kata needs nested virt a guest VM can't provide"* — strengthened by the currency audit,
> which notes nested virt is *"needed when Kata runs inside a guest VM without direct CPU access,
> i.e. Werft's own case."*

So the blocker is **not** the security model. It is that Werft is specified to run *inside* a VM,
and a guest cannot host guests without nested virtualization. Four ways out:

| Option | What it means | Cost | Verdict |
|---|---|---|---|
| **O1 — Enhanced container** (R1) | Root inside a user namespace, a nested container runtime that does not require `--privileged`, per-project egress scope, persistent caches | Lowest. Fits the existing topology, no deployment change | **Recommended first step.** Unblocks B1–B4 and B7 |
| **O2 — Bare metal + per-run microVM** | Move Werft off a guest VM onto a real machine; run each agent in a Firecracker/Cloud-Hypervisor microVM with full root | Deployment target changes from "a VM" to "a machine". New image-build pipeline for VM rootfs. Real work | **The honest R2.** This is what "its own full OS" means |
| **O3 — Nested virt on the current host** | Ask the hypervisor to expose nested virtualization, then run microVMs inside the guest | Depends entirely on the host. Performance penalty. May simply be unavailable | Check first — it is a cheap question with a possibly-free answer |
| **O4 — Ephemeral cloud VM per run** | Provision a throwaway cloud instance per run | Breaks self-hosting, adds per-run cost and a cloud credential, adds inbound surface | **Contrary to the affordability goal** (answer 7). Not recommended |

**Undefined and needed before O2/O3 can be chosen:**

- Does the current hypervisor expose nested virt? (O3 is free if yes, dead if no.)
- Boot-time budget per run. The architecture already engineered cold start down (§6.1: pre-built
  images, bare mirrors, `--reference` clones). A VM boot plus toolchain availability must be
  measured, not assumed — and if it is slow, §12's warm-pool reject gets a revisit trigger.
- Image pipeline: today `make build-runners` builds four container images by hand (§6.2). VM
  rootfs images are a different build, a different pinning story, and a different rollback story.
- Disk budget: per-run VM images plus artifacts plus paused-run retention (spec §3.5.6) plus
  mirrors plus backups, against §10.4's 500 GB.
- How the manager controls the machines. Today it drives the Docker Engine API through a socket
  proxy with a deliberately narrow allowed-endpoint set (§10.1). A VM manager is a different
  control plane with a different confinement story, and §10.1's honest caveat — *"a compromised
  manager could still create a privileged container"* — needs restating for VMs.

---

## 4. Containment audit: what full access breaks, and what survives

This is the analysis that decides whether R3 can ever be entertained, and it is the reason R2 is
shaped the way it is.

### 4.1 What survives — and why this is genuinely good news

**Doctrine #1 survives full agent access to the machine**, because the gate is not on the machine:

- The oracle runs on **GitHub-hosted runners** (§8.2), off Werft's infrastructure entirely.
- Branch protection is enforced **server-side by GitHub** — required checks in strict mode,
  include-admins, no force-push (§8.1).
- The GitHub App deliberately holds **no `administration:write`** (§8.6), specifically so that a
  leaked key cannot rewrite branch protection.

An agent with root on the Werft machine still cannot make a red branch green, because it cannot
reach the thing that decides. That is a strong structural property and it was bought by an earlier
decision made for a different reason (keeping semi-untrusted execution off the VM). **It is worth
recognising explicitly, because it means the ambition is not doctrine-hostile.**

**The corollary must be stated with equal force:** under full access, GitHub's server-side
protection becomes the **only** enforcing wall. Every Werft-side control degrades to advisory. So
the protections in §8.1/§8.2/§8.6 stop being defence-in-depth and become the perimeter — which
means the §8.6 onboarding attestation, the CODEOWNERS-on-workflows rule, the read-only
`GITHUB_TOKEN`, and the empirical verification that code-owner-review-at-0 actually behaves as
documented all become load-bearing rather than prudent.

### 4.2 What does not survive — the R3 audit

| Asset on the VM today | What full agent access does to it | Existing statement it violates |
|---|---|---|
| **Postgres** — the single source of truth | Agent can rewrite run state, attempts, quota ledger, promotions | §4.1 "One writer… Runners are DB-blind" — the root fix for v1's "two engines, one DB file" |
| **The audit journal** (`run_events`, logs) | The audited party can edit the audit trail | The discovery spec's entire logging premise |
| **GitHub App private key** | Agent mints installation tokens for **every** managed repo | §13 #7 "fleet-wide credential" |
| **All provider credentials** | Every provider's secret readable at once, permanently | §6.6 "**Never** co-mount two providers' whole-account secrets into one runner" |
| **Backup / restic credentials** | Offsite store reachable; backups deletable | §10.6, §13 #12 |
| **Other in-flight runs** | Run A can read, poison or kill run B's workspace and `result.json` | §6.3's per-run isolation; `:Z` private SELinux labels |
| **Werft's own substrate** | The "Werft is never an onboarded project of itself" guarantee becomes a convention, not a mechanism | README anti-goal #3; §10.5 |
| **The provider account itself** | Unbounded consumption; the §3.3 self-cap is enforced by the manager the agent now controls | Discovery spec §3.3; the user's top-priority requirement |

Note the last row especially: **quota protection — the user's stated #1 feature — is enforced in
manager code.** If the agent owns the manager's machine, the cap is advisory. R2's disposable
machine keeps the manager and its ledger on the far side of the boundary; R3 does not.

### 4.3 The resulting principle

> **Full access is granted to a machine that is disposable and holds nothing.** The manager, the
> database, the journal, the credentials and the backups are never on that machine. The oracle is
> never on that machine. What the agent gets is a real OS, real root, real network scope — and a
> guaranteed destruction at the end of the run.

Everything in §3 and §5 follows from that one sentence.

---

## 5. Missing OS primitives

"Agentic OS" is currently a positioning phrase; the architecture is a **dispatcher**. These are the
primitives an OS has that Werft does not yet define. Each is a gap in its own right, independent of
the full-access question.

### P1 — A scheduler with policy, not a fixed pool
Today: `MAX_CONCURRENT_RUNS = 4`, every runner fixed at 2 vCPU / 4 GB / pids 256, and a
`runs.priority SMALLINT` column with no documented algorithm consuming it. Undefined: per-task
resource requests, oversubscription, preemption, fair-share across projects, backpressure when the
host is loaded, and what `priority` actually does. A build that needs 8 GB has no way to say so.

### P2 — Per-project concurrency and work-level conflict avoidance
**Verified gap: nothing limits concurrent runs per project.** `ux_runs_one_active_per_item`
prevents two runs on one *issue*; §8.2 serializes *merges*. But four runs can branch off
`unattended` for the same project simultaneously and edit the same files. The merge serialization
then converts that into merge conflicts and parked runs — human work created by the scheduler.
Undefined: a per-project run cap, path/area leasing, or an explicit "conflicts are expected, parking
is the answer" acceptance.

### P3 — A project environment contract
**Verified gap: no mechanism exists for a project to declare what it needs.** §6.2's base image
grows *"only when a real project needs one"* and is rebuilt **by hand**. So every project's system
dependencies become manual image work with no declaration, no validation, and no per-project
isolation. The pilot needs tshark; the next project needs something else; by the fourth the base
image is a shared mutable blob that every project depends on. This does not scale past the stated
"single-digit projects", and it is the gap that most directly limits Werft's usefulness.

Needs defining: how a project declares system packages, language toolchains and versions, services
it must run to be tested, environment variables, and any credential it legitimately needs — plus how
that declaration is built, pinned, cached and rolled back.

### P4 — A capability / permission model
Today there is exactly one privilege posture: the hardening dict in §6.3, applied identically to
every run. An OS has per-process privilege. "Full access" should **not** be a global switch; it
should be a **declared, per-project capability grant**:

```
project capabilities:
  network:  { registries: [npm, pypi], hosts: [...] }
  runtime:  { nested_containers: true, root_in_namespace: true }
  resources:{ cpu: 4, memory_gb: 8, disk_gb: 20, gpu: false }
  services: [ postgres, redis ]     # brought up for integration tests
```

The pattern is already in the product: the discovery spec's §3.4 opaque-provider acknowledgement —
*warn, require explicit acceptance once, store it with the configuration, invalidate it when the
configuration changes*. **The same pattern is the right shape for capability grants**, which gives
"full access" an honest UX: it is a thing you grant a project, with the risk stated, once.

### P5 — A persistence and cache layer
**Verified gap: no persistent project context store exists.** Consequences:

- **Token cost** — every run re-discovers the repository from scratch. The discovery spec makes
  avoiding this an explicit goal ("avoid repeatedly paying for repository discovery by persisting
  structured context") with no mechanism behind it.
- **Build caches** — no npm/pip/cargo cache persists between runs, so every run re-downloads
  everything. A shared pull-through cache is *also* most of the answer to §2.1's registry problem:
  a read-only local mirror serves `npm ci` without granting the runner a bidirectional channel to
  the public registry.
- **Learned project knowledge** — what an agent discovered last time is thrown away.

Needs defining: what persists, at what scope (project / project+language / global), who may write
it, how it is invalidated, and — critically — **whether agent-written content in a persistent store
can influence later runs**, because a poisoned cache is a cross-run injection channel and would need
the same "evidence, never a control input" firewall §6.4 applies to logs.

### P6 — Device and service access
GPU is budgeted for the local-inference *service* (§10.4) but is not a runner-allocatable resource.
Projects needing a database, a message broker or any service to test against have no path (B3).

### P7 — Time
There is no scheduled or recurring work. Doctrine #5 forbids *agent-generated* backlog; it does not
obviously forbid *operator-scheduled* recurring work ("run the dependency-bump issue every Monday").
Undefined whether that is permitted, and if so how it stays inside doctrine #5's intent.

### P8 — Identity
One operator, one bearer token, no RBAC (§5.4, §12). Correct at current scale. Undefined for
anything beyond one person, and "OS" framing invites that question.

---

## 6. Contracts still owed

The discovery spec already names six (provider capability, quota telemetry, runner, journal,
checkpoint, continuation). The OS framing adds five, and they are the ones that make the rest
buildable:

| Contract | Answers | Depends on |
|---|---|---|
| **Project environment** | What a project needs to build, test and run; how it is pinned and cached | P3 |
| **Capability grant** | What a run is *allowed* to do; how the operator grants it and how the grant is invalidated | P4 |
| **Resource request** | What a task asks for; how the scheduler admits or queues it | P1 |
| **Persistence** | What survives between runs, at what scope, written by whom, and whether it may influence later runs | P5 |
| **Machine lifecycle** | How an agent machine is created, monitored, drained, destroyed, and proven destroyed | R2 / §3 |

The last one carries a subtlety worth stating now: today, run cleanup is `docker rm` plus a
shredded token (§6.3, §6.6). For a disposable VM, "destroyed" must be **verifiable** — a run that
believes its machine was destroyed but was not is a credential-leak path across runs. The proof of
destruction belongs in the journal, not in an assumption.

---

## 7. Gaps that bite before any of this

Independent of the OS ambition, this pass surfaced three items that affect slice 1 of the core-loop
proof and should be folded into the discovery spec's open questions:

1. **B1 — the runner cannot install project dependencies.** No registry is on the egress allowlist
   while §6.5 mandates lockfile installs. Either the allowlist is extended (with §2.1's exfil
   consequence accepted or mitigated), or the runner installs nothing and the agent works without
   ever running the tests locally. **This must be decided before the first run, not discovered
   during it.**
2. **B2/P3 — the pilot needs tshark, and there is no way for it to say so.** Today the answer is
   "hand-edit the base image", which works exactly once.
3. **P2 — no per-project run concurrency limit.** Four concurrent runs on one project is the
   default configuration and is a merge-conflict generator. A per-project cap of 1 for the proof is
   a one-line policy and avoids attributing scheduler-caused conflicts to agent quality.

There is also a fourth, smaller one worth recording: **§6.5's lockfile-only install policy is
currently unsatisfiable for the pilot**, which declares every Python dependency as a floating range
with no lockfile. That is already slice-0 work in the discovery spec (§4), but the *reason* it
matters is stronger than "determinism": without a lockfile there is nothing for `--require-hashes`
to verify, so the supply-chain control §13 #3 relies on is inoperative.

---

## 8. Decision queue

### 8.0 D1 and D2 — DECIDED (operator, 2026-07-27)

> **D1:** *"The goal is that Werft itself has full system access, but the agent itself gets their own
> docker container. Optimally Werft creates pre-built docker-container development environments for
> each agent to use and each one of them can install things if needed."*
>
> **D2:** *"We should stay with docker containers. Everything is disposable that way and the main VM
> doesn't get destroyed (even though not that much of a problem — I have an enterprise storage that
> makes snapshots each hour)."*

**Resolution.** The goal is **R1 (capable runner)**, not R2. R3 is discarded — the manager, database,
journal, credentials and backups stay on the far side of the container boundary, and the agent never
gets the Werft machine itself. Werft (the manager) retains full access to the VM; the agent's
disposability is provided by the container lifecycle, not by destroying the host.

**What this settles:**

- **O2/O3/O4 are off the table.** No microVMs, no bare-metal move, no per-run cloud VMs. The nested-virt
  question (old D2) is moot and needs no answer.
- **§12's Kata/gVisor/microVM reject stands unchanged** — its reasoning was never the blocker; the
  hardware premise simply no longer matters.
- **The container boundary is now the *only* isolation boundary between an agent and the VM**, where
  previously it was one of two walls with the VM as a declared second. This raises what the §6.3
  hardening dict is carrying, at the same moment the decision requires relaxing part of it.
- **Snapshots cover destruction, not disclosure.** Hourly enterprise-storage snapshots are a genuine
  and material improvement to the recovery story — they tighten §10.6's accepted RPO of ≈24 h
  (nightly `pg_dump`) to ≈1 h for VM-level damage. They do **not** mitigate credential theft or data
  exfiltration: a leaked provider session or App key cannot be un-leaked by restoring a snapshot.
  Both properties should be written into §10.6/§13 rather than one standing in for the other.

### 8.0.1 Containment invariant (operator, 2026-07-27) — binding

> **"We must make sure the agent can't escape its development environment. Only Werft itself has
> control over the whole VM. The agent does not."**

This is stated as a **hard requirement, not a preference**, and it is the constraint that all of §10
must satisfy. Written as an invariant:

> **I-1.** An agent's container is the boundary of everything the agent can affect. The agent may
> hold full control *inside* its development environment and **no** control over the VM, the
> manager, the database, the journal, the credentials of any other run, any other run's workspace,
> or the container runtime itself.

Three consequences follow immediately, and they constrain the design rather than merely describing
it:

1. **"The container is now the only wall" is no longer an acceptable framing.** §8.0 noted that this
   decision removes the second wall (VM-as-boundary) that §6.3's hardening previously leaned on.
   Under I-1 the remaining wall must be *load-bearing on its own*, which means the hardening set
   cannot simply be relaxed to permit installs — what is removed must be replaced.
2. **Container root and host root must be provably different things.** If the agent gets uid 0
   inside the container in order to install system packages, then the mechanism that keeps that from
   being uid 0 on the VM has to be named. §12 rejected `userns-remap` (v1.4) on a premise —
   *"runners already run as non-root uid 10000"* — that this decision removes.
3. **Escape is not only a runtime question.** If Werft builds a per-project image from a declaration
   that an agent can edit, the agent's content executes **as root on the VM at build time**, outside
   any container hardening. That is not an escape from the sandbox; it is never entering one. Any
   design where the environment declaration is agent-writable and built on the VM violates I-1
   before a single container starts.

**What it opens.** The decision is settled; its consequences are not. Per-project images that can
install packages at runtime touch the privilege model (§6.3), the image build and provenance chain
(§6.2/§10.1), dev-environment↔CI-oracle parity (§8.2), the egress allowlist and its exfiltration
posture (§6.7/§12), and the project-environment contract that does not yet exist (P3).

Those consequences, and the control set that satisfies I-1, are analysed in
**[`containment-design-2026-07-27.md`](containment-design-2026-07-27.md)** — the output of two
adversarially-verified sweeps (16 agents on design consequences, 23 red-teaming I-1 across six
escape vectors, plus an adversarial audit of the resulting defence). Its headline results:

- **I-1 is satisfiable with Docker containers**, under six conditions and with three stated residuals.
- **The containment problem is a data-flow problem, not a privilege problem.** Every attack that
  survived verification had the same shape: agent-writable bytes reaching a container-create body,
  an image build, or a host-side process walking an agent-writable tree.
- **The recommended answer needs no relaxation of §6.3's hardening at all** — D1 is satisfied by
  writable *paths*, not by a writable filesystem or uid 0 — which also preserves the `userns-remap`
  reject rather than forcing its reversal (correcting consequence 2 above: the reject loses one
  premise, not all of them, and loses two more only if runtime root is chosen).

### 8.1 Remaining queue

| # | Decision | Why it comes first |
|---|---|---|
| ~~D1~~ | ~~Which reading is the goal?~~ | **Decided — R1** (§8.0) |
| ~~D2~~ | ~~Does the hypervisor expose nested virtualization?~~ | **Moot — staying on containers** (§8.0) |
| **D3** | **Registry access**: extend the allowlist, or stand up a pull-through mirror (reopening the §12 reject on the exfil case, §2.1)? | Blocks slice 1 either way |
| **D4** | **Project environment contract** (P3): how does a project declare system deps and services? | Blocks the pilot (tshark) and every project after it |
| **D5** | **Per-project run concurrency** (P2): cap at 1 for the proof, or accept conflicts as normal? | One-line policy, large effect on the proof's signal quality |
| **D6** | **Capability-grant model** (P4): is "full access" a per-project grant with a stored acknowledgement, or a global mode? | Determines whether R1/R2 stay operable by one person |
| **D7** | **Persistence scope** (P5): what survives between runs, and may agent-written content influence later runs? | Token cost, cache correctness, and a cross-run injection channel |
| **D8** | **Resource model** (P1): fixed slots, or per-task requests with a real scheduler? | Needed before any project exceeds 2 vCPU / 4 GB |
| **D9** | If R2: **machine lifecycle contract**, including verifiable destruction | Cannot be retrofitted safely |
| **D10** | Restate the boundary in `README.md`: the oracle is **never** on the agent's machine, and remains off Werft's infrastructure permanently | The one guarantee that makes full access survivable (§4.1); it should be written down as a guarantee, not left as a consequence |

**Suggested sequencing against the discovery spec's slices:** D3, D4 and D5 are slice-1 blockers and
belong in that spec's §7 immediately. D1 and D2 are strategic and can be answered in parallel
without holding up slice 0 (the pilot oracle), which remains the true critical path. D6–D9 are the
R1/R2 design work and should not begin before D1 is settled.

---

## 9. What this analysis did not cover

Stated so the gaps in the gap analysis are visible too:

- **External-fact verification.** Claims about specific hypervisor and nested-container technologies
  (Firecracker boot characteristics, Kata's nested-virt requirement in current releases, whether a
  rootless nested-container runtime meets these needs today) are carried from the project's own
  2026-07-20 currency audit or from general knowledge. They warrant the same primary-source
  fact-checking discipline that audit applied before any of them is built on.
- **Cost modelling.** Per-run VM disk and boot cost, and the CI-minutes interaction if local test
  execution reduces oracle re-runs, are unquantified.
- **The multi-phase run model** (discovery spec §3.8.2) interacts with P1 and P5 and is not analysed
  here.
- **Threat modelling of the capability grant itself.** Once a project can request nested containers
  and wider egress, the grant becomes the security boundary and deserves its own adversarial pass.
