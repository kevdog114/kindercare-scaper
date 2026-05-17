import html
import json
import os
import random
import secrets
import subprocess
import sys
import threading
import time
from datetime import datetime
from urllib.parse import urlparse

import requests
from dateutil import parser as dateutil_parser
from flask import Flask, Response, request, send_from_directory

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
CONFIG_PATH = os.environ.get("CONFIG_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.local.json"))

with open(CONFIG_PATH, "r") as f:
    cfg = json.load(f)

API_URL = cfg["api_url"]
SYNC_START = cfg["sync_start_time"]
SYNC_END = cfg["sync_end_time"]
REFRESH_MINUTES = cfg["refresh_interval_minutes"]
JITTER_MAX = cfg["jitter_max_seconds"]
MEDIA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), cfg["media_download_dir"])
SERVER_PORT = cfg.get("server_port", 8080)
AUTH_TOKEN = os.environ.get("AUTH_TOKEN", secrets.token_urlsafe(32))

os.makedirs(MEDIA_DIR, exist_ok=True)

COOKIES = {}
for pair in cfg["cookie_string"].split(";"):
    pair = pair.strip()
    if "=" in pair:
        key, val = pair.split("=", 1)
        COOKIES[key.strip()] = val.strip()

start_h, start_m = map(int, SYNC_START.split(":"))
end_h, end_m = map(int, SYNC_END.split(":"))
sync_start_minutes = start_h * 60 + start_m
sync_end_minutes = end_h * 60 + end_m

# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------
cached_data = None
cache_lock = threading.Lock()
backfill_lock = threading.Lock()
sync_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def now_in_sync_window():
    current_minutes = datetime.now().hour * 60 + datetime.now().minute
    if sync_start_minutes <= sync_end_minutes:
        return sync_start_minutes <= current_minutes <= sync_end_minutes
    else:
        return current_minutes >= sync_start_minutes or current_minutes <= sync_end_minutes


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


def download_media(url, activity_id, created_at=None, updated_at=None):
    if not url:
        return False

    url = decode_url(url)
    ext = get_extension_from_url(url)
    filename = f"{activity_id}.{ext}"
    filepath = os.path.join(MEDIA_DIR, filename)

    if os.path.exists(filepath):
        return False

    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        with open(filepath, "wb") as f:
            f.write(resp.content)
        set_file_timestamps(filepath, created_at, updated_at)
        return True
    except Exception as e:
        print(f"  Failed to download {url}: {e}")
        return False


