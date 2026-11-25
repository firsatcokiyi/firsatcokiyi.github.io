"""Herkese açık bir görsel URL'sinden Instagram feed postu yayınlar."""
import time

import requests

API = "https://graph.instagram.com/v21.0"
last_error = None  # son başarısız yayının nedeni (e-posta raporunda gösterilir)


def _err(prefix, text):
    global last_error
    try:
        e = __import__("json").loads(text)["error"]
        last_error = f"{prefix}: {e.get('error_user_msg') or e.get('message')} (kod {e.get('code')}/{e.get('error_subcode', '-')})"
    except Exception:
        last_error = f"{prefix}: {text[:200]}"
    print(f"  {last_error}")


def check_token(user_id, token):
    """Token geçerliyse None, değilse hata mesajı döndürür."""
    try:
        r = requests.get(f"{API}/me", params={"fields": "user_id", "access_token": token}, timeout=20)
    except requests.RequestException as e:
        print(f"UYARI: Token kontrolü yapılamadı ({e}), devam ediliyor.")
        return None
    if r.status_code == 200:
        return None
    try:
        return r.json()["error"]["message"]
    except (ValueError, KeyError, TypeError):
        return f"{r.status_code} {r.text[:200]}"


def publish(image_url, caption, user_id, token, story=False):
    """Feed postu (veya story=True ise story) yayınlar; başarılıysa media id, değilse None.
    Not: API story'ye link sticker eklemeyi desteklemiyor."""
    data = {"image_url": image_url, "access_token": token}
    if story:
        data["media_type"] = "STORIES"
    else:
        data["caption"] = caption
    c = requests.post(f"{API}/{user_id}/media", data=data, timeout=60)
    if c.status_code != 200:
        _err("Container hatası", c.text)
        return None
    cid = c.json()["id"]

    # Instagram görseli indirip işleyene kadar bekle
    for _ in range(20):
        s = requests.get(f"{API}/{cid}", params={"fields": "status_code", "access_token": token},
                         timeout=20).json()
        status = s.get("status_code")
        if status == "FINISHED":
            break
        if status == "ERROR":
            _err("Instagram görseli işleyemedi", str(s))
            return None
        time.sleep(3)

    p = requests.post(f"{API}/{user_id}/media_publish",
                      data={"creation_id": cid, "access_token": token}, timeout=60)
    if p.status_code != 200:
        _err("Yayınlama hatası", p.text)
        return None
    return p.json().get("id")


def quota_left(user_id, token):
    """24 saatlik API yayın kotasından kalan hak (bilinmiyorsa None)."""
    try:
        r = requests.get(f"{API}/{user_id}/content_publishing_limit",
                         params={"fields": "config,quota_usage", "access_token": token}, timeout=20)
        d = r.json()["data"][0]
        return d["config"]["quota_total"] - d["quota_usage"]
    except Exception as e:  # kota okunamazsa paylaşımı engelleme
        print(f"  UYARI: Kota okunamadı ({e})")
        return None


def permalink(media_id, token):
    try:
        r = requests.get(f"{API}/{media_id}", params={"fields": "permalink", "access_token": token},
                         timeout=20)
        return r.json().get("permalink")
    except Exception:
        return None
