"""AgentTeams real-backend bridge (design §6): Team CR rendering + Matrix client.

This wires the AgentTeamsExecutor's real (non-dry-run) path. AgentTeams is
Kubernetes-native (Team/Worker CRDs reconciled by agentteams-controller) and uses
Matrix (Tuwunel) as its event source — both expose HTTP APIs, so this bridge uses
``httpx`` only (no kubernetes/matrix SDK dependency).

Verifiable here (no cluster/homeserver needed):
  - render_team_cr() produces a deterministic Team CR YAML
  - MatrixClient builds correct Matrix client-server v3 requests
Not verifiable here (requires AGENTTEAMS_CONTROLLER_URL + MATRIX_HOMESERVER):
  - live CR apply + Matrix room dispatch (gated behind env; see AgentTeamsExecutor)
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def render_team_cr(team_spec: Dict[str, Any]) -> str:
    """Render an AgentTeams Team CR (YAML) from a spec dict.

    Mirrors the agentscope-ai/AgentTeams Team CRD: apiVersion agentteams.alibaba.com/v1,
    kind Team, with workers (runtime) + skills. Deterministic — testable without a cluster.
    """
    provider = team_spec.get("provider", "default")
    runtime = team_spec.get("runtime", "hermes")
    workers = team_spec.get("workers") or []
    skills = team_spec.get("skills") or []
    name = f"xueban-{provider}".lower()

    worker_blocks = []
    for i, w in enumerate(workers):
        if isinstance(w, dict):
            wid = w.get("id") or w.get("name") or f"{name}-w{i}"
            wruntime = w.get("runtime", runtime)
            wskills = w.get("skills", skills)
        else:
            wid = f"{name}-w{i}"
            wruntime = runtime
            wskills = skills
        worker_blocks.append(
            f"  - id: {wid}\n    runtime: {wruntime}\n    skills: {_yaml_list(wskills)}"
        )
    workers_yaml = "\n".join(worker_blocks) if worker_blocks else "  []"

    return (
        f"apiVersion: agentteams.alibaba.com/v1\n"
        f"kind: Team\n"
        f"metadata:\n"
        f"  name: {name}\n"
        f"spec:\n"
        f"  manager:\n"
        f"    runtime: {runtime}\n"
        f"  workers:\n{workers_yaml}\n"
        f"  skills: {_yaml_list(skills)}\n"
    )


def _yaml_list(items: List[Any]) -> str:
    if not items:
        return "[]"
    return "[" + ", ".join(str(i) for i in items) + "]"


class _FakeAsyncHttp:
    """Minimal recorder for testing MatrixClient request construction (no network)."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self._responses: Dict[str, Any] = {}

    def set_response(self, method: str, url_suffix: str, json_body: Any) -> None:
        self._responses[f"{method} {url_suffix}"] = json_body

    async def request(self, method: str, url: str, **kwargs: Any) -> Any:
        self.calls.append({"method": method, "url": url, **kwargs})
        for key, body in self._responses.items():
            m, suffix = key.split(" ", 1)
            if m == method and url.endswith(suffix):
                return _Resp(body)
        return _Resp({})

    async def aclose(self) -> None:  # pragma: no cover
        pass


class _Resp:
    def __init__(self, body: Any) -> None:
        self._body = body

    def json(self) -> Any:
        return self._body

    @property
    def status_code(self) -> int:
        return 200


class MatrixClient:
    """Minimal Matrix client-server v3 client over httpx (injectable for tests)."""

    def __init__(
        self,
        homeserver: str,
        user: str,
        password: Optional[str] = None,
        token: Optional[str] = None,
        http_client: Any = None,
    ) -> None:
        self.homeserver = homeserver.rstrip("/")
        self.user = user
        self.password = password
        self.token = token
        self._http = http_client  # None -> lazy httpx.AsyncClient in real use
        self._txn = 0

    def _client(self) -> Any:
        if self._http is None:
            import httpx  # local import: only needed for live dispatch
            self._http = httpx.AsyncClient(timeout=30.0)
        return self._http

    def _auth_headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    async def login(self) -> str:
        url = f"{self.homeserver}/_matrix/client/v3/login"
        resp = await self._client().request(
            "POST", url,
            json={
                "type": "m.login.password",
                "identifier": {"type": "m.id.user", "user": self.user},
                "password": self.password or "",
            },
        )
        body = resp.json()
        self.token = body.get("access_token")
        return self.token

    async def create_room(self, name: str, topic: str = "", invite: Optional[List[str]] = None) -> str:
        url = f"{self.homeserver}/_matrix/client/v3/createRoom"
        resp = await self._client().request(
            "POST", url,
            json={
                "name": name,
                "topic": topic,
                "preset": "trusted_private_chat",
                **({"invite": invite} if invite else {}),
            },
            headers=self._auth_headers(),
        )
        return resp.json().get("room_id", "")

    async def send_message(self, room_id: str, body: str, msgtype: str = "m.text") -> str:
        self._txn += 1
        url = f"{self.homeserver}/_matrix/client/v3/rooms/{room_id}/send/m.room.message/{self._txn}"
        resp = await self._client().request(
            "PUT", url,
            json={"msgtype": msgtype, "body": body},
            headers=self._auth_headers(),
        )
        return resp.json().get("event_id", "")

    async def room_events(self, room_id: str, since: Optional[str] = None) -> List[Dict[str, Any]]:
        """Return timeline events for a room (Sensor pipeline source)."""
        url = f"{self.homeserver}/_matrix/client/v3/sync"
        params = {"timeout": "0"}
        if since:
            params["since"] = since
        resp = await self._client().request("GET", url, params=params, headers=self._auth_headers())
        room = resp.json().get("rooms", {}).get("join", {}).get(room_id, {})
        return list(room.get("timeline", {}).get("events", []))
