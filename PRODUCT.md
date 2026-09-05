# Werft

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

A self-hosting operator supervising autonomous software work on a dedicated VM.

## Product Purpose

An agentic operating system that coordinates approved work, disposable agent environments, provider quotas, evidence, and review. The requested frontend brings agent work and VM management into one modern workspace.

## Product Truth

The current backend dispatches approved GitHub issues into Docker containers. Code progresses through executed CI; bootstrap work requires operator review. Existing authenticated APIs expose runs, attempts, events, artifacts, quota, project onboarding, and lifecycle changes. VM power control, snapshots, arbitrary terminal sessions, and conversational dispatch are not current backend capabilities.

The dashboard's Activity Monitor polls `/activity` every three seconds while visible, reconnects when visibility returns, and surfaces stale data and heartbeat state. Worker operations remain process-local; durable run milestones come from the backend's run, attempt, event, and artifact records. Live tool stdout is unavailable until it is collected as an existing artifact.

## Product Principles

- Expose what the agent is doing and the evidence it leaves.
- Preserve the backend's approval and verification rules.
- Distinguish live observations, unavailable capabilities, and illustrative demo data.
- Make VM capacity and active workloads visible beside agent work.

## Brand Commitment

The operator explicitly prefers a predominantly white interface with blue AI-inspired accents. Green and olive are not the brand palette. Preserve the approved Werft logo geometry and lettering. Warning and error states retain distinct warm semantic colors. Clear visual guidance is essential: human review first, active agents next, activity last; machine and quota information remain quieter supporting context.

Run inspectors live-poll their selected run while visible and retain the last successful detail during transient reconnect failures, with a visible stale notice and update time.

## Operating Assumptions

Desktop is the primary operating surface; mobile supports inspection and quick actions. Visual choices are delegated through the broad implementation request; optional workflow clarification was offered. The existing Svelte 5, TypeScript, Vite 8 stack is retained.
