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

## Product Principles

- Expose what the agent is doing and the evidence it leaves.
- Preserve the backend's approval and verification rules.
- Distinguish live observations, unavailable capabilities, and illustrative demo data.
- Make VM capacity and active workloads visible beside agent work.

## Assumptions

Desktop is the primary operating surface; mobile supports inspection and quick actions. Visual choices are delegated through the broad implementation request; optional workflow clarification was offered. The existing Svelte 5, TypeScript, Vite 8 stack is retained.
