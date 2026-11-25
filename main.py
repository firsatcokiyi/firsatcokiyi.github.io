"""Amazon.com.tr'deki 365/90 günün en düşük fiyatlı ürünlerini Instagram'a paylaşır.
GitHub Actions gün içinde (TR 08-23) saatte bir çalıştırır; her çalıştırma günün
kalanını kalan saatlere bölerek günde en fazla DAILY_MAX post ve her birine story atar. Daha önce
paylaşılan ürünler posted.json'da tutulur ve bir daha seçilmez. Her çalıştırmanın
özeti GitHub issue yorumu olarak (e-posta bildirimiyle) gönderilir.

    python main.py              # tara, görselleri üret, commit+push, Instagram'a yayınla
    python main.py --dry-run    # sadece tara ve görselleri posts/ altına üret
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

from amazon_fetcher import scan, select
from instagram_poster import check_token, permalink, publish, quota_left
from notify import notify
from categories import categorize
from post_image import caption, create_post, create_story, tl
from bio_page import build as build_site

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")

INSTAGRAM_USER_ID = os.getenv("INSTAGRAM_USER_ID", "24422570594030577")
INSTAGRAM_TOKEN = os.getenv("INSTAGRAM_TOKEN")
POSTED_PATH = HERE / "posted.json"
POSTS_DIR = HERE / "posts"
SITE_DIR = HERE / "docs"
# Instagram API kotası 24 saatte 50 yayın ve story'ler de bu kotadan düşüyor.
# Her post + story 2 hak kullandığı için günde 24 post + 24 story (48 hak, 2 pay).
DAILY_MAX = 24
MAX_PER_RUN = 10
WINDOW = (8, 23)  # TR saatiyle paylaşım penceresi [8:00, 23:00)
DELAY_BETWEEN_POSTS = 20  # saniye
TR = timezone(timedelta(hours=3))


def git(*args):
    return subprocess.run(["git", *args], cwd=HERE, check=True,
                          capture_output=True, text=True).stdout.strip()


def git_commit_push(paths, message):
    git("add", *[str(p) for p in paths])
    if not git("status", "--porcelain", *[str(p) for p in paths]):
        return git("rev-parse", "HEAD")
    git("commit", "-m", message)
    git("pull", "--rebase", "--quiet")
    git("push", "--quiet")
    return git("rev-parse", "HEAD")


def repo_slug():
    if os.getenv("GITHUB_REPOSITORY"):
        return os.environ["GITHUB_REPOSITORY"]
    url = git("remote", "get-url", "origin")
    return re.sub(r"(\.git)?$", "", re.sub(r"^.*github\.com[:/]", "", url))


def wait_until_public(url, timeout=120):
    end = time.time() + timeout
    while time.time() < end:
        try:
            r = requests.head(url, timeout=15)
            if r.status_code == 200:
                return True
        except requests.RequestException:
            pass
        time.sleep(5)
    return False


def load_posted():
    if POSTED_PATH.exists():
        return json.loads(POSTED_PATH.read_text(encoding="utf-8"))
    return {}


def plan_count(now, posted_today, requested):
    """Bu çalıştırmada kaç post atılacağı. requested verilmişse (elle tetikleme) o kullanılır;
    yoksa günün kalanı pencere içindeki kalan saatlere bölünür."""
    remaining = DAILY_MAX - posted_today
    if requested:
        return max(0, min(requested, remaining))
    if not WINDOW[0] <= now.hour < WINDOW[1]:
        return 0
    runs_left = WINDOW[1] - now.hour
    return max(0, min(MAX_PER_RUN, -(-remaining // runs_left)))


def report(done, failed, posted, stories, posted_today):
    """E-posta bildirimine gidecek Markdown özet."""
    lines = [f"**{datetime.now(TR):%H:%M}** — {len(done)} post, {stories} story yayınlandı "
             f"(bugün toplam {posted_today}/{DAILY_MAX})", ""]
    for it in done:
        p = posted[it["asin"]]
        ig = f"[Instagram]({p['permalink']})" if p.get("permalink") else "Instagram"
        story = " · story ✓" if p.get("story_id") else ""
        lines.append(f"- **{it['days']}g** · -%{it['discount']} · {tl(it['price'])} · {it['cat']} — "
                     f"{it['title'][:80]}  \n  {ig} · [Amazon]({it['url']}){story}")
    if failed:
        lines += ["", f"⚠️ {len(failed)} ürün yayınlanamadı (ayrıntı Actions günlüğünde):"]
        lines += [f"- {it['title'][:80]}" for it in failed]
    if os.getenv("GITHUB_RUN_ID"):
        lines += ["", f"[Çalıştırma günlüğü]({os.getenv('GITHUB_SERVER_URL')}/"
                      f"{os.getenv('GITHUB_REPOSITORY')}/actions/runs/{os.getenv('GITHUB_RUN_ID')})"]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="Post atmadan sadece görsel üret")
    ap.add_argument("--count", type=int, default=int(os.getenv("COUNT") or 0) or None,
                    help="Bu çalıştırmada atılacak post sayısı (varsayılan: otomatik)")
    ap.add_argument("--site", action="store_true", help="Sadece docs/ sayfasını üret")
    args = ap.parse_args()

    if args.site:
        print(build_site(load_posted(), repo_slug()))
        return

    now = datetime.now(TR)
    today = now.strftime("%Y-%m-%d")
    posted = load_posted()
    posted_today = sum(1 for v in posted.values() if v.get("date") == today)
    count = plan_count(now, posted_today, args.count)
    if args.dry_run:
        count = args.count or 5
    print(f"=== Amazon paylaşımı — {now:%Y-%m-%d %H:%M}, bugün {posted_today}/{DAILY_MAX}, "
          f"bu çalıştırma: {count} ===\n")
    if count == 0:
        print("Bu saatte paylaşım yok (pencere dışı ya da günlük sınır doldu).")
        return

    if not args.dry_run:
        if not INSTAGRAM_TOKEN:
            sys.exit("HATA: INSTAGRAM_TOKEN tanımlı değil.")
        err = check_token(INSTAGRAM_USER_ID, INSTAGRAM_TOKEN)
        if err:
            msg = (f"Instagram token geçersiz: {err}\n"
                   "Meta panelinden yeni token alıp INSTAGRAM_TOKEN secret'ını güncelleyin.")
            notify(repo_slug(), today, "❌ **Paylaşım yapılamadı.** " + msg)
            sys.exit("HATA: " + msg)
        left = quota_left(INSTAGRAM_USER_ID, INSTAGRAM_TOKEN)
        if left is not None:
            print(f"Instagram 24 saatlik yayın kotasında kalan: {left}")
            count = max(0, min(count, left // 2))
            if count == 0:
                print("API kotası dolu; bu çalıştırma atlanıyor.")
                return

    print(f"Amazon taranıyor... (daha önce paylaşılan: {len(posted)} ürün)")
    found = scan()
    print(f"\n{len(found)} rozetli ürün bulundu "
          f"(365g: {sum(1 for i in found.values() if i['days'] == 365)}, "
          f"90g: {sum(1 for i in found.values() if i['days'] == 90)})")
    if not found:
        if not args.dry_run:
            notify(repo_slug(), today, "❌ Amazon'dan hiç ürün alınamadı — istekler engellenmiş olabilir.")
        sys.exit("HATA: Amazon'dan hiç ürün alınamadı — istekler engellenmiş olabilir.")

    # Görsel üretimi başarısız olursa yedek adaylara geçebilmek için fazladan seç
    candidates = select(found, posted, count=count + 5)
    picks = []
    for it in candidates:
        if len(picks) == count:
            break
        it["cat"] = categorize(it)
        it["path"] = POSTS_DIR / today / f"{it['asin']}.jpg"
        it["story_path"] = POSTS_DIR / today / f"{it['asin']}_story.jpg"
        try:
            create_post(it, it["path"])
            create_story(it, it["cat"], it["story_path"])
        except Exception as e:
            print(f"  Görsel üretilemedi ({it['asin']}): {e}")
            continue
        picks.append(it)
        print(f"  [{it['days']}g] -%{it['discount']}  {it['cat']:<15} {it['title'][:60]}")

    if not picks:
        if not args.dry_run:
            notify(repo_slug(), today, "⚠️ Paylaşılacak yeni uygun ürün bulunamadı (havuz tükenmiş olabilir).")
        sys.exit("HATA: Paylaşılacak uygun ürün bulunamadı.")

    if args.dry_run:
        print(f"\n[DRY RUN] {len(picks)} post + story görseli üretildi: {POSTS_DIR / today}")
        for it in picks:
            print("\n" + "─" * 60 + "\n" + caption(it))
        return

    # Görselleri repoya commit'le; Instagram onları raw.githubusercontent.com'dan çeker
    files = [f for it in picks for f in (it["path"], it["story_path"])]
    sha = git_commit_push(files, f"Günün Amazon görselleri: {today}")
    slug = repo_slug()

    def raw(path):
        return f"https://raw.githubusercontent.com/{slug}/{sha}/{path.relative_to(HERE).as_posix()}"

    ok = stories = 0
    done, failed = [], []
    try:
        for n, it in enumerate(picks, 1):
            print(f"\n[{n}/{len(picks)}] {it['title'][:70]}")
            left = quota_left(INSTAGRAM_USER_ID, INSTAGRAM_TOKEN)
            if left is not None and left < 2:  # post + story için iki hak gerekir
                print(f"  Instagram 24 saatlik yayın kotası dolu ({left} hak); kalanlar sonraki çalıştırmaya.")
                break
            url = raw(it["path"])
            if not wait_until_public(url):
                print(f"  Görsel herkese açık adreste görünmüyor (repo public mi?): {url}")
                failed.append(it)
                continue
            try:
                media_id = publish(url, caption(it), INSTAGRAM_USER_ID, INSTAGRAM_TOKEN)
            except requests.RequestException as e:
                print(f"  Ağ hatası: {e}")
                media_id = None
            if not media_id:
                failed.append(it)
                continue
            ok += 1
            posted_today += 1
            print(f"  ✅ Post yayınlandı ({media_id})")
            posted[it["asin"]] = {
                "date": today, "posted_at": datetime.now(TR).isoformat(timespec="seconds"),
                "days": it["days"], "price": it["price"], "old_price": it["old_price"],
                "discount": it["discount"], "title": it["title"], "category": it["cat"],
                "url": it["url"], "thumb": it["image"].replace("_AC_SL1200_", "_AC_SL400_"),
                "post_image": it["path"].relative_to(HERE).as_posix(), "media_id": media_id,
                "permalink": permalink(media_id, INSTAGRAM_TOKEN),
            }
            done.append(it)
            POSTED_PATH.write_text(json.dumps(posted, ensure_ascii=False, indent=1) + "\n",
                                   encoding="utf-8")

            # Story — her postun story'si de atılır (kotadaki yeri post öncesi ayrıldı).
            # Story başarısız olsa da feed postu sayılır.
            try:
                story_id = publish(raw(it["story_path"]), "", INSTAGRAM_USER_ID,
                                   INSTAGRAM_TOKEN, story=True)
            except requests.RequestException as e:
                print(f"  Story ağ hatası: {e}")
                story_id = None
            if story_id:
                stories += 1
                posted[it["asin"]]["story_id"] = story_id
                POSTED_PATH.write_text(json.dumps(posted, ensure_ascii=False, indent=1) + "\n",
                                       encoding="utf-8")
                print(f"  ✅ Story yayınlandı [{it['cat']}]")

            if n < len(picks):
                time.sleep(DELAY_BETWEEN_POSTS)
    finally:
        # Ne olursa olsun paylaşılanları kaydet; sonraki çalıştırma bunları tekrar seçmesin
        if ok:
            build_site(posted, slug)
            git_commit_push([POSTED_PATH, SITE_DIR],
                            f"Paylaşılanlar güncellendi: {today} {now:%H:%M} (+{ok})")
        notify(slug, today, report(done, failed, posted, stories, posted_today))

    print(f"\n=== Tamamlandı: {ok}/{len(picks)} post, {stories} story yayınlandı ===")
    if ok == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
