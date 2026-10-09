"""Sign in with ChatGPT (plan usage): script drafting that spends the connected person's
ChatGPT plan allowance instead of our own provider credits.

OAuth 2.0 authorization code + PKCE against auth.openai.com, with a loopback redirect to this
backend (http://127.0.0.1:<port>/auth/callback), so the backend must run on the machine whose
browser signs in. For a VPS, sign in locally and copy CHATGPT_AUTH_FILE to the server. One
account per install: everyone who uses this instance spends that account's plan.

This is OpenAI's open-source / local-client flow. Hosted multi-user sites need OpenAI's approval
(https://openai.com/form/sign-in-with-chatgpt-interest/). Endpoints, scopes and error codes come
from developers.openai.com/siwc; the refresh request, the /models list and the Responses event
format were taken from third-party write-ups and are UNVERIFIED: check them on the first sign-in.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlencode

import httpx
import jwt
from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

log = logging.getLogger("reachout.chatgpt")

ISSUER = "https://auth.openai.com"
AUTHORIZE = f"{ISSUER}/api/accounts/authorize"
TOKEN = f"{ISSUER}/api/accounts/oauth/token"
REVOKE = f"{ISSUER}/api/accounts/oauth/revoke"
JWKS = f"{ISSUER}/.well-known/jwks.json"
API = "https://api.openai.com/v1"
SCOPE = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
AGENT_NAME = "Reachout"
PENDING_TTL = 600
RETURN_PAGES = ("/campaigns/new", "/overview")  # where the browser lands after sign-in

router = APIRouter()
_lock = threading.Lock()
_pending: dict[str, dict] = {}
_model: str | None = None


class ChatGPTError(Exception):
    """`reason` is safe to show the user; nothing from the response body is kept."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class Reauthorize(Exception):
    """Registration finished; the browser must go round once more with the issued client id."""

    def __init__(self, url: str):
        super().__init__(url)
        self.url = url


def _auth_file() -> Path:
    return Path(os.getenv("CHATGPT_AUTH_FILE", "chatgpt_auth.json"))


def _load() -> dict:
    try:
        return json.loads(_auth_file().read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def _save(data: dict) -> None:
    path = _auth_file()
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data), "utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def _redirect_uri() -> str:
    return f"http://127.0.0.1:{os.getenv('CHATGPT_REDIRECT_PORT', '8000')}/auth/callback"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _scopes(tok: dict) -> list[str]:
    s = tok.get("scope") or tok.get("scopes") or []
    return s.split() if isinstance(s, str) else list(s)


def connected() -> bool:
    d = _load()
    return bool(d.get("refresh_token")) and PLAN_SCOPE in d.get("scopes", [])


def status() -> dict:
    d = _load()
    return {"connected": connected(), "email": d.get("email") if connected() else None,
            "redirect_uri": _redirect_uri()}


# ---------- sign-in ----------

def start(next_page: str = RETURN_PAGES[0]) -> str:
    """Authorize URL to open in the browser. Registers a client on the first sign-in."""
    d = _load()
    if not str(d.get("host_id", "")).startswith("urn:uuid:"):  # OpenAI's example shows a URN-style UUID
        d["host_id"] = f"urn:uuid:{uuid.uuid4()}"
    _save(d)
    verifier, state, nonce = secrets.token_urlsafe(48), secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    now = time.time()
    for k in [k for k, v in _pending.items() if now - v["at"] > PENDING_TTL]:
        del _pending[k]
    client_id = d.get("client_id")
    _pending[state] = {"verifier": verifier, "nonce": nonce, "client_id": client_id, "at": now,
                       "next": next_page if next_page in RETURN_PAGES else RETURN_PAGES[0]}
    q = {"client_id": client_id or "dynamic_agent_client", "ext_agent_host_id": d["host_id"],
         "response_type": "code", "redirect_uri": _redirect_uri(), "scope": SCOPE, "resource": API,
         "state": state, "nonce": nonce, "code_challenge_method": "S256",
         "code_challenge": _b64(hashlib.sha256(verifier.encode()).digest())}
    if client_id:
        if d.get("email"):
            q["login_hint"] = d["email"]
    else:
        q["agent_name_hint"] = AGENT_NAME
    return f"{AUTHORIZE}?{urlencode(q)}"


