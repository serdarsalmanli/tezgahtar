import html
import hmac
import json
import os
import secrets
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, redirect, request, session, url_for
from google import genai

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
GEMINI_KEY = os.getenv("GEMINI_SIFREM")
ADMIN_PASSWORD = os.getenv("ADMIN_SIFRE")
DB_PATH = Path(os.getenv("DB_YOLU", str(BASE_DIR / "benim_depom.db"))).resolve()
client = genai.Client(api_key=GEMINI_KEY) if GEMINI_KEY else None

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY") or ADMIN_PASSWORD or os.urandom(32)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.getenv("COOKIE_SECURE") == "1"
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024

AI_PROMPT = """
Sen bir depo tezgâhtarısın. Müşteri mesajından ürün adını ve miktarı (kg) bul.
Ürün adının ilk harfi büyük olsun. Miktar belirtilmemişse 0 yaz.
Yalnızca şu JSON biçiminde cevap ver: {"urun": "ÜrünAdı", "miktar": 1}
"""


@contextmanager
def db_baglan():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def escape(value):
    return html.escape(str(value), quote=True)


def yapay_zekaya_sor(mesaj):
    if client is None:
        return None, None
    try:
        response = client.models.generate_content(
            model="gemini-3.8-flash",
            contents=f"{AI_PROMPT}\n\nMüşteri mesajı: {mesaj}",
        )
        data = json.loads(response.text or "")
        urun, kilo = data.get("urun"), data.get("miktar")
        if not isinstance(urun, str) or not urun.strip():
            return None, None
        if isinstance(kilo, bool) or not isinstance(kilo, (int, float)):
            return None, None
        if kilo < 0 or kilo > 10000:
            return None, None
        return urun.strip()[:100], kilo
    except Exception:
        app.logger.exception("Yapay zekâ isteği başarısız oldu")
        return None, None


@app.route("/", methods=["GET", "POST"])
def vitrin():
    if request.method == "GET":
        return vitrin_html_uret()

    mesaj = (request.form.get("musteri_mesaji") or "").strip()
    eksik_urun = (request.form.get("eksik_urun") or "").strip()
    ek_mesaj = (request.form.get("tamamlayici_mesaj") or "").strip()
    if eksik_urun and ek_mesaj:
        urun, kilo = yapay_zekaya_sor(
            f"Müşteri '{eksik_urun}' istiyor. Miktarı şu: {ek_mesaj}"
        )
    elif mesaj:
        urun, kilo = yapay_zekaya_sor(mesaj)
    else:
        return vitrin_html_uret("Lütfen ne istediğini yaz.")

    if urun is None or kilo is None:
        return vitrin_html_uret("İstek işlenemedi. Ayarları kontrol edip tekrar dene.")
    if kilo == 0:
        return eksik_kilo_formu_uret(urun)
    if kilo <= 0:
        return vitrin_html_uret("Miktar 0'dan büyük olmalı.")

    sonuc_mesaji = "Ürün bulunamadı."
    try:
        with db_baglan() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT miktar FROM stoklar WHERE urun_adi = ?", (urun,)
            ).fetchone()
            if row is not None:
                stok = row["miktar"]
                if stok >= kilo:
                    conn.execute(
                        "UPDATE stoklar SET miktar = miktar - ? WHERE urun_adi = ?",
                        (kilo, urun),
                    )
                    durum = "ONAYLANDI"
                    sonuc_mesaji = f"Tamamdır! {kilo:g} kilo {urun} hazır."
                else:
                    durum = "REDDEDİLDİ"
                    sonuc_mesaji = f"Yeterli {urun} yok. Mevcut stok: {stok} kg."
                conn.execute(
                    "INSERT INTO siparis_kayitlari "
                    "(urun_adi, talep_edilen, durum) VALUES (?, ?, ?)",
                    (urun, kilo, durum),
                )
    except sqlite3.Error:
        app.logger.exception("Veritabanı işlemi başarısız oldu")
        sonuc_mesaji = "Sipariş kaydedilemedi. Tekrar deneyin."
    return vitrin_html_uret(sonuc_mesaji)


