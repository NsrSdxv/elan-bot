import json
import os
import re
import sys
import time
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup

TOKEN = os.environ["TG_BOT_TOKEN"]
CHAT_ID = str(os.environ["TG_CHAT_ID"])
API = f"https://api.telegram.org/bot{TOKEN}"
STATE_FILE = "state.json"
MAX_SEEN = 1500
MAX_SEND = 15
MAX_FILTERS = 10

ID_RE = re.compile(r"^/[a-z0-9\-]+-(\d{4,})/?$")
PRICE_RE = re.compile(r"(\d[\d\s\u00a0\u202f]*)\s*₼(?:\s*/\s*(ay|gün))?")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "az,tr;q=0.8,en;q=0.7",
}

HELP = (
    "Komutlar:\n"
    "/ekle <binalar.az linki> [ad] - filtre ekle (veya linki direkt yapıştır)\n"
    "/liste - aktif filtreler\n"
    "/sil <no> - filtre sil\n\n"
    "Önce binalar.az'da filtreleri seç, Axtar'a bas, sonra adres çubuğundaki linki buraya gönder."
)


def tg(text):
    for _ in range(3):
        try:
            r = requests.post(f"{API}/sendMessage", data={
                "chat_id": CHAT_ID, "text": text, "disable_web_page_preview": "false"}, timeout=20)
            if r.status_code == 429:
                time.sleep(r.json().get("parameters", {}).get("retry_after", 5))
                continue
            r.raise_for_status()
            return True
        except Exception as e:
            print("Telegram hatası:", e)
            time.sleep(2)
    return False


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            s = json.load(f)
    except Exception:
        s = {}
    s.setdefault("offset", 0)
    s.setdefault("filters", [])
    return s


def save_state(s):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=1)


def listing_id(href):
    u = urlsplit(href)
    if u.netloc not in ("", "binalar.az", "www.binalar.az"):
        return None
    m = ID_RE.match(u.path)
    return m.group(1) if m else None


def parse(html):
    soup = BeautifulSoup(html, "html.parser")
    first = {}
    for a in soup.find_all("a", href=True):
        lid = listing_id(a["href"])
        if lid and lid not in first:
            first[lid] = a

    items = []
    for lid, a in first.items():
        card = a
        while card.parent is not None and card.parent.name not in ("body", "html", "[document]"):
            ids = {listing_id(x["href"]) for x in card.parent.find_all("a", href=True)} - {None}
            if len(ids) > 1:
                break
            card = card.parent

        text = card.get_text(" ", strip=True)
        path = urlsplit(a["href"]).path.strip("/")
        slug_title = re.sub(r"-\d+$", "", path).replace("--", " ").replace("-", " ")
        strong = card.find(["strong", "b"])
        title = strong.get_text(" ", strip=True) if strong else slug_title

        price = ""
        pm = PRICE_RE.search(text)
        if pm:
            digits = re.sub(r"\D", "", pm.group(1))
            if digits:
                price = f"{int(digits):,}".replace(",", " ") + " ₼" + (f" / {pm.group(2)}" if pm.group(2) else "")

        info = []
        for rx, suffix in ((r"(\d+)\s*otaq", " otaq"), (r"([\d.,]+)\s*m²", " m²"),
                           (r"(\d+/\d+)\s*mərtəbə", " mərtəbə"), (r"([\d.,]+)\s*sot", " sot")):
            m = re.search(rx, text)
            if m:
                info.append(m.group(1) + suffix)

        ext = next((x["href"] for x in card.find_all("a", href=True)
                    if x["href"].startswith("http") and "binalar.az" not in x["href"]), None)

        items.append({
            "id": lid,
            "title": title,
            "price": price,
            "info": " · ".join(info),
            "link": f"https://binalar.az/{path}",
            "ext": ext,
        })
    return items


