#!/usr/bin/env python3
"""
Antigravity & Antigravity IDE Debian Package (.deb) Repackager

Features:
- Queries releases from https://antigravity.google/releases and official Cloud Run auto-updater APIs.
- Resolves download URLs for Antigravity 2.0 and Antigravity IDE (Linux amd64 & arm64).
- Checks whether upstream versions have updated before downloading.
- If versions are already up-to-date, exits immediately without parsing/downloading archives.
- Downloads tar.gz archives with streaming progress and resume support.
- Extracts and arranges payload into standard Debian package structure:
    /opt/<pkg-name>/
    /usr/bin/<pkg-name> (launcher script & symlinks)
    /usr/share/applications/<pkg-name>.desktop
    /usr/share/icons/ & /usr/share/pixmaps/
    DEBIAN/control, postinst, postrm
- Handles Electron permissions (e.g. chrome-sandbox chmod 4755).
- Builds standard .deb packages using dpkg-deb and calculates SHA256 checksums.
- Outputs GitHub Actions matrix JSON and markdown release notes.
"""

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

HUB_RELEASES_API = "https://antigravity-hub-auto-updater-974169037036.us-central1.run.app/releases"
IDE_RELEASES_API = "https://antigravity-ide-auto-updater-974169037036.us-central1.run.app/releases"
CLI_MANIFEST_API_BASE = "https://antigravity-cli-auto-updater-974169037036.us-central1.run.app/manifests"
OFFICIAL_RELEASES_PAGE = "https://antigravity.google/releases"

ALL_PRODUCTS = ["antigravity", "antigravity-ide", "antigravity-cli"]

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36 AntigravityDebBuilder/1.0"
)

# Architecture mappings: Debian arch -> Upstream platform tag
ARCH_MAP = {
    "amd64": "linux-x64",
    "x64": "linux-x64",
    "arm64": "linux-arm",
    "aarch64": "linux-arm",
}

DEBIAN_ARCH_CANONICAL = {
    "amd64": "amd64",
    "x64": "amd64",
    "arm64": "arm64",
    "aarch64": "arm64",
}


