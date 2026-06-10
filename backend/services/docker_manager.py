import asyncio
import logging
from typing import Optional

import docker
import docker.errors

from config import settings

logger = logging.getLogger(__name__)


class DockerManager:
    def __init__(self):
        self._client: Optional[docker.DockerClient] = None
        self._port_lock = asyncio.Lock()
        self._used_vnc_ports: set[int] = set()
        self._used_novnc_ports: set[int] = set()

    @property
    def client(self) -> docker.DockerClient:
        if self._client is None:
            self._client = docker.from_env()
        return self._client

    async def _run(self, func):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, func)

    async def _allocate_ports(self) -> tuple[int, int]:
        async with self._port_lock:
            vnc = self._next_free(self._used_vnc_ports, settings.vnc_base_port + 1, 100)
            novnc = self._next_free(self._used_novnc_ports, settings.novnc_base_port + 1, 100)
            self._used_vnc_ports.add(vnc)
            self._used_novnc_ports.add(novnc)
            return vnc, novnc

    def _next_free(self, used: set[int], start: int, count: int) -> int:
        for p in range(start, start + count):
            if p not in used:
                return p
        raise RuntimeError(f"No free ports in [{start}, {start + count})")

    async def release_ports(self, vnc: Optional[int], novnc: Optional[int]):
        async with self._port_lock:
            self._used_vnc_ports.discard(vnc or 0)
            self._used_novnc_ports.discard(novnc or 0)

    def recover_ports(self, vnc: Optional[int], novnc: Optional[int]):
        if vnc:
            self._used_vnc_ports.add(vnc)
        if novnc:
            self._used_novnc_ports.add(novnc)

    async def spawn_session_container(
        self,
        session_id: str,
        api_key: str,
        model: str,
        tool_version: str,
    ) -> dict:
        vnc_port, novnc_port = await self._allocate_ports()
        name = f"computeagent-{session_id[:8]}"

        def _run():
            c = self.client.containers.run(
                image=settings.session_agent_image,
                name=name,
                detach=True,
                environment={
                    "ANTHROPIC_API_KEY": api_key,
                    "SESSION_ID": session_id,
                    "BACKEND_URL": settings.backend_internal_url,
                    "MODEL": model,
                    "TOOL_VERSION": tool_version,
                    "DISPLAY_NUM": "1",
                    "WIDTH": "1024",
                    "HEIGHT": "768",
                    "PYTHONIOENCODING": "utf-8",
                    "LANG": "C.UTF-8",
                    "LC_ALL": "C.UTF-8",
                },
                ports={
                    "5900/tcp": vnc_port,
                    "6080/tcp": novnc_port,
                },
                network=settings.docker_network,
                labels={"computeagent.session_id": session_id},
                shm_size="512m",
            )
            c.reload()
            networks = c.attrs["NetworkSettings"]["Networks"]
            ip = networks.get(settings.docker_network, {}).get("IPAddress", "")
            return c.id, ip

        try:
            container_id, container_ip = await self._run(_run)
        except Exception:
            await self.release_ports(vnc_port, novnc_port)
            raise

        return {
            "container_id": container_id,
            "container_name": name,
            "container_ip": container_ip,
            "vnc_host_port": vnc_port,
            "novnc_host_port": novnc_port,
        }

    async def stop_container(
        self,
        container_id: str,
        vnc_port: Optional[int] = None,
        novnc_port: Optional[int] = None,
    ):
        def _stop():
            try:
                c = self.client.containers.get(container_id)
                c.stop(timeout=5)
                c.remove(force=True)
            except docker.errors.NotFound:
                pass

        try:
            await self._run(_stop)
        except Exception as e:
            logger.error(f"Error stopping container {container_id}: {e}")
        finally:
            await self.release_ports(vnc_port, novnc_port)

    async def is_running(self, container_id: str) -> bool:
        def _check():
            try:
                c = self.client.containers.get(container_id)
                c.reload()
                return c.status == "running"
            except Exception:
                return False

        return await self._run(_check)


docker_manager = DockerManager()
