"""Authenticated, read-only host inventory for the operator workspace.

The browser never talks to the Docker socket. Only public capacity fields
and Werft-labelled environments cross this boundary; no environment variables,
mount paths, network configuration, or unrelated containers are returned.
Workload cancellation continues through the existing run state machine.
"""

import asyncio

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from werft.runner.docker_api import DockerApiError, DockerClient

system_router = APIRouter()


class EnvironmentOut(BaseModel):
    id: str
    run_id: str
    name: str
    image: str
    state: str
    status: str


class MachineOut(BaseModel):
    name: str
    os: str
    architecture: str
    engine_version: str
    cpus: int
    memory_bytes: int
    max_concurrent_runs: int
    containers: list[EnvironmentOut]


@system_router.get("/system", response_model=MachineOut)
async def get_system(request: Request) -> MachineOut:
    try:
        async with asyncio.timeout(6):
            async with DockerClient(request.app.state.docker_url, timeout=4) as docker:
                await docker.negotiate()
                info, containers = await asyncio.gather(
                    docker.host_info(), docker.list_containers(all_=True)
                )
    except (DockerApiError, httpx.HTTPError, OSError, TimeoutError) as exc:
        raise HTTPException(
            status_code=503,
            detail="Docker host unavailable. Check the manager Docker connection.",
        ) from exc
    return MachineOut(
        name=info.get("Name") or "Docker host",
        os=info.get("OperatingSystem") or "Unknown",
        architecture=info.get("Architecture") or "Unknown",
        engine_version=info.get("ServerVersion") or "Unknown",
        cpus=info.get("NCPU") or 0,
        memory_bytes=info.get("MemTotal") or 0,
        max_concurrent_runs=request.app.state.max_concurrent_runs,
        containers=[
            EnvironmentOut(
                id=container["Id"],
                run_id=container["Labels"]["werft.run_id"],
                name=(container.get("Names") or [container["Id"][:12]])[0].lstrip("/"),
                image=container.get("Image") or "Unknown",
                state=container.get("State") or "unknown",
                status=container.get("Status") or "Unknown",
            )
            for container in containers
            if (container.get("Labels") or {}).get("werft.run_id")
        ],
    )