def http_get(url: str, timeout: int = 20) -> bytes:
    """Perform HTTP GET request with gzip decompression support."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Encoding": "gzip, deflate",
            "Accept": "*/*",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            try:
                return gzip.decompress(raw)
            except Exception:
                pass
        return raw


def check_url_exists(url: str, timeout: int = 5) -> bool:
    """Check if a remote resource exists using a fast HEAD request."""
    try:
        req = urllib.request.Request(
            url,
            method="HEAD",
            headers={"User-Agent": USER_AGENT},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def extract_icon_from_asar(asar_path: Path, dest_png: Path) -> bool:
    """
    Extract built-in high-resolution icon from Electron app.asar archive
    using standard Chromium pickle protocol (pure Python standard library).
    """
    if not asar_path.is_file():
        return False
    try:
        with open(asar_path, "rb") as f:
            header_prefix = f.read(16)
            if len(header_prefix) < 16:
                return False
            pickle_size = struct.unpack("<I", header_prefix[:4])[0]
            payload_size = struct.unpack("<I", header_prefix[4:8])[0]
            header_str_len = struct.unpack("<I", header_prefix[8:12])[0]
            json_len = struct.unpack("<I", header_prefix[12:16])[0]
            json_bytes = f.read(json_len)
            tree = json.loads(json_bytes.decode("utf-8"))
            base_offset = 8 + payload_size

            def walk_files(d: dict, prefix: str = ""):
                for name, node in d.get("files", {}).items():
                    sub = f"{prefix}/{name}" if prefix else name
                    if isinstance(node, dict) and "files" in node:
                        yield from walk_files(node, sub)
                    else:
                        yield sub, node

            files_map = dict(walk_files(tree))

            # Preferred icon paths in Antigravity app.asar
            priority_names = [
                "icon.png",
                "resources/icon.png",
                "app.png",
                "antigravity.png",
                "logo.png",
                "static/icon.png",
                "assets/icon.png",
            ]
            chosen_entry = None
            for p in priority_names:
                if p in files_map and isinstance(files_map[p], dict) and "size" in files_map[p] and "offset" in files_map[p]:
                    chosen_entry = files_map[p]
                    break

            if not chosen_entry:
                # Fallback to any standalone png
                for p, v in files_map.items():
                    if isinstance(v, dict) and p.endswith(".png") and "node_modules" not in p and v.get("size", 0) > 4096:
                        chosen_entry = v
                        break

            if chosen_entry:
                offset = int(chosen_entry["offset"])
                size = int(chosen_entry["size"])
                f.seek(base_offset + offset)
                data = f.read(size)
                if data.startswith(b"\x89PNG"):
                    dest_png.parent.mkdir(parents=True, exist_ok=True)
                    dest_png.write_bytes(data)
                    print(f"[✓] Extracted built-in icon from app.asar ({size} bytes) -> {dest_png.name}")
                    return True
    except Exception as e:
        print(f"[WARN] Failed to extract icon from asar {asar_path}: {e}", file=sys.stderr)
    return False


def install_hicolor_icons(src_png: Path, hicolor_dir: Path, icon_name: str) -> None:
    """Generate and install icons for various standard hicolor sizes."""
    try:
        from PIL import Image
        with Image.open(src_png) as img:
            for size in (256, 128, 64, 48, 32):
                size_dir = hicolor_dir / f"{size}x{size}" / "apps"
                size_dir.mkdir(parents=True, exist_ok=True)
                dest = size_dir / f"{icon_name}.png"
                if not dest.exists():
                    resized = img.resize((size, size), Image.Resampling.LANCZOS)
                    resized.save(dest, "PNG")
    except Exception:
        pass


def parse_version_tuple(v_str: str) -> Tuple[int, ...]:
    """Parse semver string into comparable tuple."""
    nums = [int(x) for x in re.findall(r"\d+", v_str)]
    return tuple(nums) if nums else (0,)


def compare_versions(v1: str, v2: str) -> int:
    """
    Compare semver strings.
    Returns 1 if v1 > v2, -1 if v1 < v2, 0 if v1 == v2.
    """
    t1 = parse_version_tuple(v1)
    t2 = parse_version_tuple(v2)
    max_len = max(len(t1), len(t2))
    t1_padded = t1 + (0,) * (max_len - len(t1))
    t2_padded = t2 + (0,) * (max_len - len(t2))
    if t1_padded > t2_padded:
        return 1
    elif t1_padded < t2_padded:
        return -1
    return 0


class ReleaseResolver:
    """Resolves release information and download URLs."""

    @staticmethod
    def fetch_hub_releases() -> List[Dict[str, str]]:
        """Fetch releases for Antigravity 2.0 (Hub/Engine)."""
        releases: List[Dict[str, str]] = []
        # 1. Try official API
        try:
            raw = http_get(HUB_RELEASES_API)
            data = json.loads(raw.decode("utf-8"))
            if isinstance(data, list):
                releases = data
            elif isinstance(data, dict) and "versions" in data:
                releases = data["versions"]
        except Exception as e:
            print(f"[WARN] Failed to fetch Hub API ({e}), falling back to releases HTML...", file=sys.stderr)

        # 2. Fallback to HTML scraping
        if not releases:
            try:
                html = http_get(OFFICIAL_RELEASES_PAGE).decode("utf-8", errors="ignore")
                m = re.search(r'data-static-versions=["\'](.*?)["\']', html)
                if m:
                    unescaped = m.group(1).replace("&quot;", '"').replace("&amp;", "&")
                    static_vers = json.loads(unescaped)
                    releases = [item for item in static_vers if parse_version_tuple(item.get("version", ""))[0] >= 2]
            except Exception as e:
                print(f"[WARN] Failed to scrape Hub releases from HTML: {e}", file=sys.stderr)

        # Filter >= 2.0.0
        filtered = [r for r in releases if parse_version_tuple(r.get("version", ""))[0] >= 2]
        filtered.sort(key=lambda r: parse_version_tuple(r.get("version", "")), reverse=True)
        return filtered

    @staticmethod
    def fetch_ide_releases() -> List[Dict[str, str]]:
        """Fetch releases for Antigravity IDE."""
        releases: List[Dict[str, str]] = []
        # 1. Try official API
        try:
            raw = http_get(IDE_RELEASES_API)
            data = json.loads(raw.decode("utf-8"))
            if isinstance(data, list):
                releases = data
            elif isinstance(data, dict) and "versions" in data:
                releases = data["versions"]
        except Exception as e:
            print(f"[WARN] Failed to fetch IDE API ({e}), falling back to releases HTML...", file=sys.stderr)

        # 2. Fallback to HTML scraping
        if not releases:
            try:
                html = http_get(OFFICIAL_RELEASES_PAGE).decode("utf-8", errors="ignore")
                m = re.search(r'data-fallback-ide=["\'](.*?)["\']', html)
                if m:
                    unescaped = m.group(1).replace("&quot;", '"').replace("&amp;", "&")
                    releases = json.loads(unescaped)
            except Exception as e:
                print(f"[WARN] Failed to scrape IDE releases from HTML: {e}", file=sys.stderr)

        filtered = [r for r in releases if parse_version_tuple(r.get("version", ""))[0] >= 2]
        filtered.sort(key=lambda r: parse_version_tuple(r.get("version", "")), reverse=True)
        return filtered

    @staticmethod
    def fetch_cli_releases(deb_arch: str = "amd64") -> List[Dict[str, str]]:
        """Fetch release info for Antigravity CLI from official manifest."""
        arch = DEBIAN_ARCH_CANONICAL.get(deb_arch.lower(), "amd64")
        platform = "linux_arm64" if arch == "arm64" else "linux_amd64"
        manifest_url = f"{CLI_MANIFEST_API_BASE}/{platform}.json"
        try:
            raw = http_get(manifest_url)
            data = json.loads(raw.decode("utf-8"))
            version = data.get("version", "")
            dl_url = data.get("url", "")
            m = re.search(r"/(\d+\.\d+\.\d+)-([0-9a-zA-Z_]+)/", dl_url)
            execution_id = m.group(2) if m else "1"
            return [
                {
                    "version": version,
                    "execution_id": execution_id,
                    "url": dl_url,
                    "sha512": data.get("sha512", ""),
                }
            ]
        except Exception as e:
            print(f"[WARN] Failed to fetch CLI manifest from {manifest_url}: {e}", file=sys.stderr)
            return []

    @staticmethod
    def get_latest_versions() -> Dict[str, Dict[str, str]]:
        """Retrieve latest release info for Antigravity 2.0, IDE, and CLI, verifying Linux availability."""
        hub = ReleaseResolver.fetch_hub_releases()
        ide = ReleaseResolver.fetch_ide_releases()
        cli = ReleaseResolver.fetch_cli_releases()
        if not hub:
            raise RuntimeError("Failed to fetch Antigravity 2.0 releases")
        if not ide:
            raise RuntimeError("Failed to fetch Antigravity IDE releases")
        if not cli:
            raise RuntimeError("Failed to fetch Antigravity CLI releases")

        # Pick the latest release that actually has published Linux binaries
        latest_hub = hub[0]
        for candidate in hub:
            v = candidate.get("version", "")
            eid = candidate.get("execution_id", "1")
            deb_url = f"https://storage.googleapis.com/antigravity-public/antigravity-hub/{v}-{eid}/linux-x64/Antigravity.deb"
            tar_url = f"https://storage.googleapis.com/antigravity-public/antigravity-hub/{v}-{eid}/linux-x64/Antigravity.tar.gz"
            if check_url_exists(deb_url) or check_url_exists(tar_url):
                latest_hub = candidate
                break

        latest_ide = ide[0]
        for candidate in ide:
            v = candidate.get("version", "")
            eid = candidate.get("execution_id", "1")
            deb_url = f"https://edgedl.me.gvt1.com/edgedl/release2/j0qc3/antigravity/stable/{v}-{eid}/linux-x64/Antigravity%20IDE.deb"
            tar_url = f"https://edgedl.me.gvt1.com/edgedl/release2/j0qc3/antigravity/stable/{v}-{eid}/linux-x64/Antigravity%20IDE.tar.gz"
            if check_url_exists(deb_url) or check_url_exists(tar_url):
                latest_ide = candidate
                break

        return {
            "antigravity": latest_hub,
            "antigravity-ide": latest_ide,
            "antigravity-cli": cli[0],
        }

    @staticmethod
    def get_download_url(product: str, version: str, execution_id: str, deb_arch: str) -> str:
        """
        Construct download URL based on product, version, execution_id, and architecture.
        Prioritizes official .deb package if upstream provides it, falling back to .tar.gz.
        """
        platform = ARCH_MAP.get(deb_arch.lower(), "linux-x64")
        if product == "antigravity":
            deb_url = (
                f"https://storage.googleapis.com/antigravity-public/antigravity-hub/"
                f"{version}-{execution_id}/{platform}/Antigravity.deb"
            )
            tar_url = (
                f"https://storage.googleapis.com/antigravity-public/antigravity-hub/"
                f"{version}-{execution_id}/{platform}/Antigravity.tar.gz"
            )
            if check_url_exists(deb_url):
                return deb_url
            return tar_url
        elif product == "antigravity-ide":
            deb_url = (
                f"https://edgedl.me.gvt1.com/edgedl/release2/j0qc3/antigravity/stable/"
                f"{version}-{execution_id}/{platform}/Antigravity%20IDE.deb"
            )
            tar_url = (
                f"https://edgedl.me.gvt1.com/edgedl/release2/j0qc3/antigravity/stable/"
                f"{version}-{execution_id}/{platform}/Antigravity%20IDE.tar.gz"
            )
            if check_url_exists(deb_url):
                return deb_url
            return tar_url
        elif product == "antigravity-cli":
            arch_dir = "linux-arm" if deb_arch in ("arm64", "aarch64") else "linux-x64"
            arch_file = "cli_linux_arm64" if deb_arch in ("arm64", "aarch64") else "cli_linux_x64"
            deb_url = (
                f"https://storage.googleapis.com/antigravity-public/antigravity-cli/"
                f"{version}-{execution_id}/{arch_dir}/{arch_file}.deb"
            )
            tar_url = (
                f"https://storage.googleapis.com/antigravity-public/antigravity-cli/"
                f"{version}-{execution_id}/{arch_dir}/{arch_file}.tar.gz"
            )
            if check_url_exists(deb_url):
                return deb_url
            return tar_url
        else:
            raise ValueError(f"Unknown product: {product}")


def load_version_file(path: Path) -> Dict[str, str]:
    """Load recorded versions from file."""
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[WARN] Could not read version file {path}: {e}", file=sys.stderr)
        return {}


def save_version_file(path: Path, data: Dict[str, str]) -> None:
    """Save updated versions to file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = load_version_file(path)
    existing.update(data)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(existing, f, indent=2)
    print(f"[i] Updated version cache in: {path}")