def fetch_and_sync():
    global cached_data
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Syncing...")

    try:
        resp = requests.get(API_URL, cookies=COOKIES, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  API fetch failed: {e}")
        return False, str(e)

    today_items = data.get("intervals", {}).get("Today", [])
    downloaded = 0

    for item in today_items:
        activity = item.get("activity", {})
        activity_id = activity.get("id")
        if not activity_id:
            continue

        created_at = activity.get("created_at", "")
        updated_at = activity.get("updated_at", "")

        image_url = activity.get("image", {}).get("url") or ""
        if image_url and download_media(image_url, activity_id, created_at, updated_at):
            downloaded += 1

        video_urls = activity.get("video_urls") or {}
        mp4_url = video_urls.get("mp4") or ""
        if mp4_url and download_media(mp4_url, activity_id, created_at, updated_at):
            downloaded += 1

    with cache_lock:
        cached_data = data

    print(f"  Sync complete. {len(today_items)} activities, {downloaded} new media files.")
    return True, f"Synced {len(today_items)} activities, {downloaded} new files"


def sync_worker():
    while True:
        if now_in_sync_window():
            with sync_lock:
                fetch_and_sync()
            jitter = random.uniform(-JITTER_MAX, JITTER_MAX)
            sleep_seconds = REFRESH_MINUTES * 60 + jitter
            sleep_seconds = max(1, sleep_seconds)
            print(f"  Next sync in ~{sleep_seconds:.0f}s")
            time.sleep(sleep_seconds)
        else:
            print(f"  Outside sync window ({SYNC_START}-{SYNC_END}). Checking in 60s.")
            time.sleep(60)


def run_backfill():
    import shutil
    from urllib.parse import parse_qs, urlencode, urlunparse

    def build_api_url(page):
        parsed = urlparse(API_URL)
        query = parse_qs(parsed.query)
        query["page"] = [str(page)]
        new_query = urlencode(query, doseq=True)
        return urlunparse(parsed._replace(query=new_query))

    exiftool = shutil.which("exiftool")

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
                url_decoded = decode_url(image_url)
                ext = get_extension_from_url(url_decoded)
                filename = f"{activity_id}.{ext}"
                filepath = os.path.join(MEDIA_DIR, filename)

                if os.path.exists(filepath):
                    total_skipped += 1
                else:
                    try:
                        resp = requests.get(url_decoded, timeout=30)
                        resp.raise_for_status()
                        with open(filepath, "wb") as f:
                            f.write(resp.content)
                        set_file_timestamps(filepath, created_at, updated_at)
                        total_downloaded += 1
                    except Exception as e:
                        print(f"    Failed to download: {e}")
                        continue

                if exiftool:
                    cmd = [exiftool, "-overwrite_original"]
                    keywords = " ".join(filter(None, [fname, lname]))
                    if keywords:
                        cmd.append(f"-Keywords={keywords}")
                    if description:
                        cmd.append(f"-ImageDescription={description}")
                    cmd.append(filepath)
                    try:
                        subprocess.run(cmd, capture_output=True, timeout=30)
                        total_tagged += 1
                    except Exception as e:
                        print(f"    exiftool failed: {e}")

            video_urls = activity.get("video_urls") or {}
            mp4_url = video_urls.get("mp4") or ""
            if mp4_url:
                url_decoded = decode_url(mp4_url)
                ext = get_extension_from_url(url_decoded)
                filename = f"{activity_id}.{ext}"
                filepath = os.path.join(MEDIA_DIR, filename)

                if os.path.exists(filepath):
                    total_skipped += 1
                else:
                    try:
                        resp = requests.get(url_decoded, timeout=30)
                        resp.raise_for_status()
                        with open(filepath, "wb") as f:
                            f.write(resp.content)
                        set_file_timestamps(filepath, created_at, updated_at)
                        total_downloaded += 1
                    except Exception as e:
                        print(f"    Failed to download video: {e}")

        page += 1

    return {
        "pages_fetched": page - 1,
        "new_files_downloaded": total_downloaded,
        "existing_files_skipped": total_skipped,
        "files_tagged": total_tagged,
        "media_dir": MEDIA_DIR
    }


# ---------------------------------------------------------------------------
# RSS
# ---------------------------------------------------------------------------

def build_rss(data, base_url):
    today_items = data.get("intervals", {}).get("Today", [])
    now = datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S +0000")

    rss = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Kindercare Activities - {datetime.utcnow().strftime("%Y-%m-%d")}</title>
    <link>https://classroom.kindercare.com</link>
    <description>Today's activities</description>
    <lastBuildDate>{now}</lastBuildDate>
    <language>en-us</language>
"""

    for item in today_items:
        activity = item.get("activity", {})
        desc = html.escape(activity.get("description", ""))
        item_id = activity.get("id", "")
        list_date = activity.get("list_date", now)
        fname = html.escape(activity.get("account_user", {}).get("account", {}).get("fname", ""))

        image_url_remote = activity.get("image", {}).get("url") or ""
        if image_url_remote:
            image_ext = get_extension_from_url(decode_url(image_url_remote))
            image_filename = f"{item_id}.{image_ext}"
            image_path = os.path.join(MEDIA_DIR, image_filename)
            image_url = f"{base_url}/media/{image_filename}" if os.path.exists(image_path) else ""
        else:
            image_url = ""

        video_urls = activity.get("video_urls") or {}
        mp4_url = video_urls.get("mp4") or ""
        if mp4_url:
            video_ext = get_extension_from_url(decode_url(mp4_url))
            video_filename = f"{item_id}.{video_ext}"
            video_path = os.path.join(MEDIA_DIR, video_filename)
            video_url = f"{base_url}/media/{video_filename}" if os.path.exists(video_path) else ""
        else:
            video_url = ""

        rss += f"""    <item>
      <title>{desc}</title>
      <description>{desc}</description>
      <link>https://classroom.kindercare.com/activity/{item_id}</link>
      <guid>{item_id}</guid>
      <pubDate>{list_date}</pubDate>
      <author>{fname}</author>"""

        if image_url:
            rss += f"""
      <enclosure url="{image_url}" type="image/jpeg" length="0"/>"""

        if video_url:
            rss += f"""
      <enclosure url="{video_url}" type="video/mp4" length="0"/>"""

        rss += """
    </item>
"""

    rss += """  </channel>
</rss>"""
    return rss


# ---------------------------------------------------------------------------
# Flask
# ---------------------------------------------------------------------------
app = Flask(__name__, static_folder=MEDIA_DIR, static_url_path="/media")


def check_auth():
    token = request.args.get("auth-token")
    if not token or token != AUTH_TOKEN:
        return Response("Unauthorized", status=401)


@app.route("/rss")
def rss():
    with cache_lock:
        data = cached_data
    if data is None:
        return Response("No data yet. Waiting for first sync.", status=503, mimetype="text/plain")
    try:
        base_url = request.host_url.rstrip("/")
        rss_xml = build_rss(data, base_url)
        return Response(rss_xml, mimetype="application/rss+xml")
    except Exception as e:
        return Response(f"Error: {e}", status=500, mimetype="text/plain")


@app.route("/media/<path:filename>")
def serve_media(filename):
    return send_from_directory(MEDIA_DIR, filename)


@app.route("/api/sync")
def api_sync():
    auth_err = check_auth()
    if auth_err:
        return auth_err

    with sync_lock:
        success, message = fetch_and_sync()

    if success:
        return Response(json.dumps({"status": "success", "message": message}), mimetype="application/json")
    else:
        return Response(json.dumps({"status": "error", "message": message}), status=500, mimetype="application/json")


@app.route("/api/backfill")
def api_backfill():
    auth_err = check_auth()
    if auth_err:
        return auth_err

    with backfill_lock:
        result = run_backfill()

    return Response(json.dumps({"status": "success", "result": result}), mimetype="application/json")


@app.route("/api/status")
def api_status():
    auth_err = check_auth()
    if auth_err:
        return auth_err

    with cache_lock:
        data = cached_data

    if data:
        today_items = data.get("intervals", {}).get("Today", [])
        return Response(json.dumps({
            "status": "running",
            "activities_cached": len(today_items),
            "sync_window": f"{SYNC_START}-{SYNC_END}",
            "in_sync_window": now_in_sync_window(),
        }), mimetype="application/json")
    else:
        return Response(json.dumps({
            "status": "no_data",
            "message": "Waiting for first sync",
        }), mimetype="application/json")


if __name__ == "__main__":
    fetch_and_sync()

    t = threading.Thread(target=sync_worker, daemon=True)
    t.start()

    print(f"Server starting on port {SERVER_PORT}")
    print(f"Auth token: {AUTH_TOKEN}")
    app.run(host="0.0.0.0", port=SERVER_PORT)