def fmt(i, n):
    lines = [f"🏠 {i['title']}"]
    if i["price"]:
        lines.append(f"💰 {i['price']}")
    if i["info"]:
        lines.append(f"📐 {i['info']}")
    lines.append(f"🔗 {i['link']}")
    if i["ext"]:
        lines.append(f"🌐 {i['ext']}")
    lines.append(f"[Filtre #{n}]")
    return "\n".join(lines)


def process_filter(n, f):
    r = requests.get(f["url"], headers=HEADERS, timeout=30)
    r.raise_for_status()
    items = parse(r.text)
    if not items:
        print(f"Filtre #{n}: ilan bulunamadı (sayfa yapısı değişmiş veya engel olabilir).")
        return

    seen = f.setdefault("seen", [])
    seen_set = set(seen)
    new = [i for i in items if i["id"] not in seen_set]

    if not f.get("seeded"):
        seen.extend(i["id"] for i in items)
        f["seeded"] = True
        tg(f"✅ Filtre #{n} aktif: {len(items)} mevcut ilan kaydedildi. Bundan sonra yeni ilanlar buraya gelecek.")
        print(f"Filtre #{n}: ilk tarama, {len(items)} ilan kaydedildi.")
    else:
        to_send = new[:MAX_SEND]
        for i in reversed(to_send):
            if tg(fmt(i, n)):
                seen.append(i["id"])
                time.sleep(1)
        for i in new[MAX_SEND:]:
            seen.append(i["id"])
        if len(new) > MAX_SEND:
            tg(f"ℹ️ Filtre #{n}: {len(new) - MAX_SEND} ilan daha çıktı. Filtreyi daraltmanı öneririm.")
        print(f"Filtre #{n}: {len(new)} yeni ilan.")

    f["seen"] = seen[-MAX_SEEN:]


def valid_url(u):
    p = urlsplit(u)
    return p.scheme == "https" and p.netloc in ("binalar.az", "www.binalar.az")


def handle_updates(state):
    try:
        r = requests.get(f"{API}/getUpdates", params={"offset": state["offset"], "timeout": 0}, timeout=30).json()
    except Exception as e:
        print("getUpdates hatası:", e)
        return
    for u in r.get("result", []):
        state["offset"] = u["update_id"] + 1
        m = u.get("message") or {}
        if str(m.get("chat", {}).get("id")) != CHAT_ID:
            continue
        parts = (m.get("text") or "").strip().split()
        if not parts:
            continue
        cmd = parts[0].split("@")[0].lower()
        args = parts[1:]

        if cmd.startswith("http"):
            cmd, args = "/ekle", parts
        if cmd in ("/start", "/yardim", "/help"):
            tg(HELP)
        elif cmd == "/ekle":
            if not args or not valid_url(args[0]):
                tg("Geçerli bir https://binalar.az/... linki gönder.")
            elif len(state["filters"]) >= MAX_FILTERS:
                tg(f"En fazla {MAX_FILTERS} filtre eklenebilir. /sil ile birini kaldır.")
            elif any(f["url"] == args[0] for f in state["filters"]):
                tg("Bu filtre zaten ekli.")
            else:
                state["filters"].append({"url": args[0], "name": " ".join(args[1:]), "seeded": False, "seen": []})
                tg(f"Filtre #{len(state['filters'])} eklendi, ilk tarama yapılıyor...")
        elif cmd == "/liste":
            if not state["filters"]:
                tg("Aktif filtre yok. /ekle ile ekle.")
            else:
                tg("\n\n".join(f"#{k} {f.get('name', '')}\n{f['url']}" for k, f in enumerate(state["filters"], 1)))
        elif cmd == "/sil":
            try:
                k = int(args[0]) - 1
                removed = state["filters"].pop(k)
                tg(f"Silindi: {removed['url']}")
            except Exception:
                tg("Kullanım: /sil <numara>  (numaralar için /liste)")


def main():
    state = load_state()
    try:
        handle_updates(state)
        for n, f in enumerate(state["filters"], 1):
            try:
                process_filter(n, f)
            except Exception as e:
                print(f"Filtre #{n} hatası:", e)
    finally:
        save_state(state)


if __name__ == "__main__":
    main()
