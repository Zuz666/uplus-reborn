"""Config flow for the U+ Reborn integration."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, override

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components.http import KEY_HASS_USER, current_request
from homeassistant.config_entries import ConfigEntryState, ConfigFlowResult
from homeassistant.const import (
    CONF_EMAIL,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    EVENT_HOMEASSISTANT_STOP,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo
from homeassistant.helpers.translation import async_get_translations

from .cloud import (
    CloudCannotConnect,
    CloudDevice,
    CloudInvalidAuth,
    CloudInvalidResponse,
    UPlusCloudClient,
)
from .const import (
    CONF_DEVICE_ID,
    CONF_ENABLE_VERIFIED_CONTROLS,
    CONF_POLL_INTERVAL,
    DEFAULT_PORT,
    DOMAIN,
    MAX_POLL_INTERVAL,
    MIN_POLL_INTERVAL,
    MODEL,
    SUPPORTED_WIFI_TYPE,
    configured_poll_interval,
)
from .discovery import DeviceNotFound, async_find_device_host, device_id_from_mac
from .discovery_catalog import CATALOG_DATE, CATALOG_SOURCE, identify_type
from .discovery_preview import DISCOVERY_FLOWS, async_setup_discovery_preview
from .local_discovery import (
    DiscoveryRecord,
    ScanActivity,
    ScanResult,
    async_get_collector,
)
from .protocol import CannotConnect, InvalidFrame, ProtocolError, UwtClient

_NON_EMPTY_STRING = vol.All(str, vol.Strip, vol.Length(min=1))
_DEVICE_ID_STRING = vol.All(_NON_EMPTY_STRING, vol.Length(max=16))
# Longer than a valid legacy device ID; this navigation choice cannot collide.
_ACTIVITY_CHOICE = "__last_search_activity__"


def _identification(type_id: str, labels: dict[str, str]) -> dict[str, str]:
    """Use the same truthful model classification in every discovery view."""
    evidence = identify_type(type_id)

    def label(group: str, key: str) -> str:
        return labels[f"component.{DOMAIN}.selector.{group}.options.{key}"]

    category = label("discovery_category", evidence.category)
    count = len(evidence.models)
    if count == 1:
        display = evidence.models[0]
        outcome = "target" if evidence.model_source == "target" else "single"
    elif count > 1:
        display = label("discovery_model", "candidates").format(
            category=category, count=count
        )
        outcome = "multiple"
    else:
        display = (
            category if evidence.category != "unknown"
            else label("discovery_model", "unknown_device")
        )
        outcome = "category_only" if evidence.category != "unknown" else "unknown"
    return {
        "display": display,
        "models": "\n".join(f"- {name}" for name in evidence.models)
        or label("discovery_model", "unknown"),
        "model_source": label("discovery_identification", outcome),
        "source_details": label("discovery_source", evidence.model_source),
        "category": category,
        "category_source": label("discovery_category_source", evidence.category_source),
        "catalog_date": CATALOG_DATE if evidence.models else "—",
        "catalog_source": CATALOG_SOURCE if evidence.models else "—",
    }


def _discovery_title(discovery_info: DhcpServiceInfo) -> str:
    """Prefer a useful DHCP hostname and otherwise use the verified model."""
    hostname = discovery_info.hostname.strip().rstrip(".")
    return hostname if hostname else MODEL


def _connection_schema(
    *, host: str | vol.Undefined = vol.UNDEFINED, port: int = DEFAULT_PORT
) -> vol.Schema:
    """Build the connection form with optional existing-value defaults."""
    host_marker = (
        vol.Required(CONF_HOST)
        if host is vol.UNDEFINED
        else vol.Required(CONF_HOST, default=host)
    )
    return vol.Schema(
        {
            host_marker: _NON_EMPTY_STRING,
            vol.Required(CONF_PORT, default=port): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=65535)
            ),
        }
    )


MANUAL_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): _NON_EMPTY_STRING,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=65535)
        ),
        vol.Required(CONF_DEVICE_ID): _DEVICE_ID_STRING,
        vol.Required("confirm_model", default=False): bool,
    }
)

CLOUD_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.EMAIL)
        ),
        vol.Required(CONF_PASSWORD): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
        ),
    }
)


async def _async_validate_connection(data: dict[str, Any]) -> None:
    """Raise when the supplied identity cannot read a valid live snapshot."""
    client = UwtClient(
        host=data[CONF_HOST],
        port=data[CONF_PORT],
        device_id=data[CONF_DEVICE_ID],
    )
    await client.read_state()


class UplusRebornConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Configure one HWD120-B1558U local connection."""

    VERSION = 1
    _discovered_data: dict[str, Any] | None = None
    _discovered_title = MODEL
    _cloud_devices: dict[str, CloudDevice] | None = None

    def __init__(self) -> None:
        """Keep observations private to this dialog."""
        self._catalog: dict[str, DiscoveryRecord] = {}
        self._latest: set[str] = set()
        self._scan_task: asyncio.Task[ScanResult] | None = None
        self._scan_issues: tuple[str, ...] = ()
        self._selected: str | None = None
        self._activity: ScanActivity | None = None
        self._preview_task: asyncio.Task[None] | None = None
        self._preview_owners: dict[object, Callable[[dict[str, Any]], None]] = {}
        self._preview_user_id: str | None = None
        self._local_form = False
        self._search_open = False
        self._removed = False
        self._shutdown_unsubscribe: Callable[[], None] | None = None

    @staticmethod
    @override
    async def async_setup_preview(hass: HomeAssistant) -> None:
        """Register the stock generic-preview backend and local image."""
        await async_setup_discovery_preview(hass)

    @override
    @callback
    def async_remove(self) -> None:
        """Stop only this dialog's collection and preview tasks."""
        self._removed = True
        self._search_open = False
        self._preview_owners.clear()
        self.hass.data.get(DISCOVERY_FLOWS, {}).pop(self.flow_id, None)
        if self._shutdown_unsubscribe is not None:
            self._shutdown_unsubscribe()
            self._shutdown_unsubscribe = None
        if self._activity is not None:
            self._activity.stop.set()
            self._activity.finish("closed")
        for task in (self._scan_task, self._preview_task):
            if task is not None and not task.done():
                task.cancel()
        super().async_remove()

    async def async_subscribe_preview(
        self, user_id: str, send: Callable[[dict[str, Any]], None]
    ) -> Callable[[], None]:
        """Acquire one authorized UI owner; a stopped interval cannot restart."""
        if (
            self._removed
            or not self._local_form
            or not self._search_open
            or user_id != self._preview_user_id
        ):
            raise ValueError("This discovery form is not active for this user")
        owner = object()
        activity = self._activity
        assert activity is not None
        self._preview_owners[owner] = send
        if self._scan_task is None:
            @callback
            def shutdown(_event: Any) -> None:
                self._shutdown_unsubscribe = None
                self._search_open = False
                activity.stop.set()
                activity.finish("shutdown")

            self._shutdown_unsubscribe = self.hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STOP, shutdown
            )
            self._scan_task = self.hass.async_create_background_task(
                async_get_collector(self.hass).scan(activity=activity),
                f"{DOMAIN} discovery collection",
                eager_start=False,
            )
            self._scan_task.add_done_callback(self._scan_finished)
            self._preview_task = self.hass.async_create_background_task(
                self._publish_preview(), f"{DOMAIN} discovery preview",
                eager_start=False,
            )

        @callback
        def unsubscribe() -> None:
            if owner not in self._preview_owners:
                return
            self._preview_owners.pop(owner)
            if not self._preview_owners and self._activity is activity:
                self._search_open = False
                activity.stop.set()
                activity.finish("disconnected")

        return unsubscribe

    @callback
    def _scan_finished(self, task: asyncio.Task[ScanResult]) -> None:
        if self._removed or task.cancelled() or self._scan_task is not task:
            return
        self._finish_scan()
        if self._shutdown_unsubscribe is not None:
            self._shutdown_unsubscribe()
            self._shutdown_unsubscribe = None
        self._search_open = False
        # Wake the publisher immediately, including fatal setup/socket failures.
        if self._preview_task is not None:
            self._preview_task.cancel()
        self._preview_task = self.hass.async_create_background_task(
            self._publish_preview(), f"{DOMAIN} discovery terminal preview"
        )

    async def _publish_preview(self) -> None:
        """Update only the mounted stock preview, never reconfigure the form."""
        labels = await async_get_translations(
            self.hass, self.hass.config.language, "selector", {DOMAIN}
        )
        while not self._removed:
            assert self._activity is not None
            state = self._activity.snapshot()
            prefix = f"component.{DOMAIN}.selector."
            event = state.events[-1].kind if state.events else "listening"
            template = labels.get(
                prefix + "discovery_status.options.summary",
                "{sent} / {replies} / {devices} / {message}",
            )
            event_label = labels.get(
                prefix + f"discovery_event.options.{event}", event
            )
            if state.issues:
                event_label += " · " + labels.get(
                    prefix + "discovery_status.options.partial", "Limited coverage"
                )
            summary = template.format(
                sent=state.sent, replies=state.replies,
                devices=state.devices, message=event_label,
            )
            attributes: dict[str, Any] = {
                "friendly_name": labels.get(
                    prefix + "discovery_status.options.columns",
                    "Sent / Replies / Found / Msg",
                )
            }
            if state.outcome == "searching":
                attributes["entity_picture"] = "/uplus_reborn_static/discovery.svg"
            else:
                attributes["icon"] = "mdi:lan-disconnect"
            payload = {
                "entity_id": "sensor.___flow_preview___",
                "state": summary,
                "attributes": attributes,
            }
            for send in tuple(self._preview_owners.values()):
                send(payload)
            if not self._search_open:
                return
            await asyncio.sleep(1)

    async def _activity_placeholders(self) -> dict[str, str]:
        """Localize bounded events and preserve exact cumulative counters."""
        if self._activity is None:
            return {"activity": ""}
        state = self._activity.snapshot()
        labels = await async_get_translations(
            self.hass, self.hass.config.language, "selector", {DOMAIN}
        )

        def label(group: str, key: str) -> str:
            return labels.get(f"component.{DOMAIN}.selector.{group}.options.{key}", key)

        rows = []
        for event in state.events:
            detail = event.detail
            if event.record is not None:
                record = event.record
                name = _identification(record.type_id, labels)["display"]
                detail = f"{name} · {record.device_id} · {record.host}"
            elif event.kind == "issue":
                detail = label("discovery_issue", detail)
            rows.append(
                f"- {event.elapsed:02d}s — {label('discovery_event', event.kind)}"
                + (f": {detail}" if detail else "")
            )
        return {
            "elapsed": str(state.elapsed),
            "rounds": str(state.rounds),
            "sent": str(state.sent),
            "replies": str(state.replies),
            "devices": str(state.devices),
            "outcome": label("discovery_outcome", state.outcome),
            "issues": "; ".join(
                label("discovery_issue", issue) for issue in state.issues
            )
            or label("discovery_outcome", "no_issues"),
            "journal": "\n".join(rows),
        }

    def _finish_scan(self) -> None:
        """Merge final observations exactly once; preserve the last activity."""
        assert self._scan_task is not None
        try:
            result = self._scan_task.result()
        except (OSError, RuntimeError, ValueError):
            result = ScanResult((), ("scan_failed",))
            if self._activity is not None:
                self._activity.issues.append("scan_failed")
                self._activity.finish("failed")
        self._latest = {record.device_id for record in result.records}
        issues = list(result.issues)
        for record in result.records:
            if record.device_id in self._catalog or len(self._catalog) < 256:
                self._catalog[record.device_id] = record
            elif "catalog_limit" not in issues:
                issues.append("catalog_limit")
        self._scan_issues = tuple(issues)
        self._scan_task = None

    async def async_step_local(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Keep one native form mounted until explicit results or Close."""
        if not self._local_form:
            self._local_form = self._search_open = True
            self._activity = ScanActivity()
            self._latest = set()
            self._scan_issues = ()
            self.hass.data.setdefault(DISCOVERY_FLOWS, {})[self.flow_id] = self
            request = current_request.get()
            self._preview_user_id = (
                request[KEY_HASS_USER].id if request is not None
                else self.context.get("user_id")
            )
        if user_input is not None:
            self._search_open = False
            assert self._activity is not None
            self._activity.stop.set()
            self._activity.finish("stopped")
            if self._scan_task is not None:
                await asyncio.shield(
                    asyncio.gather(self._scan_task, return_exceptions=True)
                )
                if self._scan_task is not None:
                    self._finish_scan()
            self._local_form = False
            self._preview_owners.clear()
            if self._preview_task is not None:
                self._preview_task.cancel()
                self._preview_task = None
            return await self.async_step_local_catalog()
        return self.async_show_form(
            step_id="local", data_schema=vol.Schema({}), preview=DOMAIN,
        )



    def _configured(self, device_id: str) -> bool:
        return any(
            str(entry.unique_id or entry.data.get(CONF_DEVICE_ID, "")).upper()
            == device_id
            for entry in self.hass.config_entries.async_entries(DOMAIN)
        )

    def _eligible(self, record: DiscoveryRecord) -> bool:
        return (
            record.supported
            and not record.conflict
            and not self._configured(record.device_id)
        )

    def _device_statuses(self, record: DiscoveryRecord) -> tuple[str, ...]:
        """Independent restrictions/freshness; recognition never grants support."""
        statuses = []
        if self._configured(record.device_id):
            statuses.append("configured")
        if record.type_id != SUPPORTED_WIFI_TYPE:
            statuses.append("unsupported_type")
        if record.security != 0:
            statuses.append("unsupported_security")
        if record.conflict:
            statuses.append("conflict")
        if record.device_id not in self._latest:
            statuses.append("stale")
        return tuple(statuses)

    async def async_step_local_activity(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Inspect the frozen search journal without selecting/probing a device."""
        if user_input is not None:
            return await self.async_step_local_catalog()
        return self.async_show_form(
            step_id="local_activity",
            data_schema=vol.Schema({}),
            description_placeholders=await self._activity_placeholders(),
        )

    async def async_step_local_catalog(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm a device/action independently; selection never means addition."""
        labels = await async_get_translations(
            self.hass, self.hass.config.language, "selector", {DOMAIN}
        )
        errors: dict[str, str] = {}
        if user_input is not None:
            action = user_input.get("action")
            if action == "local":
                return await self.async_step_local()
            if action == "manual":
                return await self.async_step_manual()
            device_id = user_input.get(CONF_DEVICE_ID)
            if action == "view" and device_id == _ACTIVITY_CHOICE:
                return await self.async_step_local_activity()
            if action == "view" and device_id in self._catalog:
                self._selected = device_id
                return await self.async_step_local_details()
            errors["base"] = "catalog_choice_required"
        options = []
        for identity, record in sorted(self._catalog.items()):
            display = _identification(record.type_id, labels)["display"]
            parts = [display, identity, record.host]
            parts.extend(
                labels[f"component.{DOMAIN}.selector.discovery_device_status.options.{key}"]
                for key in self._device_statuses(record)
            )
            options.append({"value": identity, "label": " · ".join(parts)})
        device_marker = vol.Optional(CONF_DEVICE_ID)
        if options:
            default_device = (
                self._selected if self._selected in self._catalog else options[0]["value"]
            )
            device_marker = vol.Optional(CONF_DEVICE_ID, default=default_device)
        options.append(
            {
                "value": _ACTIVITY_CHOICE,
                "label": labels.get(
                    f"component.{DOMAIN}.selector.discovery_catalog.options.activity",
                    "Last search activity",
                ),
            }
        )
        return self.async_show_form(
            step_id="local_catalog",
            data_schema=vol.Schema(
                {
                    device_marker: selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=options,
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    vol.Required(
                        "action", default="view" if self._catalog else "local"
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=["view", "local", "manual"],
                            mode=selector.SelectSelectorMode.LIST,
                            translation_key="discovery_navigation",
                        )
                    ),
                }
            ),
            errors=errors,
            description_placeholders={
                **await self._activity_placeholders(),
                "count": str(len(self._latest)),
                "retained": str(len(self._catalog)),
                "issues": "; ".join(
                    labels.get(
                        f"component.{DOMAIN}.selector.discovery_issue.options.{issue}",
                        issue,
                    )
                    for issue in self._scan_issues
                )
                or labels.get(
                    f"component.{DOMAIN}.selector.discovery_outcome.options.no_issues",
                    "—",
                ),
            },
        )

    async def async_step_local_details(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Inspect a compact card; only explicit confirmation can probe."""
        record = self._catalog.get(self._selected or "")
        if record is None:
            return await self.async_step_local_catalog()
        labels = await async_get_translations(
            self.hass, self.hass.config.language, "selector", {DOMAIN}
        )

        def boolean(value: bool) -> str:
            key = "yes" if value else "no"
            return labels[f"component.{DOMAIN}.selector.discovery_boolean.options.{key}"]

        statuses = self._device_statuses(record)
        return self.async_show_menu(
            step_id="local_details",
            menu_options=(["local_add"] if self._eligible(record) else [])
            + ["local_technical", "local_catalog", "local", "manual"],
            description_placeholders={
                **_identification(record.type_id, labels),
                "device_id": record.device_id,
                "host": record.host,
                "seen": record.last_seen.isoformat(),
                "latest": boolean(record.device_id in self._latest),
                "supported": boolean(record.supported),
                "configured": boolean(self._configured(record.device_id)),
                "conflict": boolean(record.conflict),
                "status": "\n".join(
                    "- " + labels[
                        f"component.{DOMAIN}.selector.discovery_device_status.options.{key}"
                    ]
                    for key in statuses
                ) or labels[
                    f"component.{DOMAIN}.selector.discovery_device_status.options.eligible"
                ],
            },
        )

    async def async_step_local_technical(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Read-only protocol/source evidence, returning to the selected card."""
        record = self._catalog.get(self._selected or "")
        if record is None:
            return await self.async_step_local_catalog()
        if user_input is not None:
            return await self.async_step_local_details()
        labels = await async_get_translations(
            self.hass, self.hass.config.language, "selector", {DOMAIN}
        )

        def boolean(value: bool) -> str:
            key = "yes" if value else "no"
            return labels[f"component.{DOMAIN}.selector.discovery_boolean.options.{key}"]

        registry = dr.async_get(self.hass)
        associated = sorted(
            {
                self.hass.config_entries.async_get_entry(entry_id).domain
                for device in registry.devices.values()
                if (
                    (dr.CONNECTION_NETWORK_MAC, dr.format_mac(record.device_id))
                    in device.connections
                    or any(
                        identifier.upper() == record.device_id
                        for _, identifier in device.identifiers
                    )
                )
                for entry_id in device.config_entries
                if self.hass.config_entries.async_get_entry(entry_id) is not None
                and self.hass.config_entries.async_get_entry(entry_id).domain != DOMAIN
            }
        )
        return self.async_show_form(
            step_id="local_technical",
            data_schema=vol.Schema({}),
            description_placeholders={
                **_identification(record.type_id, labels),
                "device_id": record.device_id,
                "type_id": record.type_id,
                "host": record.host,
                "advertised_host": record.advertised_host,
                "mac": dr.format_mac(record.device_id)
                if len(record.device_id) == 12
                and all(c in "0123456789ABCDEF" for c in record.device_id)
                else "—",
                "seen": record.last_seen.isoformat(),
                "latest": boolean(record.device_id in self._latest),
                "supported": boolean(record.supported),
                "configured": boolean(self._configured(record.device_id)),
                "conflict": boolean(record.conflict),
                "alternatives": "; ".join(record.alternatives) or "—",
                "busy": boolean(record.busy),
                "security": str(record.security),
                "ready": boolean(record.ready_to_bind),
                "other": ", ".join(associated) or "—",
            },
        )

    async def async_step_local_add(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Recheck eligibility and require explicit confirmation plus fresh TCP."""
        record = self._catalog.get(self._selected or "")
        if record is None or not self._eligible(record):
            return await self.async_step_local_details()
        errors: dict[str, str] = {}
        if user_input is not None:
            action = user_input.get("action", "add")
            if action == "catalog":
                return await self.async_step_local_catalog()
            if action == "local":
                return await self.async_step_local()
            if action == "manual":
                return await self.async_step_manual()
            if action != "add":
                return await self.async_step_local_details()
            data = {
                CONF_HOST: record.host,
                CONF_PORT: DEFAULT_PORT,
                CONF_DEVICE_ID: record.device_id,
            }
            try:
                await _async_validate_connection(data)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidFrame:
                errors["base"] = "invalid_response"
            except ProtocolError:
                errors["base"] = "unknown"
            else:
                if not self._eligible(record):
                    return await self.async_step_local_details()
                await self.async_set_unique_id(record.device_id)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=MODEL, data=data)
        return self.async_show_form(
            step_id="local_add",
            data_schema=vol.Schema(
                {
                    vol.Required("action", default="add"): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=["add", "catalog", "local", "manual"],
                            translation_key="discovery_confirmation",
                        )
                    )
                }
            ),
            errors=errors,
            description_placeholders={
                "host": record.host,
                "device_id": record.device_id,
            },
        )

    @override
    async def async_step_dhcp(
        self, discovery_info: DhcpServiceInfo
    ) -> ConfigFlowResult:
        """Discover an appliance and derive its UWT identity from its MAC."""
        try:
            device_id = device_id_from_mac(discovery_info.macaddress)
        except ValueError:
            return self.async_abort(reason="invalid_device_id")

        existing = await self.async_set_unique_id(device_id)
        self._abort_if_unique_id_configured(
            updates={CONF_HOST: discovery_info.ip, CONF_PORT: DEFAULT_PORT},
            reload_on_update=not (
                existing is not None
                and existing.state
                in (
                    ConfigEntryState.SETUP_IN_PROGRESS,
                    ConfigEntryState.LOADED,
                )
                and existing.update_listeners
            ),
        )

        data = {
            CONF_HOST: discovery_info.ip,
            CONF_PORT: DEFAULT_PORT,
            CONF_DEVICE_ID: device_id,
        }
        try:
            await _async_validate_connection(data)
        except CannotConnect:
            return self.async_abort(reason="cannot_connect")
        except InvalidFrame:
            return self.async_abort(reason="invalid_response")
        except ProtocolError:
            return self.async_abort(reason="unknown")

        self._discovered_data = data
        self._discovered_title = _discovery_title(discovery_info)
        self.context["title_placeholders"] = {"name": self._discovered_title}
        return await self.async_step_dhcp_confirm()

    @override
    async def async_step_dhcp_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask the owner to confirm the discovered model."""
        if user_input is not None:
            assert self._discovered_data is not None
            return self.async_create_entry(
                title=self._discovered_title,
                data=self._discovered_data,
            )

        assert self._discovered_data is not None
        self._set_confirm_only()
        return self.async_show_form(
            step_id="dhcp_confirm",
            description_placeholders={
                "host": self._discovered_data[CONF_HOST],
                "name": self._discovered_title,
            },
        )

    @override
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose native discovery, optional cloud assistance or manual setup."""
        return self.async_show_menu(
            step_id="user",
            menu_options=["local", "cloud", "manual"],
        )

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle manual local setup."""
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {
                CONF_HOST: user_input[CONF_HOST].strip(),
                CONF_PORT: user_input[CONF_PORT],
                CONF_DEVICE_ID: user_input[CONF_DEVICE_ID].strip().upper(),
            }
            known = self._catalog.get(data[CONF_DEVICE_ID])
            if known is not None and (not known.supported or known.conflict):
                errors["base"] = "incompatible_discovery"
            elif not user_input.get("confirm_model"):
                errors["base"] = "model_confirmation_required"
            if errors:
                return self.async_show_form(
                    step_id="manual",
                    data_schema=self.add_suggested_values_to_schema(
                        MANUAL_SCHEMA, user_input
                    ),
                    errors=errors,
                )
            try:
                await _async_validate_connection(data)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidFrame:
                errors["base"] = "invalid_response"
            except (UnicodeError, ValueError):
                errors["base"] = "invalid_device_id"
            except ProtocolError:
                errors["base"] = "unknown"
            else:
                if self._configured(data[CONF_DEVICE_ID]):
                    return self.async_abort(reason="already_configured")
                await self.async_set_unique_id(data[CONF_DEVICE_ID])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=MODEL, data=data)

        return self.async_show_form(
            step_id="manual",
            data_schema=self.add_suggested_values_to_schema(MANUAL_SCHEMA, user_input),
            errors=errors,
        )

    async def async_step_cloud(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Use a U+ account once to discover supported appliance identities."""
        errors: dict[str, str] = {}
        suggested_values: dict[str, Any] | None = None
        if user_input is not None:
            email = user_input[CONF_EMAIL].strip()
            password = user_input[CONF_PASSWORD]
            suggested_values = {CONF_EMAIL: email}
            if not email or not password:
                errors["base"] = "invalid_auth"
            else:
                client = UPlusCloudClient(async_get_clientsession(self.hass))
                try:
                    devices = await client.async_discover_devices(email, password)
                except CloudInvalidAuth:
                    errors["base"] = "invalid_auth"
                except CloudCannotConnect:
                    errors["base"] = "cloud_cannot_connect"
                except CloudInvalidResponse:
                    errors["base"] = "cloud_invalid_response"
                else:
                    supported = {
                        device.device_id: device
                        for device in devices
                        if device.wifi_type == SUPPORTED_WIFI_TYPE
                    }
                    if not supported:
                        errors["base"] = "no_supported_devices"
                    else:
                        self._cloud_devices = supported
                        return await self.async_step_cloud_device()

        return self.async_show_form(
            step_id="cloud",
            data_schema=self.add_suggested_values_to_schema(
                CLOUD_SCHEMA, suggested_values
            ),
            errors=errors,
        )

    async def async_step_cloud_device(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select an account appliance and resolve its current LAN address."""
        assert self._cloud_devices
        errors: dict[str, str] = {}
        selected_id = next(iter(self._cloud_devices))
        if user_input is not None:
            selected_id = user_input[CONF_DEVICE_ID]
            device = self._cloud_devices[selected_id]
            host = user_input.get(CONF_HOST, "").strip()
            if not host:
                try:
                    host = await async_find_device_host(device.device_id)
                except DeviceNotFound:
                    errors[CONF_HOST] = "device_not_found"

            if host:
                data = {
                    CONF_HOST: host,
                    CONF_PORT: DEFAULT_PORT,
                    CONF_DEVICE_ID: device.device_id,
                }
                try:
                    await _async_validate_connection(data)
                except CannotConnect:
                    errors["base"] = "cannot_connect"
                except InvalidFrame:
                    errors["base"] = "invalid_response"
                except ProtocolError:
                    errors["base"] = "unknown"
                else:
                    await self.async_set_unique_id(device.device_id)
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(
                        title=device.name or MODEL,
                        data=data,
                    )

        device_options = {
            device.device_id: (f"{device.name or MODEL} ({device.device_id[-6:]})")
            for device in self._cloud_devices.values()
        }
        schema = vol.Schema(
            {
                vol.Required(CONF_DEVICE_ID, default=selected_id): vol.In(
                    device_options
                ),
                vol.Optional(CONF_HOST, default=""): str,
            }
        )
        return self.async_show_form(
            step_id="cloud_device",
            data_schema=self.add_suggested_values_to_schema(schema, user_input),
            description_placeholders={
                "count": str(len(self._cloud_devices)),
            },
            errors=errors,
        )

    @override
    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Update the host or port while preserving appliance identity."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data_updates = {
                CONF_HOST: user_input[CONF_HOST].strip(),
                CONF_PORT: user_input[CONF_PORT],
            }
            candidate = {**entry.data, **data_updates}
            try:
                await _async_validate_connection(candidate)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except InvalidFrame:
                errors["base"] = "invalid_response"
            except ProtocolError:
                errors["base"] = "unknown"
            else:
                # Initializing/loaded entries own routing reloads through their
                # listener; entries without that owner need explicit scheduling.
                listener_handles_reload = entry.state in (
                    ConfigEntryState.SETUP_IN_PROGRESS,
                    ConfigEntryState.LOADED,
                ) and bool(entry.update_listeners)
                self.hass.config_entries.async_update_entry(entry, data=candidate)
                if not listener_handles_reload:
                    self.hass.config_entries.async_schedule_reload(entry.entry_id)
                return self.async_abort(reason="reconfigure_successful")

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                _connection_schema(
                    host=entry.data[CONF_HOST], port=entry.data[CONF_PORT]
                ),
                user_input,
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    @override
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> UplusRebornOptionsFlow:
        """Return the bounded polling and explicit control opt-in flow."""
        return UplusRebornOptionsFlow()


class UplusRebornOptionsFlow(config_entries.OptionsFlow):
    """Configure safety-sensitive integration behavior."""

    @override
    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Save integration settings without issuing an appliance command."""
        enabled = self.config_entry.options.get(CONF_ENABLE_VERIFIED_CONTROLS) is True
        schema = vol.Schema(
            {
                vol.Required(CONF_ENABLE_VERIFIED_CONTROLS, default=enabled): bool,
                vol.Required(
                    CONF_POLL_INTERVAL,
                    default=configured_poll_interval(self.config_entry.options),
                ): vol.All(
                    int, vol.Range(min=MIN_POLL_INTERVAL, max=MAX_POLL_INTERVAL)
                ),
            }
        )
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                options = schema(user_input)
            except vol.Invalid:
                errors["base"] = "invalid_options"
            else:
                if options[CONF_ENABLE_VERIFIED_CONTROLS] != enabled:
                    # Discard intent before saving, even across rapid off/on saves.
                    coordinator = getattr(self.config_entry, "runtime_data", None)
                    if coordinator is not None:
                        coordinator.clear_drafts()
                return self.async_create_entry(title="", data=options)

        coordinator = getattr(self.config_entry, "runtime_data", None)
        status = "unknown"
        if coordinator is not None:
            health = coordinator.health
            if health.freshness == "fresh":
                status = "connected"
            elif health.last_error is not None and not health.available:
                status = "offline"
        labels = await async_get_translations(
            self.hass, self.hass.config.language, "selector", {DOMAIN}
        )
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(schema, user_input),
            errors=errors,
            description_placeholders={
                "connection_status": labels[
                    f"component.{DOMAIN}.selector.connection_status.options.{status}"
                ],
            },
        )