@app.route("/admin", methods=["GET", "POST"])
def admin_paneli():
    if request.method == "POST":
        islem = request.form.get("islem")
        if islem == "giris":
            girilen = request.form.get("sifre") or ""
            if not ADMIN_PASSWORD or not hmac.compare_digest(girilen, ADMIN_PASSWORD):
                return admin_giris_html("Şifre yanlış veya admin şifresi ayarlanmamış.")
            session.clear()
            session["admin_ok"] = True
            session["csrf"] = secrets.token_urlsafe(32)
            return redirect(url_for("admin_paneli"))

        if not session.get("admin_ok"):
            return admin_giris_html("Önce admin girişi yapmalısın."), 401
        if not hmac.compare_digest(request.form.get("csrf", ""), session.get("csrf", "")):
            return "İstek doğrulanamadı. Sayfayı yenileyip tekrar dene.", 400
        if islem == "cikis":
            session.clear()
            return redirect(url_for("admin_paneli"))
        if islem == "stok_ekle":
            yeniden_sifre = request.form.get("stok_sifresi") or ""
            if not ADMIN_PASSWORD or not hmac.compare_digest(yeniden_sifre, ADMIN_PASSWORD):
                return admin_sayfasi(mesaj="Stok güncellemek için admin şifresini doğrula.")
            urun = (request.form.get("urun_adi") or "").strip()
            try:
                miktar = float((request.form.get("miktar") or "").replace(",", "."))
            except ValueError:
                miktar = 0
            if not urun or len(urun) > 100 or not 0 < miktar <= 1000000:
                return admin_sayfasi(mesaj="Ürün adı ve 0'dan büyük geçerli miktar gir.")
            try:
                with db_baglan() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    mevcut = conn.execute(
                        "SELECT miktar FROM stoklar WHERE urun_adi = ?", (urun,)
                    ).fetchone()
                    if mevcut:
                        conn.execute(
                            "UPDATE stoklar SET miktar = miktar + ? WHERE urun_adi = ?",
                            (miktar, urun),
                        )
                    else:
                        conn.execute(
                            "INSERT INTO stoklar (urun_adi, miktar) VALUES (?, ?)",
                            (urun, miktar),
                        )
            except sqlite3.Error:
                app.logger.exception("Stok ekleme işlemi başarısız oldu")
                return admin_sayfasi(mesaj="Stok eklenemedi. Veritabanını kontrol et.")
            return admin_sayfasi(mesaj=f"{urun} stoğuna {miktar:g} kg eklendi.")
        return admin_sayfasi(mesaj="Bilinmeyen işlem.")

    if not session.get("admin_ok"):
        return admin_giris_html()
    return admin_sayfasi()


