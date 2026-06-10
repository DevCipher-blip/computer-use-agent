import docker.errors
import pytest

from services.docker_manager import DockerManager

SPAWN = {
    "session_id": "1234abcd-0000-4000-8000-000000000000",
    "api_key": "k",
    "session_token": "t",
    "model": "claude-sonnet-4-5-20250929",
    "tool_version": "computer_use_20250124",
}


@pytest.fixture
def manager(docker_client):
    m = DockerManager()
    m._client = docker_client
    return m


async def test_ports_are_unique_and_released(manager):
    first = await manager._allocate_ports()
    second = await manager._allocate_ports()
    assert first == (5901, 6081)
    assert second == (5902, 6082)

    await manager.release_ports(*first)
    assert await manager._allocate_ports() == first


async def test_port_range_exhaustion_raises(manager):
    manager._used_vnc_ports.update(range(5901, 6001))
    with pytest.raises(RuntimeError, match="No free ports"):
        await manager._allocate_ports()


async def test_failed_spawn_releases_ports(manager, docker_client):
    docker_client.containers.run.side_effect = docker.errors.ImageNotFound("no image")
    with pytest.raises(docker.errors.ImageNotFound):
        await manager.spawn_session_container(**SPAWN)
    assert manager._used_vnc_ports == set()
    assert manager._used_novnc_ports == set()


async def test_spawn_reads_ip_from_configured_network(manager):
    info = await manager.spawn_session_container(**SPAWN)
    assert info["container_id"] == "c0ffee"
    assert info["container_ip"] == "10.9.0.7"


async def test_stop_tolerates_missing_container(manager, docker_client):
    vnc, novnc = await manager._allocate_ports()
    docker_client.containers.get.side_effect = docker.errors.NotFound("gone")
    await manager.stop_container("c0ffee", vnc, novnc)
    assert manager._used_vnc_ports == set()


async def test_recovered_ports_are_not_reused(manager):
    manager.recover_ports(5901, 6081)
    assert await manager._allocate_ports() == (5902, 6082)


async def test_container_uses_full_session_id_and_resource_limits(manager, docker_client):
    info = await manager.spawn_session_container(**SPAWN)
    kwargs = docker_client.containers.run.call_args.kwargs
    assert kwargs["name"] == f"computeagent-session-{SPAWN['session_id']}"
    assert info["container_name"] == kwargs["name"]
    assert kwargs["mem_limit"] == "2g"
    assert kwargs["nano_cpus"] == 1_000_000_000
    assert kwargs["pids_limit"] == 512


async def test_container_is_removed_if_it_never_joins_the_network(manager, docker_client):
    container = docker_client.containers.run.return_value
    container.attrs = {"NetworkSettings": {"Networks": {"bridge": {"IPAddress": "172.17.0.2"}}}}
    with pytest.raises(KeyError):
        await manager.spawn_session_container(**SPAWN)
    container.remove.assert_called_once_with(force=True)
    assert manager._used_vnc_ports == set()