def check_github_release_exists(tag: str) -> bool:
    """Check if a release tag already exists on GitHub."""
    if shutil.which("gh"):
        res = subprocess.run(["gh", "release", "view", tag], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return res.returncode == 0
    return False


def download_file(url: str, dest_path: Path) -> Path:
    """Download file with progress logging and resume capability."""
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = dest_path.with_suffix(".tmp")

    headers = {"User-Agent": USER_AGENT}
    existing_bytes = 0
    if temp_path.exists():
        existing_bytes = temp_path.stat().st_size
        headers["Range"] = f"bytes={existing_bytes}-"

    req = urllib.request.Request(url, headers=headers)
    print(f"[*] Downloading: {url}")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            content_length = resp.headers.get("Content-Length")
            total_size = int(content_length) + existing_bytes if content_length else None

            mode = "ab" if existing_bytes and resp.status == 206 else "wb"
            if mode == "wb":
                existing_bytes = 0

            downloaded = existing_bytes
            start_time = time.time()
            last_print = start_time

            with open(temp_path, mode) as f:
                while True:
                    chunk = resp.read(1024 * 512)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)

                    now = time.time()
                    if now - last_print >= 2.0:
                        last_print = now
                        elapsed = now - start_time
                        speed = downloaded / elapsed if elapsed > 0 else 0
                        speed_mb = speed / (1024 * 1024)
                        if total_size:
                            pct = (downloaded / total_size) * 100
                            cur_mb = downloaded / (1024 * 1024)
                            tot_mb = total_size / (1024 * 1024)
                            print(f"    -> {pct:.1f}% ({cur_mb:.1f}/{tot_mb:.1f} MB) @ {speed_mb:.2f} MB/s")
                        else:
                            cur_mb = downloaded / (1024 * 1024)
                            print(f"    -> {cur_mb:.1f} MB @ {speed_mb:.2f} MB/s")

    except urllib.error.HTTPError as e:
        if e.code == 416 and temp_path.exists():
            pass
        else:
            raise

    temp_path.replace(dest_path)
    file_size_mb = dest_path.stat().st_size / (1024 * 1024)
    print(f"[✓] Download completed: {dest_path.name} ({file_size_mb:.2f} MB)")
    return dest_path


