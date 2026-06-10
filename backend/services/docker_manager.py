import asyncio
import logging

import docker
import docker.errors

from config import settings

logger = logging.getLogger(__name__)


class DockerManager:
    def __init__(self):
        self._client: docker.DockerClient | None = None
        self._port_lock = asyncio.Lock()
        self._used_vnc_ports: set[int] = set()

    @property
    def client(self) -> docker.DockerClient:
        if self._client is None:
            self._client = docker.from_env()
        return self._client

    async def _run(self, func):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, func)

    async def _allocate_vnc_port(self) -> int:
        async with self._port_lock:
            port = self._next_free(self._used_vnc_ports, settings.vnc_base_port + 1, 100)
            self._used_vnc_ports.add(port)
            return port

    def _next_free(self, used: set[int], start: int, count: int) -> int:
        for p in range(start, start + count):
            if p not in used:
                return p
        raise RuntimeError(f"No free ports in [{start}, {start + count})")

    async def release_vnc_port(self, port: int | None):
        async with self._port_lock:
            self._used_vnc_ports.discard(port or 0)

    def recover_vnc_port(self, port: int | None):
        if port:
            self._used_vnc_ports.add(port)

    async def spawn_session_container(
        self,
        session_id: str,
        api_key: str,
        agent_token: str,
        model: str,
        tool_version: str,
    ) -> dict:
        vnc_port = await self._allocate_vnc_port()
        # The full id: an 8-character prefix can collide and make containers.run() fail.
        name = f"computeagent-session-{session_id}"

        def _run():
            c = self.client.containers.run(
                image=settings.session_agent_image,
                name=name,
                detach=True,
                environment={
                    "ANTHROPIC_API_KEY": api_key,
                    "SESSION_ID": session_id,
                    "BACKEND_URL": settings.backend_internal_url,
                    "AGENT_TOKEN": agent_token,
                    "MODEL": model,
                    "TOOL_VERSION": tool_version,
                    "DISPLAY_NUM": "1",
                    "WIDTH": "1024",
                    "HEIGHT": "768",
                    "PYTHONIOENCODING": "utf-8",
                    "LANG": "C.UTF-8",
                    "LC_ALL": "C.UTF-8",
                },
                # The VNC server has no password, so it is published on loopback only.
                # noVNC is not published at all: nginx reaches it over the Docker network.
                ports={"5900/tcp": ("127.0.0.1", vnc_port)},
                network=settings.docker_network,
                labels={"computeagent.session_id": session_id, "computeagent.managed": "true"},
                shm_size="512m",
                mem_limit=settings.session_mem_limit,
                nano_cpus=int(settings.session_cpus * 1e9),
                pids_limit=settings.session_pids_limit,
            )
            try:
                c.reload()
                networks = c.attrs["NetworkSettings"]["Networks"]
                ip = networks[settings.docker_network]["IPAddress"]
            except Exception:
                # Don't leave a running container (holding the API key) that no session row points at.
                c.remove(force=True)
                raise
            return c.id, ip

        try:
            container_id, container_ip = await self._run(_run)
        except Exception:
            await self.release_vnc_port(vnc_port)
            raise

        return {
            "container_id": container_id,
            "container_name": name,
            "container_ip": container_ip,
            "vnc_host_port": vnc_port,
        }

    async def stop_container(
        self,
        container_id: str,
        vnc_port: int | None = None,
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
            await self.release_vnc_port(vnc_port)

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
