"""
VWorld session manager — httpx client with cookie persistence and re-auth.

Credentials held in memory only for the session duration. Environment
variables (VWORLD_URL, VWORLD_USERNAME, VWORLD_PASSWORD) provide
auto-fill for the login form.

Supports both authenticated (VWorld login) and public (no-auth) modes.
"""

import logging
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urljoin

import httpx

from .respect import BROWSER_UA, check_robots

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30  # seconds


@dataclass
class SessionInfo:
    """Lightweight metadata about the session."""
    host: str
    authenticated: bool = False
    auth_required: bool = True


class CrawlSession:
    """Manages an HTTP session for crawling (auth or public).

    Replaces the old VWorldSession; supports both login-required portals
    and public data catalogues.
    """

    def __init__(
        self,
        host: str = "",
        username: str = "",
        password: str = "",
        auth_required: bool = True,
        timeout: int = DEFAULT_TIMEOUT,
    ):
        self._host = host
        self._username = username
        self._password = password
        self._auth_required = auth_required
        self._timeout = timeout
        self._client: httpx.Client | None = None
        self._authenticated = False
        self._login_url: str = ""
        self._target_url: str = ""
        self.info = SessionInfo(host=host, auth_required=auth_required)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def client(self) -> httpx.Client:
        """Return the httpx client, authenticating if needed."""
        if self._client is None:
            self._connect()
        return self._client

    @property
    def authenticated(self) -> bool:
        return self._authenticated or not self._auth_required

    @property
    def host(self) -> str:
        return self._host

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def _connect(self):
        """Create the httpx client. Without auth_required, skips login."""
        self._client = httpx.Client(
            headers={
                "User-Agent": BROWSER_UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
            },
            follow_redirects=True,
            timeout=self._timeout,
        )
        if self._auth_required:
            self._authenticated = False
        else:
            self._authenticated = True  # public: always "authenticated"
            self.info.authenticated = True

    def close(self):
        """Close the httpx client."""
        if self._client:
            self._client.close()
            self._client = None
            self._authenticated = False
            self.info.authenticated = False

    # ------------------------------------------------------------------
    # Login (auth-required mode)
    # ------------------------------------------------------------------

    def login(
        self,
        login_url: str | None = None,
        target_url: str | None = None,
    ) -> bool:
        """Authenticate to the portal. No-op for public mode."""
        if not self._auth_required:
            self._authenticated = True
            self.info.authenticated = True
            return True

        # Close old client if re-authenticating
        if self._client:
            old = self._client
            self._client = None
            try:
                old.close()
            except Exception:
                pass

        self._connect()
        assert self._client is not None

        self._login_url = login_url or f"{self._host}/login"
        self._target_url = target_url or f"{self._host}/data/download"

        try:
            check_robots(self._login_url)

            logger.info("Fetching login page: %s", self._login_url)
            resp = self._client.get(self._login_url)
            resp.raise_for_status()

            from bs4 import BeautifulSoup
            soup = BeautifulSoup(resp.text, "html.parser")
            form_data = _extract_login_form(soup, self._username, self._password)

            if form_data is None:
                form_data = {
                    "username": self._username,
                    "password": self._password,
                    "user_id": self._username,
                    "user_pw": self._password,
                }

            logger.info("Posting login credentials to %s", self._login_url)
            resp = self._client.post(
                self._login_url,
                data=form_data,
                headers={"Referer": self._login_url},
            )
            resp.raise_for_status()

            # Verify
            resp = self._client.get(self._target_url)
            resp.raise_for_status()

            # Heuristic: redirected back to login?
            if "login" in str(resp.url).lower() and resp.status_code < 400:
                soup_check = BeautifulSoup(resp.text, "html.parser")
                if soup_check.find("form", {"action": lambda a: a and "login" in a.lower()}):
                    logger.error("Login failed — redirected back to login page")
                    self._authenticated = False
                    self.info.authenticated = False
                    return False

            self._authenticated = True
            self.info.authenticated = True
            logger.info("Authenticated to %s", self._host)
            return True

        except httpx.HTTPError as e:
            logger.error("Login HTTP error: %s", e)
            self._authenticated = False
            self.info.authenticated = False
            return False
        except Exception as e:
            logger.exception("Login unexpected error: %s", e)
            self._authenticated = False
            self.info.authenticated = False
            return False

    # ------------------------------------------------------------------
    # Session health
    # ------------------------------------------------------------------

    def check_session(self) -> bool:
        """Verify the session is still valid."""
        if not self._auth_required:
            return True
        if not self._authenticated or self._client is None:
            return False
        try:
            resp = self._client.get(self._target_url)
            if resp.status_code == 200:
                if "login" in str(resp.url).lower():
                    logger.warning("Session expired (redirect to login)")
                    self._authenticated = False
                    self.info.authenticated = False
                    return False
                return True
            return False
        except Exception:
            self._authenticated = False
            self.info.authenticated = False
            return False

    def ensure_session(self) -> bool:
        """Check session; re-authenticate if expired."""
        if not self._auth_required:
            return True
        if self.check_session():
            return True
        logger.info("Re-authenticating...")
        return self.login()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_login_form(soup, username: str, password: str) -> dict | None:
    """Extract form fields from the login page HTML.

    Finds the form containing a password-type input (most reliable signal).
    """
    # Find the form that contains a password field
    pw_input = soup.find("input", {"type": "password"})
    if pw_input:
        form = pw_input.find_parent("form")
    else:
        form = soup.find("form")
        if form is None:
            for fid in ("loginForm", "login_form", "login", "memberLogin"):
                form = soup.find("form", id=fid)
                if form:
                    break

    if form is None:
        return None

    data: dict[str, str] = {}
    for inp in form.find_all(["input", "select", "textarea"]):
        name = inp.get("name")
        if not name:
            continue
        tag = inp.name.lower()
        if tag == "input" and inp.get("type", "") == "submit":
            continue
        if tag == "input" and inp.get("type", "") in ("reset", "button"):
            continue

        nlower = name.lower()
        if any(k in nlower for k in ("user", "id", "login", "email", "account")):
            data[name] = username
        elif any(k in nlower for k in ("pass", "pw", "pwd", "secret")):
            data[name] = password
        else:
            data[name] = inp.get("value", "")

    # Ensure username and password are always present
    has_user = any(any(k in n.lower() for k in ("user", "id", "login", "email")) for n in data)
    has_pass = any(any(k in n.lower() for k in ("pass", "pw", "pwd")) for n in data)
    if not has_user:
        data["username"] = username
    if not has_pass:
        data["password"] = password

    return data
