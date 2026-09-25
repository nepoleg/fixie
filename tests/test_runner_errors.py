import io
import tarfile

import pytest
from docker.errors import NotFound
from requests.exceptions import ConnectionError as RequestsConnectionError, ReadTimeout

import fixie.runner as runner
from fixie.runner import RunnerError, kill_ticket, run_ticket, running_tickets
from fixie.settings import Settings


def _settings(**overrides):
    fields = dict(
        ado_org_url="https://dev.azure.com/org",
        ado_project="P",
        ado_repo="R",
        ticket_tag="fixie",
        default_base_branch="dev",
        allowlist=(),
        claude_model="haiku",
        docker_image="fixie-agent:latest",
        mirror_volume="mirror-vol",
        gradle_ro_volume="gradle-vol",
        ticket_timeout_s=1,
        max_attempts=3,
        owner_email="owner@example.com",
        state_secret="s",
        azure_token=None,
        claude_oauth_token=None,
        ado_feed_token=None,
    )
    fields.update(overrides)
    return Settings(**fields)


class _FakeVolumes:
    def __init__(self, existing):
        self._existing = existing

    def get(self, name):
        if name in self._existing:
            return object()
        raise NotFound(f"no such volume: {name}")

    def create(self, name, labels=None):
        self._existing.add(name)


class _FakeContainer:
    def __init__(self, name, wait_results, out_files=None):
        self.name = name
        self._wait_results = list(wait_results)
        self._out_files = out_files
        self.killed = False
        self.removed_with = None
        self.remove_raises = None

    def put_archive(self, path, data):
        pass

    def start(self):
        pass

    def wait(self, timeout=None):
        outcome = self._wait_results.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def kill(self):
        self.killed = True

    def logs(self, stdout=True, stderr=True):
        return b""

    def get_archive(self, path):
        if self._out_files is None:
            raise NotFound(path)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            for name, content in self._out_files.items():
                data = content.encode() if isinstance(content, str) else content
                info = tarfile.TarInfo(name=name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        return [buf.getvalue()], {}

    def remove(self, force=True):
        if self.remove_raises is not None:
            raise self.remove_raises
        self.removed_with = force


class _FakeContainers:
    def __init__(self, container=None, list_result=None):
        self._container = container
        self.created = []
        self.list_calls = []
        self._list_result = list_result or []

    def create(self, image, **kwargs):
        self.created.append((image, kwargs))
        return self._container

    def list(self, **kwargs):
        self.list_calls.append(kwargs)
        return self._list_result


class _FakeClient:
    def __init__(self, container=None, existing_volumes=(), list_result=None):
        self.volumes = _FakeVolumes(set(existing_volumes))
        self.containers = _FakeContainers(container, list_result)
        self.closed = False

    def close(self):
        self.closed = True


def _valid_result():
    return {"out/result.json": '{"status": "done", "summary": "s", "questions": []}'}


def test_running_tickets_ignores_removed_race(monkeypatch):
    fake = _FakeClient(list_result=[])
    monkeypatch.setattr(runner.docker, "from_env", lambda: fake)

    assert running_tickets() == set()
    assert fake.containers.list_calls[0]["ignore_removed"] is True
    assert fake.closed is True


def test_kill_ticket_ignores_removed_race(monkeypatch):
    fake = _FakeClient(list_result=[])
    monkeypatch.setattr(runner.docker, "from_env", lambda: fake)

    kill_ticket("42")
    assert fake.containers.list_calls[0]["ignore_removed"] is True
    assert fake.closed is True


def test_wait_read_timeout_is_treated_as_timeout(monkeypatch, tmp_path):
    container = _FakeContainer("fixie-42-abcd1234", wait_results=[ReadTimeout(), {"StatusCode": 137}])
    fake = _FakeClient(container=container, existing_volumes={"mirror-vol", "gradle-vol"})
    monkeypatch.setattr(runner.docker, "from_env", lambda: fake)

    (tmp_path / "ticket.md").write_text("x\n", encoding="utf-8")
    outcome = run_ticket("42", tmp_path, "start", "dev", _settings())

    assert outcome.timed_out is True
    assert outcome.exit_code == 137
    assert outcome.result is None
    assert container.killed is True
    assert container.removed_with is True
    assert fake.closed is True


def test_connection_error_during_wait_is_treated_as_timeout(monkeypatch, tmp_path):
    container = _FakeContainer(
        "fixie-42-abcd1234", wait_results=[RequestsConnectionError(), {"StatusCode": 137}]
    )
    fake = _FakeClient(container=container, existing_volumes={"mirror-vol", "gradle-vol"})
    monkeypatch.setattr(runner.docker, "from_env", lambda: fake)

    (tmp_path / "ticket.md").write_text("x\n", encoding="utf-8")
    outcome = run_ticket("42", tmp_path, "start", "dev", _settings())

    assert outcome.timed_out is True
    assert outcome.exit_code == 137


def test_container_remove_exception_does_not_mask_outcome(monkeypatch, tmp_path, caplog):
    container = _FakeContainer(
        "fixie-42-abcd1234", wait_results=[{"StatusCode": 0}], out_files=_valid_result()
    )
    container.remove_raises = RequestsConnectionError("boom")
    fake = _FakeClient(container=container, existing_volumes={"mirror-vol", "gradle-vol"})
    monkeypatch.setattr(runner.docker, "from_env", lambda: fake)

    (tmp_path / "ticket.md").write_text("x\n", encoding="utf-8")
    outcome = run_ticket("42", tmp_path, "start", "dev", _settings())

    assert outcome.ok is True
    assert "not removed" in caplog.text
    assert fake.closed is True


def test_run_ticket_closes_client_on_success(monkeypatch, tmp_path):
    container = _FakeContainer(
        "fixie-42-abcd1234", wait_results=[{"StatusCode": 0}], out_files=_valid_result()
    )
    fake = _FakeClient(container=container, existing_volumes={"mirror-vol", "gradle-vol"})
    monkeypatch.setattr(runner.docker, "from_env", lambda: fake)

    (tmp_path / "ticket.md").write_text("x\n", encoding="utf-8")
    run_ticket("42", tmp_path, "start", "dev", _settings())

    assert fake.closed is True


def test_delete_ticket_volume_wraps_connection_error(monkeypatch):
    class _BoomVolumes:
        def get(self, name):
            raise RequestsConnectionError("boom")

    fake = _FakeClient()
    fake.volumes = _BoomVolumes()
    monkeypatch.setattr(runner.docker, "from_env", lambda: fake)

    with pytest.raises(RunnerError, match="cannot delete volume"):
        runner.delete_ticket_volume("42")
    assert fake.closed is True
