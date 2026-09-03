import os
import json
import time
import uuid
import tempfile
import threading
import subprocess
import requests
from datetime import datetime
from flask import Flask, request, jsonify, send_file, render_template, session
import imageio_ffmpeg
import yt_dlp

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "mchlern-super-secret-2024")
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
INTRO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "intro.mp3")
TEMP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "temp_audio")
os.makedirs(TEMP_DIR, exist_ok=True)

# ── CLEANUP old temp files (>30 min) ─────────────────────────────────────────
def cleanup_old_files():
    while True:
        try:
            now = time.time()
            for f in os.listdir(TEMP_DIR):
                fp = os.path.join(TEMP_DIR, f)
                if os.path.isfile(fp) and now - os.path.getmtime(fp) > 1800:
                    os.remove(fp)
        except Exception:
            pass
        time.sleep(300)

threading.Thread(target=cleanup_old_files, daemon=True).start()

# ── HELPER: Resolve Roblox operation ID ───────────────────────────────────────
def resolve_asset_id(api_key, operation_path, max_wait=90):
    if not operation_path or not str(operation_path).startswith("operations"):
        return str(operation_path)
    poll_url = f"https://apis.roblox.com/assets/v1/{operation_path}"
    headers = {"x-api-key": api_key}
    deadline = time.time() + max_wait
    while time.time() < deadline:
        try:
            r = requests.get(poll_url, headers=headers, timeout=10)
            if r.status_code == 200:
                data = r.json()
                if data.get("done"):
                    asset_id = (data.get("response") or {}).get("assetId") or \
                               (data.get("response") or {}).get("id")
                    if asset_id:
                        return str(asset_id)
        except Exception:
            pass
        time.sleep(2)
    return str(operation_path)

# ── HELPER: Process audio with FFmpeg ────────────────────────────────────────
def process_audio(input_path, speed=1.0, pitch=1.0, vol=1.0, with_intro=True):
    ext = ".mp3"
    out_name = f"{uuid.uuid4().hex}{ext}"
    out_path = os.path.join(TEMP_DIR, out_name)

    tempo = speed / pitch if pitch != 0 else 1.0

    if with_intro and os.path.exists(INTRO_PATH):
        filt = (
            f"[0:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo[a0];"
            f"[1:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo[a1];"
            f"[a0][a1]concat=n=2:v=0:a=1,aresample=44100,asetrate=44100*{pitch},"
            f"atempo={tempo},volume={vol}[out]"
        )
        cmd = [FFMPEG, "-y", "-i", INTRO_PATH, "-i", input_path,
               "-filter_complex", filt, "-map", "[out]", out_path]
    else:
        filt = f"aresample=44100,asetrate=44100*{pitch},atempo={tempo},volume={vol}"
        cmd = [FFMPEG, "-y", "-i", input_path, "-filter:a", filt, out_path]

    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return out_path, out_name

# ── HELPER: Process preview (15s only) ───────────────────────────────────────
def process_preview(input_path, speed=1.0, pitch=1.0, vol=1.0):
    out_name = f"prev_{uuid.uuid4().hex}.mp3"
    out_path = os.path.join(TEMP_DIR, out_name)
    tempo = speed / pitch if pitch != 0 else 1.0

    if os.path.exists(INTRO_PATH):
        filt = (
            f"[0:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo[a0];"
            f"[1:a]atrim=duration=12,aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo[a1];"
            f"[a0][a1]concat=n=2:v=0:a=1,aresample=44100,asetrate=44100*{pitch},"
            f"atempo={tempo},volume={vol}[out]"
        )
        cmd = [FFMPEG, "-y", "-i", INTRO_PATH, "-i", input_path,
               "-filter_complex", filt, "-map", "[out]", out_path]
    else:
        filt = f"aresample=44100,asetrate=44100*{pitch},atempo={tempo},volume={vol}"
        cmd = [FFMPEG, "-y", "-i", input_path, "-t", "15", "-filter:a", filt, out_path]

    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return out_path, out_name

# ═══════════════════════════════════════════════════════════════════════════════
# ROUTES
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return render_template("index.html")

# ── GET temp audio file ───────────────────────────────────────────────────────
@app.route("/temp/<filename>")
def serve_temp(filename):
    fp = os.path.join(TEMP_DIR, os.path.basename(filename))
    if os.path.exists(fp):
        return send_file(fp, mimetype="audio/mpeg")
    return jsonify({"error": "File not found"}), 404

# ── Check Roblox profile ──────────────────────────────────────────────────────
@app.route("/api/check-account", methods=["POST"])
def check_account():
    data = request.get_json()
    cid = str(data.get("creator_id", "")).strip()
    ctype = data.get("creator_type", "User")
    if not cid:
        return jsonify({"error": "No creator ID"}), 400
    try:
        if ctype == "User":
            info = requests.get(f"https://users.roblox.com/v1/users/{cid}", timeout=5).json()
            name = info.get("displayName") or info.get("name") or "Unknown"
            thumb_r = requests.get(
                f"https://thumbnails.roblox.com/v1/users/avatar-headshot?userIds={cid}&size=150x150&format=Png&isCircular=false",
                timeout=5).json()
            url = f"https://www.roblox.com/users/{cid}/profile"
        else:
            info = requests.get(f"https://groups.roblox.com/v1/groups/{cid}", timeout=5).json()
            name = info.get("name") or "Unknown Group"
            thumb_r = requests.get(
                f"https://thumbnails.roblox.com/v1/groups/icons?groupIds={cid}&size=150x150&format=Png&isCircular=false",
                timeout=5).json()
            url = f"https://www.roblox.com/groups/{cid}"

        img_url = ""
        thumbs = thumb_r.get("data", [])
        if thumbs:
            img_url = thumbs[0].get("imageUrl", "")

        return jsonify({"name": name, "avatar": img_url, "url": url})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── Fetch YouTube info + download ─────────────────────────────────────────────
