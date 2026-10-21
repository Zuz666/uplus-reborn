"""Minimal Haier U+ Euro cloud client for one-time appliance discovery."""

from __future__ import annotations

import base64
import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from aiohttp import ClientError, ClientSession
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.serialization import load_der_public_key

_BASE_URL = "https://uws-euro.haieriot.net"
_PUBLIC_KEY_PATH = "/uam/v2/mgr/getPublicKey"
_LOGIN_PATH = "/uam/v2/user/loginEmailAcounnt"
_PROFILE_PATH = "/uam/v1/users/get"
_DEVICES_PATH = "/uds/v1/protected/deviceinfos"
_APP_ID = "MB-HEYJOZB-0000"
_APP_KEY = "4fe8b49c445b39df77758bffb7606c80"
_APP_VERSION = "2.6.0"
_REQUEST_TIMEOUT = 15
_SUCCESS_CODE = "00000"
_DEVICE_ID_LENGTH = 12
_HEX_DIGITS = frozenset("0123456789ABCDEF")


class CloudError(Exception):
    """Base class for U+ cloud discovery failures."""


class CloudCannotConnect(CloudError):
    """The U+ Euro service could not be reached."""


class CloudInvalidAuth(CloudError):
    """The U+ Euro service rejected the account credentials."""


class CloudInvalidResponse(CloudError):
    """The U+ Euro service returned an unsupported response."""


@dataclass(frozen=True, slots=True)
class CloudDevice:
    """One appliance returned by the owner's U+ account."""

    device_id: str
    name: str
    online: bool
    wifi_type: str


@dataclass(frozen=True, slots=True)
class _CloudCredentials:
    """Short-lived authorization state used only inside one config flow."""

    access_token: str
    user_id: str


def _signature(path: str, body: str, timestamp: str) -> str:
    """Reproduce the request signature used by Haier U+ 2.6.0."""
    stripped_body = body.strip().translate(str.maketrans("", "", " \t\r\n"))
    stripped_key = _APP_KEY.strip().replace('"', "")
    payload = f"{path}{stripped_body}{_APP_ID}{stripped_key}{timestamp}"
    return hashlib.sha256(payload.encode()).hexdigest()


def _encrypt_password(password: str, encoded_public_key: str) -> str:
    """Encrypt a password exactly as the legacy Android client does."""
    try:
        public_key = load_der_public_key(base64.b64decode(encoded_public_key))
    except (TypeError, ValueError) as err:
        raise CloudInvalidResponse("Cloud public key is invalid") from err
    if not isinstance(public_key, rsa.RSAPublicKey):
        raise CloudInvalidResponse("Cloud public key is not RSA")

    plaintext = password.encode()
    chunk_size = public_key.key_size // 8 - 11
    encrypted = b"".join(
        public_key.encrypt(
            plaintext[offset : offset + chunk_size],
            padding.PKCS1v15(),
        )
        for offset in range(0, len(plaintext), chunk_size)
    )
    encoded = base64.b64encode(encrypted).decode()
    return " ".join(
        encoded[offset : offset + 60] for offset in range(0, len(encoded), 60)
    )


def _timezone_name() -> str:
    """Return the local IANA-style timezone name expected by the legacy API."""
    timezone = datetime.now().astimezone().tzinfo
    return getattr(timezone, "key", None) or str(timezone or "UTC")


def _device_id(value: Any) -> str:
    """Normalize and validate a cloud appliance identifier."""
    if not isinstance(value, str):
        raise CloudInvalidResponse("Cloud appliance has no device ID")
    normalized = value.strip().upper()
    if len(normalized) != _DEVICE_ID_LENGTH or not set(normalized) <= _HEX_DIGITS:
        raise CloudInvalidResponse("Cloud appliance has an invalid device ID")
    return normalized


