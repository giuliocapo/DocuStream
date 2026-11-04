"""
anythingllm_client.py - AnythingLLM REST API connector for DocuStream.

Talks to a local AnythingLLM instance (default http://localhost:3001/api/v1)
to push finished Markdown transcripts into a chosen Workspace so LanceDB
can embed them for Chronological RAG.

Endpoints covered:

* GET  /v1/auth                                - ping / API-key validation
* GET  /v1/workspaces                          - list existing Workspaces
* POST /v1/document/upload                     - upload a transcript (.md)
* POST /v1/workspace/{slug}/update-embeddings  - move the uploaded document
  into the Workspace and trigger vector (re)embedding.

Every method fails soft: errors raise ``AnythingLLMError`` with a
human-readable message so the UI can warn the user and keep the transcript
safely on disk instead of crashing.
"""

import json
import mimetypes
from pathlib import Path

import requests


DEFAULT_BASE_URL = "http://localhost:3001/api/v1"
REQUEST_TIMEOUT = (5, 60)     # (connect, read) seconds
PING_TIMEOUT = (3, 5)


class AnythingLLMError(Exception):
    """Raised for any connectivity or API-level problem with AnythingLLM."""


class AnythingLLMClient:
    """Thin, defensive wrapper around the AnythingLLM developer API."""

    def __init__(self, base_url: str = DEFAULT_BASE_URL, api_key: str = ""):
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.api_key = (api_key or "").strip()

    # ------------------------------------------------------------------ #
    # Low-level plumbing                                                  #
    # ------------------------------------------------------------------ #

    def _headers(self) -> dict:
        headers = {"accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _request(self, method: str, path: str, timeout=REQUEST_TIMEOUT,
                 **kwargs):
        url = f"{self.base_url}/{path.lstrip('/')}"
        try:
            response = requests.request(
                method, url, headers=self._headers(),
                timeout=timeout, **kwargs,
            )
        except requests.ConnectionError as exc:
            raise AnythingLLMError(
                f"Cannot reach AnythingLLM at {self.base_url}. "
                "Is AnythingLLM Desktop running?"
            ) from exc
        except requests.Timeout as exc:
            raise AnythingLLMError(
                f"AnythingLLM did not respond in time ({url})."
            ) from exc
        except requests.RequestException as exc:
            raise AnythingLLMError(f"HTTP error talking to AnythingLLM: "
                                   f"{exc}") from exc

        if response.status_code == 401:
            raise AnythingLLMError(
                "AnythingLLM rejected the API key (401 Unauthorized). "
                "Check Settings > AnythingLLM > API Key."
            )
        if response.status_code >= 400:
            snippet = response.text[:300]
            raise AnythingLLMError(
                f"AnythingLLM returned {response.status_code} for "
                f"{method} {path}: {snippet}"
            )
        try:
            return response.json()
        except ValueError:
            return {"raw": response.text}

    # ------------------------------------------------------------------ #
    # Public endpoints                                                    #
    # ------------------------------------------------------------------ #

    def ping(self) -> bool:
        """Return True if the API answers and the key is accepted."""
        try:
            self._request("GET", "/auth", timeout=PING_TIMEOUT)
            return True
        except AnythingLLMError:
            # Some builds answer 200 on / without /auth; fall back gently.
            try:
                self._request("GET", "/workspaces", timeout=PING_TIMEOUT)
                return True
            except AnythingLLMError:
                return False

    def get_workspaces(self) -> list:
        """Return a list of workspaces: [{'name': ..., 'slug': ...}, ...]."""
        data = self._request("GET", "/workspaces")
        workspaces = data.get("workspaces", [])
        result = []
        for ws in workspaces:
            name = ws.get("name")
            slug = ws.get("slug")
            if name and slug:
                result.append({"name": name, "slug": slug})
        return result

    def upload_document(self, md_path) -> dict:
        """Upload a Markdown transcript to AnythingLLM's document library.

        Returns the uploaded document descriptor, which includes the
        ``location`` string needed by ``add_to_workspace``.
        """
        md_path = Path(md_path)
        if not md_path.exists():
            raise AnythingLLMError(f"Transcript not found: {md_path}")

        mime = mimetypes.guess_type(md_path.name)[0] or "text/markdown"
        with open(md_path, "rb") as fh:
            files = {"file": (md_path.name, fh, mime)}
            data = self._request("POST", "/document/upload", files=files)

        if not data.get("success", True):
            raise AnythingLLMError(
                f"Upload rejected: {data.get('error', 'unknown error')}")

        documents = data.get("documents") or []
        if not documents:
            raise AnythingLLMError(
                "Upload succeeded but AnythingLLM returned no document "
                "descriptor; cannot continue with embedding."
            )
        return documents[0]

    def add_to_workspace(self, slug: str, doc_location: str) -> bool:
        """Move an uploaded document into a Workspace and re-embed vectors."""
        if not slug:
            raise AnythingLLMError("No workspace slug provided.")
        payload = {"adds": [doc_location], "deletes": []}
        data = self._request_json("POST",
                                  f"/workspace/{slug}/update-embeddings",
                                  payload)
        if isinstance(data, dict) and data.get("success") is False:
            raise AnythingLLMError(
                f"Embedding update rejected: "
                f"{data.get('message', 'unknown error')}")
        return True

    # ``requests.request(headers=...)`` is set in _request; to inject
    # Content-Type for JSON posts we route through this tiny shim so the
    # public methods above stay one-liners.
    def _request_json(self, method: str, path: str, payload: dict):
        url = f"{self.base_url}/{path.lstrip('/')}"
        headers = self._headers()
        headers["Content-Type"] = "application/json"
        try:
            response = requests.request(method, url, headers=headers,
                                        data=json.dumps(payload),
                                        timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            raise AnythingLLMError(f"HTTP error talking to AnythingLLM: "
                                   f"{exc}") from exc
        if response.status_code >= 400:
            raise AnythingLLMError(
                f"AnythingLLM returned {response.status_code} for "
                f"{method} {path}: {response.text[:300]}"
            )
        try:
            return response.json()
        except ValueError:
            return {"raw": response.text}

    # ------------------------------------------------------------------ #
    # High-level convenience                                              #
    # ------------------------------------------------------------------ #

    def ingest_transcript(self, md_path, workspace_slug: str) -> str:
        """Upload a transcript and embed it into ``workspace_slug``.

        Returns the document location that was embedded. Raises
        AnythingLLMError on any failure - callers should catch it, keep the
        local .md, and inform the user.
        """
        document = self.upload_document(md_path)
        location = document.get("location")
        if not location:
            raise AnythingLLMError(
                "Uploaded document has no 'location'; cannot embed.")
        self.add_to_workspace(workspace_slug, location)
        return location