@app.route("/api/fetch-yt", methods=["POST"])
def fetch_yt():
    data = request.get_json()
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL"}), 400

    dl_name = f"yt_{uuid.uuid4().hex}"
    ydl_opts = {
        "format": "bestaudio/best",
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3"}],
        "outtmpl": os.path.join(TEMP_DIR, dl_name),
        "ffmpeg_location": FFMPEG,
        "quiet": True,
        "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
        "nocheckcertificate": True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if not info:
                raise Exception("Failed to extract info")
            title = info.get("title", "Unknown")
            thumb = info.get("thumbnail") or (info.get("thumbnails") or [{}])[-1].get("url", "")
            actual = os.path.join(TEMP_DIR, dl_name + ".mp3")
            if not os.path.exists(actual):
                actual = os.path.join(TEMP_DIR, ydl.prepare_filename(info).rsplit(".", 1)[0] + ".mp3")

        return jsonify({
            "title": title,
            "thumbnail": thumb,
            "file": os.path.basename(actual),
            "source": url
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── Generate preview audio ────────────────────────────────────────────────────
@app.route("/api/preview", methods=["POST"])
def preview():
    speed = float(request.form.get("speed", 1.0))
    pitch = float(request.form.get("pitch", 1.0))
    vol   = float(request.form.get("vol", 1.0))
    yt_file = request.form.get("yt_file", "").strip()

    if yt_file:
        input_path = os.path.join(TEMP_DIR, os.path.basename(yt_file))
    elif "file" in request.files:
        f = request.files["file"]
        tmp_name = f"up_{uuid.uuid4().hex}.mp3"
        input_path = os.path.join(TEMP_DIR, tmp_name)
        f.save(input_path)
    else:
        return jsonify({"error": "No file"}), 400

    try:
        _, out_name = process_preview(input_path, speed, pitch, vol)
        return jsonify({"preview_url": f"/temp/{out_name}"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── Upload to Roblox ──────────────────────────────────────────────────────────
@app.route("/api/upload", methods=["POST"])
def upload():
    api_key    = request.form.get("api_key", "").strip()
    creator_id = request.form.get("creator_id", "").strip()
    creator_type = request.form.get("creator_type", "User")
    title      = request.form.get("title", "Audio")[:50]
    speed      = float(request.form.get("speed", 1.0))
    pitch      = float(request.form.get("pitch", 1.0))
    vol        = float(request.form.get("vol", 1.0))
    bypass     = request.form.get("bypass", "false") == "true"
    yt_file    = request.form.get("yt_file", "").strip()

    if not api_key or not creator_id:
        return jsonify({"error": "API Key & Creator ID wajib diisi!"}), 400

    # Get input file
    if yt_file:
        input_path = os.path.join(TEMP_DIR, os.path.basename(yt_file))
        if not os.path.exists(input_path):
            return jsonify({"error": "File YouTube tidak ditemukan. Fetch ulang."}), 404
    elif "file" in request.files:
        f = request.files["file"]
        tmp_name = f"up_{uuid.uuid4().hex}.mp3"
        input_path = os.path.join(TEMP_DIR, tmp_name)
        f.save(input_path)
    else:
        return jsonify({"error": "Tidak ada file"}), 400

    temp_out = None
    try:
        s = speed if bypass else 1.0
        p = pitch if bypass else 1.0
        v = vol   if bypass else 1.0
        temp_out, _ = process_audio(input_path, s, p, v, with_intro=True)

        creator_key = "userId" if creator_type == "User" else "groupId"
        req_body = {
            "assetType": "Audio",
            "displayName": title,
            "description": "MCHLERN UPLOADER WEB",
            "creationContext": {"creator": {creator_key: str(creator_id)}}
        }

        with open(temp_out, "rb") as fh:
            resp = requests.post(
                "https://apis.roblox.com/assets/v1/assets",
                headers={"x-api-key": api_key},
                files={"fileContent": (os.path.basename(temp_out), fh, "audio/mpeg")},
                data={"request": json.dumps(req_body)},
                timeout=60
            )

        if resp.status_code == 200:
            raw = resp.json()
            raw_id = raw.get("assetId") or raw.get("path") or raw.get("id") or "Unknown"
            asset_id = resolve_asset_id(api_key, str(raw_id))
            blocked = False
            return jsonify({
                "success": True,
                "asset_id": asset_id,
                "title": title,
                "blocked": blocked,
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            })
        else:
            status_text = f"Fail {resp.status_code}: {resp.text[:100]}"
            blocked = resp.status_code in (403, 401)
            return jsonify({
                "success": False,
                "asset_id": "-",
                "title": title,
                "blocked": blocked,
                "error": status_text,
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            })
    except Exception as e:
        return jsonify({"success": False, "error": str(e), "asset_id": "-", "title": title,
                        "blocked": False, "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
    finally:
        for fp in [temp_out]:
            if fp and os.path.exists(fp):
                try: os.remove(fp)
                except: pass

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
