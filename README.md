# MCHLERN TOOLS — Deploy Guide

## Cara Deploy ke Railway (GRATIS, 5 menit)

### Langkah 1: Siapkan GitHub

1. Daftar/login ke [github.com](https://github.com)
2. Buat **New Repository** → Nama: `mchlern-tools` → **Public** → Create
3. Upload semua file dari folder `web_version/` ini ke repo tersebut (drag & drop di GitHub)

File yang harus diupload:
```
server.py
requirements.txt
Procfile
intro.mp3
templates/
  └── index.html
temp_audio/    ← biarkan kosong (Railway akan buat otomatis)
```

### Langkah 2: Deploy ke Railway

1. Buka [railway.app](https://railway.app) → Login dengan GitHub
2. Klik **"New Project"**
3. Pilih **"Deploy from GitHub repo"**
4. Pilih repo `mchlern-tools`
5. Railway otomatis detect Python → klik **Deploy**
6. Tunggu ~2 menit → dapat link seperti:  
   `https://mchlern-tools-production.up.railway.app`

### Langkah 3: Set Environment Variable (Opsional)

Di Railway → Settings → Variables → Add:
```
SECRET_KEY = mchlern_rahasia_12345
```

---

## Cara Pakai (untuk orang yang dapat link)

1. Buka link dari Railway di browser (HP/PC/Mac semua bisa)
2. Isi **API Key** + **Creator ID** → klik **💾 Simpan** (tersimpan di browser)
3. Drag & drop file lagu atau paste link YouTube → klik **⬆ Upload**
4. ID hasil upload otomatis muncul di History dan ter-copy ke clipboard!

---

## Free Tier Railway

| | Detail |
|--|--|
| RAM | 512 MB |
| CPU | Shared |
| Jam/bulan | 500 jam (cukup ~16 jam/hari) |
| Harga | **GRATIS** |
| Custom domain | Bisa ditambahkan |
