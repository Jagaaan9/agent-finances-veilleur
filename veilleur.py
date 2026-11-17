"""Veilleur 24h/24 de l'agent finances de Léon.

Il ne décide RIEN. Toutes les 10 minutes (GitHub Actions, gratuit, sans IA) il ramasse
tout ce qui bouge et l'écrit dans signaux.json :
- ses positions et les grands indices du monde ;
- les plus fortes hausses, baisses et volumes du jour, les petites valeurs en croissance ;
- les gros titres économiques (France, monde, Chine, IA, IPO, rachats, résultats...).
L'agent Claude lit signaux.json à chacun de ses passages, analyse et décide.
Le veilleur réveille l'agent tout de suite seulement si quelque chose de gros bouge
(position ou marché), avec un nombre maximum de réveils par jour pour économiser les tokens.
"""
import json
import os
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = json.load(open(os.path.join(HERE, "config.json"), encoding="utf-8"))
STATE_PATH = os.path.join(HERE, "state.json")
SIGNALS_PATH = os.path.join(HERE, "signaux.json")
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"}

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
ROUTINE_URL = os.environ.get("ROUTINE_FIRE_URL", "")
ROUTINE_TOKEN = os.environ.get("ROUTINE_TOKEN", "")
PAGE = CONFIG["page_url"]
NOW = datetime.now(timezone.utc)
TODAY = NOW.strftime("%Y-%m-%d")


def load(path, default):
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return default


def get(url, timeout=20):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


TV_COLS = ["name", "description", "close", "change", "Perf.1M", "currency", "volume", "relative_volume_10d_calc",
           "market_cap_basic", "price_earnings_ttm", "exchange", "sector"]


