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
OFFICIAL_RELEASES_PAGE = "https://antigravity.google/releases"

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
    def get_latest_versions() -> Dict[str, Dict[str, str]]:
        """Retrieve latest release info for both Antigravity 2.0 and Antigravity IDE."""
        hub = ReleaseResolver.fetch_hub_releases()
        ide = ReleaseResolver.fetch_ide_releases()
        if not hub:
            raise RuntimeError("Failed to fetch Antigravity 2.0 releases")
        if not ide:
            raise RuntimeError("Failed to fetch Antigravity IDE releases")
        return {
            "antigravity": hub[0],
            "antigravity-ide": ide[0],
        }

    @staticmethod
    def get_download_url(product: str, version: str, execution_id: str, deb_arch: str) -> str:
        """
        Construct download URL based on product, version, execution_id, and architecture.
        """
        platform = ARCH_MAP.get(deb_arch.lower(), "linux-x64")
        if product == "antigravity":
            return (
                f"https://storage.googleapis.com/antigravity-public/antigravity-hub/"
                f"{version}-{execution_id}/{platform}/Antigravity.tar.gz"
            )
        elif product == "antigravity-ide":
            return (
                f"https://edgedl.me.gvt1.com/edgedl/release2/j0qc3/antigravity/stable/"
                f"{version}-{execution_id}/{platform}/Antigravity%20IDE.tar.gz"
            )
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
            subprocess.run(
                ["tar", "-zxf", str(self.archive_path), "-C", str(extract_dir)],
                check=True,
            )

            # Locate top-level extracted directory
            entries = list(extract_dir.iterdir())
            if len(entries) == 1 and entries[0].is_dir():
                payload_source = entries[0]
            else:
                payload_source = extract_dir

            opt_app_dir = staging_root / "opt" / self.product
            opt_app_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(payload_source), str(opt_app_dir))

            print(f"[*] Setting up packaging structure for {self.product}...")
            # 1. Executable launcher
            exe_name, exe_path = self._setup_executable(opt_app_dir, staging_root)

            # 2. Desktop entry & icons
            self._setup_desktop_and_icons(opt_app_dir, staging_root, exe_name)

            # 3. Permissions
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

        return target_bin.name, target_bin

    def _setup_desktop_and_icons(self, opt_app_dir: Path, staging_root: Path, exe_name: str) -> None:
        """Locate icons and install .desktop file and pixmaps."""
        applications_dir = staging_root / "usr" / "share" / "applications"
        pixmaps_dir = staging_root / "usr" / "share" / "pixmaps"
        icons_dir = staging_root / "usr" / "share" / "icons" / "hicolor" / "512x512" / "apps"

        applications_dir.mkdir(parents=True, exist_ok=True)
        pixmaps_dir.mkdir(parents=True, exist_ok=True)
        icons_dir.mkdir(parents=True, exist_ok=True)

        icon_source: Optional[Path] = None
        icon_candidates = [
            opt_app_dir / "resources" / "app" / "resources" / "linux" / "code.png",
            opt_app_dir / "resources" / "icon.png",
            opt_app_dir / "resources" / "app" / "resources" / "icon.png",
        ]
        for c in icon_candidates:
            if c.is_file():
                icon_source = c
                break

        if not icon_source:
            best_size = 0
            for p in opt_app_dir.rglob("*.png"):
                if "node_modules" in str(p):
                    continue
                size = p.stat().st_size
                if size > best_size:
                    best_size = size
                    icon_source = p

        icon_name = self.product
        if icon_source and icon_source.is_file():
            shutil.copy2(icon_source, pixmaps_dir / f"{icon_name}.png")
            shutil.copy2(icon_source, icons_dir / f"{icon_name}.png")
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
        else:
            display_name = "Antigravity IDE"
            comment = "Google Antigravity IDE - Code with Agents"
            categories = "Development;IDE;"
            startup_wm_class = "Antigravity IDE"
            mime_type = "text/plain;inode/directory;"

        desktop_file = applications_dir / f"{self.product}.desktop"
        desktop_content = f"""[Desktop Entry]
Name={display_name}
Comment={comment}
GenericName=Text Editor and Agent Workspace
Exec=/opt/{self.product}/{exe_name} %F
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

    def _fix_permissions(self, staging_root: Path, opt_app_dir: Path) -> None:
        """Fix permissions for Debian standards and Chrome Sandbox."""
        chrome_sandbox = opt_app_dir / "chrome-sandbox"
        if chrome_sandbox.exists():
            chrome_sandbox.chmod(0o4755)

        for path in staging_root.rglob("*"):
            if path == chrome_sandbox:
                continue
            if path.is_dir():
                path.chmod(0o755)
            elif path.is_file():
                if os.access(path, os.X_OK):
                    path.chmod(0o755)
                else:
                    path.chmod(0o644)

    def _setup_debian_metadata(self, staging_root: Path) -> None:
        """Create DEBIAN/control, postinst, and postrm files."""
        debian_dir = staging_root / "DEBIAN"
        debian_dir.mkdir(parents=True, exist_ok=True)

        dependencies = (
            "ca-certificates, libasound2 | libasound2t64, libatk-bridge2.0-0, libatk1.0-0, "
            "libc6, libcairo2, libcups2, libdbus-1-3, libexpat1, libfontconfig1, libgbm1, "
            "libglib2.0-0, libgtk-3-0, libnspr4, libnss3, libpango-1-0-0, libpangocairo-1.0-0, "
            "libsecret-1-0, libx11-6, libx11-xcb1, libxcb1, libxcomposite1, libxcursor1, "
            "libxdamage1, libxext6, libxfixes3, libxi6, libxrandr2, libxrender1, libxss1, libxtst6"
        )

        if self.product == "antigravity":
            pkg_name = "antigravity"
            description = (
                "Google Antigravity 2.0 Desktop Application\n"
                " Unified agentic coding platform with autonomous background task runners,\n"
                " interactive chat canvas, and multi-agent orchestration."
            )
        else:
            pkg_name = "antigravity-ide"
            description = (
                "Google Antigravity IDE\n"
                " AI-first integrated development environment built for seamless\n"
                " agentic workflows, autocomplete, and inline editing."
            )

        control_content = f"""Package: {pkg_name}
