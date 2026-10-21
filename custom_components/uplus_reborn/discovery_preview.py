"""Authenticated stock config-flow previews; no entities or custom frontend."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import UnknownFlow
from homeassistant.helpers.singleton import singleton

from .const import DOMAIN

DISCOVERY_FLOWS = f"{DOMAIN}_discovery_flows"


@singleton(f"{DOMAIN}_discovery_preview")
async def async_setup_discovery_preview(hass: HomeAssistant) -> None:
    """Serve the packaged animation and register the native preview command once."""
    await hass.http.async_register_static_paths(
        [StaticPathConfig(
            "/uplus_reborn_static/discovery.svg",
            str(Path(__file__).parent / "static" / "discovery.svg"),
            cache_headers=True,
        )]
    )
    websocket_api.async_register_command(hass, ws_start_preview)


@websocket_api.websocket_command({
    vol.Required("type"): f"{DOMAIN}/start_preview",
    vol.Required("flow_id"): str,
    vol.Required("flow_type"): "config_flow",
    vol.Required("user_input"): vol.Schema({}),
})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_start_preview(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Subscribe only to this administrator's active discovery form."""
    from .config_flow import UplusRebornConfigFlow

    try:
        status = hass.config_entries.flow.async_get(msg["flow_id"])
        flow = hass.data.get(DISCOVERY_FLOWS, {}).get(msg["flow_id"])
        if (
            status["handler"] != DOMAIN
            or status["step_id"] != "local"
            or not isinstance(flow, UplusRebornConfigFlow)
        ):
            raise ValueError("Not an active discovery form")
        assert connection.user is not None
        unsubscribe = await flow.async_subscribe_preview(
            connection.user.id,
            lambda preview: connection.send_message(
                websocket_api.event_message(msg["id"], preview)
            ),
        )
    except (UnknownFlow, ValueError):
        connection.send_error(msg["id"], "invalid_flow", "Discovery form is not active for this user")
        return
    connection.subscriptions[msg["id"]] = unsubscribe
    connection.send_result(msg["id"])