class UPlusCloudClient:
    """Discover account appliances without retaining account credentials."""

    def __init__(self, session: ClientSession) -> None:
        """Initialize one short-lived cloud session."""
        self._session = session
        self._client_id = str(uuid.uuid4())
        self._sequence = 0

    def _headers(
        self,
        path: str,
        body: str,
        *,
        access_token: str = "",
        user_id: str = "",
    ) -> dict[str, str]:
        """Build the signed headers used by the legacy Euro application."""
        timestamp = str(int(time.time() * 1000))
        sequence_id = f"{datetime.now().astimezone():%Y%m%d%H%M%S}{self._sequence:06d}"
        self._sequence = (self._sequence + 1) % 1_000_000
        return {
            "Content-Type": "application/json;charset=utf-8",
            "appId": _APP_ID,
            "appKey": _APP_KEY,
            "appVersion": _APP_VERSION,
            "clientId": self._client_id,
            "timezone": _timezone_name(),
            "accessToken": access_token,
            "userId": user_id,
            "sequenceId": sequence_id,
            "timestamp": timestamp,
            "sign": _signature(path, body, timestamp),
            "language": "en",
            "privacyVersion": "V1.0.0",
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, str] | None = None,
        credentials: _CloudCredentials | None = None,
    ) -> dict[str, Any]:
        """Send one signed request and return its JSON object."""
        encoded_body = (
            json.dumps(body, ensure_ascii=False, separators=(",", ":"))
            if body is not None
            else ""
        )
        headers = self._headers(
            path,
            encoded_body,
            access_token=credentials.access_token if credentials else "",
            user_id=credentials.user_id if credentials else "",
        )
        try:
            async with self._session.request(
                method,
                f"{_BASE_URL}{path}",
                data=encoded_body or None,
                headers=headers,
                timeout=_REQUEST_TIMEOUT,
            ) as response:
                response.raise_for_status()
                payload = await response.json(content_type=None)
        except (ClientError, TimeoutError) as err:
            raise CloudCannotConnect("U+ Euro cloud request failed") from err
        except ValueError as err:
            raise CloudInvalidResponse("U+ Euro cloud returned invalid JSON") from err

        if not isinstance(payload, dict):
            raise CloudInvalidResponse("U+ Euro cloud response is not an object")
        return payload

    async def _async_get_public_key(self) -> str:
        """Fetch the RSA key required by the legacy email login flow."""
        payload = await self._request("POST", _PUBLIC_KEY_PATH)
        if str(payload.get("retCode", "")) != _SUCCESS_CODE:
            raise CloudInvalidResponse(
                str(payload.get("retInfo") or "Public-key request rejected")
            )
        public_key = payload.get("publicKey")
        if not isinstance(public_key, str) or not public_key:
            raise CloudInvalidResponse("Public-key response has no key")
        return public_key

    async def _async_login(
        self, encrypted_email: str, encrypted_password: str
    ) -> _CloudCredentials:
        """Authenticate using the legacy application's email flow."""
        payload = await self._request(
            "POST",
            _LOGIN_PATH,
            body={
                "email": encrypted_email,
                "password": encrypted_password,
                "captcha": "",
            },
        )
        if str(payload.get("retCode", "")) != _SUCCESS_CODE:
            raise CloudInvalidAuth(str(payload.get("retInfo") or "Login rejected"))

        access_token = payload.get("accessToken")
        if not isinstance(access_token, str) or not access_token:
            raise CloudInvalidResponse("Login response has no access token")
        return _CloudCredentials(access_token=access_token, user_id="")

    async def _async_get_profile_credentials(
        self, credentials: _CloudCredentials
    ) -> _CloudCredentials:
        """Resolve the user ID required by subsequent protected requests."""
        payload = await self._request("POST", _PROFILE_PATH, credentials=credentials)
        if str(payload.get("retCode", "")) != _SUCCESS_CODE:
            raise CloudInvalidResponse(
                str(payload.get("retInfo") or "User profile rejected")
            )
        user_id = payload.get("userId")
        if not isinstance(user_id, str) or not user_id:
            raise CloudInvalidResponse("User profile has no user ID")
        return _CloudCredentials(
            access_token=credentials.access_token,
            user_id=user_id,
        )

    async def async_discover_devices(
        self, email: str, password: str
    ) -> tuple[CloudDevice, ...]:
        """Return appliances visible to the account, then discard login state."""
        if not email.strip() or not password:
            raise CloudInvalidAuth("Email and password are required")
        public_key = await self._async_get_public_key()
        credentials = await self._async_login(
            _encrypt_password(email.strip(), public_key),
            _encrypt_password(password, public_key),
        )
        credentials = await self._async_get_profile_credentials(credentials)
        payload = await self._request("GET", _DEVICES_PATH, credentials=credentials)
        if str(payload.get("retCode", "")) != _SUCCESS_CODE:
            raise CloudInvalidResponse(
                str(payload.get("retInfo") or "Device inventory rejected")
            )

        raw_devices = payload.get("deviceinfos")
        if not isinstance(raw_devices, list):
            raise CloudInvalidResponse("Device inventory has no device list")

        devices: list[CloudDevice] = []
        for raw_device in raw_devices:
            if not isinstance(raw_device, dict):
                raise CloudInvalidResponse("Device inventory item is not an object")
            name = raw_device.get("deviceName")
            wifi_type = raw_device.get("wifiType")
            devices.append(
                CloudDevice(
                    device_id=_device_id(raw_device.get("deviceId")),
                    name=name.strip() if isinstance(name, str) else "",
                    online=raw_device.get("online") is True,
                    wifi_type=wifi_type if isinstance(wifi_type, str) else "",
                )
            )
        return tuple(devices)
