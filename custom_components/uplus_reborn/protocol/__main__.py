"""Developer CLI for offline decoding and explicitly gated live intents."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ._commands import default_intent, fabric_intent, update_intent
from ._protocol import (
    ControlResult,
    ProtocolError,
    UwtClient,
    _decode_inner_frame,
    _decode_state,
    _Envelope,
    _EnvelopeDecoder,
    _field,
    _state_words,
)

_DEVICE_ID_ENV = "UPLUS_REBORN_DEVICE_ID"


def _snapshot_dict(snapshot: object) -> dict[str, Any]:
    result = asdict(snapshot)  # type: ignore[arg-type]
    result["observed_at"] = result["observed_at"].isoformat()
    return result


def _raw_profile_fields(words: tuple[int, ...]) -> dict[str, int]:
    return {
        "upper_door": _field(words, 4, 8, 1),
        "lower_door": _field(words, 27, 8, 1),
        "upper_605004": _field(words, 3, 6, 1),
        "upper_unknown_bit7": _field(words, 3, 7, 1),
        "lower_60500v": _field(words, 26, 6, 1),
        "lower_unknown_bit7": _field(words, 26, 7, 1),
        "upper_hint": _field(words, 5, 0, 8),
        "lower_hint": _field(words, 28, 0, 8),
    }


def _inspect_inner(
    data: bytes, *, include_raw: bool, observed_at: datetime
) -> dict[str, Any]:
    frame, consumed = _decode_inner_frame(data)
    if consumed != len(data):
        raise ProtocolError("Trailing bytes after inner frame")

    result: dict[str, Any] = {
        "flags": f"{frame.flags:02x}",
        "frame_type": f"{frame.frame_type:02x}",
        "payload_length": len(frame.payload),
        "reserved": frame.reserved.hex(),
    }
    if len(frame.payload) >= 2:
        result["command"] = frame.payload[:2].hex()

    if (
        frame.frame_type in (0x02, 0x06)
        and len(frame.payload) >= 2
        and frame.payload[:2] == b"\x6d\x01"
    ):
        words = _state_words(frame)
        result["state"] = _snapshot_dict(_decode_state(frame, observed_at))
        if include_raw:
            result["_diagnostics"] = {
                "raw_profile_fields": _raw_profile_fields(words),
                "state_words": [f"{word:04x}" for word in words],
            }

    return result


def _inspect_envelope(
    envelope: _Envelope, *, include_raw: bool, observed_at: datetime
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "message_id": envelope.message_id,
        "message_id_hex": f"{envelope.message_id:04x}",
        "opaque_word": f"{envelope.opaque_word:08x}",
        "opaque_identifier": {
            "present": bool(envelope.opaque_identifier),
            "length": len(envelope.opaque_identifier),
        },
        "device_id": {
            "present": bool(envelope.device_id),
            "length": len(envelope.device_id),
        },
        "sequence": envelope.sequence,
        "payload_length": len(envelope.payload),
    }
    if envelope.payload.startswith(b"\xff\xff"):
        result["inner"] = _inspect_inner(
            envelope.payload,
            include_raw=include_raw,
            observed_at=observed_at,
        )
    elif include_raw:
        result["payload_hex"] = envelope.payload.hex()
    return result


def _decode_bytes(data: bytes, *, include_raw: bool) -> dict[str, Any]:
    observed_at = datetime.now(UTC)
    if data.startswith(b"\xff\xff"):
        return {
            "kind": "inner",
            "inner": _inspect_inner(
                data, include_raw=include_raw, observed_at=observed_at
            ),
        }

    decoder = _EnvelopeDecoder()
    envelopes = decoder.feed(data)
    if decoder.buffered_bytes:
        raise ProtocolError(
            f"Incomplete UWT stream: {decoder.buffered_bytes} buffered bytes"
        )
    if not envelopes:
        raise ProtocolError("Input contains no complete UWT envelope")
    return {
        "kind": "uwt_stream",
        "envelopes": [
            _inspect_envelope(
                envelope, include_raw=include_raw, observed_at=observed_at
            )
            for envelope in envelopes
        ],
    }


def _read_decode_input(arguments: argparse.Namespace) -> bytes:
    if arguments.hex_data is not None:
        normalized = arguments.hex_data.replace(":", "")
        try:
            return bytes.fromhex(normalized)
        except ValueError as error:
            raise ProtocolError(f"Invalid hexadecimal input: {error}") from error
    return Path(arguments.file).read_bytes()


def _resolve_device_id(arguments: argparse.Namespace) -> str:
    sources = sum(
        (
            arguments.device_id is not None,
            arguments.device_id_file is not None,
            bool(os.environ.get(_DEVICE_ID_ENV)),
        )
    )
    if sources != 1:
        raise ProtocolError(
            f"Provide exactly one of --device-id, --device-id-file, or {_DEVICE_ID_ENV}"
        )
    if arguments.device_id is not None:
        return arguments.device_id
    if arguments.device_id_file is not None:
        return Path(arguments.device_id_file).read_text(encoding="ascii").strip()
    return os.environ[_DEVICE_ID_ENV].strip()


def _live_client(arguments: argparse.Namespace) -> UwtClient:
    return UwtClient(
        host=arguments.host,
        port=arguments.port,
        timeout=arguments.timeout,
        control_timeout=getattr(arguments, "control_timeout", 15.0),
        device_id=_resolve_device_id(arguments),
    )


def _control_result_dict(result: ControlResult) -> dict[str, Any]:
    return {
        "before": _snapshot_dict(result.before),
        "after": _snapshot_dict(result.after),
        "unconfirmed_settings": list(result.unconfirmed_settings),
    }


async def _read_live_state(arguments: argparse.Namespace) -> dict[str, Any]:
    if not arguments.allow_live:
        raise ProtocolError("--allow-live is required for a network connection")
    client = _live_client(arguments)
    state_result = await client._read_state_result()
    result = _snapshot_dict(state_result.snapshot)
    if arguments.include_raw:
        result["_diagnostics"] = {
            "frame_type": f"{state_result.frame.frame_type:02x}",
            "flags": f"{state_result.frame.flags:02x}",
            "outer_sequence": state_result.envelope.sequence,
            "opaque_identifier": {
                "present": bool(state_result.envelope.opaque_identifier),
                "length": len(state_result.envelope.opaque_identifier),
            },
            "raw_profile_fields": _raw_profile_fields(state_result.words),
            "reserved": state_result.frame.reserved.hex(),
            "state_words": [f"{word:04x}" for word in state_result.words],
        }
    return result


async def _run_live_control(arguments: argparse.Namespace) -> dict[str, Any]:
    if not arguments.allow_live:
        raise ProtocolError("--allow-live is required for a network connection")
    if arguments.command == "start" and not arguments.confirm_appliance_ready:
        raise ProtocolError(
            "--confirm-appliance-ready is required after checking the load, "
            "doors, absence of errors, power and physical Remote Control"
        )
    client = _live_client(arguments)
    if arguments.command == "start":
        changes = {
            key: value
            for key in (
                "wash_minutes",
                "temperature_c",
                "rinses",
                "spin_rpm",
                "drying_mode",
                "delay_half_hour_step",
            )
            if (value := getattr(arguments, key)) is not None
        }
        if arguments.function is not None:
            changes["enabled_functions"] = tuple(arguments.function)
        if arguments.fabric is not None:
            if changes:
                raise ProtocolError(
                    "Smart fabric recipes do not accept manual settings"
                )
            intent = fabric_intent(
                arguments.drum, arguments.fabric, tuple(arguments.stains or ())
            )
        else:
            if arguments.stains:
                raise ProtocolError("--stain requires --fabric")
            intent = update_intent(
                default_intent(arguments.drum, arguments.program), **changes
            )
        result = await client.start_program(intent, allow_live=arguments.allow_live)
    elif arguments.command == "set-option":
        result = await client.set_appliance_option(
            arguments.option, arguments.enabled, allow_live=arguments.allow_live
        )
    else:
        result = await client.control_drum(
            arguments.drum, arguments.command, allow_live=arguments.allow_live
        )
    return {"intent": arguments.command, "result": _control_result_dict(result)}


def _add_live_arguments(
    command: argparse.ArgumentParser,
    *,
    include_raw: bool = False,
    control: bool = False,
) -> None:
    command.add_argument("--host", required=True)
    command.add_argument("--port", type=int, default=56800)
    command.add_argument("--timeout", type=float, default=5.0)
    if control:
        command.add_argument("--control-timeout", type=float, default=15.0)
    if include_raw:
        command.add_argument(
            "--include-raw",
            action="store_true",
            help="Include redacted envelope metadata and raw state words",
        )
    identity = command.add_mutually_exclusive_group()
    identity.add_argument("--device-id")
    identity.add_argument("--device-id-file", type=Path)
    command.add_argument(
        "--allow-live",
        action="store_true",
        help="Required explicit opt-in for the network connection",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="uplus-reborn-inspect")
    commands = parser.add_subparsers(dest="command", required=True)

    decode = commands.add_parser("decode", help="Decode an inner frame or UWT stream")
    source = decode.add_mutually_exclusive_group(required=True)
    source.add_argument("--hex", dest="hex_data", help="Hexadecimal packet bytes")
    source.add_argument("--file", type=Path, help="Binary packet file")
    decode.add_argument(
        "--include-raw",
        action="store_true",
        help="Include state words or non-inner payload bytes",
    )

    status = commands.add_parser("status", help="Perform one read-only 4d01 query")
    _add_live_arguments(status, include_raw=True)

    start = commands.add_parser("start", help="Start one complete target-drum program")
    _add_live_arguments(start, control=True)
    start.add_argument("--drum", choices=("upper", "lower"), required=True)
    recipe = start.add_mutually_exclusive_group(required=True)
    recipe.add_argument("--program")
    recipe.add_argument(
        "--fabric", help="Source smart-fabric recipe, without manual options"
    )
    start.add_argument("--wash-minutes", type=int)
    start.add_argument("--temperature-c", type=int)
    start.add_argument("--rinses", type=int)
    start.add_argument("--spin-rpm", type=int)
    start.add_argument(
        "--drying-mode",
        choices=("off", "auto", "30_min", "60_min", "90_min", "120_min"),
    )
    start.add_argument(
        "--delay-half-hour-step",
        type=int,
        help="Source-derived end-time choice; only offered steps are accepted",
    )
    start.add_argument(
        "--function",
        action="append",
        choices=("high_water", "easy_iron", "intensive", "eco", "soak", "night"),
    )
    start.add_argument(
        "--stain",
        dest="stains",
        action="append",
        choices=tuple(f"W{index}" for index in range(1, 31)),
    )
    start.add_argument("--confirm-appliance-ready", action="store_true")

    for action in ("pause", "resume", "cancel", "reset"):
        command = commands.add_parser(action, help=f"Request target-drum {action}")
        _add_live_arguments(command, control=True)
        command.add_argument("--drum", choices=("upper", "lower"), required=True)

    option = commands.add_parser("set-option", help="Set one shared appliance option")
    _add_live_arguments(option, control=True)
    option.add_argument(
        "--option", choices=("power", "buzzer", "child_lock"), required=True
    )
    option.add_argument(
        "--enabled", action=argparse.BooleanOptionalAction, required=True
    )
    return parser


def main() -> int:
    parser = _parser()
    arguments = parser.parse_args()
    try:
        if arguments.command == "decode":
            result = _decode_bytes(
                _read_decode_input(arguments), include_raw=arguments.include_raw
            )
        elif arguments.command == "status":
            result = asyncio.run(_read_live_state(arguments))
        else:
            result = asyncio.run(_run_live_control(arguments))
    except (OSError, UnicodeError, ProtocolError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
