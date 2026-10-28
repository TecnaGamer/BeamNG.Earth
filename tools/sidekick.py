#!/usr/bin/env python3
"""BeamNG.Earth Sidekick: Standalone companion compute offloader for handhelds and weak PCs.

Runs on a powerful desktop PC or home server to generate far-field terrain LOD meshes
and textures for a connected Steam Deck, ROG Ally, Lenovo Legion Go, or lightweight laptop.
"""
import argparse
import ipaddress
import logging
import os
import socket
import sys

# Ensure server/ modules and repo root can be imported in both source script and PyInstaller
ROOT = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))
sys.path.insert(0, ROOT)

import peerwork


def get_hardware_tier(cpus: int) -> str:
    """Quirky hardware tier nickname based on CPU thread count."""
    if cpus <= 4:
        return "Pocket Potato (Handheld/Mini-PC APU)"
    elif cpus <= 8:
        return "Scrapyard V6 (Quad/Hexa Core Rig)"
    elif cpus <= 16:
        return "Twin-Turbo V8 Rig (8-Core Enthusiast Workstation)"
    elif cpus <= 24:
        return "12-Cylinder Thread Ripper (12-Core Multi-Thread Beast)"
    else:
        return "Orbital Geo-Foundry (16+ Core HEDT / Server Titan)"


def get_primary_ip():
    """Detect the machine's primary outbound IP route."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("1.1.1.1", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


def detect_ip_addresses():
    """Discover all local LAN, Wi-Fi, and Tailscale IPv4 addresses."""
    found = []
    seen = set()
    primary = get_primary_ip()
    if primary and not primary.startswith("127."):
        found.append(primary)
        seen.add(primary)

    try:
        _, _, ips = socket.gethostbyname_ex(socket.gethostname())
        for ip in ips:
            if ip not in seen:
                try:
                    addr = ipaddress.ip_address(ip)
                    if not addr.is_loopback:
                        found.append(ip)
                        seen.add(ip)
                except ValueError:
                    pass
    except Exception:
        pass

    if not found:
        found.append("127.0.0.1")

    classified = []
    ts_net = ipaddress.ip_network("100.64.0.0/10")
    for ip in found:
        try:
            addr = ipaddress.ip_address(ip)
            if addr.is_loopback:
                label = "Local Loopback"
            elif addr in ts_net:
                label = "Tailscale Mesh VPN"
            elif addr in ipaddress.ip_network("192.168.0.0/16") or addr in ipaddress.ip_network("10.0.0.0/8"):
                label = "Local LAN / Wi-Fi"
            elif addr in ipaddress.ip_network("172.16.0.0/12"):
                label = "Local Subnet / Virtual Adapter"
            else:
                label = "Network Adapter"
        except ValueError:
            label = "Unknown Adapter"
        classified.append((ip, label))

    return classified


def print_banner(version_str: str, code_id_str: str, cpus: int, workers: int, port: int, ips):
    tier = get_hardware_tier(cpus)
    banner = rf"""
========================================================================================
   ____                       _   _  ____     _____            _   _     
  |  _ \                     | \ | |/ ___|   | ____|__ _ _ __ | |_| |__  
  | |_) | ___  __ _ _ __ ___ |  \| | |  _    |  _| / _` | '__|| __| '_ \ 
  |  _ < / _ \/ _` | '_ ` _ \| |\  | |_| | _ | |__| (_| | |   | |_| | | |
  |_| \_\___/\__,_|_| |_| |_|_| \_|\____|(_)|_____|\__,_|_|    \__|_| |_|
            SIDEKICK // HANDHELD TURBO CO-PROCESSOR v{version_str}
========================================================================================
[+] Host Hardware Rig:   {cpus} Threads Detected -> Tier: {tier}
[+] Worker Allocation:   {workers} Dedicated Tile Generator Processes
[+] Compute Fingerprint: Code ID: {code_id_str} | Protocol: v{peerwork.PROTOCOL}
[+] Service Listener:    TCP Port {port}

[+] Handheld Connection Setup (Steam Deck / ROG Ally / Legion Go / Laptop):"""
    print(banner.strip())
    for ip, label in ips:
        print(f"    * {ip:<16} ({label})")
        print(f"      -> Launch option:   --peers {ip}:{port}")
        print(f"      -> Environment var: BNGE_PEERS={ip}:{port}")
    print("\n[+] Connect your handheld to any address above and drive the world!")
    print("========================================================================================\n")


def main(argv=None):
    cpus = os.cpu_count() or 4
    default_workers = max(1, cpus - 2)
    turbo_workers = max(1, cpus - 1)

    ap = argparse.ArgumentParser(
        description="BeamNG.Earth Sidekick: Handheld Turbo Co-Processor for far-tile compute offloading."
    )
    ap.add_argument("--host", default="0.0.0.0", help="Address to bind (default: 0.0.0.0 / all interfaces)")
    ap.add_argument("--port", type=int, default=peerwork.PORT, help=f"TCP port to listen on (default: {peerwork.PORT})")
    ap.add_argument(
        "--workers",
        type=int,
        default=None,
        help=f"Number of parallel worker processes (default: {default_workers} on this machine)",
    )
    ap.add_argument(
        "--turbo",
        action="store_true",
        help=f"Enable Turbo mode: reserve only 1 core for OS/game, use {turbo_workers} workers",
    )
    ap.add_argument(
        "--cache-dir",
        default=os.path.expanduser("~/.cache/beamng-earth"),
        help="Directory to cache imagery and elevation data",
    )
    ap.add_argument(
        "--check",
        action="store_true",
        help="Print diagnostic banner, hardware tier, detected IPs, version, and code_id, then exit 0",
    )

    args = ap.parse_args(argv)

    if args.workers is not None:
        workers = max(1, args.workers)
    elif args.turbo:
        workers = turbo_workers
    else:
        workers = default_workers

    ver = peerwork.version()
    cid = peerwork.code_id()
    ips = detect_ip_addresses()

    print_banner(ver, cid, cpus, workers, args.port, ips)

    if args.check:
        print(f"CHECK OK: Sidekick v{ver} (code_id={cid}, workers={workers})")
        return 0

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] [Sidekick] %(message)s",
        datefmt="%H:%M:%S",
    )

    out_dir = os.path.join(args.cache_dir, "peer-out")
    os.makedirs(out_dir, exist_ok=True)
    helper = peerwork.Helper(args.cache_dir, workers, out_dir)
    helper.serve(args.host, args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
