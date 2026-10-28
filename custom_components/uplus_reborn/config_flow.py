"""Config flow for the U+ Reborn integration."""

from __future__ import annotations

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
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import section
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo
from homeassistant.helpers.translation import async_get_translations

from .capture_lifecycle import async_save_capture_options
from .capture_options import (
    CAPTURE_DURATIONS,
    CONF_DIAGNOSTIC_DURATION,
    CONF_DIAGNOSTIC_EVENTS,
    CONF_DIAGNOSTIC_OPERATIONS,
    CONF_DIAGNOSTIC_RAW_BYTES,
    MAX_EVENTS,
    MAX_OPERATIONS,
    MAX_RAW_BYTES,
    capture_preferences,
    estimated_trace_depth,
)
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
from .discovery_interval import DiscoveryInterval
from .discovery_preview import DISCOVERY_INTERVALS, async_setup_discovery_preview
from .local_discovery import DiscoveryRecord, ScanResult
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
        self._scan_issues: tuple[str, ...] = ()
        self._selected: str | None = None
        self._interval: DiscoveryInterval | None = None
        self._local_form = False

    @staticmethod
    @override
    async def async_setup_preview(hass: HomeAssistant) -> None:
        """Register the stock generic-preview backend and local image."""
        await async_setup_discovery_preview(hass)

    @override
    @callback
    def async_remove(self) -> None:
        """Close only this dialog's current interval."""
        if self._interval is not None:
            self._interval.close()
        self.hass.data.get(DISCOVERY_INTERVALS, {}).pop(self.flow_id, None)
        super().async_remove()

    async def _activity_placeholders(self) -> dict[str, str]:
        """Localize bounded events and preserve exact cumulative counters."""
        if self._interval is None:
            return {"activity": ""}
        state = self._interval.progress
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

    def _merge_scan(self, result: ScanResult) -> None:
        """Merge this interval's final observations into the dialog catalog."""
        self._latest = {record.device_id for record in result.records}
        issues = list(result.issues)
        for record in result.records:
            if record.device_id in self._catalog or len(self._catalog) < 256:
                self._catalog[record.device_id] = record
            elif "catalog_limit" not in issues:
                issues.append("catalog_limit")
        self._scan_issues = tuple(issues)

    async def async_step_local(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Keep one native form mounted until explicit results or Close."""
        if not self._local_form:
            self._local_form = True
            self._latest = set()
            self._scan_issues = ()
            request = current_request.get()
            user_id = (
                request[KEY_HASS_USER].id if request is not None
                else self.context.get("user_id")
            )
            self._interval = DiscoveryInterval(self.hass, user_id)
            self.hass.data.setdefault(DISCOVERY_INTERVALS, {})[self.flow_id] = self._interval
        if user_input is not None:
            assert self._interval is not None
            self._merge_scan(await self._interval.async_stop())
            self._local_form = False
            self.hass.data.get(DISCOVERY_INTERVALS, {}).pop(self.flow_id, None)
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
        """Return integration settings with explicit capture confirmation."""
        return UplusRebornOptionsFlow()


class UplusRebornOptionsFlow(config_entries.OptionsFlow):
    """Configure safety-sensitive integration behavior."""

    def __init__(self) -> None:
        """Capture connectivity once, independently of the form user's locale."""
        super().__init__()
        self._connection_status: str | None = None
        self._pending_options: dict[str, Any] | None = None
        self._pending_start_capture = False

    @override
    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect settings without applying them or probing the appliance."""
        enabled = self.config_entry.options.get(CONF_ENABLE_VERIFIED_CONTROLS) is True
        coordinator = getattr(self.config_entry, "runtime_data", None)
        if self._connection_status is None:
            self._connection_status = "unknown"
            if coordinator is not None:
                health = coordinator.health
                if health.freshness == "fresh":
                    self._connection_status = "connected"
                elif health.last_error is not None and not health.available:
                    self._connection_status = (
                        "offline_polling_disabled"
                        if self.config_entry.pref_disable_polling
                        else "offline"
                    )
        preferences = capture_preferences(self.config_entry.options)
        diagnostic_schema = vol.Schema(
            {
                vol.Required(
                    CONF_DIAGNOSTIC_OPERATIONS, default=preferences.operations
                ): vol.All(int, vol.Range(min=1, max=MAX_OPERATIONS)),
                vol.Required(
                    CONF_DIAGNOSTIC_EVENTS, default=preferences.events
                ): vol.All(int, vol.Range(min=1, max=MAX_EVENTS)),
                vol.Required(
                    CONF_DIAGNOSTIC_RAW_BYTES, default=preferences.raw_bytes
                ): vol.All(int, vol.Range(min=1, max=MAX_RAW_BYTES)),
                vol.Required(
                    CONF_DIAGNOSTIC_DURATION, default=str(preferences.duration)
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[str(value) for value in CAPTURE_DURATIONS],
                        translation_key="diagnostic_duration",
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Optional("start_capture", default=False): bool,
            }
        )
        schema = vol.Schema(
            {
                vol.Optional(
                    "connection_status", default=self._connection_status
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[self._connection_status],
                        translation_key="connection_status",
                        read_only=True,
                    )
                ),
                vol.Required(CONF_ENABLE_VERIFIED_CONTROLS, default=enabled): bool,
                vol.Required(
                    CONF_POLL_INTERVAL,
                    default=configured_poll_interval(self.config_entry.options),
                ): vol.All(
                    int, vol.Range(min=MIN_POLL_INTERVAL, max=MAX_POLL_INTERVAL)
                ),
                vol.Required(
                    "diagnostics", default=lambda: diagnostic_schema({})
                ): section(diagnostic_schema, {"collapsed": True}),
            }
        )
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                options = schema(user_input)
            except vol.Invalid:
                errors["base"] = "invalid_options"
            else:
                diagnostics = options.pop("diagnostics")
                if any(
                    type(diagnostics[key]) is not int
                    for key in (
                        CONF_DIAGNOSTIC_OPERATIONS,
                        CONF_DIAGNOSTIC_EVENTS,
                        CONF_DIAGNOSTIC_RAW_BYTES,
                    )
                ):
                    errors["base"] = "invalid_options"
                else:
                    diagnostics[CONF_DIAGNOSTIC_DURATION] = int(
                        diagnostics[CONF_DIAGNOSTIC_DURATION]
                    )
                    options.pop("connection_status", None)
                    self._pending_start_capture = diagnostics.pop("start_capture")
                    options.update(diagnostics)
                    self._pending_options = options
                    return await self.async_step_confirm()

        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(schema, user_input),
            errors=errors,
            last_step=False,
            description_placeholders={
                "control_help_url": "https://github.com/Zuz666/uplus-reborn#control-model",
            },
        )

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Apply preferences and one-shot consent only on the final Save."""
        if self._pending_options is None:
            return await self.async_step_init()
        errors: dict[str, str] = {}
        if user_input is not None:
            start = self._pending_start_capture
            coordinator = getattr(self.config_entry, "runtime_data", None)
            if (
                self._pending_options[CONF_ENABLE_VERIFIED_CONTROLS]
                != (self.config_entry.options.get(CONF_ENABLE_VERIFIED_CONTROLS) is True)
                and coordinator is not None
            ):
                coordinator.clear_drafts()
            try:
                await async_save_capture_options(
                    self.hass, self.config_entry, self._pending_options, start
                )
            except HomeAssistantError:
                errors["base"] = "capture_start_failed"
            else:
                # Another flow may have saved newer preferences during our reload.
                return self.async_create_entry(
                    title="", data=dict(self.config_entry.options)
                )
        preferences = capture_preferences(self._pending_options)
        previous = capture_preferences(self.config_entry.options)
        review: dict[Any, Any] = {}

        def information(name: str, value: str, translation_key: str | None = None) -> None:
            config = selector.SelectSelectorConfig(options=[value], read_only=True)
            if translation_key:
                config["translation_key"] = translation_key
            review[vol.Optional(name, default=value)] = selector.SelectSelector(config)

        controls_before = self.config_entry.options.get(CONF_ENABLE_VERIFIED_CONTROLS) is True
        controls_after = self._pending_options[CONF_ENABLE_VERIFIED_CONTROLS]
        interval_before = configured_poll_interval(self.config_entry.options)
        interval_after = configured_poll_interval(self._pending_options)
        budget_changes = {
            name: (getattr(previous, attribute), getattr(preferences, attribute))
            for name, attribute in (
                (CONF_DIAGNOSTIC_OPERATIONS, "operations"),
                (CONF_DIAGNOSTIC_EVENTS, "events"),
                (CONF_DIAGNOSTIC_RAW_BYTES, "raw_bytes"),
                (CONF_DIAGNOSTIC_DURATION, "duration"),
            )
        }
        diagnostics_changed = any(before != after for before, after in budget_changes.values())
        changed = (
            controls_before != controls_after or interval_before != interval_after
            or diagnostics_changed
        )
        information(
            "review_action",
            "start" if self._pending_start_capture else "preferences" if changed else "no_changes",
            "diagnostic_review_action",
        )
        if controls_before != controls_after:
            information(
                "controls_change", "enable" if controls_after else "disable",
                "diagnostic_control_change",
            )
        if interval_before != interval_after:
            information(CONF_POLL_INTERVAL, f"{interval_before} → {interval_after}")
        for name, (before, after) in budget_changes.items():
            if before != after or self._pending_start_capture:
                information(name, f"{before} → {after}" if before != after else str(after))
        if diagnostics_changed or self._pending_start_capture:
            information(
                "estimated_seconds",
                str(0 if self.config_entry.pref_disable_polling
                    else estimated_trace_depth(preferences, interval_after)),
            )
        if (
            diagnostics_changed
            and not self._pending_start_capture
            and controls_before == controls_after
        ):
            information(
                "capture_preferences", "next_capture", "diagnostic_preferences_effect"
            )
        if self._pending_start_capture:
            information("capture_replacement", "replace", "diagnostic_capture_replacement")
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema(review),
            errors=errors,
            last_step=True,
        )