def sha256_file(path: Path) -> str:
    """Calculate SHA256 hex digest of file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1024 * 512):
            h.update(chunk)
    return h.hexdigest()


class DebPackager:
    """Repackages extracted Linux archive into a standard Debian .deb package."""

    def __init__(
        self,
        product: str,
        version: str,
        deb_arch: str,
        archive_path: Path,
        output_dir: Path,
    ):
        self.product = product
        self.version = version
        self.deb_arch = DEBIAN_ARCH_CANONICAL.get(deb_arch.lower(), "amd64")
        self.archive_path = archive_path
        self.output_dir = output_dir

    def package(self) -> Path:
        """Run the complete extraction and packaging workflow."""
        deb_filename = f"{self.product}_{self.version}_{self.deb_arch}.deb"
        final_deb_path = self.output_dir / deb_filename
        self.output_dir.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory(prefix="agy_deb_build_") as build_dir_str:
            build_root = Path(build_dir_str)
            staging_root = build_root / "staging"
            staging_root.mkdir(parents=True, exist_ok=True)

            print(f"[*] Extracting {self.archive_path.name}...")
            extract_dir = build_root / "extract"
            extract_dir.mkdir(parents=True, exist_ok=True)
            if self.archive_path.suffix == ".deb":
                subprocess.run(
                    ["dpkg-deb", "-x", str(self.archive_path), str(extract_dir)],
                    check=True,
                )
            else:
                subprocess.run(
                    ["tar", "-zxf", str(self.archive_path), "-C", str(extract_dir)],
                    check=True,
                )

            if self.product == "antigravity-cli":
                print(f"[*] Setting up packaging structure for {self.product}...")
                self._setup_cli_package(extract_dir, staging_root)
            else:
                # Handle Debian package structure (with /opt and /usr) or .tar.gz directory
                extracted_opt = extract_dir / "opt"
                if extracted_opt.is_dir():
                    opt_dirs = [d for d in extracted_opt.iterdir() if d.is_dir()]
                    payload_source = opt_dirs[0] if opt_dirs else extracted_opt
                    extracted_usr = extract_dir / "usr"
                    if extracted_usr.is_dir():
                        staging_usr = staging_root / "usr"
                        shutil.copytree(str(extracted_usr), str(staging_usr), dirs_exist_ok=True)
                else:
                    # Locate top-level extracted directory
                    entries = list(extract_dir.iterdir())
                    if len(entries) == 1 and entries[0].is_dir():
                        payload_source = entries[0]
                    else:
                        payload_source = extract_dir

                opt_app_dir = staging_root / "opt" / self.product
                opt_app_dir.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(payload_source), str(opt_app_dir))

                # Provide compatibility symlink for /opt/Antigravity -> /opt/antigravity
                if self.product == "antigravity":
                    alt_opt = staging_root / "opt" / "Antigravity"
                    if not alt_opt.exists() and not alt_opt.is_symlink():
                        alt_opt.symlink_to(self.product)

                print(f"[*] Setting up packaging structure for {self.product}...")
                # 1. Executable launcher
                exe_name, exe_path = self._setup_executable(opt_app_dir, staging_root)

                # 2. Desktop entry & icons
                self._setup_desktop_and_icons(opt_app_dir, staging_root, exe_name, extract_dir)

                # 3. AppArmor profile (Ubuntu 24.04+ compatibility)
                self._setup_apparmor_profile(opt_app_dir, staging_root, extract_dir)

                # 4. Permissions
                self._fix_permissions(staging_root, opt_app_dir)

            # 4. Debian metadata
            self._setup_debian_metadata(staging_root)

            # 5. Build .deb package
            print(f"[*] Building deb package: {deb_filename}...")
            subprocess.run(
                ["dpkg-deb", "--build", "--root-owner-group", str(staging_root), str(final_deb_path)],
                check=True,
            )

        print(f"[✓] Successfully built package: {final_deb_path}")
        print(f"    Size: {final_deb_path.stat().st_size / (1024 * 1024):.2f} MB")
        print(f"    SHA256: {sha256_file(final_deb_path)}")
        return final_deb_path

    def _setup_cli_package(self, extract_dir: Path, staging_root: Path) -> None:
        """Setup standard Unix CLI layout for Antigravity CLI."""
        usr_bin = staging_root / "usr" / "bin"
        usr_bin.mkdir(parents=True, exist_ok=True)

        bin_candidates = [p for p in extract_dir.rglob("*") if p.is_file() and not p.is_symlink()]
        if not bin_candidates:
            raise RuntimeError("No binary found in CLI archive")

        src_bin = bin_candidates[0]
        for c in bin_candidates:
            if c.name in ("antigravity", "agy"):
                src_bin = c
                break

        # Main command: /usr/bin/agy
        dest_bin = usr_bin / "agy"
        shutil.copy2(src_bin, dest_bin)
        dest_bin.chmod(0o755)

        # Alias symlink: /usr/bin/antigravity-cli -> agy
        cli_symlink = usr_bin / "antigravity-cli"
        if not cli_symlink.exists() and not cli_symlink.is_symlink():
            cli_symlink.symlink_to("agy")

        # Set permissions for staging files
        for p in staging_root.rglob("*"):
            if p.is_symlink():
                continue
            try:
                if p.is_dir():
                    p.chmod(0o755)
                elif p.is_file():
                    p.chmod(0o755 if os.access(p, os.X_OK) else 0o644)
            except Exception as e:
                print(f"[WARN] Failed to chmod {p}: {e}", file=sys.stderr)

    def _setup_executable(self, opt_app_dir: Path, staging_root: Path) -> Tuple[str, Path]:
        """Detect the main binary and create /usr/bin launcher."""
        usr_bin = staging_root / "usr" / "bin"
        usr_bin.mkdir(parents=True, exist_ok=True)

        target_bin: Optional[Path] = None

        candidates = [
            self.product,
            "antigravity",
            "Antigravity",
            "code",
            "antigravity-ide",
        ]
        for c in candidates:
            p = opt_app_dir / c
            if p.is_file() and os.access(p, os.X_OK):
                target_bin = p
                break

        if not target_bin:
            for item in opt_app_dir.iterdir():
                if item.is_file() and not item.is_symlink():
                    if item.name in ("chrome-sandbox", "crashpad_handler"):
                        continue
                    if item.suffix in (".so", ".json", ".txt", ".pak", ".bin"):
                        continue
                    try:
                        with open(item, "rb") as f:
                            if f.read(4) == b"\x7fELF":
                                target_bin = item
                                break
                    except Exception:
                        pass

        if not target_bin:
            target_bin = opt_app_dir / self.product

        if target_bin.exists():
            target_bin.chmod(target_bin.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        launcher_script = usr_bin / self.product
        opt_bin_path = f"/opt/{self.product}/{target_bin.name}"

        wrapper_content = f"""#!/bin/sh
