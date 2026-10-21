"""Tests for one-time Haier U+ Euro cloud discovery."""

from __future__ import annotations

import base64
from unittest.mock import AsyncMock, call, patch

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from custom_components.uplus_reborn import cloud


def test_password_encryption_matches_apk_rsa_format() -> None:
    """The APK uses DER public keys, PKCS#1 v1.5, and spaced base64."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=1024)
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    encoded = cloud._encrypt_password("secret", base64.b64encode(public_key).decode())

    assert [len(part) for part in encoded.split(" ")] == [60, 60, 52]
    plaintext = private_key.decrypt(
        base64.b64decode(encoded),
        padding.PKCS1v15(),
    )
    assert plaintext == b"secret"


async def test_discover_devices_uses_short_lived_account_state() -> None:
    """Discovery authenticates, resolves user ID, and returns typed devices."""
    client = cloud.UPlusCloudClient(AsyncMock())
    request = AsyncMock(
        side_effect=[
            {"retCode": "00000", "publicKey": "public-key"},
            {"retCode": "00000", "accessToken": "short-token"},
            {"retCode": "00000", "userId": "cloud-user"},
            {
                "retCode": "00000",
                "deviceinfos": [
                    {
                        "deviceId": "aabbccddeeff",
                        "deviceName": " Laundry ",
                        "online": True,
                        "wifiType": "profile-id",
                    }
                ],
            },
        ]
    )
    client._request = request

    with patch(
        "custom_components.uplus_reborn.cloud._encrypt_password",
        side_effect=lambda value, _key: f"encrypted:{value}",
    ):
        devices = await client.async_discover_devices(" owner@example.com ", "secret")

    assert devices == (
        cloud.CloudDevice(
            device_id="AABBCCDDEEFF",
            name="Laundry",
            online=True,
            wifi_type="profile-id",
        ),
    )
    assert request.await_args_list[0] == call("POST", cloud._PUBLIC_KEY_PATH)
    assert request.await_args_list[1] == call(
        "POST",
        cloud._LOGIN_PATH,
        body={
            "email": "encrypted:owner@example.com",
            "password": "encrypted:secret",
            "captcha": "",
        },
    )
    profile_credentials = request.await_args_list[2].kwargs["credentials"]
    assert profile_credentials.access_token == "short-token"
    assert profile_credentials.user_id == ""
    inventory_credentials = request.await_args_list[3].kwargs["credentials"]
    assert inventory_credentials.access_token == "short-token"
    assert inventory_credentials.user_id == "cloud-user"


async def test_discover_devices_rejects_failed_login() -> None:
    """A non-success login code is an authentication error, not an empty list."""
    client = cloud.UPlusCloudClient(AsyncMock())
    client._request = AsyncMock(
        side_effect=[
            {"retCode": "00000", "publicKey": "public-key"},
            {"retCode": "10001", "retInfo": "rejected"},
        ]
    )

    with (
        patch(
            "custom_components.uplus_reborn.cloud._encrypt_password",
            return_value="encrypted",
        ),
        pytest.raises(cloud.CloudInvalidAuth, match="rejected"),
    ):
        await client.async_discover_devices("owner@example.com", "wrong")
