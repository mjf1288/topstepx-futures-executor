"""
topstep_api.py — Direct ProjectX Gateway REST client.

Replaces the abandoned `project-x-py` wrapper library for all REST calls.
Talks directly to https://api.topstepx.com/api. No SDK dependency.

This module is intentionally small and boring:
- One class, `TopstepAPI`, that holds a JWT token and account id
- Methods that map 1:1 to ProjectX endpoints
- Every method raises on non-200 or on missing expected fields
- No caching, no state beyond auth token and account id
- No threading, no async — plain requests. The engine wraps calls
  in loop.run_in_executor() when needed.

Design rules:
- If a call fails, it fails LOUD. No silent empty returns.
- Auth token refresh happens exactly once on 401, then re-raises.
- All timestamps use ISO 8601 UTC with 'Z' suffix.
- Contract IDs are ProjectX-format strings, e.g. 'CON.F.US.MES.U26'.

Endpoints implemented (all documented at
https://gateway.docs.projectx.com):
- POST /Auth/loginKey                — authenticate, get JWT
- POST /Account/search                — list accounts, get account_id
- POST /Contract/search               — resolve symbol → contract id (unused
                                          in engine but useful for setup)
- POST /History/retrieveBars          — historical OHLCV bars
- POST /Order/place                   — place new order
- POST /Order/cancel                  — cancel by order id
- POST /Order/searchOpen              — list working orders
- POST /Position/searchOpen           — list open positions
- POST /Position/closeContract        — flatten a specific contract
"""

import os
import time
from datetime import datetime, timezone, timedelta
from typing import Any

import requests


BASE_URL = "https://api.topstepx.com/api"

# HTTP settings
REQUEST_TIMEOUT_S = 15
MAX_RETRIES_ON_5XX = 2
BACKOFF_S = 1.0


class TopstepAPIError(RuntimeError):
    """Raised on any non-recoverable API failure."""


class AccountInfo:
    """Lightweight account-info holder that mirrors the SDK's Account shape
    so existing engine code that reads account.id / account.name / account.balance
    keeps working unchanged."""

    def __init__(self, raw: dict):
        self.raw = raw
        self.id: int = raw["id"]
        self.name: str = raw.get("name", "")
        self.balance: float = float(raw.get("balance", 0.0))
        self.can_trade: bool = bool(raw.get("canTrade", True))
        self.simulated: bool = bool(raw.get("simulated", False))