set -e
exec "{opt_bin_path}" "$@"
"""
        with open(launcher_script, "w", encoding="utf-8") as f:
            f.write(wrapper_content)
        launcher_script.chmod(0o755)

        if self.product == "antigravity-ide":
            alias_script = usr_bin / "agy-ide"
            with open(alias_script, "w", encoding="utf-8") as f:
                f.write(wrapper_content)
            alias_script.chmod(0o755)

            # Legacy compatibility symlink: /opt/antigravity-ide/antigravity -> antigravity-ide
            alias_bin = opt_app_dir / "antigravity"
            if not alias_bin.exists():
                alias_bin.symlink_to(target_bin.name)

        return target_bin.name, target_bin

    def _setup_desktop_and_icons(
        self,
        opt_app_dir: Path,
        staging_root: Path,
        exe_name: str,
        extract_dir: Optional[Path] = None,
    ) -> None:
        """Locate icons and install .desktop file and pixmaps."""
        applications_dir = staging_root / "usr" / "share" / "applications"
        pixmaps_dir = staging_root / "usr" / "share" / "pixmaps"
        hicolor_dir = staging_root / "usr" / "share" / "icons" / "hicolor"
        icons_dir = hicolor_dir / "512x512" / "apps"

        applications_dir.mkdir(parents=True, exist_ok=True)
        pixmaps_dir.mkdir(parents=True, exist_ok=True)
        icons_dir.mkdir(parents=True, exist_ok=True)

        icon_name = self.product
        icon_source: Optional[Path] = None

        # 1. Check if upstream package already provided an icon in staging or extract usr/share
        candidates_in_staging = [
            staging_root / "usr" / "share" / "icons" / "hicolor" / "512x512" / "apps" / f"{icon_name}.png",
            staging_root / "usr" / "share" / "pixmaps" / f"{icon_name}.png",
        ]
        if extract_dir:
            candidates_in_staging.extend([
                extract_dir / "usr" / "share" / "icons" / "hicolor" / "512x512" / "apps" / f"{icon_name}.png",
                extract_dir / "usr" / "share" / "pixmaps" / f"{icon_name}.png",
            ])
        for c in candidates_in_staging:
            if c.is_file() and c.stat().st_size > 0:
                icon_source = c
                break

        # 2. Check standard unpacked application resources
        if not icon_source:
            icon_candidates = [
                opt_app_dir / "resources" / "app" / "resources" / "linux" / "code.png",
                opt_app_dir / "resources" / "icon.png",
                opt_app_dir / "resources" / "app" / "resources" / "icon.png",
            ]
            for c in icon_candidates:
                if c.is_file():
                    icon_source = c
                    break

        # 3. Check inside Electron app.asar (Antigravity stores its official icon here!)
        if not icon_source:
            asar_path = opt_app_dir / "resources" / "app.asar"
            if asar_path.is_file():
                temp_icon = (extract_dir / f"{icon_name}_native.png") if extract_dir else (staging_root.parent / f"{icon_name}_native.png")
                if extract_icon_from_asar(asar_path, temp_icon):
                    icon_source = temp_icon

        # 4. Search recursively for any high-resolution PNG in opt_app_dir
        if not icon_source:
            best_size = 0
            for p in opt_app_dir.rglob("*.png"):
                if "node_modules" in str(p):
                    continue
                size = p.stat().st_size
                if size > best_size:
                    best_size = size
                    icon_source = p

        # Install icons
        if icon_source and icon_source.is_file():
            dest_pixmap = pixmaps_dir / f"{icon_name}.png"
            if icon_source.resolve() != dest_pixmap.resolve():
                shutil.copy2(icon_source, dest_pixmap)
            dest_icon = icons_dir / f"{icon_name}.png"
            if icon_source.resolve() != dest_icon.resolve():
                shutil.copy2(icon_source, dest_icon)
            install_hicolor_icons(icon_source, hicolor_dir, icon_name)
            print(f"[✓] Using native icon for {icon_name}: {icon_source.name}")
        else:
            svg_content = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
  <rect width="100" height="100" rx="20" fill="#1e1e2e"/>
  <circle cx="50" cy="50" r="30" fill="#89b4fa"/>
  <text x="50" y="58" font-size="24" text-anchor="middle" fill="#11111b" font-family="sans-serif" font-weight="bold">AG</text>
</svg>"""
            fallback_svg = pixmaps_dir / f"{icon_name}.svg"
            with open(fallback_svg, "w", encoding="utf-8") as f:
                f.write(svg_content)

        if self.product == "antigravity":
            display_name = "Antigravity"
            comment = "Google Antigravity 2.0 Agentic Desktop Application"
            categories = "Development;Utility;"
            startup_wm_class = "Antigravity"
            mime_type = "x-scheme-handler/antigravity;"
            exec_cmd = "/usr/bin/antigravity %U"
        else:
            display_name = "Antigravity IDE"
            comment = "Google Antigravity IDE - Code with Agents"
            categories = "Development;IDE;"
            startup_wm_class = "Antigravity IDE"
            mime_type = "text/plain;inode/directory;"
            exec_cmd = "/usr/bin/antigravity-ide %F"

        desktop_file = applications_dir / f"{self.product}.desktop"
        desktop_content = f"""[Desktop Entry]
Name={display_name}
Comment={comment}
GenericName=Text Editor and Agent Workspace
Exec={exec_cmd}
Icon={icon_name}
Type=Application
StartupNotify=true
StartupWMClass={startup_wm_class}
Categories={categories}
MimeType={mime_type}
Terminal=false
"""
        with open(desktop_file, "w", encoding="utf-8") as f:
            f.write(desktop_content)
        desktop_file.chmod(0o644)

    def _setup_apparmor_profile(self, opt_app_dir: Path, staging_root: Path, extract_dir: Path) -> None:
        """Setup AppArmor profile for Ubuntu 24.04+ unprivileged user namespace compatibility."""
        if self.product != "antigravity":
            return
        apparmor_dir = staging_root / "etc" / "apparmor.d"
        apparmor_dir.mkdir(parents=True, exist_ok=True)
        target_profile = apparmor_dir / "antigravity"

        upstream_profile = extract_dir / "etc" / "apparmor.d" / "antigravity"
        if upstream_profile.is_file():
            shutil.copy2(upstream_profile, target_profile)
        else:
            profile_content = """abi <abi/4.0>,
include <tunables/global>

profile antigravity /opt/antigravity/antigravity flags=(unconfined) {
  userns,
  include if exists <local/antigravity>
}
"""
            target_profile.write_text(profile_content, encoding="utf-8")
        target_profile.chmod(0o644)

        res_profile = opt_app_dir / "resources" / "apparmor-profile"
        if not res_profile.exists():
            shutil.copy2(target_profile, res_profile)

    def _fix_permissions(self, staging_root: Path, opt_app_dir: Path) -> None:
        """Fix permissions for Debian standards and Chrome Sandbox."""
        chrome_sandbox = opt_app_dir / "chrome-sandbox"
        if chrome_sandbox.exists():
            chrome_sandbox.chmod(0o4755)

        for path in staging_root.rglob("*"):
            if path.is_symlink() or path == chrome_sandbox:
                continue
            try:
                if path.is_dir():
                    path.chmod(0o755)
                elif path.is_file():
                    if os.access(path, os.X_OK):
                        path.chmod(0o755)
                    else:
                        path.chmod(0o644)
            except Exception as e:
                print(f"[WARN] Failed to chmod {path}: {e}", file=sys.stderr)

    def _setup_debian_metadata(self, staging_root: Path) -> None:
        """Create DEBIAN/control, postinst, and postrm files."""
        debian_dir = staging_root / "DEBIAN"
        debian_dir.mkdir(parents=True, exist_ok=True)

        dependencies = (
            "ca-certificates, libasound2 | libasound2t64, libatk-bridge2.0-0, libatk1.0-0, "
            "libc6, libcairo2, libcups2, libdbus-1-3, libexpat1, libfontconfig1, libgbm1, "
            "libglib2.0-0, libgtk-3-0, libnspr4, libnss3, libpango-1.0-0, libpangocairo-1.0-0, "
            "libsecret-1-0, libx11-6, libx11-xcb1, libxcb1, libxcomposite1, libxcursor1, "
            "libxdamage1, libxext6, libxfixes3, libxi6, libxrandr2, libxrender1, libxss1, libxtst6, "
            "xdg-utils"
        )

        if self.product == "antigravity":
            pkg_name = "antigravity"
            section = "devel"
            deps = dependencies
            description = (
                "Google Antigravity 2.0 Desktop Application\n"
                " Unified agentic coding platform with autonomous background task runners,\n"
                " interactive chat canvas, and multi-agent orchestration."
            )
        elif self.product == "antigravity-ide":
            pkg_name = "antigravity-ide"
            section = "devel"
            deps = dependencies
            description = (
                "Google Antigravity IDE\n"
                " AI-first integrated development environment built for seamless\n"
                " agentic workflows, autocomplete, and inline editing."
            )
        elif self.product == "antigravity-cli":
            pkg_name = "antigravity-cli"
            section = "utils"
            deps = "ca-certificates, libc6"
            description = (
                "Google Antigravity CLI (agy)\n"
                " Autonomous agentic command-line interface for terminal-driven coding,\n"
                " multi-agent orchestration, and background task execution."
            )
        else:
            raise ValueError(f"Unknown product: {self.product}")

        control_content = f"""Package: {pkg_name}
Version: {self.version}
Section: {section}
Priority: optional
Architecture: {self.deb_arch}
Maintainer: Antigravity Community <noreply@antigravity.google>
Depends: {deps}
Homepage: https://antigravity.google
Description: {description}
"""
        with open(debian_dir / "control", "w", encoding="utf-8") as f:
            f.write(control_content)
        (debian_dir / "control").chmod(0o644)

        if self.product == "antigravity-cli":
            postinst_content = """#!/bin/sh
set -e
exit 0
"""
            postrm_content = """#!/bin/sh
set -e
exit 0
"""
        else:
            postinst_content = """#!/bin/sh
set -e

if which update-desktop-database >/dev/null 2>&1; then
    update-desktop-database -q /usr/share/applications || true
fi

if which update-icon-caches >/dev/null 2>&1; then
    update-icon-caches /usr/share/icons/* || true
elif which gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -q /usr/share/icons/hicolor || true
fi

if which update-mime-database >/dev/null 2>&1; then
    update-mime-database /usr/share/mime || true
fi

if hash apparmor_status 2>/dev/null && apparmor_status --enabled > /dev/null 2>&1; then
    APPARMOR_PROFILE_SOURCE='/opt/antigravity/resources/apparmor-profile'
    APPARMOR_PROFILE_TARGET='/etc/apparmor.d/antigravity'
    if [ -f "$APPARMOR_PROFILE_SOURCE" ] && hash apparmor_parser 2>/dev/null; then
        if apparmor_parser --skip-kernel-load --debug "$APPARMOR_PROFILE_SOURCE" > /dev/null 2>&1; then
            cp -f "$APPARMOR_PROFILE_SOURCE" "$APPARMOR_PROFILE_TARGET"
            if ! { [ -x '/usr/bin/ischroot' ] && /usr/bin/ischroot; }; then
                apparmor_parser --replace --write-cache --skip-read-cache "$APPARMOR_PROFILE_TARGET" 2>/dev/null || true
            fi
        fi
    fi
fi

exit 0
"""
            postrm_content = """#!/bin/sh
set -e

if [ "$1" = "remove" ] || [ "$1" = "purge" ]; then
    if which update-desktop-database >/dev/null 2>&1; then
        update-desktop-database -q /usr/share/applications || true
    fi

    if which update-icon-caches >/dev/null 2>&1; then
        update-icon-caches /usr/share/icons/* || true
    elif which gtk-update-icon-cache >/dev/null 2>&1; then
        gtk-update-icon-cache -q /usr/share/icons/hicolor || true
    fi

    if which update-mime-database >/dev/null 2>&1; then
        update-mime-database /usr/share/mime || true
    fi

    if [ -f /etc/apparmor.d/antigravity ]; then
        if hash apparmor_parser 2>/dev/null; then
            apparmor_parser -R /etc/apparmor.d/antigravity 2>/dev/null || true
        fi
        rm -f /etc/apparmor.d/antigravity
    fi
fi

exit 0
"""
        postinst_file = debian_dir / "postinst"
        with open(postinst_file, "w", encoding="utf-8") as f:
            f.write(postinst_content)
        postinst_file.chmod(0o755)

        postrm_file = debian_dir / "postrm"
        with open(postrm_file, "w", encoding="utf-8") as f:
            f.write(postrm_content)
        postrm_file.chmod(0o755)


