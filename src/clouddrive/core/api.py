"""Microsoft Graph API client for OneDrive operations.

Provides async methods for all OneDrive file operations:
listing, uploading, downloading, deleting, and delta queries.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from pathlib import Path, PurePosixPath
from typing import Any, AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

from clouddrive.core.auth import AuthManager
from clouddrive.core.config import GRAPH_BASE_URL

logger = logging.getLogger(__name__)

# OneDrive uses 10 MiB upload chunks for large files
UPLOAD_CHUNK_SIZE = 10 * 1024 * 1024  # 10 MiB
# Files larger than 4 MiB should use resumable upload
SIMPLE_UPLOAD_LIMIT = 4 * 1024 * 1024  # 4 MiB

# Maximum number of automatic retries on HTTP 429 rate-limiting
_MAX_RATE_LIMIT_RETRIES = 5

# Maximum pagination pages per request (prevent infinite loops)
_MAX_PAGES = 10000

# Allowed download redirect domains (Microsoft CDN endpoints)
_TRUSTED_DOWNLOAD_HOSTS = (
    ".sharepoint.com",
    ".1drv.com",
    ".microsoft.com",
    ".live.com",
    ".office.com",
    ".office365.com",
)


def _sanitize_name(name: str) -> str:
    """Sanitize a file/folder name from the API.

    Strips null bytes, control characters, and path separators that
    could be used for path traversal or filesystem exploits.
    """
    # Remove null bytes and non-printable control characters
    name = "".join(c for c in name if c.isprintable() and ord(c) != 0)
    # Remove path separators that shouldn't appear in a single name
    name = name.replace("/", "_").replace("\\", "_")
    # Remove leading dots (hidden files) and spaces
    name = name.lstrip(". ")
    # Collapse runs of dots/spaces at the end
    name = name.rstrip(". ")
    return name or "unnamed"


@dataclass
class DriveItem:
    """Represents a file or folder in OneDrive."""

    id: str
    name: str
    path: str  # Full path from drive root, e.g. "/Documents/file.txt"
    size: int = 0
    is_folder: bool = False
    modified_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    created_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    sha256_hash: str = ""
    quick_xor_hash: str = ""
    download_url: str = ""
    parent_id: str = ""
    etag: str = ""
    c_tag: str = ""

    @classmethod
    def from_api_response(cls, item: dict[str, Any]) -> DriveItem:
        """Create a DriveItem from a Microsoft Graph API response."""
        parent_ref = item.get("parentReference", {})
        parent_path = parent_ref.get("path", "")
        # Strip the "/drive/root:" prefix
        if ":" in parent_path:
            parent_path = parent_path.split(":", 1)[1]
        if not parent_path:
            parent_path = "/"

        # Sanitize the item name to prevent path injection.
        # Null bytes, path separators, and control chars are stripped.
        raw_name = item.get("name", "")
        safe_name = _sanitize_name(raw_name)

        full_path = str(PurePosixPath(parent_path) / safe_name)

        hashes = item.get("file", {}).get("hashes", {})

        modified = item.get("lastModifiedDateTime", "")
        created = item.get("createdDateTime", "")

        return cls(
            id=item["id"],
            name=safe_name,
            path=full_path,
            size=item.get("size", 0),
            is_folder="folder" in item,
            modified_time=datetime.fromisoformat(modified.replace("Z", "+00:00")) if modified else datetime.now(timezone.utc),
            created_time=datetime.fromisoformat(created.replace("Z", "+00:00")) if created else datetime.now(timezone.utc),
            sha256_hash=hashes.get("sha256Hash", ""),
            quick_xor_hash=hashes.get("quickXorHash", ""),
            download_url=item.get("@microsoft.graph.downloadUrl", ""),
            parent_id=parent_ref.get("id", ""),
            etag=item.get("eTag", ""),
            c_tag=item.get("cTag", ""),
        )


@dataclass
class DeltaPage:
    """A page of delta query results."""

    items: list[DriveItem]
    deleted_ids: list[str]
    delta_link: str | None = None  # For next delta query
    next_link: str | None = None  # For pagination within this delta


@dataclass
class DriveQuota:
    """OneDrive storage quota information."""

    total: int = 0
    used: int = 0
    remaining: int = 0
    state: str = "normal"  # normal, nearing, critical, exceeded


class GraphAPIError(Exception):
    """Error from Microsoft Graph API."""

    def __init__(self, status_code: int, error_code: str, message: str) -> None:
        self.status_code = status_code
        self.error_code = error_code
        super().__init__(f"Graph API {status_code} [{error_code}]: {message}")


class OneDriveClient:
    """Async client for Microsoft OneDrive via Graph API."""

    def __init__(self, auth: AuthManager) -> None:
        self._auth = auth
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            token = self._auth.get_access_token()
            if token is None:
                raise GraphAPIError(401, "no_token", "Not authenticated")
            self._client = httpx.AsyncClient(
                base_url=GRAPH_BASE_URL,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                },
                timeout=httpx.Timeout(30.0, read=120.0),
                verify=True,  # Enforce TLS certificate verification
            )
        return self._client

    async def _refresh_token(self) -> None:
        """Refresh the access token and update the client headers."""
        token = self._auth.get_access_token()
        if token and self._client:
            self._client.headers["Authorization"] = f"Bearer {token}"

    @staticmethod
    def _validate_download_url(url: str) -> None:
        """Validate that a download redirect URL points to a trusted Microsoft domain.

        Prevents open-redirect attacks where a compromised or spoofed API
        response could redirect downloads to a malicious server.
        """
        parsed = urlparse(url)
        if parsed.scheme != "https":
            raise GraphAPIError(
                403, "insecure_download",
                f"Download URL uses insecure scheme: {parsed.scheme}",
            )
        host = parsed.hostname or ""
        if not any(host.endswith(domain) for domain in _TRUSTED_DOWNLOAD_HOSTS):
            raise GraphAPIError(
                403, "untrusted_download_host",
                f"Download URL points to untrusted host: {host}",
            )

    @staticmethod
    def _validate_delta_link(url: str) -> bool:
        """Validate that a delta link is a legitimate Graph API URL."""
        parsed = urlparse(url)
        if parsed.scheme != "https":
            return False
        host = parsed.hostname or ""
        return host.endswith(".microsoft.com") or host.endswith(".office.com")

    async def _request(
        self,
        method: str,
        url: str,
        retry: bool = True,
        _rate_limit_attempt: int = 0,
        **kwargs: Any,
    ) -> httpx.Response:
        """Make an authenticated request with automatic token refresh."""
        client = await self._get_client()
        response = await client.request(method, url, **kwargs)

        if response.status_code == 401 and retry:
            await self._refresh_token()
            client = await self._get_client()
            response = await client.request(method, url, **kwargs)

        if response.status_code == 429:
            if _rate_limit_attempt >= _MAX_RATE_LIMIT_RETRIES:
                raise GraphAPIError(
                    429, "rate_limited",
                    f"Rate limited after {_MAX_RATE_LIMIT_RETRIES} retries",
                )
            # Rate limited — respect Retry-After header (capped at 120s)
            retry_after = min(int(response.headers.get("Retry-After", "5")), 120)
            logger.warning(
                "Rate limited (attempt %d/%d), waiting %d seconds",
                _rate_limit_attempt + 1, _MAX_RATE_LIMIT_RETRIES, retry_after,
            )
            await asyncio.sleep(retry_after)
            return await self._request(
                method, url, retry=False,
                _rate_limit_attempt=_rate_limit_attempt + 1, **kwargs,
            )

        if response.status_code >= 400:
            error_body = response.json() if response.content else {}
            error = error_body.get("error", {})
            raise GraphAPIError(
                response.status_code,
                error.get("code", "unknown"),
                error.get("message", response.text[:200]),
            )

        return response

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    # === Account & Drive Info ===

    async def get_drive_info(self) -> dict[str, Any]:
        """Get information about the user's drive."""
        resp = await self._request("GET", "/me/drive")
        return resp.json()

    async def get_quota(self) -> DriveQuota:
        """Get storage quota information."""
        info = await self.get_drive_info()
        quota = info.get("quota", {})
        return DriveQuota(
            total=quota.get("total", 0),
            used=quota.get("used", 0),
            remaining=quota.get("remaining", 0),
            state=quota.get("state", "normal"),
        )

    async def get_user_profile(self) -> dict[str, Any]:
        """Get the signed-in user's profile."""
        resp = await self._request("GET", "/me")
        return resp.json()

    # === File & Folder Operations ===

    async def list_children(self, item_id: str = "root") -> list[DriveItem]:
        """List immediate children of a folder."""
        items: list[DriveItem] = []
        url = f"/me/drive/items/{item_id}/children"

        while url:
            resp = await self._request("GET", url, params={"$top": "200"})
            data = resp.json()
            for raw in data.get("value", []):
                items.append(DriveItem.from_api_response(raw))
            url = data.get("@odata.nextLink", "")
            if url:
                # nextLink is a full URL, strip the base
                url = url.replace(GRAPH_BASE_URL, "")

        return items

    async def list_children_by_path(self, path: str = "/") -> list[DriveItem]:
        """List children of a folder by its path."""
        if path == "/" or path == "":
            return await self.list_children("root")
        # Encode the path for the API
        encoded = path.lstrip("/")
        url = f"/me/drive/root:/{encoded}:/children"
        items: list[DriveItem] = []

        while url:
            resp = await self._request("GET", url, params={"$top": "200"})
            data = resp.json()
            for raw in data.get("value", []):
                items.append(DriveItem.from_api_response(raw))
            url = data.get("@odata.nextLink", "")
            if url:
                url = url.replace(GRAPH_BASE_URL, "")

        return items

    async def get_item(self, item_id: str) -> DriveItem:
        """Get metadata for a specific item by ID."""
        resp = await self._request("GET", f"/me/drive/items/{item_id}")
        return DriveItem.from_api_response(resp.json())

    async def get_item_by_path(self, path: str) -> DriveItem:
        """Get metadata for a specific item by path."""
        encoded = path.lstrip("/")
        resp = await self._request("GET", f"/me/drive/root:/{encoded}")
        return DriveItem.from_api_response(resp.json())

    async def create_folder(self, parent_id: str, name: str) -> DriveItem:
        """Create a folder under the given parent."""
        body = {
            "name": name,
            "folder": {},
            "@microsoft.graph.conflictBehavior": "fail",
        }
        resp = await self._request(
            "POST", f"/me/drive/items/{parent_id}/children", json=body
        )
        return DriveItem.from_api_response(resp.json())

    async def delete_item(self, item_id: str) -> None:
        """Delete an item (move to recycle bin)."""
        await self._request("DELETE", f"/me/drive/items/{item_id}")

    async def move_item(
        self, item_id: str, new_parent_id: str, new_name: str | None = None
    ) -> DriveItem:
        """Move/rename an item."""
        body: dict[str, Any] = {"parentReference": {"id": new_parent_id}}
        if new_name:
            body["name"] = new_name
        resp = await self._request("PATCH", f"/me/drive/items/{item_id}", json=body)
        return DriveItem.from_api_response(resp.json())

    # === Upload ===

    async def upload_small(self, parent_id: str, filename: str, data: bytes) -> DriveItem:
        """Upload a file <= 4 MiB using simple upload."""
        resp = await self._request(
            "PUT",
            f"/me/drive/items/{parent_id}:/{filename}:/content",
            content=data,
            headers={"Content-Type": "application/octet-stream"},
        )
        return DriveItem.from_api_response(resp.json())

    async def upload_large(
        self,
        parent_id: str,
        filename: str,
        file_path: Path,
        progress_callback: Any = None,
    ) -> DriveItem:
        """Upload a large file using resumable upload session.

        Args:
            parent_id: Parent folder ID.
            filename: Name for the file in OneDrive.
            file_path: Local path to the file.
            progress_callback: Optional callable(bytes_sent, total_bytes).
        """
        file_size = file_path.stat().st_size

        # Create upload session
        body = {
            "item": {
                "@microsoft.graph.conflictBehavior": "replace",
                "name": filename,
            }
        }
        resp = await self._request(
            "POST",
            f"/me/drive/items/{parent_id}:/{filename}:/createUploadSession",
            json=body,
        )
        upload_url = resp.json()["uploadUrl"]

        # Upload chunks
        client = await self._get_client()
        bytes_sent = 0

        with open(file_path, "rb") as f:
            while bytes_sent < file_size:
                chunk = f.read(UPLOAD_CHUNK_SIZE)
                chunk_size = len(chunk)
                end = bytes_sent + chunk_size - 1

                resp = await client.put(
                    upload_url,
                    content=chunk,
                    headers={
                        "Content-Range": f"bytes {bytes_sent}-{end}/{file_size}",
                        "Content-Length": str(chunk_size),
                    },
                    timeout=httpx.Timeout(300.0),
                )

                if resp.status_code in (200, 201):
                    # Upload complete
                    return DriveItem.from_api_response(resp.json())

                if resp.status_code != 202:
                    raise GraphAPIError(
                        resp.status_code, "upload_error",
                        f"Chunk upload failed: {resp.text[:200]}"
                    )

                bytes_sent += chunk_size
                if progress_callback:
                    progress_callback(bytes_sent, file_size)

        raise GraphAPIError(500, "upload_incomplete", "Upload did not complete")

    async def upload_file(
        self,
        parent_id: str,
        filename: str,
        file_path: Path,
        progress_callback: Any = None,
    ) -> DriveItem:
        """Upload a file, automatically choosing simple or resumable upload."""
        file_size = file_path.stat().st_size

        if file_size <= SIMPLE_UPLOAD_LIMIT:
            data = file_path.read_bytes()
            return await self.upload_small(parent_id, filename, data)
        else:
            return await self.upload_large(
                parent_id, filename, file_path, progress_callback
            )

    # === Download ===

    async def download_file(
        self,
        item_id: str,
        dest_path: Path,
        progress_callback: Any = None,
    ) -> None:
        """Download a file to a local path.

        Args:
            item_id: The OneDrive item ID.
            dest_path: Local destination path.
            progress_callback: Optional callable(bytes_received, total_bytes).
        """
        resp = await self._request(
            "GET", f"/me/drive/items/{item_id}/content", follow_redirects=False
        )

        # Graph API returns a 302 redirect to the download URL
        if resp.status_code == 302:
            download_url = resp.headers["Location"]
        else:
            raise GraphAPIError(
                resp.status_code, "download_error", "Expected redirect to download URL"
            )

        self._validate_download_url(download_url)

        dest_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = dest_path.with_suffix(dest_path.suffix + ".clouddrive-tmp")

        client = await self._get_client()
        try:
            async with client.stream("GET", download_url) as stream:
                total = int(stream.headers.get("Content-Length", 0))
                received = 0
                fd = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                try:
                    with open(fd, "wb") as f:
                        async for chunk in stream.aiter_bytes(chunk_size=65536):
                            f.write(chunk)
                            received += len(chunk)
                            if progress_callback:
                                progress_callback(received, total)
                except Exception:
                    os.close(fd) if not f.closed else None
                    raise

            # Atomic rename
            tmp_path.replace(dest_path)
        except Exception:
            # Clean up partial download
            if tmp_path.exists():
                tmp_path.unlink()
            raise

    # === Delta Queries (Efficient Sync) ===

    async def get_delta(self, delta_link: str | None = None) -> DeltaPage:
        """Get changes since the last delta query.

        Args:
            delta_link: The delta link from a previous query, or None for initial sync.

        Returns a DeltaPage with changed items and a new delta link.
        """
        if delta_link:
            if not self._validate_delta_link(delta_link):
                logger.warning("Ignoring invalid delta link, starting fresh")
                url = "/me/drive/root/delta"
            else:
                url = delta_link.replace(GRAPH_BASE_URL, "")
        else:
            url = "/me/drive/root/delta"

        items: list[DriveItem] = []
        deleted_ids: list[str] = []

        pages = 0
        while url:
            if pages >= _MAX_PAGES:
                logger.warning("Delta query exceeded max pages (%d), stopping", _MAX_PAGES)
                break
            pages += 1
            resp = await self._request("GET", url, params={"$top": "200"})
            data = resp.json()

            for raw in data.get("value", []):
                if "deleted" in raw:
                    deleted_ids.append(raw["id"])
                else:
                    items.append(DriveItem.from_api_response(raw))

            next_link = data.get("@odata.nextLink", "")
            new_delta = data.get("@odata.deltaLink", "")

            if next_link:
                url = next_link.replace(GRAPH_BASE_URL, "")
            elif new_delta:
                return DeltaPage(
                    items=items,
                    deleted_ids=deleted_ids,
                    delta_link=new_delta,
                )
            else:
                break

        return DeltaPage(items=items, deleted_ids=deleted_ids)

    # === Metadata-First: Full Tree Fetch ===

    async def fetch_full_tree(
        self,
        root_id: str = "root",
        progress_callback: Any = None,
    ) -> list[DriveItem]:
        """Recursively fetch the entire file tree metadata (no file content).

        This allows us to immediately show all files/folders in the
        correct location before any actual downloads begin.
        Returns a flat list of all items in the drive.
        """
        all_items: list[DriveItem] = []
        queue = [root_id]
        total_fetched = 0

        while queue:
            parent_id = queue.pop(0)
            children = await self.list_children(parent_id)

            for child in children:
                all_items.append(child)
                total_fetched += 1
                if child.is_folder:
                    queue.append(child.id)

            if progress_callback:
                progress_callback(total_fetched, f"Discovered {total_fetched} items...")

        return all_items

    async def fetch_tree_delta(
        self,
        delta_link: str | None = None,
    ) -> tuple[list[DriveItem], list[str], str | None]:
        """Fetch complete tree via delta (more efficient than recursive listing).

        On first call (delta_link=None), returns the entire tree.
        On subsequent calls, returns only changes since last call.

        Returns: (items, deleted_ids, new_delta_link)
        """
        all_items: list[DriveItem] = []
        all_deleted: list[str] = []

        if delta_link:
            if not self._validate_delta_link(delta_link):
                logger.warning("Ignoring invalid delta link, starting fresh")
                url = "/me/drive/root/delta"
            else:
                url = delta_link.replace(GRAPH_BASE_URL, "")
        else:
            url = "/me/drive/root/delta"

        final_delta: str | None = None

        while url:
            resp = await self._request("GET", url, params={"$top": "200"})
            data = resp.json()

            for raw in data.get("value", []):
                if "deleted" in raw:
                    all_deleted.append(raw["id"])
                else:
                    all_items.append(DriveItem.from_api_response(raw))

            next_link = data.get("@odata.nextLink", "")
            new_delta = data.get("@odata.deltaLink", "")

            if next_link:
                url = next_link.replace(GRAPH_BASE_URL, "")
            elif new_delta:
                final_delta = new_delta
                break
            else:
                break

        return all_items, all_deleted, final_delta

    # === File Versioning ===

    async def get_versions(self, item_id: str) -> list[dict[str, Any]]:
        """Get version history for a file.

        Returns list of version info dicts with id, modified time, size, and author.
        """
        url = f"/me/drive/items/{item_id}/versions"
        versions: list[dict[str, Any]] = []

        while url:
            resp = await self._request("GET", url)
            data = resp.json()

            for v in data.get("value", []):
                modified_by = v.get("lastModifiedBy", {}).get("user", {})
                versions.append({
                    "id": v.get("id", ""),
                    "modified_time": v.get("lastModifiedDateTime", ""),
                    "size": v.get("size", 0),
                    "modified_by": modified_by.get("displayName", "Unknown"),
                })

            url = data.get("@odata.nextLink", "")
            if url:
                url = url.replace(GRAPH_BASE_URL, "")

        return versions

    async def download_version(
        self,
        item_id: str,
        version_id: str,
        dest_path: Path,
    ) -> None:
        """Download a specific version of a file."""
        resp = await self._request(
            "GET",
            f"/me/drive/items/{item_id}/versions/{version_id}/content",
            follow_redirects=False,
        )

        if resp.status_code == 302:
            download_url = resp.headers["Location"]
        else:
            raise GraphAPIError(
                resp.status_code, "download_error",
                "Expected redirect for version download"
            )

        self._validate_download_url(download_url)

        dest_path.parent.mkdir(parents=True, exist_ok=True)
        client = await self._get_client()
        async with client.stream("GET", download_url) as stream:
            with open(dest_path, "wb") as f:
                async for chunk in stream.aiter_bytes(chunk_size=65536):
                    f.write(chunk)

    async def restore_version(self, item_id: str, version_id: str) -> None:
        """Restore a file to a previous version."""
        await self._request(
            "POST",
            f"/me/drive/items/{item_id}/versions/{version_id}/restoreVersion",
        )

    # === SharePoint Sites & Libraries ===

    async def search_sites(self, query: str) -> list[dict[str, Any]]:
        """Search for SharePoint sites by keyword."""
        resp = await self._request(
            "GET", "/sites", params={"search": query}
        )
        sites = []
        for site in resp.json().get("value", []):
            sites.append({
                "id": site.get("id", ""),
                "name": site.get("displayName", ""),
                "url": site.get("webUrl", ""),
                "description": site.get("description", ""),
            })
        return sites

    async def get_site_drives(self, site_id: str) -> list[dict[str, Any]]:
        """List document libraries (drives) in a SharePoint site."""
        resp = await self._request("GET", f"/sites/{site_id}/drives")
        drives = []
        for d in resp.json().get("value", []):
            drives.append({
                "id": d.get("id", ""),
                "name": d.get("name", ""),
                "description": d.get("description", ""),
                "web_url": d.get("webUrl", ""),
                "quota": d.get("quota", {}),
            })
        return drives

    async def list_site_drive_children(
        self, site_id: str, drive_id: str, item_id: str = "root"
    ) -> list[DriveItem]:
        """List children in a SharePoint document library."""
        url = f"/sites/{site_id}/drives/{drive_id}/items/{item_id}/children"
        items: list[DriveItem] = []
        while url:
            resp = await self._request("GET", url, params={"$top": "200"})
            data = resp.json()
            for raw in data.get("value", []):
                items.append(DriveItem.from_api_response(raw))
            url = data.get("@odata.nextLink", "")
            if url:
                url = url.replace(GRAPH_BASE_URL, "")
        return items

    async def get_site_drive_delta(
        self, site_id: str, drive_id: str, delta_link: str | None = None,
    ) -> DeltaPage:
        """Get delta changes for a SharePoint library."""
        if delta_link:
            url = delta_link.replace(GRAPH_BASE_URL, "")
        else:
            url = f"/sites/{site_id}/drives/{drive_id}/root/delta"

        items: list[DriveItem] = []
        deleted_ids: list[str] = []

        while url:
            resp = await self._request("GET", url, params={"$top": "200"})
            data = resp.json()
            for raw in data.get("value", []):
                if "deleted" in raw:
                    deleted_ids.append(raw["id"])
                else:
                    items.append(DriveItem.from_api_response(raw))
            next_link = data.get("@odata.nextLink", "")
            new_delta = data.get("@odata.deltaLink", "")
            if next_link:
                url = next_link.replace(GRAPH_BASE_URL, "")
            elif new_delta:
                return DeltaPage(items=items, deleted_ids=deleted_ids, delta_link=new_delta)
            else:
                break

        return DeltaPage(items=items, deleted_ids=deleted_ids)

    # === Shared Items ===

    async def list_shared_with_me(self) -> list[DriveItem]:
        """List files and folders shared with the current user."""
        url = "/me/drive/sharedWithMe"
        items: list[DriveItem] = []
        while url:
            resp = await self._request("GET", url)
            data = resp.json()
            for raw in data.get("value", []):
                item = DriveItem.from_api_response(raw)
                # Mark shared items with a prefix path
                item.path = "/Shared/" + item.path.lstrip("/")
                items.append(item)
            url = data.get("@odata.nextLink", "")
            if url:
                url = url.replace(GRAPH_BASE_URL, "")
        return items

    async def get_shared_item_children(
        self, remote_item_id: str, drive_id: str
    ) -> list[DriveItem]:
        """List children of a shared folder (using the remote drive reference)."""
        url = f"/drives/{drive_id}/items/{remote_item_id}/children"
        items: list[DriveItem] = []
        while url:
            resp = await self._request("GET", url, params={"$top": "200"})
            data = resp.json()
            for raw in data.get("value", []):
                items.append(DriveItem.from_api_response(raw))
            url = data.get("@odata.nextLink", "")
            if url:
                url = url.replace(GRAPH_BASE_URL, "")
        return items
