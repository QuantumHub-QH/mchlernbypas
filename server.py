import os
import json
import time
import uuid
import tempfile
import threading
import subprocess
import requests
import sqlite3
import urllib.parse
from datetime import datetime, date, timedelta
from flask import Flask, request, jsonify, send_file, render_template, render_template_string, session, redirect
import imageio_ffmpeg
import yt_dlp

try:
    from google.oauth2 import id_token
    from google.auth.transport import requests as google_requests
except ImportError:
    id_token = None

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "mchlern-super-secret-2024")

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
DISCORD_CLIENT_ID = os.environ.get("DISCORD_CLIENT_ID", "")
DISCORD_CLIENT_SECRET = os.environ.get("DISCORD_CLIENT_SECRET", "")
DISCORD_GUILD_ID = os.environ.get("DISCORD_GUILD_ID", "")
DISCORD_REDIRECT_URI = os.environ.get("DISCORD_REDIRECT_URI", "https://mchlernbypas-production.up.railway.app/callback/discord")

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
INTRO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "intro.mp3")
TEMP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "temp_audio")

# DATA_DIR: set ke /data di Railway (pakai Volume) supaya database tidak hilang saat restart
DATA_DIR = os.environ.get("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(DATA_DIR, "usage.db")

os.makedirs(TEMP_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)

# ── DATABASE SETUP ────────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS usage (
                    identifier TEXT,
                    upload_date TEXT,
                    count INTEGER,
                    PRIMARY KEY (identifier, upload_date)
                 )''')
    c.execute('''CREATE TABLE IF NOT EXISTS premium (
                    identifier TEXT PRIMARY KEY,
                    expiry_date TEXT
                 )''')
    c.execute('''CREATE TABLE IF NOT EXISTS users (
                    discord_id TEXT PRIMARY KEY,
                    discord_username TEXT,
                    discord_avatar TEXT,
                    joined_at TEXT
                 )''')
    c.execute('''CREATE TABLE IF NOT EXISTS comments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT,
                    avatar TEXT,
                    content TEXT,
                    rating INTEGER,
                    created_at TEXT
                 )''')
    c.execute('''CREATE TABLE IF NOT EXISTS products (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    category TEXT,
                    title TEXT,
                    price TEXT,
                    image_url TEXT,
                    description TEXT
                 )''')
    c.execute('''CREATE TABLE IF NOT EXISTS categories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE
                 )''')
    try:
        c.execute("ALTER TABLE products ADD COLUMN description TEXT")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()

# In-memory tracking for online users
online_users = {}

init_db()

def get_usage(identifier):
    today = str(date.today())
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT count FROM usage WHERE identifier=? AND upload_date=?", (identifier, today))
    row = c.fetchone()
    conn.close()
    return row[0] if row else 0

def increment_usage(identifier):
    today = str(date.today())
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT count FROM usage WHERE identifier=? AND upload_date=?", (identifier, today))
    row = c.fetchone()
    if row:
        c.execute("UPDATE usage SET count=count+1 WHERE identifier=? AND upload_date=?", (identifier, today))
    else:
        c.execute("INSERT INTO usage (identifier, upload_date, count) VALUES (?, ?, 1)", (identifier, today))
    conn.commit()
    conn.close()

def verify_google_token(token):
    if not token or not id_token: return None
    try:
        idinfo = id_token.verify_oauth2_token(token, google_requests.Request(), GOOGLE_CLIENT_ID)
        return idinfo['email']
    except Exception as e:
        return None

def get_real_ip():
    """Ambil IP asli user, bukan IP proxy Railway"""
    forwarded = request.headers.get('X-Forwarded-For', '')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return request.remote_addr

def check_limits(token, client_ip, discord_id=None):
    """
    Prioritas identifier:
    1. Google email (jika login Google) → limit 10/hari
    2. Discord ID (jika login Discord) → limit 2/hari  
    3. Real IP (fallback) → limit 2/hari
    """
    email = verify_google_token(token)
    if email:
        identifier = f"google_{email}"
        max_limit = 10
    elif discord_id:
        identifier = f"discord_{discord_id}"
        max_limit = 2
    else:
        real_ip = get_real_ip()
        identifier = f"ip_{real_ip}"
        max_limit = 2
        
    # Cek Premium (cek juga via Discord ID kalau ada)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT expiry_date FROM premium WHERE identifier=?", (identifier,))
    row = c.fetchone()
    
    # Kalau tidak premium via identifier utama, cek juga via discord_id (untuk premium yang di-add via discord)
    if not row and discord_id and not identifier.startswith("discord_"):
        c.execute("SELECT expiry_date FROM premium WHERE identifier=?", (f"discord_{discord_id}",))
        row = c.fetchone()
    conn.close()
    
    if row:
        expiry = datetime.fromisoformat(row[0])
        if datetime.now() <= expiry:
            max_limit = 999999  # UNLIMITED
        
    current = get_usage(identifier)
    return identifier, current, max_limit




# ── DISCORD OAUTH2 ────────────────────────────────────────────────────────────
@app.route("/login/discord")
def login_discord():
    # Menggunakan scope 'identify' dan 'guilds' untuk mengecek server
    scope = "identify guilds"
    url = f"https://discord.com/api/oauth2/authorize?client_id={DISCORD_CLIENT_ID}&redirect_uri={urllib.parse.quote(DISCORD_REDIRECT_URI)}&response_type=code&scope={urllib.parse.quote(scope)}"
    return redirect(url)

@app.route("/callback/discord")
def callback_discord():
    code = request.args.get('code')
    if not code:
        return redirect("/?error=discord_auth_failed")
    
    # Tukar code dengan token
    data = {
        'client_id': DISCORD_CLIENT_ID,
        'client_secret': DISCORD_CLIENT_SECRET,
        'grant_type': 'authorization_code',
        'code': code,
        'redirect_uri': DISCORD_REDIRECT_URI
    }
    headers = {'Content-Type': 'application/x-www-form-urlencoded'}
    r = requests.post("https://discord.com/api/oauth2/token", data=data, headers=headers)
    if r.status_code != 200:
        return redirect("/?error=discord_token_failed")
    
    token = r.json().get('access_token')
    
    # Ambil data user
    headers_auth = {"Authorization": f"Bearer {token}"}
    user_req = requests.get("https://discord.com/api/users/@me", headers=headers_auth)
    user_data = user_req.json()
    
    # Ambil daftar server (guilds) user
    guilds_req = requests.get("https://discord.com/api/users/@me/guilds", headers=headers_auth)
    guilds_data = guilds_req.json()
    
    is_in_server = False
    for g in guilds_data:
        if str(g.get("id")) == str(DISCORD_GUILD_ID):
            is_in_server = True
            break
            
    if not is_in_server:
        return redirect("/?error=not_in_server")
        
    # Sukses — simpan ke session
    discord_id   = user_data.get('id')
    username     = user_data.get('username')
    avatar_hash  = user_data.get('avatar')
    avatar_url   = f"https://cdn.discordapp.com/avatars/{discord_id}/{avatar_hash}.png" if avatar_hash else "https://cdn.discordapp.com/embed/avatars/0.png"

    session['discord_user']   = username
    session['discord_id']     = discord_id
    session['discord_avatar'] = avatar_url

    # Daftarkan user ke tabel users (INSERT OR IGNORE supaya tidak duplikat)
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute(
            "INSERT OR IGNORE INTO users (discord_id, discord_username, discord_avatar, joined_at) VALUES (?, ?, ?, ?)",
            (discord_id, username, avatar_url, datetime.now().isoformat())
        )
        conn.commit()
        conn.close()
    except Exception:
        pass

    return redirect("/?success=discord_ok")

@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")


@app.before_request
def track_user_activity():
    if not request.path.startswith('/static'):
        uid = session.get('discord_user') or get_real_ip()
        online_users[uid] = time.time()

        # Cleanup old online users (> 5 minutes inactive)
        current_time = time.time()
        to_delete = [k for k, v in online_users.items() if current_time - v > 300]
        for k in to_delete:
            del online_users[k]

@app.route("/api/comments", methods=["GET", "POST"])
def api_comments():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if request.method == "POST":
        data = request.json
        c.execute("INSERT INTO comments (username, avatar, content, rating, created_at) VALUES (?, ?, ?, ?, ?)", 
                  (data.get("username", "Anonymous"), data.get("avatar", ""), data.get("content", ""), data.get("rating", 5), datetime.now().isoformat()))
        conn.commit()
        conn.close()
        return jsonify({"success": True})
    else:
        c.execute("SELECT id, username, avatar, content, rating, created_at FROM comments ORDER BY id DESC LIMIT 50")
        rows = c.fetchall()
        conn.close()
        return jsonify([{"id": r[0], "username": r[1], "avatar": r[2], "content": r[3], "rating": r[4], "created_at": r[5]} for r in rows])

@app.route("/api/products", methods=["GET"])
def api_products():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, category, title, price, image_url, description FROM products ORDER BY id DESC")
    rows = c.fetchall()
    conn.close()
    return jsonify([{"id": r[0], "category": r[1], "title": r[2], "price": r[3], "image_url": r[4], "description": r[5] or ""} for r in rows])

@app.route("/api/categories", methods=["GET"])
def api_categories():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT name FROM categories ORDER BY name ASC")
    rows = c.fetchall()
    conn.close()
    return jsonify([r[0] for r in rows])

# ── CLEANUP old temp files (>30 min) ─────────────────────────────────────────
def cleanup_old_files():
    while True:
        try:
            now = time.time()
            for f in os.listdir(TEMP_DIR):
                fp = os.path.join(TEMP_DIR, f)
                if os.path.isfile(fp) and now - os.path.getmtime(fp) > 1800:
                    os.remove(fp)
        except Exception: pass
        time.sleep(300)

threading.Thread(target=cleanup_old_files, daemon=True).start()

# ── HELPER: Total Users ───────────────────────────────────────────────────────
def get_total_users():
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM users")
        count = c.fetchone()[0]
        conn.close()
        return count
    except:
        return 0

@app.route("/api/total-users")
def api_total_users():
    """Endpoint untuk polling realtime total pengguna di frontend dan online count"""
    return jsonify({
        "total": get_total_users(),
        "online": len(online_users)
    })

# ── HELPER: Resolve Roblox operation ID ───────────────────────────────────────
def resolve_asset_id(api_key, operation_path, max_wait=90):
    if not operation_path or not str(operation_path).startswith("operations"): return str(operation_path)
    poll_url = f"https://apis.roblox.com/assets/v1/{operation_path}"
    headers = {"x-api-key": api_key}
    deadline = time.time() + max_wait
    while time.time() < deadline:
        try:
            r = requests.get(poll_url, headers=headers, timeout=10)
            if r.status_code == 200:
                data = r.json()
                if data.get("done"):
                    asset_id = (data.get("response") or {}).get("assetId") or (data.get("response") or {}).get("id")
                    if asset_id: return str(asset_id)
        except Exception: pass
        time.sleep(2)
    return str(operation_path)

# ── HELPER: Send Webhook ──────────────────────────────────────────────────────
def send_webhook(discord_id):
    webhook_url = "https://discord.com/api/webhooks/1545146549221462047/DCxQlmU2ebWc2NIryMDUsknCiODFCpsbVGrcvIPL4hP398kQLAQANQ_Z-YXnQ_ieLg4p"
    data = {
        "content": f"<@{discord_id}> BYPAS AUDIO BERHASIL SILAHKAN CEK"
    }
    try:
        requests.post(webhook_url, json=data, timeout=5)
    except:
        pass

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
    return render_template("index.html", 
                           discord_user=session.get('discord_user'),
                           discord_avatar=session.get('discord_avatar'),
                           total_users=get_total_users())

@app.route("/temp/<filename>")
def serve_temp(filename):
    fp = os.path.join(TEMP_DIR, os.path.basename(filename))
    if os.path.exists(fp): return send_file(fp, mimetype="audio/mpeg")
    return jsonify({"error": "File not found"}), 404

@app.route("/api/limits", methods=["POST"])
def get_limits():
    data = request.get_json() or {}
    token = data.get("token", "")
    ip = get_real_ip()
    discord_id = session.get('discord_id')
    identifier, current, max_limit = check_limits(token, ip, discord_id=discord_id)
    
    # Ambil expiry premium untuk ditampilkan ke user
    premium_expiry = None
    if max_limit > 100:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT expiry_date FROM premium WHERE identifier=?", (identifier,))
        row = c.fetchone()
        if not row and discord_id:
            c.execute("SELECT expiry_date FROM premium WHERE identifier=?", (f"discord_{discord_id}",))
            row = c.fetchone()
        conn.close()
        if row:
            premium_expiry = row[0]  # ISO format string

    return jsonify({
        "remaining": max(0, max_limit - current),
        "max": max_limit,
        "used": current,
        "is_google": identifier.startswith("google_"),
        "is_discord": identifier.startswith("discord_"),
        "is_premium": max_limit > 100,
        "premium_expiry": premium_expiry  # e.g. "2026-09-11 17:00:00" atau None
    })

@app.route("/api/check-account", methods=["POST"])
def check_account():
    data = request.get_json() or {}
    creator_id = data.get("creator_id", "").strip()
    creator_type = data.get("creator_type", "User")
    api_key = data.get("api_key", "").strip()
    
    if not creator_id or not api_key:
        return jsonify({"error": "API Key dan Creator ID wajib diisi"}), 400
        
    try:
        if creator_type == "User":
            user_res = requests.get(f"https://users.roblox.com/v1/users/{creator_id}", timeout=10)
            if user_res.status_code == 200:
                name = user_res.json().get("name", "Unknown")
                thumb_res = requests.get(f"https://thumbnails.roblox.com/v1/users/avatar-headshot?userIds={creator_id}&size=150x150&format=Png&isCircular=true", timeout=10)
                thumb = ""
                if thumb_res.status_code == 200:
                    t_data = thumb_res.json().get("data", [])
                    if t_data: thumb = t_data[0].get("imageUrl", "")
                return jsonify({"success": True, "name": name, "thumbnail": thumb})
        elif creator_type == "Group":
            group_res = requests.get(f"https://groups.roblox.com/v1/groups/{creator_id}", timeout=10)
            if group_res.status_code == 200:
                name = group_res.json().get("name", "Unknown Group")
                thumb_res = requests.get(f"https://thumbnails.roblox.com/v1/groups/icons?groupIds={creator_id}&size=150x150&format=Png&isCircular=true", timeout=10)
                thumb = ""
                if thumb_res.status_code == 200:
                    t_data = thumb_res.json().get("data", [])
                    if t_data: thumb = t_data[0].get("imageUrl", "")
                return jsonify({"success": True, "name": name, "thumbnail": thumb})
                
        return jsonify({"error": "Creator ID tidak ditemukan di Roblox!"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/fetch-yt", methods=["POST"])
def fetch_yt():
    data = request.get_json()
    url = data.get("url", "").strip()
    if not url: return jsonify({"error": "No URL"}), 400

    dl_name = f"yt_{uuid.uuid4().hex}"

    # ── Spotify: ambil judul via oEmbed lalu search di YouTube ──────────────
    spotify_thumb = ""
    if "spotify.com/track" in url or "open.spotify.com" in url:
        try:
            oembed_resp = requests.get(
                f"https://open.spotify.com/oembed?url={url}", timeout=10,
                headers={"User-Agent": "Mozilla/5.0"}
            )
            if oembed_resp.status_code != 200:
                return jsonify({"error": "Link Spotify tidak valid atau privat"}), 400
            sp_info = oembed_resp.json()
            search_query = sp_info.get("title", "")
            spotify_thumb = sp_info.get("thumbnail_url", "")
            if not search_query:
                return jsonify({"error": "Tidak bisa baca judul lagu dari Spotify"}), 400
            # Ubah jadi pencarian YouTube
            url = f"ytsearch1:{search_query}"
        except Exception as e:
            return jsonify({"error": f"Gagal fetch Spotify: {str(e)}"}), 500

    # ── Pilih extractor args sesuai platform ────────────────────────────────
    is_tiktok = "tiktok.com" in url
    
    ydl_opts = {
        "format": "ba/b/best/bestvideo+bestaudio",
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3"}],
        "outtmpl": os.path.join(TEMP_DIR, dl_name),
        "ffmpeg_location": FFMPEG,
        "quiet": True,
        "extractor_args": {} if is_tiktok else {"youtube": {"player_client": ["ios", "tv_embedded", "android", "web"]}},
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Referer": "https://www.tiktok.com/" if is_tiktok else "https://www.youtube.com/",
            "Accept-Language": "en-US,en;q=0.9",
        },
        "nocheckcertificate": True,
        "ignoreerrors": False,
    }
    
    # Deteksi cookies.txt untuk bypass YouTube bot (Simpan file cookies.txt di folder web_version)
    cookie_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt")
    if os.path.exists(cookie_path):
        ydl_opts["cookiefile"] = cookie_path
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if not info: raise Exception("Gagal ambil info audio")
            # Untuk ytsearch, info punya 'entries'
            if "entries" in info:
                info = info["entries"][0]
            title = info.get("title", "Unknown")
            thumb = spotify_thumb or info.get("thumbnail") or (info.get("thumbnails") or [{}])[-1].get("url", "")
            actual = os.path.join(TEMP_DIR, dl_name + ".mp3")
            if not os.path.exists(actual):
                actual = os.path.join(TEMP_DIR, ydl.prepare_filename(info).rsplit(".", 1)[0] + ".mp3")
        return jsonify({"title": title, "thumbnail": thumb, "file": os.path.basename(actual), "source": url})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

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
    else: return jsonify({"error": "No file"}), 400

    try:
        _, out_name = process_preview(input_path, speed, pitch, vol)
        return jsonify({"preview_url": f"/temp/{out_name}"})
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route("/api/generate-spritesheet", methods=["POST"])
def generate_spritesheet():
    url = request.form.get("url", "").strip()
    start_time = float(request.form.get("start", 0))
    end_time = float(request.form.get("end", 9))
    duration = min(end_time - start_time, 9.0)
    if duration <= 0: duration = 9.0

    output_filename = f"sprite_{uuid.uuid4().hex}.png"
    output_path = os.path.join(TEMP_DIR, output_filename)

    try:
        if url:
            # Menggunakan yt-dlp untuk mendapatkan URL video langsung
            ydl_opts = {'format': 'worst[ext=mp4]/worst', 'quiet': True}
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                # Ambil URL asli video (jika ada list format, ambil yang pertama/terburuk yg ada URL-nya)
                video_url = info.get("url") or info.get("entries", [{}])[0].get("url")
                if not video_url: return jsonify({"error": "Gagal ekstrak URL video."}), 400
                
                # Gunakan FFmpeg untuk trim & tile
                cmd = [
                    FFMPEG, "-y", 
                    "-ss", str(start_time), "-t", str(duration),
                    "-i", video_url,
                    "-vf", "fps=6.2,scale=128:128,tile=7x8", 
                    "-vframes", "1", 
                    output_path
                ]
                subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif "file" in request.files:
            f = request.files["file"]
            tmp_gif = os.path.join(TEMP_DIR, f"tmp_{uuid.uuid4().hex}.gif")
            f.save(tmp_gif)
            cmd = [
                FFMPEG, "-y", "-i", tmp_gif,
                "-vf", "fps=6.2,scale=128:128,tile=7x8", 
                "-vframes", "1", 
                output_path
            ]
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            os.remove(tmp_gif)
        else:
            return jsonify({"error": "URL atau File wajib diisi!"}), 400

        if not os.path.exists(output_path):
            return jsonify({"error": "Gagal generate spritesheet"}), 500

        # Kembalikan URL menggunakan route /temp/ yang sudah ada
        return jsonify({"success": True, "file": output_filename, "url": f"/temp/{output_filename}"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/upload", methods=["POST"])
def upload():
    # Pastikan Discord login dulu sebelum upload, just in case
    discord_id = session.get('discord_id')
    if not discord_id:
        return jsonify({"error": "Harap login Discord terlebih dahulu!"}), 403

    token = request.form.get("google_token", "")
    ip = get_real_ip()
    identifier, current, max_limit = check_limits(token, ip, discord_id=discord_id)
    
    if current >= max_limit:
        return jsonify({
            "limit_exceeded": True, 
            "error": f"Batas harian tercapai ({current}/{max_limit}). Besok bisa lagi atau upgrade Premium!",
            "remaining": 0,
            "max": max_limit
        }), 403

    api_key    = request.form.get("api_key", "").strip()
    creator_id = request.form.get("creator_id", "").strip()
    creator_type = request.form.get("creator_type", "User")
    title      = request.form.get("title", "Audio")[:40]
    speed      = float(request.form.get("speed", 1.0))
    pitch      = float(request.form.get("pitch", 1.0))
    vol        = float(request.form.get("vol", 1.0))
    bypass     = request.form.get("bypass", "false") == "true"
    yt_file    = request.form.get("yt_file", "").strip()
    asset_type = request.form.get("asset_type", "Audio")  # BISA Decal ATAU Audio

    if not api_key or not creator_id: return jsonify({"error": "API Key & Creator ID wajib diisi!"}), 400

    if yt_file:
        input_path = os.path.join(TEMP_DIR, os.path.basename(yt_file))
        if not os.path.exists(input_path): return jsonify({"error": "File YouTube hilang"}), 404
    elif "file" in request.files:
        f = request.files["file"]
        # Bisa mp3/ogg untuk audio, png/jpg untuk decal
        ext = os.path.splitext(f.filename)[1]
        tmp_name = f"up_{uuid.uuid4().hex}{ext}"
        input_path = os.path.join(TEMP_DIR, tmp_name)
        f.save(input_path)
    else: return jsonify({"error": "Tidak ada file"}), 400

    temp_out = None
    try:
        if asset_type == "Decal":
            temp_out = input_path
            mime_type = "image/png" if input_path.endswith('.png') else "image/jpeg"
        else:
            s = speed if bypass else 1.0
            p = pitch if bypass else 1.0
            v = vol   if bypass else 1.0
            temp_out, _ = process_audio(input_path, s, p, v, with_intro=True)
            mime_type = "audio/mpeg"

        creator_key = "userId" if creator_type == "User" else "groupId"
        req_body = {
            "assetType": asset_type, "displayName": title, "description": "MCHLERN UPLOADER",
            "creationContext": {"creator": {creator_key: str(creator_id)}}
        }

        with open(temp_out, "rb") as fh:
            resp = requests.post(
                "https://apis.roblox.com/assets/v1/assets",
                headers={"x-api-key": api_key},
                files={"fileContent": (os.path.basename(temp_out), fh, mime_type)},
                data={"request": json.dumps(req_body)},
                timeout=60
            )

        if resp.status_code == 200:
            raw = resp.json()
            raw_id = raw.get("assetId") or raw.get("path") or raw.get("id") or "Unknown"
            asset_id = resolve_asset_id(api_key, str(raw_id))
            increment_usage(identifier)
            threading.Thread(target=send_webhook, args=(discord_id,), daemon=True).start()
            return jsonify({"success": True, "asset_id": asset_id, "title": title, "blocked": False})
        else:
            blocked = resp.status_code in (403, 401)
            err = resp.json().get("errors", [{}])[0].get("message", resp.text)
            return jsonify({"success": False, "error": f"Roblox API: {err}", "blocked": blocked})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        # Jika file hasil proses audio (temp_out), hapus. Tapi kalau Decal (input_path langsung), hapus juga?
        # Supaya aman, kita hapus temp_out jika dia bukan original file atau jika dia file upload sementara
        if temp_out and os.path.exists(temp_out):
            try: os.remove(temp_out)
            except: pass
        if yt_file and os.path.exists(input_path) and input_path != temp_out:
             try: os.remove(input_path)
             except: pass

STORE_DIR = os.path.join(DATA_DIR, "store_images")
os.makedirs(STORE_DIR, exist_ok=True)

@app.route("/store_img/<filename>")
def serve_store_img(filename):
    return send_file(os.path.join(STORE_DIR, filename))

@app.route("/admin-panel-rahasia", methods=["GET", "POST"])
def admin_panel():
    if request.method == "POST":
        if request.form.get("password") == "rikigantengZ55":
            session['admin_logged_in'] = True
            return redirect("/admin-panel-rahasia")
        elif not session.get('admin_logged_in'):
            pass
        elif request.form.get("action") == "add":
            email = request.form.get("email").strip()
            identifier = f"google_{email}"
            duration = request.form.get("duration")
            days = 7 if duration == "week" else 30
            expiry = (datetime.now() + timedelta(days=days)).isoformat()
            
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("INSERT OR REPLACE INTO premium (identifier, expiry_date) VALUES (?, ?)", (identifier, expiry))
            conn.commit()
            conn.close()
            return redirect("/admin-panel-rahasia")
        elif request.form.get("action") == "delete":
            identifier = request.form.get("identifier")
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("DELETE FROM premium WHERE identifier=?", (identifier,))
            conn.commit()
            conn.close()
            return redirect("/admin-panel-rahasia")
        elif request.form.get("action") == "add_product":
            title = request.form.get("title").strip()
            category = request.form.get("category").strip()
            price = request.form.get("price").strip()
            desc = request.form.get("description", "").strip()
            image_url = ""
            if "image" in request.files:
                f = request.files["image"]
                if f.filename:
                    ext = os.path.splitext(f.filename)[1]
                    tmp_name = f"prod_{uuid.uuid4().hex}{ext}"
                    f.save(os.path.join(STORE_DIR, tmp_name))
                    image_url = f"/store_img/{tmp_name}"
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("INSERT INTO products (category, title, price, image_url, description) VALUES (?, ?, ?, ?, ?)", (category, title, price, image_url, desc))
            conn.commit()
            conn.close()
            return redirect("/admin-panel-rahasia")
        elif request.form.get("action") == "delete_product":
            pid = request.form.get("product_id")
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("DELETE FROM products WHERE id=?", (pid,))
            conn.commit()
            conn.close()
            return redirect("/admin-panel-rahasia")
        elif request.form.get("action") == "add_category":
            cat_name = request.form.get("category_name").strip().upper()
            if cat_name:
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                try:
                    c.execute("INSERT INTO categories (name) VALUES (?)", (cat_name,))
                    conn.commit()
                except sqlite3.IntegrityError:
                    pass
                conn.close()
            return redirect("/admin-panel-rahasia")
        elif request.form.get("action") == "delete_category":
            cat_id = request.form.get("category_id")
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("DELETE FROM categories WHERE id=?", (cat_id,))
            conn.commit()
            conn.close()
            return redirect("/admin-panel-rahasia")

    if not session.get('admin_logged_in'):
        return """
        <body style="background:#05050a; color:#fff; display:flex; justify-content:center; align-items:center; height:100vh; font-family:sans-serif;">
        <form method="POST" style="background:rgba(255,255,255,0.05); padding:30px; border-radius:12px; border:1px solid #333; text-align:center;">
            <h2 style="color:#00e5ff;">Admin Area</h2>
            <input type="password" name="password" placeholder="Password" style="width:100%; padding:10px; margin:15px 0; border-radius:5px; border:1px solid #555; background:#000; color:#fff;" autofocus>
            <button type="submit" style="background:#00e5ff; color:#000; font-weight:bold; padding:10px 20px; border:none; border-radius:5px; cursor:pointer; width:100%;">Login</button>
        </form>
        </body>
        """
        
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT identifier, expiry_date FROM premium")
    premiums = c.fetchall()
    c.execute("SELECT id, category, title, price, image_url, description FROM products ORDER BY id DESC")
    products = c.fetchall()
    c.execute("SELECT id, name FROM categories ORDER BY name ASC")
    categories = c.fetchall()
    conn.close()
    
    html = f"""
    <body style="background:#05050a; color:#fff; font-family:sans-serif; padding:40px;">
        <h1 style="color:#00e5ff;">MCHLERN Admin Panel</h1>
        
        <!-- ONLINE USERS -->
        <div style="background:rgba(255,255,255,0.05); padding:20px; border-radius:12px; border:1px solid #333; margin-bottom:20px;">
            <h3 style="margin-top:0; color:#00e676;">🟢 Users Online Real-time (Active in last 5 mins)</h3>
            <div style="display:flex; flex-wrap:wrap; gap:10px;">
    """
    if not online_users:
        html += "<p style='color:#888;'>Tidak ada user online.</p>"
    for uid, last_active in online_users.items():
        time_ago = int(time.time() - last_active)
        html += f"<span style='background:#111; padding:5px 10px; border-radius:20px; border:1px solid #333;'>{uid} <small style='color:#888'>({time_ago}s ago)</small></span>"
        
    html += """
            </div>
        </div>
        
        <div style="display:flex; gap:20px; flex-wrap:wrap;">
            <!-- PREMIUM SECTION -->
            <div style="flex:1; min-width:400px; background:rgba(255,255,255,0.05); padding:20px; border-radius:12px; border:1px solid #333;">
                <h3 style="margin-top:0;">Tambah Premium User</h3>
                <form method="POST" style="display:flex; gap:10px; align-items:center; margin-bottom:20px;">
                    <input type="hidden" name="action" value="add">
                    <input type="email" name="email" placeholder="Email Google User" required style="padding:10px; border-radius:5px; border:1px solid #555; background:#000; color:#fff; width:200px;">
                    <select name="duration" style="padding:10px; border-radius:5px; border:1px solid #555; background:#000; color:#fff;">
                        <option value="week">1 Minggu</option>
                        <option value="month">1 Bulan</option>
                    </select>
                    <button type="submit" style="background:#00e676; color:#000; font-weight:bold; padding:10px 20px; border:none; border-radius:5px; cursor:pointer;">Tambah</button>
                </form>
                
                <h3 style="margin-top:0;">Daftar Premium Aktif</h3>
                <table style="width:100%; border-collapse:collapse; text-align:left;">
                    <tr style="border-bottom:1px solid #333;"><th style="padding:10px;">User Identifier</th><th style="padding:10px;">Berakhir Pada</th><th style="padding:10px;">Aksi</th></tr>
    """
    for p in premiums:
        html += f"""
                    <tr style="border-bottom:1px solid #222;">
                        <td style="padding:10px;">{p[0]}</td>
                        <td style="padding:10px;">{p[1][:10]}</td>
                        <td style="padding:10px;">
                            <form method="POST" style="margin:0;">
                                <input type="hidden" name="action" value="delete">
                                <input type="hidden" name="identifier" value="{p[0]}">
                                <button style="background:#ff3366; color:#fff; border:none; padding:5px 10px; border-radius:3px; cursor:pointer;">Hapus</button>
                            </form>
                        </td>
                    </tr>
        """
    html += """
                </table>
            </div>
            
            <!-- STORE PRODUCTS SECTION -->
            <div style="flex:1; min-width:400px; background:rgba(255,255,255,0.05); padding:20px; border-radius:12px; border:1px solid #333;">
                <h3 style="margin-top:0; color:#f59e0b;">Tambah Produk Toko</h3>
                <form method="POST" enctype="multipart/form-data" style="display:flex; flex-direction:column; gap:10px; margin-bottom:20px;">
                    <input type="hidden" name="action" value="add_product">
                    <input type="text" name="title" placeholder="Nama Produk" required style="padding:10px; border-radius:5px; border:1px solid #555; background:#000; color:#fff;">
                    <select name="category" style="padding:10px; border-radius:5px; border:1px solid #555; background:#000; color:#fff;">
                        <option value="ALBUM FUNKOT">ALBUM FUNKOT</option>
                        <option value="ALBUM DJ">ALBUM DJ</option>
                        <option value="ALBUM GALAU">ALBUM GALAU</option>
                        <option value="ALBUM BKB">ALBUM BKB</option>
                        <option value="ALBUM JAWA">ALBUM JAWA</option>
                        <option value="ALBUM SUNDA">ALBUM SUNDA</option>
                    </select>
                    <input type="text" name="price" placeholder="Harga (contoh: Rp 50.000)" required style="padding:10px; border-radius:5px; border:1px solid #555; background:#000; color:#fff;">
                    <input type="file" name="image" accept="image/*" required style="padding:10px; border-radius:5px; border:1px solid #555; background:#000; color:#fff;">
                    <button type="submit" style="background:#f59e0b; color:#000; font-weight:bold; padding:10px 20px; border:none; border-radius:5px; cursor:pointer;">Tambah Produk</button>
                </form>
                
                <h3 style="margin-top:0;">Daftar Produk Toko</h3>
                <div style="max-height:400px; overflow-y:auto;">
                    <table style="width:100%; border-collapse:collapse; text-align:left;">
                        <tr style="border-bottom:1px solid #333;"><th style="padding:10px;">IMG</th><th style="padding:10px;">Kategori</th><th style="padding:10px;">Nama</th><th style="padding:10px;">Harga</th><th style="padding:10px;">Aksi</th></tr>
    """
    for pr in products:
        html += f"""
                        <tr style="border-bottom:1px solid #222;">
                            <td style="padding:10px;"><img src="{pr[4]}" width="40" height="40" style="border-radius:5px; object-fit:cover;"></td>
                            <td style="padding:10px; font-size:12px; color:#888;">{pr[1]}</td>
                            <td style="padding:10px; font-weight:bold;">{pr[2]}</td>
                            <td style="padding:10px; color:#00e676;">{pr[3]}</td>
                            <td style="padding:10px;">
                                <form method="POST" style="margin:0;">
                                    <input type="hidden" name="action" value="delete_product">
                                    <input type="hidden" name="product_id" value="{pr[0]}">
                                    <button style="background:#ff3366; color:#fff; border:none; padding:5px 10px; border-radius:3px; cursor:pointer;">Hapus</button>
                                </form>
                            </td>
                        </tr>
        """
    html += """
                    </table>
                </div>
            </div>
        </div>
    </body>
    """
    return render_template_string(html)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