def list_releases() -> None:
    """Print available releases for all products."""
    print("==================================================")
    print("  Antigravity 2.0 (Hub/Engine) Releases")
    print("==================================================")
    hub_releases = ReleaseResolver.fetch_hub_releases()
    if not hub_releases:
        print("  (No releases found)")
    else:
        for r in hub_releases[:10]:
            print(f"  - Version: {r['version']} (execution_id: {r.get('execution_id', 'N/A')})")

    print("\n==================================================")
    print("  Antigravity IDE Releases")
    print("==================================================")
    ide_releases = ReleaseResolver.fetch_ide_releases()
    if not ide_releases:
        print("  (No releases found)")
    else:
        for r in ide_releases[:10]:
            print(f"  - Version: {r['version']} (execution_id: {r.get('execution_id', 'N/A')})")

    print("\n==================================================")
    print("  Antigravity CLI Releases")
    print("==================================================")
    cli_releases = ReleaseResolver.fetch_cli_releases()
    if not cli_releases:
        print("  (No releases found)")
    else:
        for r in cli_releases:
            print(f"  - Version: {r['version']} (execution_id: {r.get('execution_id', 'N/A')})")
    print()


def check_updates(
    version_file_path: Path,
    force: bool = False,
    target_product: Optional[str] = None,
) -> Tuple[bool, Dict[str, Dict[str, str]], List[str]]:
    """
    Check if upstream has newer releases than recorded in version_file.
    Returns:
        (has_update, latest_releases_map, updated_products_list)
    """
    latest_map = ReleaseResolver.get_latest_versions()
    recorded_versions = load_version_file(version_file_path)

    check_prods = ALL_PRODUCTS if (not target_product or target_product in ("both", "all")) else [target_product]
    updated_products = []
    print("[*] Checking for updates against recorded versions:", file=sys.stderr)
    for prod in check_prods:
        upstream_ver = latest_map[prod]["version"]
        rec_ver = recorded_versions.get(prod)

        is_new = False
        if force:
            is_new = True
            reason = "Force build requested"
        elif not rec_ver:
            is_new = True
            reason = "No previous build recorded"
        elif compare_versions(upstream_ver, rec_ver) > 0:
            is_new = True
            reason = f"Upstream ({upstream_ver}) > Recorded ({rec_ver})"
        else:
            reason = f"Up to date ({rec_ver})"

        print(f"    - {prod:16s}: upstream={upstream_ver:<8s} recorded={str(rec_ver):<8s} -> {reason}", file=sys.stderr)
        if is_new:
            updated_products.append(prod)

    has_update = len(updated_products) > 0
    return has_update, latest_map, updated_products