def tv_post(screener, payload):
    req = urllib.request.Request(f"https://scanner.tradingview.com/{screener}/scan", data=json.dumps(payload).encode(),
                                 headers={**UA, "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read()).get("data", [])


def tv_row(d):
    v = dict(zip(TV_COLS, d["d"]))
    return {"symbole": d["s"], "nom": v["description"] or v["name"], "prix": v["close"], "devise": v["currency"] or "",
            "jour_pct": round(v["change"], 2) if v["change"] is not None else None,
            "mois_pct": round(v["Perf.1M"], 2) if v["Perf.1M"] is not None else None,
            "volume": v["volume"], "volume_relatif": round(v["relative_volume_10d_calc"], 2) if v["relative_volume_10d_calc"] else None,
            "capitalisation": v["market_cap_basic"], "per": round(v["price_earnings_ttm"], 1) if v["price_earnings_ttm"] else None,
            "bourse": v["exchange"], "secteur": v["sector"]}


def tv_quote(symbol):
    for scr in ("global", "america", "cfd", "forex", "crypto"):
        try:
            rows = tv_post(scr, {"symbols": {"tickers": [symbol]}, "columns": TV_COLS})
        except Exception:
            continue
        if rows:
            return tv_row(rows[0])
    return None


def tv_screen(market, sort_by, order, extra_filter=None, n=10):
    flt = [{"left": "market_cap_basic", "operation": "greater", "right": 2e8},
           {"left": "volume", "operation": "greater", "right": 1e5}] + (extra_filter or [])
    rows = tv_post(market, {"filter": flt, "columns": TV_COLS, "sort": {"sortBy": sort_by, "sortOrder": order},
                            "range": [0, n], "options": {"lang": "fr"}})
    return [tv_row(r) for r in rows]


def quote(symbol):
    if ":" in symbol:
        return tv_quote(symbol)
    return yahoo_quote(symbol)


def yahoo_quote(symbol):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}?interval=1d&range=1mo"
    res = json.loads(get(url))["chart"]["result"][0]
    meta = res["meta"]
    closes = [c for c in (res.get("indicators", {}).get("quote", [{}])[0].get("close") or []) if c]
    price, prev = meta.get("regularMarketPrice"), meta.get("chartPreviousClose") or meta.get("previousClose")
    if price is None:
        return None
    day = (price - closes[-2]) / closes[-2] * 100 if len(closes) >= 2 else ((price - prev) / prev * 100 if prev else None)
    month = (price - closes[0]) / closes[0] * 100 if closes else None
    return {"prix": round(price, 4), "devise": meta.get("currency", ""), "jour_pct": round(day, 2) if day is not None else None,
            "mois_pct": round(month, 2) if month is not None else None}


def screener(scr_id, count=15):
    url = f"https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?scrIds={scr_id}&count={count}&lang=fr-FR&region=FR"
    data = json.loads(get(url))
    out = []
    for q in data["finance"]["result"][0]["quotes"]:
        out.append({
            "symbole": q.get("symbol"), "nom": q.get("shortName") or q.get("longName"),
            "prix": q.get("regularMarketPrice"), "jour_pct": round(q.get("regularMarketChangePercent") or 0, 2),
            "volume": q.get("regularMarketVolume"), "volume_moyen_3m": q.get("averageDailyVolume3Month"),
            "capitalisation": q.get("marketCap"), "per": q.get("trailingPE"), "bourse": q.get("fullExchangeName"),
        })
    return out


def headlines(query, lang):
    hl, gl = ("fr", "FR") if lang == "fr" else ("en-US", "US")
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode({"q": query + " when:1d", "hl": hl, "gl": gl, "ceid": f"{gl}:{hl.split('-')[0]}"})
    root = ET.fromstring(get(url))
    return [{"titre": i.findtext("title", ""), "lien": i.findtext("link", ""), "date": i.findtext("pubDate", ""), "id": i.findtext("guid", "")}
            for i in root.iter("item")][:20]


def notify(title, message, priority="high", tags="rotating_light"):
    if not NTFY_TOPIC:
        print("[ntfy non configuré]", title)
        return
    req = urllib.request.Request(f"https://ntfy.sh/{NTFY_TOPIC}", data=message.encode("utf-8"), method="POST",
                                 headers={"Title": title.encode("utf-8"), "Priority": priority, "Tags": tags, "Click": PAGE})
    urllib.request.urlopen(req, timeout=20).read()


def wake_agent(text):
    if not (ROUTINE_URL and ROUTINE_TOKEN):
        print("[routine non configurée]")
        return False
    req = urllib.request.Request(ROUTINE_URL, data=json.dumps({"text": text}).encode("utf-8"), method="POST", headers={
        "Authorization": f"Bearer {ROUTINE_TOKEN}", "anthropic-beta": "experimental-cc-routine-2026-04-01",
        "anthropic-version": "2023-06-01", "Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=30).read()
    return True


def digest(sig, paris):
    """Résumé de tout ce que le veilleur a ramassé, envoyé à l'agent (moins de 60 Ko)."""
    moment = {7: "PASSAGE DU MATIN (avant l'ouverture de Trade Republic à 7h30)", 15: "PASSAGE DE L'APRÈS-MIDI (avant l'ouverture de Wall Street à 15h30)",
              23: "PASSAGE DU SOIR (après la clôture américaine) avec bilan du jour"}.get(paris.hour, "PASSAGE")
    lines = [f"{moment}. Voici tout ce que le veilleur a ramassé (données brutes, à toi d'analyser et de décider).", "", "POSITIONS ET INDICES :"]
    for p in sig["positions"] + sig["indices"]:
        lines.append(f"- {p['nom']} ({p['symbole']}) : {p['prix']} {p['devise']}, jour {p['jour_pct']} %, mois {p['mois_pct']} %")
    for scr, items in sig["mouvements"].items():
        lines.append(f"\nÉCRAN {scr} :")
        for q in items[:10]:
            lines.append(f"- {q['nom']} ({q['symbole']}, secteur {q.get('secteur')}) jour {q['jour_pct']} %, mois {q.get('mois_pct')} %, prix {q['prix']} {q.get('devise','')}, capi {q['capitalisation']}, PER {q['per']}, volume x{q.get('volume_relatif')} la normale")
    lines.append("\nACTUALITÉS (dernières 48 h) :")
    for a in sig["actualites"][:150]:
        lines.append(f"- [{a['sujet']}] {a['titre']}")
    if sig["erreurs"]:
        lines.append(f"\nSources indisponibles ce passage : {len(sig['erreurs'])} (exemple : {sig['erreurs'][0][:150]})")
    text = "\n".join(lines)
    return text[:60000]


def main():
    state = load(STATE_PATH, {"seen": [], "wakes": {}, "flags": {}})
    sig = {"mis_a_jour": NOW.isoformat(), "positions": [], "indices": [], "mouvements": {}, "actualites": [], "erreurs": []}
    urgent = []

    # 1. Positions et indices
    for grp, key in (("positions", "positions"), ("indices", "indices")):
        for it in CONFIG[grp]:
            try:
                q = quote(it["symbole"])
            except Exception as e:
                sig["erreurs"].append(f"{it['symbole']}: {e}")
                continue
            if not q:
                continue
            sig[key].append({"nom": it["nom"], "symbole": it["symbole"], **q})
            jp = q["jour_pct"]
            if jp is not None and abs(jp) >= it["reveil_pct"]:
                flag = f"{it['symbole']}:{TODAY}:{int(abs(jp) // it['reveil_pct'])}:{'b' if jp < 0 else 'h'}"
                if flag not in state["flags"]:
                    state["flags"][flag] = TODAY
                    urgent.append(f"{it['nom']} ({it['symbole']}) {jp:+.2f} % aujourd'hui, {q['mois_pct']:+.2f} % sur un mois, cours {q['prix']} {q['devise']}.")

    # 2. Ce qui bouge sur tout le marché, dans plusieurs pays
    for ec in CONFIG["ecrans_marche"]:
        try:
            sig["mouvements"][ec["nom"]] = tv_screen(ec["marche"], ec["tri"], ec["ordre"], ec.get("filtre"))
        except Exception as e:
            sig["erreurs"].append(f"écran {ec['nom']}: {e}")

    # 3. Actualités (nouvelles depuis le dernier passage, gardées 48 h dans signaux.json)
    seen = set(state["seen"])
    cutoff = (NOW - timedelta(hours=48)).isoformat()
    previous = [a for a in load(SIGNALS_PATH, {}).get("actualites", []) if a.get("vu_le", "") >= cutoff]
    fresh = []
    for rq in CONFIG["actualites"]:
        try:
            items = headlines(rq["requete"], rq.get("langue", "fr"))
        except Exception as e:
            sig["erreurs"].append(f"actu {rq['sujet']}: {e}")
            continue
        for h in items:
            hid = h["id"] or h["lien"]
            if hid in seen:
                continue
            seen.add(hid)
            state["seen"].append(hid)
            item = {"sujet": rq["sujet"], "titre": h["titre"], "lien": h["lien"], "date": h["date"], "vu_le": NOW.isoformat()}
            fresh.append(item)
            if rq.get("position") and re.search(CONFIG["mots_urgents"], h["titre"], re.I):
                urgent.append(f"Titre important sur {rq['sujet']} : {h['titre']} ({h['lien']})")
    sig["actualites"] = (fresh + previous)[:400]
    state["seen"] = state["seen"][-3000:]

    # Premier passage : on mémorise sans réveiller
    if not state.get("initialise"):
        state["initialise"] = True
        urgent = [u for u in urgent if not u.startswith("Titre important")]

    # 4. Réveil de l'agent si quelque chose de gros bouge (plafonné par jour)
    state["wakes"] = {d: n for d, n in state["wakes"].items() if d == TODAY}
    if urgent and state["wakes"].get(TODAY, 0) < CONFIG["reveils_max_par_jour"]:
        state["wakes"][TODAY] = state["wakes"].get(TODAY, 0) + 1
        notify("Agent finances : ça bouge", "\n".join(urgent)[:500] + "\n\nTon agent analyse et te dit s'il faut faire quelque chose.")
        text = ("RÉVEIL PAR LE VEILLEUR : quelque chose de gros bouge. Ce n'est pas une règle de vente ni d'achat, c'est un signal à analyser. "
                "Lis aussi signaux.json, analyse en profondeur, mets la page à jour et dis clairement à Léon s'il faut agir ou non, et pourquoi :\n- "
                + "\n- ".join(urgent))
        try:
            wake_agent(text)
        except Exception as e:
            sig["erreurs"].append(f"réveil agent: {e}")
            notify("Agent finances : réveil impossible", "Le veilleur n'a pas pu réveiller l'agent Claude.", priority="default", tags="warning")

    # 5. Rendez-vous d'analyse : 7h, 15h et 23h (heure de Paris), le veilleur envoie à l'agent tout ce qu'il a ramassé
    paris = NOW.astimezone(ZoneInfo("Europe/Paris"))
    slot = f"{paris:%Y-%m-%d}-{paris.hour}"
    if paris.hour in CONFIG["rendez_vous_heures"] and state.get("dernier_rdv") != slot:
        state["dernier_rdv"] = slot
        try:
            wake_agent(digest(sig, paris))
        except Exception as e:
            sig["erreurs"].append(f"rendez-vous agent: {e}")

    state["flags"] = {k: v for k, v in state["flags"].items() if v == TODAY}
    json.dump(state, open(STATE_PATH, "w", encoding="utf-8"), ensure_ascii=False)
    json.dump(sig, open(SIGNALS_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"{len(sig['positions'])} positions, {len(sig['indices'])} indices, {sum(len(v) for v in sig['mouvements'].values())} mouvements, {len(fresh)} nouvelles actus, {len(urgent)} signaux forts, {len(sig['erreurs'])} erreurs")


if __name__ == "__main__":
    main()
