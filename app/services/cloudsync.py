"""Cloud folder sync - Google Drive and Microsoft OneDrive.

A supplier connects one folder, read-only. The platform mirrors it into the
credential register, classifies each file, reads where it says it can be
checked, and refreshes the organisation's profile. It then watches the folder on
a schedule or on demand, and keeps a revision history of every file it has seen.

Five rules govern it, and each one exists because the obvious alternative goes
wrong in a way the supplier would not notice until a buyer did.

**Read-only.** The platform never writes to, moves, renames or deletes anything
in the supplier's drive. Scopes are the read-only ones, and there is no code
path here that issues a write.

**The provider's file id is the identity of a file.** A rename or a move inside
the folder is the same file. Keying on the name would turn every rename into a
new credential and every renewal saved under a new filename into a duplicate.

**Unchanged means unchanged bytes.** Revision ids and modified times are used to
avoid downloading what has not changed; but a new revision whose bytes hash the
same (a sharing change, a metadata touch) does not mint a new version. Version
history should record changes to the credential, not to the drive.

**Nothing new publishes itself.** A file seen for the first time lands as a
draft for a person to confirm, exactly as a manual upload does - a classifier's
guess is not a supplier's statement. Once a person has confirmed a file, later
revisions *of that same file* are renewals and publish automatically, which is
what keeps the profile current without anybody remembering to do it. A
look-alike verification link stops that: such a revision stays a draft and
raises an action.

**Removing a file from the folder removes nothing from the register.** The
credential and its history stay; an action tells the supplier the source has
gone. Deleting a published credential because somebody tidied a folder would
withdraw evidence a buyer may be relying on.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from ..config import (CLOUD_SYNC_MAX_FILES, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET,
                      MICROSOFT_CLIENT_ID, MICROSOFT_CLIENT_SECRET,
                      MICROSOFT_TENANT, SECRET_KEY)
from ..models import (CloudConnection, Document, DocumentType, DocumentVersion,
                      SyncedFile, SyncedFileRevision, SyncRun, Task, Tenant, now)
from . import audit, authority

MAX_FILE_BYTES = 25 * 1024 * 1024
SUPPORTED = (".pdf", ".png", ".jpg", ".jpeg", ".docx", ".txt", ".tif", ".tiff")

SCHEDULES = {
    "manual": None,
    "hourly": timedelta(hours=1),
    "daily": timedelta(days=1),
    "weekly": timedelta(days=7),
}

PROVIDER_LABEL = {"google": "Google Drive", "microsoft": "Microsoft OneDrive",
                  "demo": "Demo folder"}


# --------------------------------------------------------------------------
# Token storage
# --------------------------------------------------------------------------
def _fernet():
    from cryptography.fernet import Fernet

    key = base64.urlsafe_b64encode(
        hashlib.sha256(f"{SECRET_KEY}::cloud-tokens".encode()).digest())
    return Fernet(key)


def seal(value: str | None) -> str | None:
    return _fernet().encrypt(value.encode()).decode() if value else None


def unseal(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return _fernet().decrypt(value.encode()).decode()
    except Exception:
        # SECRET_KEY changed since the token was stored. The connection needs
        # re-authorising; saying so beats failing with a cryptography error.
        return None


# --------------------------------------------------------------------------
# Providers
# --------------------------------------------------------------------------
@dataclass
class RemoteFile:
    id: str
    name: str
    path: str
    mime_type: str = ""
    revision: str = ""
    modified_at: datetime | None = None
    size: int = 0
    extra: dict = field(default_factory=dict)


class ProviderError(Exception):
    pass


class Provider:
    key = ""
    configured = False

    def authorise_url(self, redirect_uri: str, state: str) -> str: ...
    def exchange(self, code: str, redirect_uri: str) -> dict: ...
    def refresh(self, refresh_token: str) -> dict: ...
    def account(self, token: str) -> str: ...
    def folders(self, token: str, parent: str | None) -> list[dict]: ...
    def walk(self, token: str, folder_id: str) -> list[RemoteFile]: ...
    def download(self, token: str, item: RemoteFile) -> bytes: ...


def _http():
    import httpx
    return httpx.Client(timeout=30, follow_redirects=True)


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
            timezone.utc).replace(tzinfo=None)
    except ValueError:
        return None


class GoogleDrive(Provider):
    key = "google"
    # drive.readonly, not drive: the platform has no reason to be able to write.
    SCOPES = "openid email https://www.googleapis.com/auth/drive.readonly"
    FOLDER = "application/vnd.google-apps.folder"
    # Native Google formats have no bytes to download; they are exported.
    EXPORT = {"application/vnd.google-apps.document": ("application/pdf", ".pdf")}

    @property
    def configured(self):
        return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)

    def authorise_url(self, redirect_uri, state):
        return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
            "client_id": GOOGLE_CLIENT_ID, "redirect_uri": redirect_uri,
            "response_type": "code", "scope": self.SCOPES, "state": state,
            "access_type": "offline", "prompt": "consent",
            "include_granted_scopes": "true"})

    def _token(self, data):
        with _http() as client:
            response = client.post("https://oauth2.googleapis.com/token", data={
                **data, "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET})
        if response.status_code != 200:
            raise ProviderError(f"Google token endpoint returned "
                                f"{response.status_code}: {response.text[:200]}")
        return response.json()

    def exchange(self, code, redirect_uri):
        return self._token({"code": code, "redirect_uri": redirect_uri,
                            "grant_type": "authorization_code"})

    def refresh(self, refresh_token):
        return self._token({"refresh_token": refresh_token,
                            "grant_type": "refresh_token"})

    def _get(self, token, url, params=None):
        with _http() as client:
            response = client.get(url, params=params,
                                  headers={"Authorization": f"Bearer {token}"})
        if response.status_code == 401:
            raise ProviderError("unauthorised")
        if response.status_code != 200:
            raise ProviderError(f"Google Drive returned {response.status_code}: "
                                f"{response.text[:200]}")
        return response

    def account(self, token):
        data = self._get(token, "https://www.googleapis.com/drive/v3/about",
                         {"fields": "user(emailAddress)"}).json()
        return data.get("user", {}).get("emailAddress", "")

    def _list(self, token, query, fields):
        out, page = [], None
        while True:
            params = {"q": query, "fields": f"nextPageToken,files({fields})",
                      "pageSize": 200, "supportsAllDrives": "true",
                      "includeItemsFromAllDrives": "true"}
            if page:
                params["pageToken"] = page
            data = self._get(token, "https://www.googleapis.com/drive/v3/files",
                             params).json()
            out.extend(data.get("files", []))
            page = data.get("nextPageToken")
            if not page:
                return out

    def folders(self, token, parent):
        rows = self._list(token, f"'{parent or 'root'}' in parents and "
                                 f"mimeType='{self.FOLDER}' and trashed=false",
                          "id,name")
        return [{"id": r["id"], "name": r["name"]} for r in rows]

    def walk(self, token, folder_id, prefix=""):
        files = []
        rows = self._list(token, f"'{folder_id}' in parents and trashed=false",
                          "id,name,mimeType,modifiedTime,md5Checksum,"
                          "headRevisionId,size,version")
        for row in rows:
            path = f"{prefix}/{row['name']}" if prefix else row["name"]
            if row["mimeType"] == self.FOLDER:
                files.extend(self.walk(token, row["id"], path))
                continue
            files.append(RemoteFile(
                id=row["id"], name=row["name"], path=path,
                mime_type=row["mimeType"],
                revision=row.get("headRevisionId") or row.get("md5Checksum")
                or str(row.get("version", "")),
                modified_at=_parse_time(row.get("modifiedTime")),
                size=int(row.get("size") or 0)))
        return files

    def download(self, token, item):
        if item.mime_type in self.EXPORT:
            mime, _ = self.EXPORT[item.mime_type]
            return self._get(token, f"https://www.googleapis.com/drive/v3/files/"
                                    f"{item.id}/export", {"mimeType": mime}).content
        return self._get(token, f"https://www.googleapis.com/drive/v3/files/{item.id}",
                         {"alt": "media", "supportsAllDrives": "true"}).content


class OneDrive(Provider):
    key = "microsoft"
    # Files.Read.All, not ReadWrite: the platform reads the chosen folder only.
    SCOPES = "offline_access openid email User.Read Files.Read.All"
    GRAPH = "https://graph.microsoft.com/v1.0"

    @property
    def configured(self):
        return bool(MICROSOFT_CLIENT_ID and MICROSOFT_CLIENT_SECRET)

    def _authority(self):
        return f"https://login.microsoftonline.com/{MICROSOFT_TENANT}/oauth2/v2.0"

    def authorise_url(self, redirect_uri, state):
        return f"{self._authority()}/authorize?" + urlencode({
            "client_id": MICROSOFT_CLIENT_ID, "redirect_uri": redirect_uri,
            "response_type": "code", "response_mode": "query",
            "scope": self.SCOPES, "state": state, "prompt": "select_account"})

    def _token(self, data):
        with _http() as client:
            response = client.post(f"{self._authority()}/token", data={
                **data, "client_id": MICROSOFT_CLIENT_ID,
                "client_secret": MICROSOFT_CLIENT_SECRET, "scope": self.SCOPES})
        if response.status_code != 200:
            raise ProviderError(f"Microsoft token endpoint returned "
                                f"{response.status_code}: {response.text[:200]}")
        return response.json()

    def exchange(self, code, redirect_uri):
        return self._token({"code": code, "redirect_uri": redirect_uri,
                            "grant_type": "authorization_code"})

    def refresh(self, refresh_token):
        return self._token({"refresh_token": refresh_token,
                            "grant_type": "refresh_token"})

    def _get(self, token, url, params=None):
        with _http() as client:
            response = client.get(url, params=params,
                                  headers={"Authorization": f"Bearer {token}"})
        if response.status_code == 401:
            raise ProviderError("unauthorised")
        if response.status_code != 200:
            raise ProviderError(f"Microsoft Graph returned {response.status_code}: "
                                f"{response.text[:200]}")
        return response

    def account(self, token):
        data = self._get(token, f"{self.GRAPH}/me",
                         {"$select": "mail,userPrincipalName"}).json()
        return data.get("mail") or data.get("userPrincipalName") or ""

    def _children(self, token, item_id):
        url = (f"{self.GRAPH}/me/drive/root/children" if not item_id
               else f"{self.GRAPH}/me/drive/items/{item_id}/children")
        params = {"$select": "id,name,file,folder,lastModifiedDateTime,eTag,cTag,size",
                  "$top": 200}
        out = []
        while url:
            data = self._get(token, url, params).json()
            out.extend(data.get("value", []))
            url, params = data.get("@odata.nextLink"), None
        return out

    def folders(self, token, parent):
        return [{"id": r["id"], "name": r["name"]}
                for r in self._children(token, parent) if "folder" in r]

    def walk(self, token, folder_id, prefix=""):
        files = []
        for row in self._children(token, folder_id):
            path = f"{prefix}/{row['name']}" if prefix else row["name"]
            if "folder" in row:
                files.extend(self.walk(token, row["id"], path))
                continue
            files.append(RemoteFile(
                id=row["id"], name=row["name"], path=path,
                mime_type=(row.get("file") or {}).get("mimeType", ""),
                # cTag changes with content; eTag also changes with metadata.
                revision=row.get("cTag") or row.get("eTag") or "",
                modified_at=_parse_time(row.get("lastModifiedDateTime")),
                size=int(row.get("size") or 0)))
        return files

    def download(self, token, item):
        return self._get(token, f"{self.GRAPH}/me/drive/items/{item.id}/content").content


PROVIDERS: dict[str, Provider] = {"google": GoogleDrive(), "microsoft": OneDrive()}
# Providers that are not offered as real connections (the demo folder) register
# themselves here, rather than being found by a lazy import - a lazy import
# does not survive the single-file build.
EXTRA_PROVIDERS: dict[str, Provider] = {}


def register_provider(key: str, provider: Provider) -> None:
    EXTRA_PROVIDERS[key] = provider


def provider_for(key: str) -> Provider:
    if key in PROVIDERS:
        return PROVIDERS[key]
    if key in EXTRA_PROVIDERS:
        return EXTRA_PROVIDERS[key]
    raise ProviderError(f"Unknown provider '{key}'")


def providers_status() -> list[dict]:
    return [{"key": key, "label": PROVIDER_LABEL[key],
             "configured": provider.configured,
             "env": ("GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET" if key == "google"
                     else "MICROSOFT_CLIENT_ID / MICROSOFT_CLIENT_SECRET")}
            for key, provider in PROVIDERS.items()]


# --------------------------------------------------------------------------
# Connection lifecycle
# --------------------------------------------------------------------------
def new_state() -> str:
    return secrets.token_urlsafe(24)


def store_tokens(connection: CloudConnection, tokens: dict) -> None:
    if tokens.get("access_token"):
        connection.access_token_enc = seal(tokens["access_token"])
    if tokens.get("refresh_token"):
        connection.refresh_token_enc = seal(tokens["refresh_token"])
    expires = int(tokens.get("expires_in") or 3600)
    connection.token_expires_at = now() + timedelta(seconds=max(60, expires - 120))


def access_token(db: Session, connection: CloudConnection) -> str:
    """A live access token, refreshed when it has expired."""
    if connection.provider == "demo":
        return "demo"
    token = unseal(connection.access_token_enc)
    if token and connection.token_expires_at and connection.token_expires_at > now():
        return token
    refresh = unseal(connection.refresh_token_enc)
    if not refresh:
        raise ProviderError("The connection needs to be authorised again.")
    store_tokens(connection, provider_for(connection.provider).refresh(refresh))
    db.flush()
    return unseal(connection.access_token_enc)


def schedule_next(connection: CloudConnection) -> None:
    interval = SCHEDULES.get(connection.schedule)
    connection.next_sync_at = (now() + interval) if interval else None


def disconnect(db: Session, connection: CloudConnection, actor) -> None:
    """Stops watching and forgets the tokens. Everything already filed stays."""
    connection.status = "disconnected"
    connection.disconnected_at = now()
    connection.access_token_enc = None
    connection.refresh_token_enc = None
    connection.next_sync_at = None
    db.flush()
    audit.record(db, tenant_id=connection.tenant_id, action="cloud.disconnected",
                 actor_id=getattr(actor, "id", None),
                 actor_label=getattr(actor, "email", "system"),
                 object_type="cloud_connection", object_id=connection.id,
                 payload={"provider": connection.provider})


# --------------------------------------------------------------------------
# The sync
# --------------------------------------------------------------------------
def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _catalogue(db: Session) -> list[DocumentType]:
    return list(db.scalars(select(DocumentType).where(and_(
        DocumentType.is_active.is_(True), DocumentType.scope == "global"))).all())


def _as_date(value):
    from datetime import date
    try:
        return date.fromisoformat(str(value)) if value else None
    except (ValueError, TypeError):
        return None


def _flag(db: Session, tenant: Tenant, title: str, severity: str = "high") -> None:
    existing = db.scalar(select(Task).where(and_(
        Task.tenant_id == tenant.id, Task.title == title, Task.status == "open")))
    if existing is None:
        db.add(Task(tenant_id=tenant.id, kind="cloud_sync", title=title[:255],
                    severity=severity, status="open",
                    due_at=now() + timedelta(days=3)))


def _process(db: Session, *, tenant: Tenant, connection: CloudConnection,
             run: SyncRun, record: SyncedFile, item: RemoteFile, data: bytes,
             digest: str, change: str, catalogue: list, actor) -> dict:
    """One new or changed file: store, read, classify, version, check."""
    from . import ai, documents as docsvc, textextract

    path, _ = docsvc.store_upload(tenant.id, item.name, data)
    text, _method = textextract.extract(data, item.name)
    found = authority.findings(data, item.name, text)

    result = ai.classify_document(db, tenant.id, filename=item.name, text=text,
                                  catalogue=catalogue)
    confidence = float(result.get("confidence") or 0)
    fields = result.get("fields") or {}

    # A file already bound to a credential stays bound to it: a renewal saved
    # over the old file is the same credential, whatever the classifier makes of
    # the new wording this time.
    document = db.get(Document, record.document_id) if record.document_id else None
    rebound_from = None
    if document is not None and confidence >= 0.6 and result.get("type_code") and \
            result["type_code"] != document.document_type.code:
        # The same file now holds a different kind of document - somebody saved
        # over "Insurance.pdf" with something else. Versioning the old credential
        # with it would put, say, a policy statement into the insurance history.
        # The file is re-filed as what it now is; the old credential is left
        # exactly as it was.
        rebound_from = document.document_type.code
        document = None
        record.document_id = None
        record.confirmed = False
        record.current_version_id = None
    if document is None:
        dtype = (db.scalar(select(DocumentType).where(
            DocumentType.code == result.get("type_code")))
            if result.get("type_code") else None)
        if dtype is None or confidence < 0.5:
            revision = SyncedFileRevision(
                synced_file_id=record.id, sync_run_id=run.id, change=change,
                provider_revision=item.revision, modified_at=item.modified_at,
                content_hash=digest, file_name=item.name, confidence=confidence,
                authority_findings_json=json.dumps(found, default=str)[:6000],
                note="Not filed: nothing in the catalogue matched with confidence. "
                     "Upload it manually to choose a type.")
            db.add(revision)
            record.state = "unclassified"
            return {"outcome": "unclassified", "found": found}
        document = db.scalar(select(Document).where(and_(
            Document.tenant_id == tenant.id, Document.document_type_id == dtype.id,
            Document.deleted_at.is_(None))))
        if document is None or dtype.multi_instance:
            document = Document(tenant_id=tenant.id, document_type_id=dtype.id,
                                title=dtype.name, owner_user_id=getattr(actor, "id", None))
            db.add(document)
            db.flush()
        record.document_id = document.id

    best = found.get("best")
    version = docsvc.create_version(
        db, document=document, actor=actor,
        issue_date=_as_date(fields.get("issue_date")),
        effective_from=_as_date(fields.get("issue_date")) or now().date(),
        expiry_date=_as_date(fields.get("expiry_date")),
        certificate_number=fields.get("certificate_number"),
        issuer_name=fields.get("issuer_name"),
        holder_name=fields.get("holder_name") or tenant.name,
        scope_statement=fields.get("scope_statement"),
        verification_url=best["url"] if best else None,
        file_path=path, file_name=item.name,
        mime_type=item.mime_type or "application/octet-stream",
        file_size=len(data), content_hash=digest,
        change_reason=(f"Synced from {PROVIDER_LABEL.get(connection.provider)}: "
                       f"{item.path}" + (" (file updated at source)"
                                         if change == "updated" else "")))

    suspicious = bool(found.get("lookalike") or found.get("qr_text_disagree"))
    published = False
    previous = (db.get(DocumentVersion, record.current_version_id)
                if record.current_version_id else None)
    record.confirmed = record.confirmed or bool(
        previous and previous.status in {"active", "superseded"})

    if suspicious:
        hosts = ", ".join(e["host"] for e in found.get("lookalike", [])) or "a non-authority site"
        _flag(db, tenant, f"Check {item.name}: its QR or link points to {hosts}")
        run.flagged += 1
    elif record.confirmed and connection.auto_publish_renewals:
        docsvc.publish_version(db, version, actor)
        published = True
        run.published += 1
        if version.verification_url:
            from . import verification
            try:
                verification.run_check(db, version, actor)
            except Exception as exc:
                print(f"[cloudsync] verification skipped: {exc}", flush=True)

    if best:
        run.authority_links += 1

    record.current_version_id = version.id
    record.state = "active"
    db.add(SyncedFileRevision(
        synced_file_id=record.id, sync_run_id=run.id, change=change,
        provider_revision=item.revision, modified_at=item.modified_at,
        content_hash=digest, file_name=item.name, document_version_id=version.id,
        type_code=document.document_type.code, confidence=confidence,
        authority_findings_json=json.dumps(found, default=str)[:6000],
        note=((f"This file used to hold {rebound_from} and now holds a different kind "
               "of document. It was filed separately; the earlier credential is "
               "unchanged. " if rebound_from else "")
              + ("Published automatically as a renewal of a confirmed file"
                 if published else
              "Held: the verification link needs checking" if suspicious else
                 "Draft: confirm it once and later revisions publish themselves"))))
    return {"outcome": "published" if published else "draft", "found": found,
            "version_id": version.id}


def run_sync(db: Session, connection: CloudConnection, *, trigger: str = "manual",
             actor=None) -> SyncRun:
    from . import documents as docsvc, onboarding, trust

    tenant = db.get(Tenant, connection.tenant_id)
    run = SyncRun(connection_id=connection.id, tenant_id=tenant.id, trigger=trigger)
    db.add(run)
    # Committed now, not at the end: the one-sync-per-folder guard reads running
    # rows, and an uncommitted one is invisible to the request that checks.
    db.commit()

    try:
        provider = provider_for(connection.provider)
        token = access_token(db, connection)
        remote = [f for f in provider.walk(token, connection.folder_id)
                  if f.name.lower().endswith(SUPPORTED)
                  or f.mime_type in getattr(provider, "EXPORT", {})][:CLOUD_SYNC_MAX_FILES]
    except Exception as exc:
        # Any failure before the walk completes - not only a provider error -
        # must close the run. A run left "running" holds the one-sync-per-folder
        # lock until it times out.
        if not isinstance(exc, ProviderError):
            exc = ProviderError(f"{type(exc).__name__}: {exc}")
        run.status, run.error = "failed", str(exc)
        run.finished_at = now()
        connection.last_result = "failed"
        connection.error = ("Access was revoked or has expired. Reconnect to resume."
                            if str(exc) == "unauthorised" else str(exc))
        if str(exc) == "unauthorised":
            connection.status = "reauthorise"
        schedule_next(connection)
        db.flush()
        return run

    before = trust.compute(db, tenant, persist=False)
    catalogue = _catalogue(db)
    existing = {f.provider_file_id: f for f in db.scalars(select(SyncedFile).where(
        SyncedFile.connection_id == connection.id)).all()}
    seen_ids = set()
    outcomes = []

    for item in remote:
        seen_ids.add(item.id)
        run.seen += 1
        record = existing.get(item.id)
        known = record
        try:
            # A savepoint per file: one bad file rolls back alone and the
            # rest of the folder still syncs.
            with db.begin_nested():
                # Cheap check first: same revision, same modified time - skip the
                # download entirely.
                if (record and record.state != "removed"
                        and record.provider_revision == item.revision
                        and record.modified_at == item.modified_at):
                    if record.path != item.path or record.name != item.name:
                        record.name, record.path = item.name, item.path
                    run.unchanged += 1
                    continue
                if item.size and item.size > MAX_FILE_BYTES:
                    raise ProviderError(f"{item.size // 1_048_576} MB exceeds the "
                                        f"{MAX_FILE_BYTES // 1_048_576} MB limit")

                data = provider.download(token, item)
                digest = _sha(data)

                if record is None:
                    record = SyncedFile(connection_id=connection.id, tenant_id=tenant.id,
                                        provider_file_id=item.id, name=item.name,
                                        path=item.path)
                    db.add(record)
                    db.flush()
                    change = "added"
                elif record.content_hash == digest and record.state != "removed":
                    # New revision, same bytes: a metadata change, not a new version.
                    record.provider_revision = item.revision
                    record.modified_at = item.modified_at
                    record.name, record.path = item.name, item.path
                    run.unchanged += 1
                    continue
                else:
                    change = "updated" if record.state != "removed" else "added"

                result = _process(db, tenant=tenant, connection=connection, run=run,
                                  record=record, item=item, data=data, digest=digest,
                                  change=change, catalogue=catalogue, actor=actor)
                record.provider_revision = item.revision
                record.modified_at = item.modified_at
                record.content_hash = digest
                record.name, record.path, record.mime_type = item.name, item.path, item.mime_type
                record.last_error = None
                if change == "added":
                    run.added += 1
                else:
                    run.updated += 1
                outcomes.append({"file": item.path, "change": change, **{
                    k: v for k, v in result.items() if k != "found"},
                    "authority": (result["found"].get("best") or {}).get("authority"),
                    "lookalike": bool(result["found"].get("lookalike"))})
                db.flush()
        except Exception as exc:
            run.failed += 1
            if known is not None:
                known.last_error = f"{type(exc).__name__}: {exc}"[:500]
            outcomes.append({"file": item.path, "change": "failed",
                             "error": f"{type(exc).__name__}: {exc}"[:200]})


    # Gone from the folder: recorded, never deleted from the register.
    for file_id, record in existing.items():
        if file_id in seen_ids or record.state == "removed":
            continue
        record.state = "removed"
        db.add(SyncedFileRevision(synced_file_id=record.id, sync_run_id=run.id,
                                  change="removed", file_name=record.name,
                                  note="Removed from the source folder. The credential "
                                       "and its history are kept."))
        if record.document_id:
            _flag(db, tenant, f"{record.name} was removed from "
                              f"“{connection.folder_name or 'your connected folder'}”. "
                              "The credential is kept; upload a replacement if it "
                              "has been superseded.", severity="medium")
        run.removed += 1
        outcomes.append({"file": record.path or record.name, "change": "removed"})

    # The profile refresh: statuses, gaps, Trust Index, onboarding - once, after
    # the whole folder, so the history shows one change rather than fifty.
    docsvc.run_maintenance(db, tenant)
    after = trust.compute(db, tenant)
    onboarding.sync_steps(db, tenant)
    onboarding.observe(db, tenant)

    run.status = "complete" if not run.failed else "partial"
    run.finished_at = now()
    run.summary_json = json.dumps({
        "outcomes": outcomes[:200],
        "trust_before": round(float(getattr(before, "composite", 0) or 0), 1),
        "trust_after": round(float(getattr(after, "composite", 0) or 0), 1)}, default=str)
    connection.last_sync_at = now()
    connection.last_result = run.status
    connection.error = None
    if connection.status in {"pending", "reauthorise"}:
        connection.status = "active"
    schedule_next(connection)
    db.flush()

    audit.record(db, tenant_id=tenant.id, action="cloud.synced",
                 actor_id=getattr(actor, "id", None),
                 actor_label=getattr(actor, "email", f"{trigger} sync"),
                 object_type="cloud_connection", object_id=connection.id,
                 payload={"trigger": trigger, "seen": run.seen, "added": run.added,
                          "updated": run.updated, "removed": run.removed,
                          "published": run.published, "flagged": run.flagged,
                          "failed": run.failed})
    return run


def run_due(db: Session) -> int:
    """Called by the background loop. One connection's failure never stops the
    others."""
    due = db.scalars(select(CloudConnection).where(and_(
        CloudConnection.status == "active",
        CloudConnection.next_sync_at.is_not(None),
        CloudConnection.next_sync_at <= now()))).all()
    count = 0
    for connection in due:
        try:
            run_sync(db, connection, trigger="schedule")
            db.commit()
            count += 1
        except Exception as exc:
            db.rollback()
            connection = db.get(CloudConnection, connection.id)
            connection.error = f"{type(exc).__name__}: {exc}"[:500]
            schedule_next(connection)
            db.commit()
            print(f"[cloudsync] {connection.id} failed: {exc}", flush=True)
    return count