def finish(params: dict[str, str]) -> None:
    """Handle the callback; raises ChatGPTError with a user-safe reason."""
    pend = _pending.pop(params.get("state", ""), None)
    if not pend or time.time() - pend["at"] > PENDING_TTL:
        raise ChatGPTError("The sign-in expired. Start again.")
    if params.get("error"):
        raise ChatGPTError("Sign-in was cancelled." if params["error"] == "access_denied" else "Sign-in was refused.")
    code, issued = params.get("code"), params.get("client_id") or pend["client_id"]
    if not code or not issued or (pend["client_id"] and params.get("client_id") not in (None, pend["client_id"])):
        raise ChatGPTError("Sign-in did not finish. Start again.")
    step = "token exchange"
    try:
        resp = httpx.post(TOKEN, timeout=30, data={
            "grant_type": "authorization_code", "client_id": issued, "code": code, "code_verifier": pend["verifier"],
            "redirect_uri": _redirect_uri(), "resource": API})
        if resp.status_code != 200:
            err = _error_code(resp)
            log.warning("ChatGPT sign-in: token exchange returned HTTP %s, error=%r", resp.status_code, err)
            if err.startswith("invalid_grant") and not pend["client_id"]:
                # First round only registered the client: keep its id and authorize again with it (once).
                with _lock:
                    _save(_load() | {"client_id": issued})
                raise Reauthorize(start(pend["next"]))
            raise ChatGPTError("Sign-in could not be completed.")
        tok = resp.json()
        step = "ID token check"
        claims = jwt.decode(tok["id_token"], jwt.PyJWKClient(JWKS).get_signing_key_from_jwt(tok["id_token"]).key,
                            algorithms=["RS256", "ES256"], audience=issued, issuer=ISSUER)
    except (ChatGPTError, Reauthorize):
        raise
    except Exception as exc:  # network, HTTP, bad token: never echo the body or the tokens
        log.warning("ChatGPT sign-in failed at %s: %s: %s", step, type(exc).__name__, str(exc)[:200])
        raise ChatGPTError("Sign-in could not be completed.") from exc
    if claims.get("nonce") != pend["nonce"]:
        raise ChatGPTError("Sign-in could not be verified.")
    scopes = _scopes(tok) or params.get("scope", "").split()
    if PLAN_SCOPE not in scopes or not tok.get("refresh_token"):
        raise ChatGPTError("Plan usage was not allowed, so ChatGPT cannot be used here.")
    with _lock:
        d = _load()
        if d.get("sub") and d["sub"] != claims["sub"]:
            d = {"host_id": d.get("host_id")}  # a different account replaces the old credentials
        _save(d | {"client_id": issued, "sub": claims["sub"], "email": claims.get("email"), "scopes": scopes,
                   "access_token": tok["access_token"], "refresh_token": tok["refresh_token"],
                   "expires_at": time.time() + int(tok.get("expires_in", 3600))})


def disconnect() -> None:
    with _lock:
        d = _load()
        if d.get("refresh_token") and d.get("client_id"):
            try:
                httpx.post(REVOKE, timeout=10, data={"client_id": d["client_id"], "token": d["refresh_token"]})
            except httpx.HTTPError:
                pass
        _save({"host_id": d.get("host_id")} if d.get("host_id") else {})


def _token() -> str:
    with _lock:
        d = _load()
        if not connected():
            raise ChatGPTError("ChatGPT is not connected")
        if d["expires_at"] - 60 > time.time():
            return d["access_token"]
        try:
            resp = httpx.post(TOKEN, timeout=30, data={
                "grant_type": "refresh_token", "client_id": d["client_id"],
                "refresh_token": d["refresh_token"], "resource": API})
            resp.raise_for_status()
            tok = resp.json()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (400, 401):  # refresh token expired or revoked
                _save({k: d[k] for k in ("host_id", "client_id", "sub", "email") if k in d})
                raise ChatGPTError("The ChatGPT sign-in expired; connect again") from exc
            raise ChatGPTError("ChatGPT sign-in could not be refreshed") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise ChatGPTError("ChatGPT sign-in could not be refreshed") from exc
        # the refresh token rotates: store the replacement together with the new access token
        d |= {"access_token": tok["access_token"], "refresh_token": tok.get("refresh_token", d["refresh_token"]),
              "expires_at": time.time() + int(tok.get("expires_in", 3600)), "scopes": _scopes(tok) or d["scopes"]}
        _save(d)
        return d["access_token"]


