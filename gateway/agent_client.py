#!/usr/bin/env python3
"""
Aaka Agent Client SDK

Usage:
    from gateway.agent_client import AakaClient

    client = AakaClient(api_key=os.environ["AAKA_AGENT_KEY"])

    # Fire-and-forget
    client.send("Flight booked! Departs 14:30 LHR.")

    # Blocking ask — polls until user replies or timeout
    seat = client.ask(
        "Window or aisle?",
        options=["Window", "Aisle"],
        timeout_minutes=60,
    )
    # seat == "Window"

Dependencies: stdlib only (uses `requests` if installed, else `urllib.request`).
"""
import time
import uuid
from typing import Optional

try:
    import requests as _requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False
    import urllib.request
    import urllib.error
    import json as _json_lib


class AakaClientError(Exception):
    pass


class AakaClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = "http://localhost:18790",
        sender: Optional[str] = None,
        channel_id: Optional[str] = None,
        source: str = "telegram",
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.sender = sender
        self.channel_id = channel_id
        self.source = source

    def _headers(self) -> dict:
        return {"X-Agent-Key": self.api_key, "Content-Type": "application/json"}

    def _post(self, path: str, body: dict, timeout: int = 10) -> dict:
        url = self.base_url + path
        if _HAS_REQUESTS:
            r = _requests.post(url, json=body, headers=self._headers(), timeout=timeout)
            if r.status_code not in (200, 201):
                raise AakaClientError(f"POST {path} → {r.status_code}: {r.text}")
            return r.json()
        else:
            import json
            data = json.dumps(body).encode()
            req = urllib.request.Request(url, data=data, headers=self._headers(), method="POST")
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return json.loads(resp.read())
            except urllib.error.HTTPError as exc:
                raise AakaClientError(f"POST {path} → {exc.code}: {exc.read().decode()}") from exc

    def _get(self, path: str) -> "dict | None":
        """Returns None on 404, raises on other errors."""
        url = self.base_url + path
        if _HAS_REQUESTS:
            r = _requests.get(url, headers=self._headers(), timeout=10)
            if r.status_code == 404:
                return None
            if r.status_code != 200:
                raise AakaClientError(f"GET {path} → {r.status_code}: {r.text}")
            return r.json()
        else:
            import json
            req = urllib.request.Request(url, headers=self._headers(), method="GET")
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    return json.loads(resp.read())
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    return None
                raise AakaClientError(f"GET {path} → {exc.code}") from exc

    def _patch(self, path: str, body: dict, timeout: int = 10) -> dict:
        import json as _json
        url = self.base_url + path
        data = _json.dumps(body).encode()
        headers = {**self._headers(), "Content-Type": "application/json"}
        if _HAS_REQUESTS:
            r = _requests.patch(url, json=body, headers=self._headers(), timeout=timeout)
            if r.status_code not in (200, 201):
                raise AakaClientError(f"PATCH {path} → {r.status_code}: {r.text}")
            return r.json()
        else:
            req = urllib.request.Request(url, data=data, headers=headers, method="PATCH")
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return _json.loads(resp.read().decode())
            except urllib.error.HTTPError as exc:
                raise AakaClientError(f"PATCH {path} → {exc.code}: {exc.read().decode()}") from exc

    def _delete(self, path: str) -> None:
        url = self.base_url + path
        if _HAS_REQUESTS:
            r = _requests.delete(url, headers=self._headers(), timeout=10)
            if r.status_code not in (200, 204):
                raise AakaClientError(f"DELETE {path} → {r.status_code}: {r.text}")
        else:
            req = urllib.request.Request(url, headers=self._headers(), method="DELETE")
            try:
                urllib.request.urlopen(req, timeout=10)
            except urllib.error.HTTPError as exc:
                if exc.code not in (200, 204):
                    raise AakaClientError(f"DELETE {path} → {exc.code}") from exc

    def _delete_json(self, path: str) -> dict:
        """DELETE and return the JSON response body (for endpoints that return 200 + body)."""
        import json as _json
        url = self.base_url + path
        if _HAS_REQUESTS:
            r = _requests.delete(url, headers=self._headers(), timeout=10)
            if r.status_code not in (200, 204):
                raise AakaClientError(f"DELETE {path} → {r.status_code}: {r.text}")
            return r.json() if r.content else {}
        else:
            req = urllib.request.Request(url, headers=self._headers(), method="DELETE")
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    return _json.loads(resp.read().decode())
            except urllib.error.HTTPError as exc:
                if exc.code not in (200, 204):
                    raise AakaClientError(f"DELETE {path} → {exc.code}") from exc
                return {}

    def notify(self, text: str, *, silent: bool = False) -> dict:
        """Post a notification to the configured notifications group.

        Returns {"outbox_item_id": str, "chat_id": str}. Raises if no
        notifications group is configured or this agent isn't allowed
        to post to it.

        Use this instead of `send()` for status pings, alerts, and
        anything not directed at a specific user.
        """
        return self._post("/v1/notify", {"text": text, "silent": silent})

    def send(
        self,
        text: str,
        *,
        sender: Optional[str] = None,
        reply_options: Optional[list] = None,
        silent: bool = False,
        reply_to_message_id: Optional[str] = None,
    ) -> str:
        """Fire-and-forget: send a message to the user. Returns outbox_item_id."""
        body: dict = {
            "text": text,
            "source": self.source,
            "await_reply": False,
            "silent": silent,
        }
        if sender or self.sender:
            body["sender"] = sender or self.sender
        if self.channel_id:
            body["channel_id"] = self.channel_id
        if reply_options:
            body["reply_options"] = reply_options
        if reply_to_message_id:
            body["reply_to_message_id"] = reply_to_message_id
        result = self._post("/v1/messages", body)
        return result["outbox_item_id"]

    def ask(
        self,
        text: str,
        *,
        options: Optional[list] = None,
        timeout_minutes: int = 1440,
        poll_interval: int = 10,
        sender: Optional[str] = None,
        silent: bool = False,
        correlation_id: Optional[str] = None,
    ) -> str:
        """Send a message and block until user replies. Returns reply text.

        Polls every poll_interval seconds. Raises AakaClientError on timeout.
        """
        corr_id = correlation_id or str(uuid.uuid4())
        body: dict = {
            "text": text,
            "source": self.source,
            "await_reply": True,
            "ttl_minutes": timeout_minutes,
            "correlation_id": corr_id,
            "silent": silent,
        }
        if sender or self.sender:
            body["sender"] = sender or self.sender
        if self.channel_id:
            body["channel_id"] = self.channel_id
        if options:
            body["reply_options"] = options
        self._post("/v1/messages", body)

        deadline = time.time() + timeout_minutes * 60
        while time.time() < deadline:
            reply = self._get(f"/v1/replies/{corr_id}")
            if reply:
                self._delete(f"/v1/replies/{corr_id}")
                return reply["text"]
            time.sleep(poll_interval)

        raise AakaClientError(
            f"ask() timed out after {timeout_minutes}m (correlation_id={corr_id})"
        )

    def gmail_emails(
        self,
        label: str,
        *,
        member_id: str = "",
        max_results: int = 20,
    ) -> "list[dict]":
        """Fetch emails from the given Gmail label via the agent gateway.

        Returns a list of dicts: {id, subject, from_addr, date, body}.
        Body is plain text, truncated to 3000 chars.

        No server-side dedup — track seen IDs locally (e.g. messages/seen.json):
            seen = json.loads(seen_file.read_text()) if seen_file.exists() else []
            new = [e for e in client.gmail_emails(label) if e["id"] not in set(seen)]

        Requires: gateway running at self.base_url and gmail.readonly token for member_id.
        """
        params = f"label={label}&max_results={max_results}"
        if member_id:
            params += f"&member_id={member_id}"
        result = self._get(f"/v1/gmail/emails?{params}")
        return result.get("emails", []) if result else []

    def send_email(
        self,
        to_member_id: str,
        subject: str,
        body: str,
        *,
        html_body: Optional[str] = None,
        schedule_at: Optional[str] = None,
        from_member_id: str = "aakash",
    ) -> dict:
        """Queue an email from aakash's Gmail to a registered family member.

        Delivered by the always-on VPS sensor (within ~60s if schedule_at omitted).
        to_member_id: member id from aaka.yaml (e.g. "alex", "tsu").
        schedule_at: ISO8601 with explicit TZ, e.g. "2026-06-01T09:00:00+02:00".
        Returns {"id", "status", "scheduled_at_utc", "to"}.

        Poll get_scheduled_message(id) for status → "sent" + result.message_id.

        Example:
            # Send within ~60s
            r = client.send_email("alex", "Flight confirmed", "Departs 14:30...")
            # Schedule for 9am Berlin time tomorrow
            r = client.send_email("alex", "Reminder", "Don't forget...",
                                  schedule_at="2026-06-01T09:00:00+02:00")
        """
        payload: dict = {
            "to_member_id": to_member_id,
            "subject": subject,
            "body": body,
            "from_member_id": from_member_id,
        }
        if html_body:
            payload["html_body"] = html_body
        if schedule_at:
            payload["schedule_at"] = schedule_at
        return self._post("/v1/email/send", payload)

    # ── Scheduled outbound (generic) ─────────────────────────────────────────

    def schedule_outbound(
        self,
        channel: str,
        payload: dict,
        schedule_at: Optional[str] = None,
    ) -> dict:
        """Schedule any outbound message on any channel.

        channel: 'email' | 'telegram' | 'whatsapp'
        schedule_at: ISO8601 with TZ; omit for near-immediate (~60s) delivery.
        Returns {"id", "status", "scheduled_at_utc", "channel"}.

        Payload shapes:
          email:    {"to_member_id", "subject", "body", "html_body"?}
          telegram: {"channel_id" (or "recipient"), "text", "silent"?, "reply_markup"?}
          whatsapp: {"channel_id" (or "recipient"), "text"}
        """
        body: dict = {"channel": channel, "payload": payload}
        if schedule_at:
            body["schedule_at"] = schedule_at
        return self._post("/v1/scheduled/messages", body)

    def get_scheduled_message(self, msg_id: str) -> dict:
        """Get status of a scheduled message. Returns the row dict."""
        result = self._get(f"/v1/scheduled/messages/{msg_id}")
        if result is None:
            raise AakaClientError(f"Scheduled message {msg_id} not found")
        return result

    def list_scheduled_messages(
        self, status: Optional[str] = None
    ) -> "list[dict]":
        """List scheduled messages for this agent.

        status: optional filter — 'pending' | 'sent' | 'cancelled' | 'error'
        """
        path = "/v1/scheduled/messages"
        if status:
            path += f"?status={status}"
        result = self._get(path)
        return result if isinstance(result, list) else []

    def cancel_scheduled_message(self, msg_id: str) -> None:
        """Cancel a pending scheduled message (idempotent)."""
        self._delete(f"/v1/scheduled/messages/{msg_id}")

    def gmail_create_label(
        self,
        name: str,
        *,
        member_id: str = "",
        label_list_visibility: str = "labelShow",
        message_list_visibility: str = "show",
    ) -> dict:
        """Create a new Gmail label. Returns {id, name, type}.

        Use "/" in name for nested labels, e.g. "Travel/JP-2026".
        Requires: gmail.labels scope on the member's token.

        Example:
            label = client.gmail_create_label("Travel/JP-2026", member_id="alex")
            # label["id"] is the stable label ID for future use
        """
        body: dict = {
            "name": name,
            "member_id": member_id,
            "label_list_visibility": label_list_visibility,
            "message_list_visibility": message_list_visibility,
        }
        return self._post("/v1/gmail/labels", body)

    def gmail_create_draft(
        self,
        to: "str | list[str]",
        subject: str,
        body: str,
        *,
        html_body: Optional[str] = None,
        cc: "str | list[str] | None" = None,
        bcc: "str | list[str] | None" = None,
        reply_to_message_id: Optional[str] = None,
    ) -> dict:
        """Create a Gmail draft in the admin (primary) account.

        Drafts land in Gmail's Drafts folder — no email is sent.  Open Gmail to
        review, edit, and send.

        to/cc/bcc: a single address or list of addresses (raw strings, not member IDs).
        reply_to_message_id: thread the draft under an existing Gmail message ID.

        Returns {draft_id, message_id, thread_id}.

        Example:
            draft = client.gmail_create_draft(
                to="cto@company.com",
                subject="Intro: Alex Smith",
                body="Hi,\\n\\nI wanted to reach out...",
            )
            # Open Gmail, find the draft, review and send.

            # With cc/bcc
            draft = client.gmail_create_draft(
                to=["alice@co.com", "bob@co.com"],
                subject="Follow-up",
                body="Thanks for the meeting...",
                cc="manager@co.com",
            )
        """
        def _to_list(v: "str | list[str] | None") -> "list[str] | None":
            if v is None:
                return None
            return [v] if isinstance(v, str) else list(v)

        payload: dict = {
            "to": _to_list(to) or [],
            "subject": subject,
            "body": body,
        }
        if html_body:
            payload["html_body"] = html_body
        if cc:
            payload["cc"] = _to_list(cc)
        if bcc:
            payload["bcc"] = _to_list(bcc)
        if reply_to_message_id:
            payload["reply_to_message_id"] = reply_to_message_id
        return self._post("/v1/gmail/drafts", payload)

    # ── Calendar ─────────────────────────────────────────────────────────────

    def add_event(
        self,
        title: str,
        date: str,
        *,
        start_time: str = "",
        end_time: str = "",
        end_date: str = "",
        description: str = "",
        location: str = "",
        member_id: str = "",
        calendar_tag: str = "",
        guests: "list[str] | None" = None,
        private: bool = False,
        timezone: str = "",
        occurrences: "list[dict] | None" = None,
    ) -> dict:
        """Add a calendar event. Returns {event_ids: [...], count: N}.

        For a single event, pass date (+ optional start_time/end_time).
        For multiple occurrences, pass the occurrences list directly.

        Example:
            # Single timed event
            client.add_event("Team standup", "2026-05-10",
                             start_time="09:00", end_time="09:30")

            # All-day event
            client.add_event("Holiday", "2026-12-25")

            # Multiple occurrences
            client.add_event("Piano lesson", "2026-05-10", occurrences=[
                {"date": "2026-05-10", "start_time": "16:00", "end_time": "17:00"},
                {"date": "2026-05-17", "start_time": "16:00", "end_time": "17:00"},
            ])
        """
        if occurrences is None:
            occ: dict = {"date": date}
            if start_time:
                occ["start_time"] = start_time
            if end_time:
                occ["end_time"] = end_time
            if end_date:
                occ["end_date"] = end_date
            occurrences = [occ]

        body: dict = {
            "title": title,
            "occurrences": occurrences,
        }
        if description:
            body["description"] = description
        if location:
            body["location"] = location
        if member_id:
            body["member_id"] = member_id
        if calendar_tag:
            body["calendar_tag"] = calendar_tag
        if guests:
            body["guests"] = guests
        if private:
            body["private"] = private
        if timezone:
            body["timezone"] = timezone
        return self._post("/v1/calendar/events", body)

    # ── Tasks ────────────────────────────────────────────────────────────────

    def add_task(
        self,
        title: str,
        *,
        owner: str = "",
        due_date: str = "",
        urgent: bool = False,
        starred: bool = False,
        tags: "list[str] | None" = None,
        description: str = "",
        recurring: bool = False,
        project: str = "",   # entity/project slug, e.g. "japan26", "flo172"
        skill: str = "",     # skill that created this task, e.g. "book_flight"
    ) -> dict:
        """Add a task. Returns {task_id, title, due_date, agent, project, skill}.

        `agent` is always auto-populated from the API key used to authenticate.
        `project` and `skill` are optional provenance fields for traceability.

        Example:
            client.add_task("Book restaurant for May 15",
                            owner="alex", due_date="2026-05-14",
                            tags=["#travel"], project="japan26",
                            skill="restaurant_search")
        """
        body: dict = {"title": title}
        if owner:
            body["owner"] = owner
        if due_date:
            body["due_date"] = due_date
        if urgent:
            body["urgent"] = urgent
        if starred:
            body["starred"] = starred
        if tags:
            body["tags"] = tags
        if description:
            body["description"] = description
        if recurring:
            body["recurring"] = recurring
        if project:
            body["project"] = project
        if skill:
            body["skill"] = skill
        return self._post("/v1/tasks", body)

    def update_task(self, task_id: str, **fields) -> dict:
        """Patch any fields on an existing task by ID.

        Allowed fields: title, owner, due_date, urgent, starred, tags,
        description, recurring, project, skill, status ("open"|"done"|"deleted").

        Example:
            client.update_task("a1b2c3d4", project="japan26", skill="hotel_search")
            client.update_task("a1b2c3d4", status="done")
        """
        return self._patch(f"/v1/tasks/{task_id}", fields)

    def delete_task(self, task_id: str) -> None:
        """Soft-delete a task by ID."""
        self._delete(f"/v1/tasks/{task_id}")

    def list_tasks(
        self,
        *,
        project: str = "",
        skill: str = "",
        owner: str = "",
        status: str = "open",
        tag: "str | list[str] | None" = None,
        title_prefix: str = "",
        limit: int = 200,
    ) -> "list[dict]":
        """List tasks created by this agent. Returns task dicts including id + tags.

        All filters are optional. Results are always scoped to the calling agent.

        Args:
            project:      Exact match on project field (e.g. "2607-Japan-China-f").
            skill:        Exact match on skill field (e.g. "weather").
            owner:        Exact match on owner field (e.g. "alex").
            status:       "open" (default) | "done" | "deleted" | "all".
            tag:          Match if value is in task's tags list. Pass a list to
                          require any of multiple tags.
            title_prefix: Case-sensitive prefix match on title (e.g. "Pack: ").
            limit:        Max rows returned (default 200, hard cap 1000).

        Example:
            # Check for existing packing tasks before creating duplicates
            existing = client.list_tasks(
                project="2607-Japan-China-f",
                skill="weather",
                title_prefix="Pack: ",
            )
            existing_titles = {t["title"] for t in existing}
            if "Pack: Umbrella" not in existing_titles:
                client.add_task("Pack: Umbrella", project="...", skill="weather")
        """
        params = f"status={status}&limit={limit}"
        if project:
            params += f"&project={project}"
        if skill:
            params += f"&skill={skill}"
        if owner:
            params += f"&owner={owner}"
        if title_prefix:
            import urllib.parse
            params += f"&title_prefix={urllib.parse.quote(title_prefix)}"
        if tag:
            tags = [tag] if isinstance(tag, str) else tag
            for t in tags:
                import urllib.parse
                params += f"&tag={urllib.parse.quote(t)}"
        result = self._get(f"/v1/tasks?{params}")
        return result.get("tasks", []) if result else []

    # ── Delete by action_id ────────────────────────────────────────────────

    def delete(self, action_id: str) -> None:
        """Delete resource(s) created by a previous action, identified by action_id.

        The action_id is returned by add_event() and add_task() in their response dicts.

        Example:
            result = client.add_event("Standup", "2026-05-10", start_time="09:00")
            # later...
            client.delete(result["action_id"])  # removes the calendar event(s)
        """
        self._delete(f"/v1/actions/{action_id}")

    # ── Job scheduler ────────────────────────────────────────────────────────

    def schedule_job(
        self,
        name: str,
        schedule: str,
        *,
        payload: "dict | None" = None,
    ) -> dict:
        """Create or update a scheduled job. Returns job dict.

        schedule: cron expression ("*/10 * * * *") or interval ("15m", "1h", "1d").
        payload: JSON data passed to the executor when the job fires.

        Example:
            client.schedule_job(
                "fetch-travel-emails",
                "*/10 * * * *",
                payload={"label": "Travel/AAKA-JP", "member_id": "alex"},
            )
        """
        body: dict = {"name": name, "schedule": schedule}
        if payload:
            body["payload"] = payload
        return self._post("/v1/jobs", body)

    def list_jobs(self) -> "list[dict]":
        """List all scheduled jobs for this agent."""
        result = self._get("/v1/jobs")
        return result if isinstance(result, list) else []

    def delete_job(self, job_id: str) -> None:
        """Delete a scheduled job by ID."""
        self._delete(f"/v1/jobs/{job_id}")

    # ── PDF ─────────────────────────────────────────────────────────────────

    def pdf_compress(
        self,
        file_path: str,
        *,
        quality: int = 80,
        dpi: int = 0,
        output_path: str = "",
    ) -> str:
        """Compress a PDF. Returns path to compressed output file.

        Args:
            quality: JPEG quality 1–95 (lower = smaller, default 80).
            dpi: downsample images above this DPI (0 = off; 150 = screen; 200 = print).

        Example:
            out = client.pdf_compress("scan.pdf", quality=60, dpi=150)
        """
        import base64, os
        from pathlib import Path as _P
        data = _P(file_path).read_bytes()
        r = self._post("/v1/pdf/compress", {
            "file_b64": base64.b64encode(data).decode(),
            "filename": _P(file_path).name,
            "quality": quality,
            "dpi": dpi,
        }, timeout=120)
        if not output_path:
            p = _P(file_path)
            output_path = str(p.with_name(p.stem + "_compressed.pdf"))
        _P(output_path).write_bytes(base64.b64decode(r["file_b64"]))
        return output_path

    def pdf_extract(
        self,
        file_path: str,
        *,
        output_dir: str = "",
    ) -> dict:
        """Extract per-page text and metadata from a PDF.

        Returns {text: str, pages: int, metadata: dict, text_file: str}.
        Writes a <stem>_text.txt sidecar to output_dir (or input directory).

        Example:
            r = client.pdf_extract("report.pdf")
            print(r["text"][:500])
        """
        import base64
        from pathlib import Path as _P
        data = _P(file_path).read_bytes()
        r = self._post("/v1/pdf/extract", {
            "file_b64": base64.b64encode(data).decode(),
            "filename": _P(file_path).name,
        }, timeout=120)
        out_dir = _P(output_dir) if output_dir else _P(file_path).parent
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = _P(file_path).stem
        txt_path = str(out_dir / f"{stem}_text.txt")
        _P(txt_path).write_text(r["text"], encoding="utf-8")
        return {
            "text": r["text"],
            "pages": r["pages"],
            "metadata": r["metadata"],
            "text_file": txt_path,
        }

    def pdf_ocr(
        self,
        file_path: str,
        *,
        language: str = "eng",
        output_dir: str = "",
    ) -> dict:
        """OCR a scanned PDF via Tesseract; returns the extracted text.

        Returns {text, pages, ocr_pages, chars, text_file}.
        Writes a <stem>_text.txt sidecar to output_dir (or input directory).

        Example:
            r = client.pdf_ocr("scan.pdf")
            first_chars = r["text"][:1000]
        """
        import base64
        from pathlib import Path as _P
        data = _P(file_path).read_bytes()
        r = self._post("/v1/pdf/ocr", {
            "file_b64": base64.b64encode(data).decode(),
            "filename": _P(file_path).name,
            "language": language,
        }, timeout=300)
        out_dir = _P(output_dir) if output_dir else _P(file_path).parent
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = _P(file_path).stem
        txt_path = str(out_dir / f"{stem}_text.txt")
        _P(txt_path).write_text(r["text"], encoding="utf-8")
        return {
            "text": r["text"],
            "pages": r["pages"],
            "ocr_pages": r["ocr_pages"],
            "chars": r["chars"],
            "text_file": txt_path,
        }

    def pdf_split(
        self,
        file_path: str,
        spec: str,
        *,
        output_dir: str = "",
    ) -> list:
        """Split a PDF by page spec. Returns list of output file paths.

        spec: "1,5,9" (split points), "3-5" (range), "2s" (every 2 pages).

        Example:
            parts = client.pdf_split("book.pdf", "2s", output_dir="/tmp/parts/")
        """
        import base64
        from pathlib import Path as _P
        data = _P(file_path).read_bytes()
        r = self._post("/v1/pdf/split", {
            "file_b64": base64.b64encode(data).decode(),
            "filename": _P(file_path).name,
            "spec": spec,
        }, timeout=120)
        out_dir = _P(output_dir) if output_dir else _P(file_path).parent
        out_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for part in r["files"]:
            dest = str(out_dir / part["filename"])
            _P(dest).write_bytes(base64.b64decode(part["file_b64"]))
            paths.append(dest)
        return paths

    def pdf_merge(
        self,
        file_paths: list,
        *,
        output_path: str = "",
    ) -> str:
        """Merge PDFs and images into one PDF. Returns path to merged output.

        Accepts .pdf, .jpg, .png, and other common image formats.

        Example:
            out = client.pdf_merge(["cover.pdf", "body.pdf", "photo.jpg"])
        """
        import base64
        from pathlib import Path as _P
        files = []
        for p in file_paths:
            data = _P(p).read_bytes()
            files.append({
                "file_b64": base64.b64encode(data).decode(),
                "filename": _P(p).name,
            })
        r = self._post("/v1/pdf/merge", {"files": files}, timeout=180)
        if not output_path:
            p = _P(file_paths[0])
            output_path = str(p.with_name(p.stem + "_merged.pdf"))
        _P(output_path).write_bytes(base64.b64decode(r["file_b64"]))
        return output_path

    def pdf_delete_pages(
        self,
        file_path: str,
        spec: str,
        *,
        output_path: str = "",
    ) -> str:
        """Remove pages from a PDF. Returns path to trimmed output.

        spec: "3-5" (remove range), "1,3,5" (specific pages), "2s" (every 2nd page).

        Example:
            out = client.pdf_delete_pages("report.pdf", "3-5")
        """
        import base64
        from pathlib import Path as _P
        data = _P(file_path).read_bytes()
        r = self._post("/v1/pdf/delete", {
            "file_b64": base64.b64encode(data).decode(),
            "filename": _P(file_path).name,
            "spec": spec,
        }, timeout=120)
        if not output_path:
            p = _P(file_path)
            output_path = str(p.with_name(p.stem + "_trimmed.pdf"))
        _P(output_path).write_bytes(base64.b64decode(r["file_b64"]))
        return output_path

    # ── Photo ────────────────────────────────────────────────────────────────

    def send_photo(
        self,
        photo_path: str,
        *,
        caption: str = "",
        reply_options: Optional[list] = None,
        sender: Optional[str] = None,
        silent: bool = False,
    ) -> dict:
        """Send a photo to the user via Telegram. Returns {"ok": True, "message_id": ...}.

        Args:
            photo_path: Local path to the image file (JPEG, PNG, etc.).
            caption:    Optional caption text (Markdown supported, max 1024 chars).
            reply_options: Optional list of button labels shown as inline keyboard.
            sender:     Override recipient Telegram chat ID.
            silent:     Send without notification sound.

        Example:
            client.send_photo("draft.png", caption="*Post draft*\\n\\nHere's your next post.")
            choice = client.ask("Approve?", options=["Approve", "Edit", "Reject"])
        """
        import base64
        from pathlib import Path as _P
        data = _P(photo_path).read_bytes()
        body: dict = {
            "photo_b64": base64.b64encode(data).decode(),
            "filename": _P(photo_path).name,
            "caption": caption,
            "silent": silent,
        }
        if sender or self.sender:
            body["sender"] = sender or self.sender
        if self.channel_id:
            body["channel_id"] = self.channel_id
        if reply_options:
            body["reply_options"] = reply_options
        return self._post("/v1/photos", body, timeout=60)

    # ── LLM ─────────────────────────────────────────────────────────────────

    @staticmethod
    def llm_image(path_or_bytes, *, label: str = "", media_type: str = "") -> dict:
        """Build one entry for `llm(images=[...])` from a file path or raw bytes.

        media_type is inferred from the extension (.png / .jpg / .jpeg) when not
        given; bytes without a media_type default to image/png.
        """
        import base64 as _b64
        from pathlib import Path as _Path
        if isinstance(path_or_bytes, (str, _Path)):
            p = _Path(path_or_bytes)
            data = p.read_bytes()
            if not media_type:
                media_type = "image/jpeg" if p.suffix.lower() in (".jpg", ".jpeg") else "image/png"
        else:
            data = bytes(path_or_bytes)
            media_type = media_type or "image/png"
        return {"data": _b64.b64encode(data).decode("ascii"),
                "media_type": media_type, "label": label}

    def llm(
        self,
        prompt: str,
        *,
        response_format: str = "json",
        complexity: str = "low",
        timeout: int = 120,
        images: Optional[list[dict]] = None,
        raw: bool = False,
    ):
        """Call an LLM via the agent gateway. Returns the response text.

        Uses the local Claude subscription (zero marginal cost) with Gemini fallback.

        Args:
            prompt: The prompt to send.
            response_format: "json" (structured output) or "text" (plain text).
            complexity: "low" (haiku), "medium" (sonnet), "high" (opus).
            timeout: Max seconds to wait for LLM response.
            images: Optional list of {data (base64), media_type, label} — build
                them with `AakaClient.llm_image(...)`. Max 4, each ≤ 2 MB decoded,
                ≤ 8 MB total. Refer to them in the prompt as "Image 1", "Image 2 (label)".
            raw: Return the whole response dict (text, model, provider,
                duration_ms, saw_images) instead of just the text. Use it with
                images so you can tell whether the answer was vision-backed.

        Example:
            data = client.llm('Extract {name, date} from: "Dinner May 10"')
            parsed = json.loads(data)

            summary = client.llm("Summarize: ...", response_format="text", complexity="medium")

            r = client.llm("Explain the question in Image 1 to a ten-year-old.",
                           images=[client.llm_image("q14.png", label="the question")],
                           response_format="text", raw=True)
            r["text"], r["saw_images"]   # saw_images == 0 → answer was text-only
        """
        body = {
            "prompt": prompt,
            "response_format": response_format,
            "complexity": complexity,
        }
        if images:
            body["images"] = images
        result = self._post("/v1/llm", body, timeout=timeout + 10)
        return result if raw else result["text"]

    def register_tag(
        self,
        tag: str,
        *,
        alias: Optional[str] = None,
        route: Optional[str] = None,
        owner: Optional[str] = None,
    ) -> dict:
        """Register a tag alias and/or route so users can take notes with just the tag name.

        Call this when creating a new project or trip. Once registered:
          - `n jp bought rail pass` lands in the right vault folder automatically
          - `/tag jp` shows the full routing details

        Args:
            tag:    Short tag name (e.g. "jp", "tax26").
            alias:  Canonical long name (e.g. "2607-japan-china"). The short tag
                    becomes an alias that resolves to this.
            route:  Vault folder path (e.g. "travel/japan26", "finance/tax/2026").
            owner:  Member id whose vault this belongs to
                    (e.g. "family", "ari", "alex").

        Returns dict with status ("registered" or "already_set") and echoed fields.

        Example:
            # On trip creation
            client.register_tag("jp", alias="2607-japan-china",
                                 route="travel/japan26", owner="family")

            # On project creation
            client.register_tag("tax26", route="finance/tax/2026")
        """
        body: dict = {"tag": tag}
        if alias:
            body["alias"] = alias
        if route:
            body["route"] = route
        if owner:
            body["owner"] = owner
        return self._post("/v1/tags", body)

    # ── LinkedIn ─────────────────────────────────────────────────────────────

    def post_linkedin(
        self,
        text: str,
        *,
        image_path: Optional[str] = None,
        schedule_at: Optional[str] = None,
        visibility: str = "PUBLIC",
    ) -> dict:
        """Queue a LinkedIn post for human approval, then publish or schedule.

        Sends a Telegram preview with Approve/Cancel buttons. The post is
        published (or scheduled) only after the user taps Approve.

        Args:
            text:        Post body text.
            image_path:  Local path to an image (optional).
            schedule_at: ISO 8601 UTC datetime string to schedule the post
                         (e.g. "2026-05-18T09:00:00Z"). None = publish
                         immediately when approved.
            visibility:  "PUBLIC" (default) or "CONNECTIONS".

        Returns:
            {"approval_id": str, "item_id": str, "status": "awaiting_confirm"}

        Poll GET /v1/approvals/{approval_id} for status updates:
            "awaiting_confirm" → "scheduled" / "confirmed" → "done" / "cancelled"

        Example:
            # Post now (after approval)
            r = client.post_linkedin("Excited to share our latest work...")
            approval_id = r["approval_id"]

            # Schedule for Monday 9am UTC (after approval)
            r = client.post_linkedin(
                "Weekly update...",
                schedule_at="2026-05-19T09:00:00Z",
            )
        """
        payload: dict = {"text": text, "visibility": visibility}
        if image_path:
            payload["image_path"] = image_path
        body: dict = {"intent": "linkedin_post", "payload": payload}
        if schedule_at:
            body["schedule_at"] = schedule_at
        if self.sender:
            body["sender"] = self.sender
        if self.channel_id:
            body["channel_id"] = self.channel_id
        return self._post("/v1/approvals", body)

    def get_approval_status(self, approval_id: str) -> dict:
        """Poll the status of a queued approval request.

        Returns {"approval_id", "item_id", "status", "result"}.
        Status values: awaiting_confirm | scheduled | confirmed | done | cancelled | error
        """
        result = self._get(f"/v1/approvals/{approval_id}")
        if result is None:
            raise AakaClientError(f"Approval {approval_id} not found")
        return result

    def retire_tag(self, tag: str) -> dict:
        """Retire a tag — remove its alias and route so the name can be reused.

        Call this when a project is archived or a trip is complete.
        The vault folder and _context.md are NOT deleted (they remain as archive).

        Returns dict with status ("retired") and what was removed.

        Example:
            client.retire_tag("jp")   # frees "jp" for a future trip
        """
        return self._delete_json(f"/v1/tags/{tag}")