class TopstepAPI:
    """Direct REST client for the ProjectX Gateway API.

    Usage:
        api = TopstepAPI(username="...", api_key="...", account_name="EXPRESS-V2-CT-...")
        api.authenticate()
        api.select_account()  # populates self.account_id
        bars = api.get_bars(contract_id="CON.F.US.MES.U26", unit=2, unit_number=5, days=5)
        api.place_limit_order(contract_id="CON.F.US.MES.U26", side=0, size=1, limit_price=7500.0)
    """

    def __init__(self, username: str, api_key: str, account_name: str):
        if not username:
            raise ValueError("username required")
        if not api_key:
            raise ValueError("api_key required")
        if not account_name:
            raise ValueError("account_name required")

        self.username = username
        self.api_key = api_key
        self.account_name = account_name

        self._token: str | None = None
        self._token_expires_at: float | None = None  # unix seconds
        self.account_id: int | None = None
        self._account_info: AccountInfo | None = None

    # ─────────────────────────────────────────────────────────────
    # Internals
    # ─────────────────────────────────────────────────────────────
    def _headers(self) -> dict:
        if not self._token:
            raise TopstepAPIError("Not authenticated. Call .authenticate() first.")
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    def _post(self, path: str, payload: dict, retry_auth: bool = True) -> dict:
        """POST with auth, retry-on-5xx, and single 401-retry after re-auth."""
        url = f"{BASE_URL}{path}"

        last_exc: Exception | None = None
        for attempt in range(1 + MAX_RETRIES_ON_5XX):
            try:
                resp = requests.post(
                    url,
                    json=payload,
                    headers=self._headers() if self._token else {"Content-Type": "application/json"},
                    timeout=REQUEST_TIMEOUT_S,
                )
            except requests.RequestException as e:
                last_exc = e
                if attempt < MAX_RETRIES_ON_5XX:
                    time.sleep(BACKOFF_S * (attempt + 1))
                    continue
                raise TopstepAPIError(f"Network error on POST {path}: {e!r}") from e

            # 401 → re-auth once, retry
            if resp.status_code == 401 and retry_auth and self._token:
                self._token = None
                self.authenticate()
                return self._post(path, payload, retry_auth=False)

            # 5xx → transient, retry with backoff
            if 500 <= resp.status_code < 600:
                last_exc = TopstepAPIError(
                    f"{path} returned {resp.status_code}: {resp.text[:200]}"
                )
                if attempt < MAX_RETRIES_ON_5XX:
                    time.sleep(BACKOFF_S * (attempt + 1))
                    continue
                raise last_exc

            # 4xx (other than 401) → raise with body
            if resp.status_code >= 400:
                raise TopstepAPIError(
                    f"{path} returned {resp.status_code}: {resp.text[:500]}"
                )

            # 200 OK
            try:
                return resp.json()
            except ValueError as e:
                raise TopstepAPIError(
                    f"{path} returned 200 but body was not JSON: {resp.text[:200]}"
                ) from e

        # Shouldn't reach here, but be safe.
        raise TopstepAPIError(f"POST {path} failed after retries: {last_exc!r}")

    # ─────────────────────────────────────────────────────────────
    # Auth
    # ─────────────────────────────────────────────────────────────
    def authenticate(self) -> str:
        """Fetch a fresh JWT token. Returns the token string.

        POST /Auth/loginKey
        Body:  {"userName": "...", "apiKey": "..."}
        Response: {"token": "eyJ...", "success": true, ...}
        """
        url = f"{BASE_URL}/Auth/loginKey"
        try:
            resp = requests.post(
                url,
                json={"userName": self.username, "apiKey": self.api_key},
                headers={"Content-Type": "application/json"},
                timeout=REQUEST_TIMEOUT_S,
            )
        except requests.RequestException as e:
            raise TopstepAPIError(f"Network error during authenticate: {e!r}") from e

        if resp.status_code != 200:
            raise TopstepAPIError(
                f"Auth failed ({resp.status_code}): {resp.text[:300]}"
            )

        try:
            data = resp.json()
        except ValueError as e:
            raise TopstepAPIError(f"Auth returned non-JSON: {resp.text[:200]}") from e

        token = data.get("token")
        if not token:
            raise TopstepAPIError(
                f"Auth 200 but no token in response: {data}"
            )

        self._token = token
        # Empirically JWTs from ProjectX last ~24hrs. We don't parse the token;
        # we just track when we fetched it and refresh proactively at 20hrs.
        self._token_expires_at = time.time() + 20 * 3600
        return token

    def maybe_refresh_token(self) -> None:
        """Call this periodically from the engine loop. Cheap when not needed."""
        if not self._token or not self._token_expires_at:
            self.authenticate()
            return
        if time.time() >= self._token_expires_at:
            self.authenticate()

    def get_jwt(self) -> str:
        """Return the current JWT — for passing to the WebSocket client."""
        if not self._token:
            raise TopstepAPIError("Not authenticated.")
        return self._token

    # ─────────────────────────────────────────────────────────────
    # SDK compatibility shim — lets existing engine code that calls
    # client.get_session_token() and client.base_url keep working
    # with no changes to place_or_update_entry(), check_and_bracket_fills(),
    # seed_historical(), or the position-monitoring loop.
    # ─────────────────────────────────────────────────────────────
    def get_session_token(self) -> str:
        """SDK-compat: same as get_jwt(). Engine code calls this."""
        return self.get_jwt()

    @property
    def base_url(self) -> str:
        """SDK-compat: base URL for direct-HTTP calls in the engine."""
        return BASE_URL

    # ─────────────────────────────────────────────────────────────
    # Account selection
    # ─────────────────────────────────────────────────────────────
    def select_account(self) -> AccountInfo:
        """Look up account by name. Populates self.account_id and
        self._account_info. Returns the AccountInfo object.

        POST /Account/search
        Body:  {"onlyActiveAccounts": true}
        Response: {"accounts": [{"id": 123, "name": "EXPRESS-...", ...}, ...]}
        """
        data = self._post("/Account/search", {"onlyActiveAccounts": True})
        accounts = data.get("accounts", [])
        if not accounts:
            raise TopstepAPIError("Account/search returned no accounts")

        for acct in accounts:
            if acct.get("name") == self.account_name:
                self.account_id = acct["id"]
                self._account_info = AccountInfo(acct)
                return self._account_info

        # Not found — list what IS available
        avail = [a.get("name") for a in accounts]
        raise TopstepAPIError(
            f"Account '{self.account_name}' not found. Available: {avail}"
        )

    def get_account_info(self) -> AccountInfo:
        """SDK-compat. Returns the AccountInfo populated by select_account()."""
        if not hasattr(self, "_account_info") or self._account_info is None:
            raise TopstepAPIError("Call select_account() first")
        return self._account_info

    # ─────────────────────────────────────────────────────────────
    # Contracts
    # ─────────────────────────────────────────────────────────────
    def search_contract(self, symbol: str) -> list[dict]:
        """Search for contract by root symbol (e.g. 'MES', 'MNQ'). Returns list.

        POST /Contract/search
        Body:  {"searchText": "MES", "live": false}
        Response: {"contracts": [{"id": "CON.F.US.MES.U26", "name": "MESU6", ...}, ...]}
        """
        data = self._post("/Contract/search", {"searchText": symbol, "live": False})
        return data.get("contracts", [])

    # ─────────────────────────────────────────────────────────────
    # Historical bars
    # ─────────────────────────────────────────────────────────────
    def get_bars(
        self,
        contract_id: str,
        unit: int,
        unit_number: int,
        days: int | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        limit: int = 5000,
        live: bool = False,
        include_partial: bool = True,
    ) -> list[dict]:
        """Fetch historical OHLCV bars.

        POST /History/retrieveBars
        unit: 1=sec, 2=min, 3=hour, 4=day
        unit_number: bar size (e.g. 5 for 5-min bars when unit=2)

        Either pass `days` (window back from now) or explicit start_time/end_time.

        Returns list of dicts with keys t (ISO string), o, h, l, c, v.
        """
        if start_time is None or end_time is None:
            if days is None:
                raise ValueError("Pass either `days` or (start_time, end_time)")
            end_time = datetime.now(timezone.utc)
            start_time = end_time - timedelta(days=days)

        payload = {
            "contractId": contract_id,
            "live": live,
            "startTime": start_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "endTime": end_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "unit": unit,
            "unitNumber": unit_number,
            "limit": limit,
            "includePartialBar": include_partial,
        }

        data = self._post("/History/retrieveBars", payload)
        return data.get("bars", [])

    # ─────────────────────────────────────────────────────────────
    # Orders
    # ─────────────────────────────────────────────────────────────
    # ProjectX order side: 0=Buy, 1=Sell
    # ProjectX order type: 1=Limit, 2=Market, 3=StopLimit, 4=Stop, 5=TrailingStop

    def place_limit_order(
        self,
        contract_id: str,
        side: int,
        size: int,
        limit_price: float,
        custom_tag: str | None = None,
    ) -> dict:
        """Place a LIMIT (type=1) order. Returns response dict with orderId."""
        if self.account_id is None:
            raise TopstepAPIError("account_id not set; call select_account() first")
        if side not in (0, 1):
            raise ValueError(f"side must be 0 (buy) or 1 (sell), got {side}")
        if size <= 0:
            raise ValueError(f"size must be positive, got {size}")

        payload = {
            "accountId": self.account_id,
            "contractId": contract_id,
            "type": 1,
            "side": side,
            "size": size,
            "limitPrice": limit_price,
        }
        if custom_tag:
            payload["customTag"] = custom_tag

        data = self._post("/Order/place", payload)
        if not data.get("success"):
            raise TopstepAPIError(f"Order/place failed: {data}")
        return data

    def place_stop_order(
        self,
        contract_id: str,
        side: int,
        size: int,
        stop_price: float,
        custom_tag: str | None = None,
    ) -> dict:
        """Place a STOP (type=4) order. Used for stop-losses on filled entries."""
        if self.account_id is None:
            raise TopstepAPIError("account_id not set; call select_account() first")
        if side not in (0, 1):
            raise ValueError(f"side must be 0 (buy) or 1 (sell), got {side}")

        payload = {
            "accountId": self.account_id,
            "contractId": contract_id,
            "type": 4,
            "side": side,
            "size": size,
            "stopPrice": stop_price,
        }
        if custom_tag:
            payload["customTag"] = custom_tag

        data = self._post("/Order/place", payload)
        if not data.get("success"):
            raise TopstepAPIError(f"Order/place (stop) failed: {data}")
        return data

    def cancel_order(self, order_id: int) -> dict:
        """Cancel a working order by id."""
        if self.account_id is None:
            raise TopstepAPIError("account_id not set; call select_account() first")

        payload = {"accountId": self.account_id, "orderId": order_id}
        return self._post("/Order/cancel", payload)

    def get_open_orders(self) -> list[dict]:
        """List all working orders for this account.

        POST /Order/searchOpen
        Body: {"accountId": 123}
        Response: {"orders": [...]} or bare list depending on API version.
        """
        if self.account_id is None:
            raise TopstepAPIError("account_id not set; call select_account() first")

        data = self._post("/Order/searchOpen", {"accountId": self.account_id})
        # Handle both response shapes defensively (same fix that landed on main in bf23719)
        if isinstance(data, dict):
            return data.get("orders", [])
        if isinstance(data, list):
            return data
        raise TopstepAPIError(f"Order/searchOpen unexpected shape: {type(data).__name__}")

    # ─────────────────────────────────────────────────────────────
    # Positions
    # ─────────────────────────────────────────────────────────────
    def get_open_positions(self) -> list[dict]:
        """List all open positions for this account.

        POST /Position/searchOpen
        Body: {"accountId": 123}
        Response: {"positions": [...]} or bare list.
        """
        if self.account_id is None:
            raise TopstepAPIError("account_id not set; call select_account() first")

        data = self._post("/Position/searchOpen", {"accountId": self.account_id})
        if isinstance(data, dict):
            return data.get("positions", [])
        if isinstance(data, list):
            return data
        raise TopstepAPIError(f"Position/searchOpen unexpected shape: {type(data).__name__}")

    def close_position(self, contract_id: str) -> dict:
        """Flatten (market close) an open position on a specific contract."""
        if self.account_id is None:
            raise TopstepAPIError("account_id not set; call select_account() first")

        payload = {"accountId": self.account_id, "contractId": contract_id}
        return self._post("/Position/closeContract", payload)


# ─────────────────────────────────────────────────────────────
# Convenience: load from .env and construct a ready client
# ─────────────────────────────────────────────────────────────
def from_env() -> TopstepAPI:
    """Read PROJECT_X_USERNAME / _API_KEY / _ACCOUNT_NAME from environment
    (populated by dotenv in the engine's startup) and return an authenticated
    + account-selected client ready to use."""
    username = os.environ.get("PROJECT_X_USERNAME")
    api_key = os.environ.get("PROJECT_X_API_KEY")
    account = os.environ.get("PROJECT_X_ACCOUNT_NAME")

    if not username or not api_key or not account:
        raise TopstepAPIError(
            "Missing PROJECT_X_USERNAME / PROJECT_X_API_KEY / "
            "PROJECT_X_ACCOUNT_NAME in environment"
        )

    api = TopstepAPI(username=username, api_key=api_key, account_name=account)
    api.authenticate()
    api.select_account()
    return api