# ---------- model call ----------

def _pick_model(headers: dict) -> str:
    global _model
    if os.getenv("CHATGPT_MODEL"):
        return os.environ["CHATGPT_MODEL"]
    if _model is None:
        resp = httpx.get(f"{API}/models", headers=headers, timeout=30)
        resp.raise_for_status()
        listed = [m["id"] for m in resp.json().get("data", []) if m.get("visibility", "list") == "list"]
        if not listed:
            raise ChatGPTError("ChatGPT offered no model; set CHATGPT_MODEL")
        _model = listed[0]
    return _model


def _error_code(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        err = body.get("error", {})
        if isinstance(err, dict):
            return str(err.get("code") or err.get("type") or "")
        return f"{err} {body.get('error_description', '')}".strip()[:200]
    except ValueError:
        return ""


def complete(messages: list[dict]) -> str:
    """One Responses API call on the connected plan; returns the text. Raises ChatGPTError."""
    headers = {"Authorization": f"Bearer {_token()}"}
    try:
        model = _pick_model(headers)
        body = {"model": model, "stream": True, "store": False,
                "instructions": "\n".join(m["content"] for m in messages if m["role"] == "system"),
                "input": [{"role": m["role"], "content": m["content"]} for m in messages if m["role"] != "system"]}
        parts: list[str] = []
        with httpx.stream("POST", f"{API}/responses", headers=headers, json=body, timeout=120) as resp:
            if resp.status_code != 200:
                resp.read()
                code = _error_code(resp)
                if code == "subscription_sharing_usage_limit_exceeded":
                    raise ChatGPTError("the ChatGPT plan limit for this app is used up")
                if code == "subscription_sharing_user_not_eligible":
                    raise ChatGPTError("this ChatGPT account cannot share plan usage")
                raise ChatGPTError(f"ChatGPT returned HTTP {resp.status_code}")
            for line in resp.iter_lines():
                if not line.startswith("data:") or line[5:].strip() == "[DONE]":
                    continue
                ev = json.loads(line[5:])
                kind = ev.get("type")
                if kind == "response.output_text.delta":
                    parts.append(ev.get("delta", ""))
                elif kind in ("response.failed", "error"):
                    raise ChatGPTError("ChatGPT could not finish the reply")
    except ChatGPTError:
        raise
    except (httpx.HTTPError, ValueError) as exc:
        raise ChatGPTError("ChatGPT could not be reached") from exc
    if not parts:
        raise ChatGPTError("ChatGPT returned no text")
    return "".join(parts)


# ---------- endpoints ----------

@router.get("/api/auth/chatgpt")
def get_status():
    return status()


class StartReq(BaseModel):
    next: str = RETURN_PAGES[0]


@router.post("/api/auth/chatgpt/start")
def post_start(body: StartReq | None = None):
    return {"url": start(body.next if body else RETURN_PAGES[0])}


@router.post("/api/auth/chatgpt/disconnect")
def post_disconnect():
    disconnect()
    return status()


@router.get("/auth/callback")
def callback(code: str = "", state: str = "", error: str = "", client_id: str = "", scope: str = ""):
    """OpenAI redirects the browser here; then on to the dashboard with the result."""
    nxt = _pending.get(state, {}).get("next", RETURN_PAGES[0])
    back = os.getenv("FRONTEND_URL", "http://localhost:3000").rstrip("/") + nxt
    try:
        finish({"code": code, "state": state, "error": error, "client_id": client_id, "scope": scope})
    except Reauthorize as again:
        return RedirectResponse(again.url)
    except ChatGPTError as exc:
        return RedirectResponse(f"{back}?{urlencode({'chatgpt': 'error', 'reason': exc.reason})}")
    return RedirectResponse(f"{back}?chatgpt=connected")
