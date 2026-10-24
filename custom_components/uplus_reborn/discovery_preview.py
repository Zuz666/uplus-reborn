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
from homeassistant.helpers.translation import async_get_translations

from .const import DOMAIN
from .discovery_interval import DiscoveryInterval
from .local_discovery import ScanProgress

DISCOVERY_INTERVALS = f"{DOMAIN}_discovery_intervals"


def _preview_payload(state: ScanProgress, labels: dict[str, str]) -> dict[str, Any]:
    """Render immutable observations without owning the interval's lifecycle."""
    prefix = f"component.{DOMAIN}.selector."
    event = state.events[-1].kind if state.events else "listening"
    template = labels.get(
        prefix + "discovery_status.options.summary",
        "{sent} / {replies} / {devices} / {message}",
    )
    event_label = labels.get(prefix + f"discovery_event.options.{event}", event)
    if state.issues:
        event_label += " · " + labels.get(
            prefix + "discovery_status.options.partial", "Limited coverage"
        )
    attributes = {
        "friendly_name": labels.get(
            prefix + "discovery_status.options.columns", "Sent / Replies / Found / Msg"
        )
    }
    if state.outcome == "searching":
        attributes["entity_picture"] = "/uplus_reborn_static/discovery.svg"
    else:
        attributes["icon"] = "mdi:lan-disconnect"
    return {
        "entity_id": "sensor.___flow_preview___",
        "state": template.format(
            sent=state.sent, replies=state.replies, devices=state.devices, message=event_label
        ),
        "attributes": attributes,
    }


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
    """Bind only this administrator's active interval to the stock preview."""
    try:
        status = hass.config_entries.flow.async_get(msg["flow_id"])
        interval = hass.data.get(DISCOVERY_INTERVALS, {}).get(msg["flow_id"])
        if (
            status["handler"] != DOMAIN
            or status["step_id"] != "local"
            or not isinstance(interval, DiscoveryInterval)
        ):
            raise ValueError("Not an active discovery form")
        labels = await async_get_translations(
            hass, hass.config.language, "selector", {DOMAIN}
        )
        assert connection.user is not None
        unsubscribe = await interval.async_subscribe(
            connection.user.id,
            lambda state: connection.send_message(
                websocket_api.event_message(msg["id"], _preview_payload(state, labels))
            ),
        )
    except (UnknownFlow, ValueError):
        connection.send_error(msg["id"], "invalid_flow", "Discovery form is not active for this user")
        return
    connection.subscriptions[msg["id"]] = unsubscribe
    connection.send_result(msg["id"])