def generate_matrix_json(
    product_req: str,
    arch_req: str,
    version_file: Path,
    check_update_mode: bool,
    force: bool,
) -> Dict[str, Any]:
    """Produce matrix JSON for GitHub Actions."""
    has_update, latest_map, updated_products = check_updates(
        version_file_path=version_file,
        force=force,
        target_product=product_req,
    )

    hub_rel = latest_map["antigravity"]
    ide_rel = latest_map["antigravity-ide"]
    cli_rel = latest_map["antigravity-cli"]

    all_products = ALL_PRODUCTS if product_req in ("both", "all") else [product_req]
    arches = ["amd64", "arm64"] if arch_req in ("both", "all") else [arch_req]

    # Filter target products
    if check_update_mode and not force:
        if not has_update:
            target_products = []
        elif product_req in ("both", "all"):
            # When an update is detected for any product and user requested all/both,
            # include all products so the GitHub release artifacts are complete.
            target_products = all_products
        else:
            target_products = [p for p in all_products if p in updated_products]
    else:
        target_products = all_products

    matrix_list = []
    for p in target_products:
        rel = latest_map[p]
        for a in arches:
            matrix_list.append(
                {
                    "product": p,
                    "arch": a,
                    "version": rel["version"],
                    "execution_id": rel["execution_id"],
                }
            )

    should_build = len(matrix_list) > 0
    return {
        "updated": has_update,
        "should_build": should_build,
        "hub_version": hub_rel["version"],
        "ide_version": ide_rel["version"],
        "cli_version": cli_rel["version"],
        "updated_products": target_products,
        "matrix": matrix_list,
    }