def admin_sayfasi(mesaj=""):
    try:
        with db_baglan() as conn:
            stoklar = conn.execute(
                "SELECT urun_adi, miktar FROM stoklar ORDER BY urun_adi"
            ).fetchall()
            siparisler = conn.execute(
                "SELECT islem_id AS id, urun_adi, talep_edilen, durum "
                "FROM siparis_kayitlari ORDER BY islem_id DESC"
            ).fetchall()
    except sqlite3.Error:
        app.logger.exception("Admin verileri okunamadı")
        return "Veritabanı okunamadı.", 500
    stok_html = "".join(
        f"<li><b>{escape(r['urun_adi'])}</b>: {escape(r['miktar'])} kg</li>"
        for r in stoklar
    ) or "<li>Depo boş.</li>"
    siparis_html = "".join(
        f"<li>#{escape(r['id'])} | {escape(r['urun_adi'])} "
        f"({escape(r['talep_edilen'])} kg) - <b>{escape(r['durum'])}</b></li>"
        for r in siparisler
    ) or "<li>Henüz sipariş yok.</li>"
    csrf = escape(session.get("csrf", ""))
    return f"""<!doctype html><html lang="tr"><meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Depo yönetimi</title><main style="font-family:Arial;max-width:900px;margin:40px auto;padding:20px">
    <h1>Depo yönetimi</h1><p role="status">{escape(mesaj)}</p>
    <h2>Stok artır</h2><form method="post">
    <input type="hidden" name="csrf" value="{csrf}"><input type="hidden" name="islem" value="stok_ekle">
    <label>Admin şifresi <input type="password" name="stok_sifresi" autocomplete="current-password" required></label>
    <label>Ürün adı <input name="urun_adi" maxlength="100" required></label>
    <label>Eklenecek miktar (kg) <input name="miktar" type="number" min="0.01" step="0.01" required></label>
    <button type="submit">Stoğa ekle</button></form>
    <h2>Güncel stoklar</h2><ul>{stok_html}</ul>
    <h2>Siparişler ({len(siparisler)})</h2><ol>{siparis_html}</ol>
    <form method="post"><input type="hidden" name="csrf" value="{csrf}">
    <input type="hidden" name="islem" value="cikis"><button>Admin çıkışı</button></form>
    <a href="/">Vitrine dön</a></main></html>"""


def admin_giris_html(hata=""):
    return f"""<!doctype html><html lang="tr"><meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Admin girişi</title><main style="font-family:Arial;text-align:center;margin:80px auto">
    <h1>Patron girişi</h1><p style="color:#b00">{escape(hata)}</p>
    <form method="post"><input type="hidden" name="islem" value="giris">
    <input type="password" name="sifre" required
    autocomplete="current-password" placeholder="Admin şifresi"><button>Giriş</button>
    </form><a href="/">Vitrine dön</a></main></html>"""


def vitrin_html_uret(mesaj_ekle=""):
    return f"""<!doctype html><html lang="tr"><head><meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Serdar'ın Deposu</title><style>
    body {{background:#f0f2f5;font-family:Arial;display:flex;justify-content:center;
    align-items:center;min-height:100vh;margin:0;padding:16px;box-sizing:border-box}}
    .box {{background:white;padding:32px;border-radius:20px;text-align:center;
    width:100%;max-width:450px;box-shadow:0 10px 30px #0002}}
    input {{width:90%;padding:15px;font-size:16px;border:2px solid #e0e0e0;border-radius:10px}}
    button {{background:#007bff;color:white;border:0;padding:15px 30px;font-size:16px;
    border-radius:10px;cursor:pointer;width:90%;margin-top:20px}}
    </style></head><body><main class="box"><h1>🍏 Serdar'ın Deposu</h1>
    <h3 style="color:#d83b01">{escape(mesaj_ekle)}</h3>
    <p>Bugün sana nasıl yardımcı olayım, ne saralım sana?</p>
    <form method="post"><input name="musteri_mesaji" maxlength="500" required
    placeholder="Örn: 16 kilo elma sar"><button>Yapay zekâya gönder</button></form>
    <a href="/admin">Patron girişi</a></main></body></html>"""


def eksik_kilo_formu_uret(urun_adi):
    urun = escape(urun_adi)
    return f"""<!doctype html><html lang="tr"><meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Miktar seç</title><main style="font-family:Arial;text-align:center;margin:50px auto">
    <h2>{urun} için kaç kilo hazırlayayım?</h2><form method="post">
    <input type="hidden" name="eksik_urun" value="{urun}">
    <input name="tamamlayici_mesaj" maxlength="200" required placeholder="Örn: 5 kilo olsun">
    <button>Miktarı söyle</button></form></main></html>"""


if __name__ == "__main__":
    # Yerel kullanım içindir. Yayında Gunicorn veya Waitress gibi WSGI sunucusu kullan.
    app.run(debug=False)
