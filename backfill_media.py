import json
import os
import shutil
import subprocess
import time
from datetime import datetime
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

import requests
from dateutil import parser as dateutil_parser

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

with open(CONFIG_PATH, "r") as f:
    cfg = json.load(f)

API_URL = cfg["api_url"]
MEDIA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), cfg["media_download_dir"])
os.makedirs(MEDIA_DIR, exist_ok=True)

COOKIES = {}
for pair in cfg["cookie_string"].split(";"):
    pair = pair.strip()
    if "=" in pair:
        key, val = pair.split("=", 1)
        COOKIES[key.strip()] = val.strip()

EXIFTOOL = shutil.which("exiftool")
if not EXIFTOOL:
    print("WARNING: exiftool not found. Image metadata will NOT be embedded.")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def build_api_url(page):
    parsed = urlparse(API_URL)
    query = parse_qs(parsed.query)
    query["page"] = [str(page)]
    new_query = urlencode(query, doseq=True)
    return urlunparse(parsed._replace(query=new_query))


def decode_url(url):
    if not url:
        return url
    return url.replace("\\u0026", "&")


def parse_timestamp(dt_string):
    if not dt_string:
        return None
    try:
        dt = dateutil_parser.isoparse(dt_string)
        return dt.timestamp()
    except Exception:
        return None


def get_extension_from_url(url):
    parsed = urlparse(url)
    path = parsed.path
    if "." in path:
        return path.rsplit(".", 1)[1].lower()
    return "jpg"


def set_file_timestamps(filepath, created_at, updated_at):
    atime = parse_timestamp(created_at)
    mtime = parse_timestamp(updated_at)
    if atime or mtime:
        if not atime:
            atime = mtime
        if not mtime:
            mtime = atime
        try:
            os.utime(filepath, (atime, mtime))
        except Exception:
            pass


def download_media(url, activity_id):
    """Download media file. Returns (filepath, is_new) or (None, False)."""
    if not url:
        return None, False

    url = decode_url(url)
    ext = get_extension_from_url(url)
    filename = f"{activity_id}.{ext}"
    filepath = os.path.join(MEDIA_DIR, filename)

    if os.path.exists(filepath):
        return filepath, False

    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        with open(filepath, "wb") as f:
            f.write(resp.content)
        return filepath, True
    except Exception as e:
        print(f"    Failed to download: {e}")
        return None, False


def tag_with_exiftool(filepath, fname, lname, description):
    """Embed metadata using exiftool."""
    if not EXIFTOOL:
        return

    cmd = [EXIFTOOL, "-overwrite_original"]

    keywords = " ".join(filter(None, [fname, lname]))
    if keywords:
        cmd.append(f"-Keywords={keywords}")

    if description:
        cmd.append(f"-ImageDescription={description}")

    cmd.append(filepath)

    try:
        subprocess.run(cmd, capture_output=True, timeout=30)
    except Exception as e:
        print(f"    exiftool failed: {e}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    total_downloaded = 0
    total_tagged = 0
    total_skipped = 0
    page = 1

    while True:
        url = build_api_url(page)
        print(f"\nFetching page {page}...")

        try:
            resp = requests.get(url, cookies=COOKIES, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"  API request failed: {e}")
            break

        intervals = data.get("intervals", {})
        all_activities = []
        for interval_name, activities in intervals.items():
            if activities:
                for activity_data in activities:
                    all_activities.append((interval_name, activity_data))

        if not all_activities:
            print(f"  No activities on page {page}. Done.")
            break

        print(f"  Page {page}: {len(all_activities)} activities")

        for interval_name, item in all_activities:
            activity = item.get("activity", {})
            activity_id = activity.get("id")
            if not activity_id:
                continue

            created_at = activity.get("created_at", "")
            updated_at = activity.get("updated_at", "")
            description = activity.get("description", "")
            fname = activity.get("account_user", {}).get("account", {}).get("fname", "")
            lname = activity.get("account_user", {}).get("account", {}).get("lname", "")

            image_url = activity.get("image", {}).get("url") or ""
            if image_url:
                filepath, is_new = download_media(image_url, activity_id)
                if filepath:
                    ext = get_extension_from_url(decode_url(image_url))
                    tag_with_exiftool(filepath, fname, lname, description)
                    set_file_timestamps(filepath, created_at, updated_at)
                    total_tagged += 1
                    if is_new:
                        total_downloaded += 1
                        print(f"  [{total_downloaded + total_skipped}] {activity_id}.{ext} - {description[:60] or '(no description)'} ({fname} {lname}) [downloaded]")
                    else:
                        total_skipped += 1

            video_urls = activity.get("video_urls") or {}
            mp4_url = video_urls.get("mp4") or ""
            if mp4_url:
                filepath, is_new = download_media(mp4_url, activity_id)
                if filepath:
                    ext = get_extension_from_url(decode_url(mp4_url))
                    set_file_timestamps(filepath, created_at, updated_at)
                    if is_new:
                        total_downloaded += 1
                        print(f"  [{total_downloaded + total_skipped}] {activity_id}.{ext} - {description[:60] or '(no description)'} ({fname} {lname}) [video, downloaded]")
                    else:
                        total_skipped += 1

        page += 1

    print(f"\n{'='*60}")
    print(f"Complete.")
    print(f"  Pages fetched: {page - 1}")
    print(f"  New files downloaded: {total_downloaded}")
    print(f"  Existing files skipped: {total_skipped}")
    print(f"  Files tagged with exiftool: {total_tagged}")
    print(f"  Media directory: {MEDIA_DIR}")


if __name__ == "__main__":
    main()
