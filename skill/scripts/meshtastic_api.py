#!/usr/bin/env python3
"""
Meshtastic JSON API for OpenClaw.

Exposes mesh radio operations via CLI with JSON input/output.
Transport layer is abstracted so USB/TCP/custom backends can be swapped.
"""

import argparse
import json
import logging
import sys
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("meshtastic_api")


# ---------------------------------------------------------------------------
# Transport abstraction
# ---------------------------------------------------------------------------

class MeshtasticTransport(ABC):
    """Abstract transport interface for mesh radio communication."""

    @abstractmethod
    def connect(self) -> None:
        """Establish connection to the device."""

    @abstractmethod
    def close(self) -> None:
        """Close the connection."""

    @abstractmethod
    def send_text(self, text: str, destination: Optional[str] = None) -> dict:
        """Send a text message. Returns send result metadata."""

    @abstractmethod
    def get_nodes(self) -> dict:
        """Return dict of known nodes keyed by node ID."""

    @abstractmethod
    def get_my_info(self) -> dict:
        """Return local node information."""

    @abstractmethod
    def receive_messages(self, timeout: int = 30) -> list[dict]:
        """Listen for incoming messages up to timeout seconds."""

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        """Whether the transport is currently connected."""


class USBTransport(MeshtasticTransport):
    """Transport using meshtastic-python over USB serial."""

    def __init__(self, device: Optional[str] = None):
        self._device = device
        self._interface = None
        self._received: list[dict] = []

    def connect(self) -> None:
        import meshtastic.serial_interface
        kwargs = {}
        if self._device:
            kwargs["devPath"] = self._device
        logger.info("Connecting via USB serial (device=%s)", self._device or "auto")
        self._interface = meshtastic.serial_interface.SerialInterface(**kwargs)
        self._subscribe()

    def close(self) -> None:
        if self._interface:
            try:
                self._interface.close()
            except Exception:
                pass
            self._interface = None

    @property
    def is_connected(self) -> bool:
        return self._interface is not None

    def send_text(self, text: str, destination: Optional[str] = None) -> dict:
        iface = self._get_interface()
        kwargs: dict[str, Any] = {}
        if destination:
            kwargs["destinationId"] = destination
        result = iface.sendText(text, **kwargs)
        return {"sent": True, "id": str(result.id) if hasattr(result, "id") else None}

    def get_nodes(self) -> dict:
        iface = self._get_interface()
        nodes_raw = iface.nodes or {}
        return {nid: _format_node(n) for nid, n in nodes_raw.items()}

    def get_my_info(self) -> dict:
        iface = self._get_interface()
        my_info = iface.myInfo
        node = iface.getMyNodeInfo()
        return {
            "my_node_num": my_info.my_node_num if my_info else None,
            "firmware_version": getattr(my_info, "firmware_version", None),
            "node": _format_node(node) if node else None,
        }

    def receive_messages(self, timeout: int = 30) -> list[dict]:
        self._received.clear()
        deadline = time.time() + timeout
        logger.info("Listening for messages (timeout=%ds)", timeout)
        while time.time() < deadline:
            time.sleep(0.5)
        msgs = list(self._received)
        self._received.clear()
        return msgs

    # -- internal --

    def _get_interface(self):
        if not self._interface:
            raise RuntimeError("Not connected")
        return self._interface

    def _subscribe(self):
        from pubsub import pub

        def on_receive(packet, interface):  # noqa: ARG001
            try:
                self._received.append(_format_packet(packet))
            except Exception:
                logger.debug("Failed to format packet", exc_info=True)

        pub.subscribe(on_receive, "meshtastic.receive")

    def __del__(self):
        self.close()