def generate_release_notes(
    hub_version: str,
    ide_version: str,
    cli_version: str,
    hub_eid: str = "",
    ide_eid: str = "",
    cli_eid: str = "",
    out_file: Optional[Path] = None,
) -> str:
    """Generate Markdown release notes for GitHub Releases, including upstream direct links."""
    hub_deb_x64 = f"https://storage.googleapis.com/antigravity-public/antigravity-hub/{hub_version}-{hub_eid}/linux-x64/Antigravity.deb"
    hub_deb_arm = f"https://storage.googleapis.com/antigravity-public/antigravity-hub/{hub_version}-{hub_eid}/linux-arm/Antigravity.deb"
    hub_tar_x64 = f"https://storage.googleapis.com/antigravity-public/antigravity-hub/{hub_version}-{hub_eid}/linux-x64/Antigravity.tar.gz"
    hub_tar_arm = f"https://storage.googleapis.com/antigravity-public/antigravity-hub/{hub_version}-{hub_eid}/linux-arm/Antigravity.tar.gz"
    ide_tar_x64 = f"https://edgedl.me.gvt1.com/edgedl/release2/j0qc3/antigravity/stable/{ide_version}-{ide_eid}/linux-x64/Antigravity%20IDE.tar.gz"
    ide_tar_arm = f"https://edgedl.me.gvt1.com/edgedl/release2/j0qc3/antigravity/stable/{ide_version}-{ide_eid}/linux-arm/Antigravity%20IDE.tar.gz"
    cli_tar_x64 = f"https://storage.googleapis.com/antigravity-public/antigravity-cli/{cli_version}-{cli_eid}/linux-x64/cli_linux_x64.tar.gz"
    cli_tar_arm = f"https://storage.googleapis.com/antigravity-public/antigravity-cli/{cli_version}-{cli_eid}/linux-arm/cli_linux_arm64.tar.gz"

    notes = f"""## 🚀 Google Antigravity, Antigravity IDE & CLI Linux (.deb) Releases

Automated Debian packages built directly from official Google Antigravity release archives.

---

### 📦 Packages in this Release (Community Repackaged)

| Application | Version | Arch | Package Name | Description |
| :--- | :--- | :--- | :--- | :--- |
| **Antigravity 2.0** | `{hub_version}` | `amd64` | `antigravity_{hub_version}_amd64.deb` | Agentic desktop workspace & platform |
| **Antigravity 2.0** | `{hub_version}` | `arm64` | `antigravity_{hub_version}_arm64.deb` | Agentic desktop workspace & platform |
| **Antigravity IDE** | `{ide_version}` | `amd64` | `antigravity-ide_{ide_version}_amd64.deb` | AI-first coding editor |
| **Antigravity IDE** | `{ide_version}` | `arm64` | `antigravity-ide_{ide_version}_arm64.deb` | AI-first coding editor |
| **Antigravity CLI** | `{cli_version}` | `amd64` | `antigravity-cli_{cli_version}_amd64.deb` | Command-line interface (`agy`) |
| **Antigravity CLI** | `{cli_version}` | `arm64` | `antigravity-cli_{cli_version}_arm64.deb` | Command-line interface (`agy`) |

---

### 🌐 官方上游直链 (Official Upstream Direct Download Links)

如果您偏好直接从 Google 官方存储桶（GCS）或 CDN 下载未经加工的原生安装包，可使用以下官方直链：

| 组件 / 平台 | 架构 | 格式 | 官方原生直链 | 来源 |
| :--- | :--- | :--- | :--- | :--- |
| **Antigravity 2.0** | `amd64 (x86_64)` | **官方 `.deb`** | [Antigravity.deb]({hub_deb_x64}) | Google Cloud Storage |
| **Antigravity 2.0** | `arm64 (AArch64)` | **官方 `.deb`** | [Antigravity.deb]({hub_deb_arm}) | Google Cloud Storage |
| **Antigravity 2.0** | `amd64 (x86_64)` | 官方 `.tar.gz` | [Antigravity.tar.gz]({hub_tar_x64}) | Google Cloud Storage |
| **Antigravity 2.0** | `arm64 (AArch64)` | 官方 `.tar.gz` | [Antigravity.tar.gz]({hub_tar_arm}) | Google Cloud Storage |
| **Antigravity IDE** | `amd64 (x86_64)` | 官方 `.tar.gz` | [Antigravity IDE.tar.gz]({ide_tar_x64}) | Google GVT1 CDN |
| **Antigravity IDE** | `arm64 (AArch64)` | 官方 `.tar.gz` | [Antigravity IDE.tar.gz]({ide_tar_arm}) | Google GVT1 CDN |
| **Antigravity CLI** | `amd64 (x86_64)` | 官方 `.tar.gz` | [cli_linux_x64.tar.gz]({cli_tar_x64}) | Google Cloud Storage |
| **Antigravity CLI** | `arm64 (AArch64)` | 官方 `.tar.gz` | [cli_linux_arm64.tar.gz]({cli_tar_arm}) | Google Cloud Storage |

---

### 💻 Quick Installation

#### Ubuntu / Debian / Pop!_OS / Linux Mint
```bash
# 1. Download the deb packages for your architecture
# 2. Install with apt (automatically resolves any dependencies):
sudo apt update
sudo apt install ./antigravity_{hub_version}_amd64.deb
sudo apt install ./antigravity-ide_{ide_version}_amd64.deb
sudo apt install ./antigravity-cli_{cli_version}_amd64.deb
```

#### Launching
- Run `antigravity` to launch Antigravity 2.0 Desktop Platform.
- Run `antigravity-ide` (or `agy-ide`) to launch Antigravity IDE.
- Run `agy` (or `antigravity-cli`) to use the Antigravity CLI tool in terminal.
- Desktop applications are integrated into your desktop application menu.

---
*Upstream releases sourced from [https://antigravity.google/releases](https://antigravity.google/releases).*
"""
    if out_file:
        out_file.parent.mkdir(parents=True, exist_ok=True)
        with open(out_file, "w", encoding="utf-8") as f:
            f.write(notes)
    return notes


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Repackage Google Antigravity 2.0, IDE & CLI into Debian (.deb) packages"
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List available upstream releases and exit",
    )
    parser.add_argument(
        "--check-update",
        action="store_true",
        help="Check if upstream has new versions before downloading; exits early if no update",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force download and package even if recorded version is already up-to-date",
    )
    parser.add_argument(
        "--version-file",
        default="./versions.json",
        help="Path to versions.json tracking file (default: ./versions.json)",
    )
    parser.add_argument(
        "--product",
        choices=["antigravity", "antigravity-ide", "antigravity-cli", "both", "all"],
        default="all",
        help="Product to build (default: all)",
    )
    parser.add_argument(
        "--arch",
        choices=["amd64", "arm64", "both", "all"],
        default="amd64",
        help="Debian architecture (amd64, arm64, or both; default: amd64)",
    )
    parser.add_argument(
        "--out-dir",
        default="./dist",
        help="Output directory for generated .deb files (default: ./dist)",
    )
    parser.add_argument(
        "--cache-dir",
        default="./cache",
        help="Directory to cache downloaded tar.gz files (default: ./cache)",
    )
    parser.add_argument(
        "--only-resolve",
        action="store_true",
        help="Only resolve and print download URLs without downloading or building",
    )
    parser.add_argument(
        "--matrix-json",
        action="store_true",
        help="Output GitHub Actions matrix JSON to stdout and exit",
    )
    parser.add_argument(
        "--generate-release-notes",
        metavar="FILE",
        help="Generate GitHub Release notes markdown to specified file path",
    )

    args = parser.parse_args()

    if args.list:
        list_releases()
        return

    version_file = Path(args.version_file).resolve()

    if args.matrix_json:
        m = generate_matrix_json(
            product_req=args.product,
            arch_req=args.arch,
            version_file=version_file,
            check_update_mode=args.check_update,
            force=args.force,
        )
        print(json.dumps(m))
        return

    # Check for updates
    has_update, latest_map, updated_products = check_updates(
        version_file_path=version_file,
        force=args.force,
        target_product=args.product,
    )

    if args.check_update and not has_update and not args.force:
        print("\n[i] All target products are already up-to-date. No new versions found. Exiting.")
        sys.exit(0)

    # Determine which products to build
    if args.product in ("both", "all"):
        if args.check_update and not args.force:
            products_to_build = ALL_PRODUCTS if has_update else []
        else:
            products_to_build = ALL_PRODUCTS
    else:
        if args.check_update and not args.force and args.product not in updated_products:
            print(f"\n[i] Product '{args.product}' is already up-to-date. Exiting.")
            sys.exit(0)
        products_to_build = [args.product]

    if not products_to_build:
        print("\n[i] No products require building.")
        sys.exit(0)

    if args.generate_release_notes:
        notes = generate_release_notes(
            hub_version=latest_map["antigravity"]["version"],
            ide_version=latest_map["antigravity-ide"]["version"],
            cli_version=latest_map["antigravity-cli"]["version"],
            hub_eid=latest_map["antigravity"].get("execution_id", ""),
            ide_eid=latest_map["antigravity-ide"].get("execution_id", ""),
            cli_eid=latest_map["antigravity-cli"].get("execution_id", ""),
            out_file=Path(args.generate_release_notes),
        )
        print(notes)
        return

    arches = ["amd64", "arm64"] if args.arch in ("both", "all") else [args.arch]
    out_dir = Path(args.out_dir).resolve()
    cache_dir = Path(args.cache_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)

    built_packages: List[Path] = []
    versions_to_save = {}

    for prod in products_to_build:
        rel = latest_map[prod]
        ver = rel["version"]
        eid = rel["execution_id"]
        print(f"\n{'=' * 60}")
        print(f"Target: {prod} (Latest Version: {ver}, Execution ID: {eid})")
        print(f"{'=' * 60}")

        for arch in arches:
            print(f"\n--- Building {prod} {ver} ({arch}) ---")
            download_url = ReleaseResolver.get_download_url(prod, ver, eid, arch)
            print(f"Archive URL: {download_url}")

            if args.only_resolve:
                continue

            ext = ".deb" if download_url.endswith(".deb") else ".tar.gz"
            archive_name = f"{prod}-{ver}-{arch}{ext}"
            cached_archive = cache_dir / archive_name

            if not cached_archive.exists() or cached_archive.stat().st_size == 0:
                download_file(download_url, cached_archive)
            else:
                print(f"[i] Using cached archive: {cached_archive} ({cached_archive.stat().st_size / (1024 * 1024):.1f} MB)")

            packager = DebPackager(
                product=prod,
                version=ver,
                deb_arch=arch,
                archive_path=cached_archive,
                output_dir=out_dir,
            )
            deb_path = packager.package()
            built_packages.append(deb_path)
            versions_to_save[prod] = ver

    if args.only_resolve:
        return

    if built_packages:
        print(f"\n{'=' * 60}")
        print("Build Summary:")
        print(f"{'=' * 60}")
        sums_file = out_dir / "SHA256SUMS.txt"
        with open(sums_file, "w", encoding="utf-8") as sf:
            for deb in built_packages:
                sha = sha256_file(deb)
                line = f"{sha}  {deb.name}\n"
                sf.write(line)
                print(f"  Package: {deb.name}")
                print(f"    Path:   {deb}")
                print(f"    SHA256: {sha}")
        print(f"\nChecksum file written to: {sums_file}")

        # Update recorded versions
        save_version_file(version_file, versions_to_save)


if __name__ == "__main__":
    main()
