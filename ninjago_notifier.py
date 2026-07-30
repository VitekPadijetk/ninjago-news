#!/usr/bin/env python3
"""
Sleduje oficiální YouTube kanál LEGO a posílá push notifikaci přes ntfy.sh,
pokaždé když vyjde nové video týkající se Ninjaga.

Ostatní videa (City, Friends, Star Wars, ...) jsou tiše ignorována.
Pokud v jednom běhu přibude více Ninjago videí najednou (typicky různé
jazykové verze téhož dílu vydané ve stejnou dobu), pošle se jen jedna
souhrnná notifikace místo zahlcení víc oznámeními.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

# Oficiální LEGO kanál (@LEGO)
CHANNEL_ID = "UCP-Ng5SXUEt0VE-TXqRdL6g"
FEED_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"

KEYWORD = "ninjago"
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen_videos.json")
MAX_SEEN = 300          # kolik ID videí si maximálně pamatovat
MAX_BATCH_LINES = 15    # kolik videí max vypsat v souhrnné notifikaci (kvůli limitu velikosti zprávy u ntfy)

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
}


FEED_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def fetch_feed(url: str, retries: int = 3, backoff_seconds: float = 8.0) -> bytes:
    # YouTube feed endpoint občas vrací přechodné 404 i pro platné kanály -
    # jde o dobře známý, dlouhodobě hlášený jev, ne chybu v URL/ID. Pár pokusů
    # s malou pauzou to ve většině případů vyřeší.
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": FEED_UA})
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.read()
        except (urllib.error.HTTPError, urllib.error.URLError) as e:
            last_error = e
            print(f"Pokus {attempt}/{retries} o stažení feedu selhal ({e}).", file=sys.stderr)
            if attempt < retries:
                time.sleep(backoff_seconds)
    raise last_error


def parse_entries(xml_bytes: bytes):
    root = ET.fromstring(xml_bytes)
    entries = []
    for entry in root.findall("atom:entry", NS):
        video_id_el = entry.find("yt:videoId", NS)
        title_el = entry.find("atom:title", NS)
        link_el = entry.find("atom:link", NS)

        video_id = video_id_el.text if video_id_el is not None else None
        title = title_el.text if title_el is not None and title_el.text else ""
        link = link_el.attrib.get("href", "") if link_el is not None else ""

        if video_id:
            entries.append({"id": video_id, "title": title, "link": link})
    return entries


def load_seen(path: str):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_seen(path: str, seen_list):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(seen_list[-MAX_SEEN:], f, ensure_ascii=False, indent=2)


def is_ninjago(entry) -> bool:
    # Záměrně jen NÁZEV videa. LEGO dává do popisků obecný text o kanálu
    # ("...LEGO NINJAGO, LEGO Star Wars, ... LEGO DreamZzz..."), takže
    # kontrola popisku by mylně chytala i videa z úplně jiných témat.
    return KEYWORD in entry["title"].lower()


def _post_ntfy(body: bytes, title: str, click: str) -> None:
    if not NTFY_TOPIC:
        print("Chybí NTFY_TOPIC (env proměnná) - notifikaci neposílám.", file=sys.stderr)
        return
    url = f"{NTFY_SERVER}/{NTFY_TOPIC}"
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            # Hlavičky drž čistě ASCII - text s diakritikou/emoji patří jen do těla zprávy.
            "Title": title,
            "Click": click,
            "Tags": "lego,ninjago",
            "Priority": "default",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        resp.read()


def send_single(entry) -> None:
    body = f"{entry['title']}\n{entry['link']}".encode("utf-8")
    _post_ntfy(body, title="New Ninjago video!", click=entry["link"])
    print(f"Odeslána notifikace: {entry['title']}")


def send_batch(entries) -> None:
    shown = entries[:MAX_BATCH_LINES]
    remaining = len(entries) - len(shown)
    lines = [f"{e['title']}\n{e['link']}" for e in shown]
    if remaining > 0:
        lines.append(f"... a dalších {remaining} video(í), viz kanál.")
    body = "\n\n".join(lines).encode("utf-8")
    _post_ntfy(
        body,
        title=f"{len(entries)} new Ninjago videos!",
        click=entries[0]["link"],
    )
    print(f"Odeslána souhrnná notifikace za {len(entries)} nových Ninjago videí.")


def main() -> None:
    first_run = not os.path.exists(STATE_FILE)
    seen = load_seen(STATE_FILE)
    seen_set = set(seen)

    try:
        xml_bytes = fetch_feed(FEED_URL)
    except Exception as e:
        # Feed se nepodařilo stáhnout ani po opakovaných pokusech - typicky
        # přechodný výpadek na straně YouTube. Skript skončí v klidu a příští
        # naplánovaný běh (za pár minut) to zkusí znovu, žádná notifikace se
        # tím neztratí.
        print(f"Feed se nepodařilo stáhnout ani po opakovaných pokusech ({e}). Zkusím to příští běh.", file=sys.stderr)
        return

    entries = parse_entries(xml_bytes)

    if first_run:
        # První spuštění: jen zaznamenáme, co už na kanálu existuje, ať nás
        # nezasype notifikacemi za starý obsah. Notifikace se posílají až
        # od dalšího běhu, jen za opravdu nová videa.
        all_ids = [e["id"] for e in entries]
        save_seen(STATE_FILE, all_ids)
        print(f"První spuštění: zaznamenáno {len(all_ids)} existujících videí, žádné notifikace se neposílaly.")
        return

    new_entries = [e for e in reversed(entries) if e["id"] not in seen_set]  # od nejstaršího po nejnovější

    if not new_entries:
        print("Žádná nová videa od posledního běhu.")
    else:
        ninjago_new = [e for e in new_entries if is_ninjago(e)]
        other_new = [e for e in new_entries if not is_ninjago(e)]

        for e in other_new:
            print(f"Nové video, ale není o Ninjagu (ignoruji): {e['title']}")

        if len(ninjago_new) == 1:
            send_single(ninjago_new[0])
        elif len(ninjago_new) > 1:
            # Typicky víc jazykových verzí téhož dílu vydaných najednou.
            send_batch(ninjago_new)

        save_seen(STATE_FILE, seen + [e["id"] for e in new_entries])


if __name__ == "__main__":
    main()
