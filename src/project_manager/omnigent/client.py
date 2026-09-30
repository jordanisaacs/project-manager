"""Small stdlib client for Omnigent's first-class projects API."""

from __future__ import annotations

import builtins
import http.client
import json
import os
import subprocess
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import quote, urlsplit

from project_manager.config import Omnigent

_HTTP_BAD_REQUEST = 400


class OmnigentApiError(RuntimeError):
    """An Omnigent request or response failed."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class Project:
    id: str
    name: str
    config: dict[str, Any]


class ProjectsClient:
    """HTTP client for the subset of `/v1/projects` PM needs."""

    def __init__(self, settings: Omnigent) -> None:
        self._settings = settings
        self._headers = {
            "Accept": "application/json",
            "User-Agent": "project-manager/omnigent-sync",
        }
        token = _resolve_token(settings)
        if token is not None:
            self._headers["Authorization"] = f"Bearer {token}"
        self._parsed_url = urlsplit(settings.server_url)

    def list(self) -> builtins.list[Project]:
        body = self._request("GET", "/v1/projects")
        data = body.get("data")
        if not isinstance(data, list):
            raise OmnigentApiError("Omnigent returned an invalid project list")
        return [_parse_project(item) for item in data]

    def create(self, name: str, config: dict[str, Any]) -> Project:
        return _parse_project(
            self._request("POST", "/v1/projects", payload={"name": name, "config": config})
        )

    def update(
        self,
        project_id: str,
        *,
        name: str | None = None,
        config: dict[str, Any] | None = None,
    ) -> Project:
        payload: dict[str, Any] = {}
        if name is not None:
            payload["name"] = name
        if config is not None:
            payload["config"] = config
        return _parse_project(
            self._request(
                "PATCH",
                f"/v1/projects/{quote(project_id, safe='')}",
                payload=payload,
            )
        )

    def delete(self, project_id: str) -> None:
        self._request("DELETE", f"/v1/projects/{quote(project_id, safe='')}")

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = dict(self._headers)
        data: bytes | None = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload, separators=(",", ":")).encode()
        base_path = self._parsed_url.path.rstrip("/")
        request_path = f"{base_path}{path}"
        connection = self._connection()
        try:
            connection.request(method, request_path, body=data, headers=headers)
            response = connection.getresponse()
            raw = response.read()
        except (TimeoutError, OSError, http.client.HTTPException) as error:
            raise OmnigentApiError(
                f"could not reach {self._settings.server_url}: {error}"
            ) from error
        finally:
            connection.close()
        if response.status >= _HTTP_BAD_REQUEST:
            message = _error_message(raw) or f"HTTP {response.status} {response.reason}"
            raise OmnigentApiError(message, status_code=response.status)
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise OmnigentApiError("Omnigent returned invalid JSON") from error
        if not isinstance(parsed, dict):
            raise OmnigentApiError("Omnigent returned a non-object JSON response")
        return parsed

    def _connection(self) -> http.client.HTTPConnection:
        hostname = self._parsed_url.hostname
        assert hostname is not None
        connection_type = (
            http.client.HTTPSConnection
            if self._parsed_url.scheme == "https"
            else http.client.HTTPConnection
        )
        return connection_type(
            hostname,
            port=self._parsed_url.port,
            timeout=self._settings.timeout_seconds,
        )


def _parse_project(raw: object) -> Project:
    if not isinstance(raw, dict):
        raise OmnigentApiError("Omnigent returned an invalid project object")
    data = cast("dict[str, Any]", raw)
    project_id = data.get("id")
    name = data.get("name")
    config = data.get("config", {})
    if not isinstance(project_id, str) or not isinstance(name, str) or not isinstance(config, dict):
        raise OmnigentApiError("Omnigent returned an invalid project object")
    return Project(id=project_id, name=name, config=dict(config))


def _resolve_token(settings: Omnigent) -> str | None:
    if settings.token_env is not None:
        token = os.environ.get(settings.token_env, "").strip()
        if not token:
            raise OmnigentApiError(f"environment variable {settings.token_env} is unset or empty")
        return token
    if settings.token_command is None:
        return None
    try:
        result = subprocess.run(
            settings.token_command,
            check=False,
            capture_output=True,
            text=True,
            timeout=settings.timeout_seconds,
        )
    except subprocess.TimeoutExpired as error:
        raise OmnigentApiError("Omnigent token command timed out") from error
    except OSError as error:
        raise OmnigentApiError(f"could not run Omnigent token command: {error}") from error
    if result.returncode != 0:
        raise OmnigentApiError(f"Omnigent token command failed with exit code {result.returncode}")
    token = _token_from_output(result.stdout)
    if not token:
        raise OmnigentApiError("Omnigent token command returned no token")
    return token


def _token_from_output(output: str) -> str | None:
    value = output.strip()
    if not value:
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return value
    if isinstance(parsed, str):
        return parsed.strip() or None
    if isinstance(parsed, dict):
        for key in ("access_token", "token"):
            token = parsed.get(key)
            if isinstance(token, str) and token.strip():
                return token.strip()
    return None


def _error_message(raw: bytes) -> str | None:
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    error = parsed.get("error")
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        return error["message"]
    detail = parsed.get("detail")
    if isinstance(detail, str):
        return detail
    message = parsed.get("message")
    return message if isinstance(message, str) else None