class TCPTransport(MeshtasticTransport):
    """Transport using meshtastic-python over TCP."""

    def __init__(self, host: str = "localhost", port: int = 4403):
        self._host = host
        self._port = port
        self._interface = None
        self._received: list[dict] = []

    def connect(self) -> None:
        import meshtastic.tcp_interface
        logger.info("Connecting via TCP (%s:%d)", self._host, self._port)
        self._interface = meshtastic.tcp_interface.TCPInterface(
            hostname=self._host, portNumber=self._port
        )
        self._subscribe()

    def close(self) -> None:
        if self._interface:
            try:
                self._interface.close()
            except Exception:
                pass
            self._interface = None

    @property
    def is_connected(self) -> bool:
        return self._interface is not None

    def send_text(self, text: str, destination: Optional[str] = None) -> dict:
        iface = self._get_interface()
        kwargs: dict[str, Any] = {}
        if destination:
            kwargs["destinationId"] = destination
        result = iface.sendText(text, **kwargs)
        return {"sent": True, "id": str(result.id) if hasattr(result, "id") else None}

    def get_nodes(self) -> dict:
        iface = self._get_interface()
        nodes_raw = iface.nodes or {}
        return {nid: _format_node(n) for nid, n in nodes_raw.items()}

    def get_my_info(self) -> dict:
        iface = self._get_interface()
        my_info = iface.myInfo
        node = iface.getMyNodeInfo()
        return {
            "my_node_num": my_info.my_node_num if my_info else None,
            "firmware_version": getattr(my_info, "firmware_version", None),
            "node": _format_node(node) if node else None,
        }

    def receive_messages(self, timeout: int = 30) -> list[dict]:
        self._received.clear()
        deadline = time.time() + timeout
        logger.info("Listening for messages (timeout=%ds)", timeout)
        while time.time() < deadline:
            time.sleep(0.5)
        msgs = list(self._received)
        self._received.clear()
        return msgs

    def _get_interface(self):
        if not self._interface:
            raise RuntimeError("Not connected")
        return self._interface

    def _subscribe(self):
        from pubsub import pub

        def on_receive(packet, interface):  # noqa: ARG001
            try:
                self._received.append(_format_packet(packet))
            except Exception:
                logger.debug("Failed to format packet", exc_info=True)

        pub.subscribe(on_receive, "meshtastic.receive")

    def __del__(self):
        self.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _format_node(node: dict) -> dict:
    """Extract useful fields from a meshtastic node dict."""
    if not node:
        return {}
    user = node.get("user", {})
    pos = node.get("position", {})
    metrics = node.get("deviceMetrics", {})
    return {
        "id": user.get("id"),
        "long_name": user.get("longName"),
        "short_name": user.get("shortName"),
        "hw_model": user.get("hwModel"),
        "role": user.get("role"),
        "position": {
            "latitude": pos.get("latitude"),
            "longitude": pos.get("longitude"),
            "altitude": pos.get("altitude"),
            "time": pos.get("time"),
        } if pos else None,
        "battery_level": metrics.get("batteryLevel"),
        "voltage": metrics.get("voltage"),
        "snr": node.get("snr"),
        "last_heard": node.get("lastHeard"),
        "hops_away": node.get("hopsAway"),
    }


def _format_packet(packet: dict) -> dict:
    """Extract useful fields from a received packet."""
    decoded = packet.get("decoded", {})
    return {
        "from": packet.get("fromId"),
        "to": packet.get("toId"),
        "text": decoded.get("text"),
        "portnum": decoded.get("portnum"),
        "rx_time": packet.get("rxTime"),
        "rx_snr": packet.get("rxSnr"),
        "rx_rssi": packet.get("rxRssi"),
        "hop_limit": packet.get("hopLimit"),
        "hop_start": packet.get("hopStart"),
    }


# ---------------------------------------------------------------------------
# Connection manager with reconnection logic
# ---------------------------------------------------------------------------