Version: {self.version}
Section: devel
Priority: optional
Architecture: {self.deb_arch}
Maintainer: Antigravity Community <noreply@antigravity.google>
Depends: {dependencies}
Homepage: https://antigravity.google
Description: {description}
"""
        with open(debian_dir / "control", "w", encoding="utf-8") as f:
            f.write(control_content)
        (debian_dir / "control").chmod(0o644)

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

exit 0
"""
        postinst_file = debian_dir / "postinst"
        with open(postinst_file, "w", encoding="utf-8") as f:
            f.write(postinst_content)
        postinst_file.chmod(0o755)

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
fi

exit 0
"""
        postrm_file = debian_dir / "postrm"
        with open(postrm_file, "w", encoding="utf-8") as f:
            f.write(postrm_content)
        postrm_file.chmod(0o755)


def list_releases() -> None:
    """Print available releases for both products."""
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
    print()


def check_updates(
    version_file_path: Path,
    force: bool = False,
) -> Tuple[bool, Dict[str, Dict[str, str]], List[str]]:
    """
    Check if upstream has newer releases than recorded in version_file.
    Returns:
        (has_update, latest_releases_map, updated_products_list)
    """
    latest_map = ReleaseResolver.get_latest_versions()
    recorded_versions = load_version_file(version_file_path)

    updated_products = []
    print("[*] Checking for updates against recorded versions:", file=sys.stderr)
    for prod in ["antigravity", "antigravity-ide"]:
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
    )

    hub_rel = latest_map["antigravity"]
    ide_rel = latest_map["antigravity-ide"]

    all_products = ["antigravity", "antigravity-ide"] if product_req == "both" else [product_req]
    arches = ["amd64", "arm64"] if arch_req == "both" else [arch_req]

    # Filter target products
    if check_update_mode and not force:
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
        "updated_products": target_products,
        "matrix": matrix_list,
    }


def generate_release_notes(hub_version: str, ide_version: str, out_file: Optional[Path] = None) -> str:
    """Generate Markdown release notes for GitHub Releases."""
    notes = f"""## 🚀 Google Antigravity & Antigravity IDE Linux (.deb) Releases

Automated Debian packages built directly from official Google Antigravity release archives.

---

### 📦 Packages in this Release

| Application | Version | Arch | Package Name |
| :--- | :--- | :--- | :--- |
| **Antigravity 2.0** | `{hub_version}` | `amd64` | `antigravity_{hub_version}_amd64.deb` |
| **Antigravity 2.0** | `{hub_version}` | `arm64` | `antigravity_{hub_version}_arm64.deb` |
| **Antigravity IDE** | `{ide_version}` | `amd64` | `antigravity-ide_{ide_version}_amd64.deb` |
| **Antigravity IDE** | `{ide_version}` | `arm64` | `antigravity-ide_{ide_version}_arm64.deb` |

---

### 💻 Quick Installation

#### Ubuntu / Debian / Pop!_OS / Linux Mint
```bash
# 1. Download the deb package for your architecture
# 2. Install with apt (automatically resolves dependencies):
sudo apt update
sudo apt install ./antigravity_{hub_version}_amd64.deb
sudo apt install ./antigravity-ide_{ide_version}_amd64.deb
```

#### Launching
- Run `antigravity` to launch Antigravity 2.0 Desktop Platform.
- Run `antigravity-ide` (or `agy-ide`) to launch Antigravity IDE.
- Both applications are integrated into your desktop application menu.

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
        description="Repackage Google Antigravity 2.0 & Antigravity IDE into Debian (.deb) packages"
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
        choices=["antigravity", "antigravity-ide", "both"],
        default="both",
        help="Product to build (default: both)",
    )
    parser.add_argument(
        "--arch",
        choices=["amd64", "arm64", "both"],
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
    )

    if args.check_update and not has_update and not args.force:
        print("\n[i] All target products are already up-to-date. No new versions found. Exiting.")
        sys.exit(0)

    # Determine which products to build
    if args.product == "both":
        products_to_build = updated_products if (args.check_update and not args.force) else ["antigravity", "antigravity-ide"]
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
            latest_map["antigravity"]["version"],
            latest_map["antigravity-ide"]["version"],
            Path(args.generate_release_notes),
        )
        print(notes)
        return

    arches = ["amd64", "arm64"] if args.arch == "both" else [args.arch]
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

            archive_name = f"{prod}-{ver}-{arch}.tar.gz"
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
