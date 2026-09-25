"""Web page interaction and consent-mediated external form submission.

Enforces:
1. Web pages and forms are untrusted data, never instructions. Prompt injection cannot grant privileges.
2. Forms can be read, inspected, and drafted locally.
3. External submission (POST/GET with side effects) strictly requires owner approval bound to the exact action URL, method, and submitted payload digest.
4. If parameters or URL differ, the submission fails closed.
5. Sensitive credentials (passwords, tokens, api keys) are masked/redacted in summaries and logs.
6. Uncertain submission outcomes (network drops, broken pipe, timeouts) are reported as OUTCOME_UNKNOWN and never retried blindly.
"""
from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Tuple

from jarvis_hermes.approval import ApprovalError, ApprovalStore
from jarvis_hermes.projects import redact_secrets

MAX_WEB_PAGE_BYTES = 1048576  # 1 MiB
DEFAULT_TIMEOUT_SECONDS = 15


class WebError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class _HTMLFormExtractor(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__()
        self.base_url = base_url
        self.title: str = ""
        self._in_title = False
        self.text_parts: list[str] = []
        self.forms: list[dict[str, Any]] = []
        self._current_form: dict[str, Any] | None = None
        self._current_textarea: dict[str, Any] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        attr_dict = {k.lower(): v for k, v in attrs if v is not None}
        if tag == "title":
            self._in_title = True
        elif tag == "form":
            action = attr_dict.get("action", "")
            full_action = urllib.parse.urljoin(self.base_url, action)
            method = attr_dict.get("method", "GET").upper()
            self._current_form = {
                "action": full_action,
                "method": method,
                "fields": []
            }
            self.forms.append(self._current_form)
        elif tag == "input" and self._current_form is not None:
            name = attr_dict.get("name")
            if name:
                self._current_form["fields"].append({
                    "name": name,
                    "type": attr_dict.get("type", "text").lower(),
                    "value": attr_dict.get("value", "")
                })
        elif tag == "textarea" and self._current_form is not None:
            name = attr_dict.get("name")
            if name:
                self._current_textarea = {
                    "name": name,
                    "type": "textarea",
                    "value": ""
                }
                self._current_form["fields"].append(self._current_textarea)

    def handle_endtag(self, tag: str):
        if tag == "title":
            self._in_title = False
        elif tag == "form":
            self._current_form = None
        elif tag == "textarea":
            self._current_textarea = None

    def handle_data(self, data: str):
        if self._in_title:
            self.title += data
        if self._current_textarea is not None:
            self._current_textarea["value"] += data
        clean = data.strip()
        if clean:
            self.text_parts.append(clean)


def extract_page_content(html_text: str, base_url: str) -> dict[str, Any]:
    parser = _HTMLFormExtractor(base_url)
    parser.feed(html_text)
    return {
        "title": parser.title.strip(),
        "text": " ".join(parser.text_parts),
        "forms": parser.forms
    }


def extract_page_form(html_text: str, base_url: str, form_index: int = 0) -> dict[str, Any]:
    extracted = extract_page_content(html_text, base_url)
    forms = extracted["forms"]
    if form_index < 0 or form_index >= len(forms):
        raise WebError("FORM_NOT_FOUND", f"Form at index {form_index} not found on page.")
    return forms[form_index]


def mask_sensitive_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """Mask password and credential fields in approval summaries and logs."""
    masked = {}
    for k, v in fields.items():
        k_lower = k.lower()
        if any(term in k_lower for term in ("pass", "secret", "token", "key", "auth", "credential")):
            masked[k] = "[REDACTED]"
        else:
            masked[k] = redact_secrets(str(v))
    return masked


class WebManager:
    def __init__(self, approval_store: ApprovalStore | None = None):
        self.approval_store = approval_store

    def read_web_page(self, url: str, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS) -> dict[str, Any]:
        """Fetch and parse a web page extracting text and available forms."""
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise WebError("INVALID_URL", f"Only http and https schemes are allowed: {url!r}")

        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Jarvis-Hermes/1.0 (+https://github.com/nuno80/Jarvis-hermes)"}
        )

        try:
            with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
                raw_bytes = resp.read(MAX_WEB_PAGE_BYTES + 1)
                if len(raw_bytes) > MAX_WEB_PAGE_BYTES:
                    raw_bytes = raw_bytes[:MAX_WEB_PAGE_BYTES]
                charset = resp.headers.get_content_charset() or "utf-8"
                content_text = raw_bytes.decode(charset, errors="replace")
                data = extract_page_content(content_text, base_url=url)
                return {
                    "url": url,
                    "status_code": resp.status,
                    "title": data["title"],
                    "text": data["text"][:16000],  # bounded text preview
                    "forms": data["forms"]
                }
        except urllib.error.HTTPError as exc:
            raise WebError("HTTP_ERROR", f"HTTP {exc.code} fetching {url}")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise WebError("NETWORK_ERROR", f"Failed to connect to {url}: {exc}")

    def draft_form_submission(
        self,
        page_url: str,
        form_index: int = 0,
        fields: dict[str, Any] | None = None,
        actor_id: int | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    ) -> dict[str, Any]:
        """Inspect a page form, prepare draft values, and register a single-use approval request."""
        page_data = self.read_web_page(page_url, timeout_seconds=timeout_seconds)
        forms = page_data.get("forms", [])
        if form_index < 0 or form_index >= len(forms):
            raise WebError("FORM_NOT_FOUND", f"Form at index {form_index} not found on {page_url}")

        target_form = forms[form_index]
        action_url = target_form["action"]
        method = target_form["method"]

        # Fill default fields from form inputs
        draft: dict[str, Any] = {}
        for f in target_form.get("fields", []):
            draft[f["name"]] = f.get("value", "")

        # Override with user specified fields
        if fields:
            draft.update(fields)

        # Mask secrets in user-facing summary
        masked_draft = mask_sensitive_fields(draft)
        summary_text = (
            f"Invio modulo web:\n"
            f"- Pagina origine: {page_url}\n"
            f"- Destinazione: {action_url}\n"
            f"- Metodo: {method}\n"
            f"- Campi:\n" + "\n".join(f"  • {k}: {v}" for k, v in masked_draft.items())
        )

        approval_token = None
        digest = None
        if self.approval_store and actor_id:
            target = f"web_submit:{action_url}"
            arguments = {"method": method, "fields": draft}
            req = self.approval_store.request(actor_id=actor_id, target=target, arguments=arguments)
            approval_token = req["token"]
            digest = req["digest"]

        return {
            "status": "approval_required",
            "page_url": page_url,
            "action_url": action_url,
            "method": method,
            "draft": draft,
            "summary_text": summary_text,
            "approval_token": approval_token,
            "digest": digest
        }

    def submit_web_form(
        self,
        action_url: str,
        method: str,
        fields: dict[str, Any],
        approval_token: str | None,
        actor_id: int,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    ) -> dict[str, Any]:
        """Submit a form to an external URL strictly validating the single-use approval token."""
        if not approval_token:
            raise WebError("APPROVAL_REQUIRED", "Submitting external web forms strictly requires explicit user approval.")

        if not self.approval_store:
            raise WebError("APPROVAL_UNAVAILABLE", "Approval store not configured.")

        parsed = urllib.parse.urlparse(action_url)
        if parsed.scheme not in ("http", "https"):
            raise WebError("INVALID_URL", f"Only http and https schemes are allowed: {action_url!r}")

        target = f"web_submit:{action_url}"
        arguments = {"method": method.upper(), "fields": fields}

        try:
            decision = self.approval_store.decide(
                token=approval_token,
                actor_id=actor_id,
                target=target,
                arguments=arguments,
                approve=True
            )
        except ApprovalError as exc:
            raise WebError(exc.code, f"Approval check failed: {exc.code}")

        if decision.get("status") != "executed":
            raise WebError("APPROVAL_DENIED", "Approval was not granted for this exact submission.")

        # Perform the actual HTTP submission
        method = method.upper()
        if method == "POST":
            encoded_data = urllib.parse.urlencode(fields).encode("utf-8")
            req = urllib.request.Request(
                action_url,
                data=encoded_data,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": "Jarvis-Hermes/1.0 (+https://github.com/nuno80/Jarvis-hermes)"
                },
                method="POST"
            )
        elif method == "GET":
            query = urllib.parse.urlencode(fields)
            delim = "&" if "?" in action_url else "?"
            full_url = f"{action_url}{delim}{query}" if query else action_url
            req = urllib.request.Request(
                full_url,
                headers={"User-Agent": "Jarvis-Hermes/1.0 (+https://github.com/nuno80/Jarvis-hermes)"},
                method="GET"
            )
        else:
            raise WebError("UNSUPPORTED_METHOD", f"Method {method} is not supported for web form submit.")

        try:
            with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
                resp_body = resp.read(65536).decode("utf-8", errors="replace")
                return {
                    "submitted": True,
                    "status_code": resp.status,
                    "action_url": action_url,
                    "method": method,
                    "response_preview": redact_secrets(resp_body[:1000])
                }
        except urllib.error.HTTPError as exc:
            # An HTTP response was received (4xx, 5xx)
            return {
                "submitted": True,
                "status_code": exc.code,
                "action_url": action_url,
                "method": method,
                "response_preview": f"HTTP error {exc.code}"
            }
        except (TimeoutError, ConnectionResetError, BrokenPipeError, urllib.error.URLError, OSError) as exc:
            # Uncertain outcome: the request might have been sent and processed by the server,
            # or failed in transit. AT04 / C2: Never retry blindly!
            raise WebError("OUTCOME_UNKNOWN", f"Connection dropped or timed out during submission: {exc}. Do not retry blindly.")