class ConnectionManager:
    """Manages transport lifecycle with reconnection."""

    def __init__(self, config: dict):
        self._config = config
        self._transport: Optional[MeshtasticTransport] = None

    def get_transport(self) -> MeshtasticTransport:
        if self._transport and self._transport.is_connected:
            return self._transport

        transport_type = self._config.get("transport", "usb")
        attempts = self._config.get("reconnect_attempts", 3)
        delay = self._config.get("reconnect_delay", 5)

        for attempt in range(1, attempts + 1):
            try:
                if transport_type == "tcp":
                    tcp_cfg = self._config.get("tcp", {})
                    t = TCPTransport(
                        host=tcp_cfg.get("host", "localhost"),
                        port=tcp_cfg.get("port", 4403),
                    )
                else:
                    usb_cfg = self._config.get("usb", {})
                    t = USBTransport(device=usb_cfg.get("device"))

                t.connect()
                self._transport = t
                logger.info("Connected (attempt %d/%d)", attempt, attempts)
                return t
            except Exception as exc:
                logger.warning(
                    "Connection attempt %d/%d failed: %s", attempt, attempts, exc
                )
                if attempt < attempts:
                    time.sleep(delay)
                else:
                    raise

        raise RuntimeError("Unreachable")  # pragma: no cover

    def close(self):
        if self._transport:
            self._transport.close()
            self._transport = None


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

def cmd_send(transport: MeshtasticTransport, args: dict) -> dict:
    text = args.get("text")
    if not text:
        return {"ok": False, "error": "Missing 'text' argument"}
    dest = args.get("destination")
    result = transport.send_text(text, destination=dest)
    return {"ok": True, "data": result}


def cmd_receive(transport: MeshtasticTransport, args: dict) -> dict:
    timeout = int(args.get("timeout", 30))
    msgs = transport.receive_messages(timeout=timeout)
    return {"ok": True, "data": {"messages": msgs, "count": len(msgs)}}


def cmd_nodes(transport: MeshtasticTransport, args: dict) -> dict:  # noqa: ARG001
    nodes = transport.get_nodes()
    return {"ok": True, "data": {"nodes": nodes, "count": len(nodes)}}


def cmd_node_info(transport: MeshtasticTransport, args: dict) -> dict:
    node_id = args.get("node_id")
    if not node_id:
        return {"ok": False, "error": "Missing 'node_id' argument"}
    nodes = transport.get_nodes()
    node = nodes.get(node_id)
    if not node:
        return {"ok": False, "error": f"Node {node_id} not found"}
    return {"ok": True, "data": node}


def cmd_my_info(transport: MeshtasticTransport, args: dict) -> dict:  # noqa: ARG001
    info = transport.get_my_info()
    return {"ok": True, "data": info}


def cmd_ping(transport: MeshtasticTransport, args: dict) -> dict:  # noqa: ARG001
    return {"ok": True, "data": {"connected": transport.is_connected}}


COMMANDS = {
    "send": cmd_send,
    "receive": cmd_receive,
    "nodes": cmd_nodes,
    "node_info": cmd_node_info,
    "my_info": cmd_my_info,
    "ping": cmd_ping,
}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Meshtastic JSON API")
    parser.add_argument("--config", required=True, help="Path to config.json")
    parser.add_argument("--command", required=True, help="Command to execute")
    parser.add_argument("--args", default="{}", help="JSON arguments")
    cli_args = parser.parse_args()

    # Load config
    config_path = Path(cli_args.config)
    if not config_path.exists():
        _output({"ok": False, "error": f"Config not found: {config_path}"})
        sys.exit(1)

    config = json.loads(config_path.read_text())

    # Setup logging
    log_level = config.get("log_level", "INFO")
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    # Parse args
    try:
        args = json.loads(cli_args.args)
    except json.JSONDecodeError as exc:
        _output({"ok": False, "error": f"Invalid JSON args: {exc}"})
        sys.exit(1)

    command = cli_args.command
    if command == "help":
        _output({"ok": True, "data": {"commands": list(COMMANDS.keys())}})
        return

    handler = COMMANDS.get(command)
    if not handler:
        _output({"ok": False, "error": f"Unknown command: {command}. Available: {list(COMMANDS.keys())}"})
        sys.exit(1)

    # Connect and execute
    mgr = ConnectionManager(config)
    try:
        transport = mgr.get_transport()
        result = handler(transport, args)
        _output(result)
    except Exception as exc:
        logger.error("Command failed: %s", exc, exc_info=True)
        _output({"ok": False, "error": str(exc)})
        sys.exit(1)
    finally:
        mgr.close()


def _output(data: dict):
    """Write JSON response to stdout."""
    json.dump(data, sys.stdout, default=str)
    sys.stdout.write("\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
