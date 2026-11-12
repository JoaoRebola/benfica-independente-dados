"""
Robô do Benfica Independente.

Corre sozinho no GitHub (de 30 em 30 minutos) e prepara para a app:
  data/agenda.json    - próximos jogos de todas as modalidades
  data/results.json   - últimos resultados das equipas principais
  data/videos.json    - programa de cada vídeo do canal que as regras não reconhecem
  data/status.json    - quando correu e quem fez o trabalho (robô ou agente)

O "agente" (Gemini) só entra se existir a chave GEMINI_API_KEY nos segredos do
repositório. Se não houver chave, se falhar ou se acabarem os pedidos grátis do
dia, o robô faz tudo sozinho e a app continua a funcionar na mesma.
Só usa a biblioteca normal do Python (nada para instalar).
"""
import html
import json
import os
import re
import sys
import time
import unicodedata
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

SOURCE = "https://www.slbenfica.pt"
AGENDA_URL = SOURCE + "/pt-pt/agora/agenda"
RESULTS_URL = SOURCE + "/api/sitecore/Calendar/CalendarEvents"
MAIN_TEAM = "16094ecf-9e78-4e3e-bcdf-28e4f765de9f"
CHANNEL = "UCig3rOWj-2NsbOXRBUaiQqQ"
AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
LISBON = ZoneInfo("Europe/Lisbon")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "").strip()

SPORTS = [
    ("Futebol", "{ECCFEB41-A0FD-4830-A3BB-7E57A0A15D00}", True),
    ("Futebol Feminino", "{37A610A0-2CCD-4F89-A589-A1F995F8FCB5}", False),
    ("Basquetebol", "{14FD66DA-8731-4AE2-8347-4B803DC94683}", True),
    ("Basquetebol Feminino", "{14FD66DA-8731-4AE2-8347-4B803DC94683}", False),
    ("Futsal", "{F4A1EA09-FEC1-4541-9D07-A4A8EC922744}", True),
    ("Futsal Feminino", "{F4A1EA09-FEC1-4541-9D07-A4A8EC922744}", False),
    ("Hóquei em Patins", "{A8B9BC3D-E1BA-4AB6-8C92-BFEE8771E59C}", True),
    ("Hóquei em Patins Feminino", "{A8B9BC3D-E1BA-4AB6-8C92-BFEE8771E59C}", False),
    ("Andebol", "{698B2A89-1519-40B6-85E6-DD80B3325693}", True),
    ("Voleibol", "{61FF7D89-5327-4A5E-82C8-6E4CD35D8314}", True),
    ("Voleibol Feminino", "{61FF7D89-5327-4A5E-82C8-6E4CD35D8314}", False),
]

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
status = {"robo": {}, "agente": {}}


def log(*a):
    print(*a, flush=True)


def get(url, timeout=60, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": AGENT, "Accept-Language": "pt-PT,pt;q=0.9", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def post_json(url, body, timeout=120):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"User-Agent": AGENT, "Content-Type": "application/json", "Accept-Language": "pt-PT"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def plain(fragment):
    text = re.sub(r"<br\s*/?>", " ", fragment)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def load(name, default):
    try:
        with open(os.path.join(DATA, name), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save(name, value):
    path = os.path.join(DATA, name)
    new = json.dumps(value, ensure_ascii=False, indent=1, sort_keys=False)
    try:
        with open(path, encoding="utf-8") as f:
            if f.read() == new:
                return False
    except FileNotFoundError:
        pass
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(new)
    return True


# ---------------------------------------------------------------- agente (Gemini)
_model = None


def pick_model():
    """Escolhe sozinho o modelo "flash" mais recente disponível para esta chave (os nomes mudam com o tempo)."""
    global _model
    if _model:
        return _model
    try:
        req = urllib.request.Request("https://generativelanguage.googleapis.com/v1beta/models?pageSize=200",
                                     headers={"x-goog-api-key": GEMINI_KEY})
        with urllib.request.urlopen(req, timeout=30) as r:
            models = json.loads(r.read().decode()).get("models", [])
        names = []
        for m in models:
            name = m.get("name", "").split("/")[-1]
            if "generateContent" not in m.get("supportedGenerationMethods", []):
                continue
            if "flash" not in name or any(w in name for w in ("lite", "image", "tts", "audio", "live", "exp", "thinking")):
                continue
            ver = re.search(r"gemini-(\d+(?:\.\d+)?)", name)
            names.append((float(ver.group(1)) if ver else 0, "preview" not in name, "latest" in name, name))
        if names:
            _model = max(names)[3]
    except Exception as e:
        log("não consegui ver os modelos:", type(e).__name__)
    _model = _model or GEMINI_MODEL
    log("agente: modelo", _model)
    status["agente"]["modelo"] = _model
    return _model


def gemini(prompt, timeout=240):
    """Pergunta ao Gemini e devolve JSON. Devolve None se não houver chave ou falhar."""
    if not GEMINI_KEY:
        return None
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{pick_model()}:generateContent"
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0},
    }
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json", "x-goog-api-key": GEMINI_KEY},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode())
        text = d["candidates"][0]["content"]["parts"][0]["text"]
        status["agente"]["ok"] = True
        return json.loads(text)
    except urllib.error.HTTPError as e:
        # 429 = acabaram os pedidos grátis do dia: o robô continua sozinho.
        log("agente indisponível:", e.code)
        status["agente"]["erro"] = e.code
        return None
    except Exception as e:
        log("agente falhou:", type(e).__name__)
        status["agente"]["erro"] = type(e).__name__
        return None


# ---------------------------------------------------------------- agenda
def parse_agenda(page):
    def field(chunk, cls):
        m = re.search(r'class="%s"[^>]*>(.*?)</div>' % cls, chunk, re.S)
        return plain(m.group(1)) if m else ""

    today = datetime.now(LISBON).replace(hour=0, minute=0, second=0, microsecond=0)
    out, seen = [], set()
    for chunk in page.split('class="agenda-event-item"')[1:]:
        try:
            start = datetime.strptime(field(chunk, "startDateForCalendar"), "%m/%d/%Y %H:%M:%S").replace(tzinfo=LISBON)
        except ValueError:
            continue
        if start < today:
            continue
        title, detail = field(chunk, "titleForCalendar"), field(chunk, "descriptionForCalendar")
        if not title and not detail:
            continue
        key = (start, title, detail)
        if key in seen:
            continue
        seen.add(key)
        sport = re.search(r'<p class="sport">(.*?)</p>', chunk, re.S)
        out.append({
            "t": int(start.timestamp() * 1000), "title": title, "detail": detail,
            "loc": field(chunk, "locationForCalendar"), "sport": plain(sport.group(1)) if sport else "",
        })
    out.sort(key=lambda e: e["t"])
    return out[:150]


def agent_agenda(page):
    text = plain(page)
    i = text.find("Agenda")
    text = text[max(0, i):][:60000]
    now = datetime.now(LISBON).strftime("%Y-%m-%d %H:%M")
    res = gemini(
        "És um assistente que extrai a agenda de jogos do Benfica do texto abaixo. Agora são " + now +
        " (hora de Lisboa). Devolve um array JSON de objetos com: date (\"YYYY-MM-DD HH:MM\" hora de Lisboa), "
        "title (ex.: \"Benfica x Porto\"), detail (competição), loc (local) e sport (modalidade em português). "
        "Só jogos de hoje em diante. Não inventes nada.\n\n" + text
    )
    if not isinstance(res, list):
        return []
    out = []
    for e in res:
        try:
            start = datetime.strptime(e["date"], "%Y-%m-%d %H:%M").replace(tzinfo=LISBON)
            out.append({"t": int(start.timestamp() * 1000), "title": e.get("title", ""), "detail": e.get("detail", ""),
                        "loc": e.get("loc", ""), "sport": e.get("sport", "")})
        except Exception:
            pass
    return sorted(out, key=lambda e: e["t"])


def do_agenda():
    page = get(AGENDA_URL, timeout=90)
    events = parse_agenda(page)
    who = "robo"
    if not events:
        # O site mudou de formato? O agente tenta perceber a página.
        events = agent_agenda(page)
        who = "agente"
    if events:
        save("agenda.json", events)
        status[who]["agenda"] = len(events)
    log("agenda:", len(events), "por", who)


# ---------------------------------------------------------------- resultados
DAY = re.compile(r"^(segunda|terça|quarta|quinta|sexta|sábado|domingo)", re.I)


def parse_results(page, sport):
    out = []
    for m in re.finditer(r'id="calendar-item-description-[^"]*"[^>]*>(.*?)</span>', page, re.S):
        parts = plain(m.group(1)).split(", ")
        if len(parts) < 4:
            continue
        score = re.fullmatch(r"(\d+) a (\d+)", parts[3].strip())
        if not score:
            continue
        di = next((i for i, p in enumerate(parts) if DAY.search(p.strip())), -1)
        out.append({
            "s": sport, "c": parts[0].strip(), "h": parts[1].strip(), "a": parts[2].strip(),
            "hg": int(score.group(1)), "ag": int(score.group(2)),
            "v": ", ".join(parts[4:di]) if di > 4 else "", "d": parts[di].strip() if di >= 4 else "",
        })
    return out


def agent_results(page, sport):
    res = gemini(
        "Extrai os resultados dos jogos do Benfica (" + sport + ") do texto abaixo. Devolve um array JSON de objetos com: "
        "c (competição), h (equipa da casa), a (equipa de fora), hg (golos/pontos da casa, número), ag (de fora, número), "
        "v (local), d (data por extenso, ex.: \"domingo 08 novembro\"). Não inventes nada.\n\n" + plain(page)[:50000]
    )
    if not isinstance(res, list):
        return []
    out = []
    for r in res:
        try:
            out.append({"s": sport, "c": str(r["c"]), "h": str(r["h"]), "a": str(r["a"]), "hg": int(r["hg"]),
                        "ag": int(r["ag"]), "v": str(r.get("v", "")), "d": str(r.get("d", ""))})
        except Exception:
            pass
    return out


def one_sport(sport):
    name, sid, male = sport
    body = {"filters": {"Menu": "last", "Modality": sid, "IsMaleTeam": male, "Rank": MAIN_TEAM,
                        "Tournaments": [], "Seasons": [], "PageNumber": 0}}
    for attempt in range(2):
        try:
            page = post_json(RESULTS_URL, body, timeout=150)
            got = parse_results(page, name)
            if got:
                return name, got, "robo"
            if "calendar-item" in page:
                agent = agent_results(page, name)
                if agent:
                    return name, agent, "agente"
            return name, [], "robo"
        except Exception as e:
            log("resultados", name, "falhou:", type(e).__name__)
            time.sleep(5)
    return name, None, "robo"


def do_results():
    old = load("results.json", [])
    with ThreadPoolExecutor(max_workers=len(SPORTS)) as pool:
        parts = list(pool.map(one_sport, SPORTS))
    if all(p[1] is None for p in parts):
        log("resultados: nada (mantém os anteriores)")
        return
    merged = []
    for name, got, who in parts:
        if got is None or (not got and any(r["s"] == name for r in old)):
            merged += [r for r in old if r["s"] == name]  # falhou: mantém o que havia
        else:
            merged += got
            if got:
                status[who]["resultados_" + name] = len(got)
    save("results.json", merged)
    log("resultados:", len(merged))


# ---------------------------------------------------------------- vídeos
RULE_WORDS = ["WATCHALONG", "WATCH ALONG", "VOO PICADO", "RESCALDO", "ANTEVISAO", "MODALIDADES", "VIAGENS EM VERMELHO",
              "VIAGENS EUROPEIAS", "FORUM BENFI", "ANALISE TA", "BI ON TOUR", "HISTORIA GLORIOSA", "BRINCO DO BAPTISTA",
              "BANCADA JOVEM", "NACIONAL 1904", "TEMA QUENTE", "MUNDO MUNDIAL", "ELEICOES", "ENTREVISTA"]
PROGRAMS = ["Watchalong", "Voo Picado", "Rescaldos", "Antevisões", "Modalidades Benfica", "Viagens em Vermelho",
            "Fórum Benfica", "Análise Tática", "BI On Tour", "A História Gloriosa", "O Brinco do Baptista",
            "Bancada Jovem", "Nacional 1904", "Tema Quente", "Mundo Mundial", "Eleições", "Entrevistas",
            "Podcast Benfica Independente"]


def up(t):
    return unicodedata.normalize("NFD", t).encode("ascii", "ignore").decode().upper()


def channel_titles():
    vids = {}
    for tab in ("videos", "streams"):
        try:
            page = get(f"https://www.youtube.com/channel/{CHANNEL}/{tab}", headers={"Cookie": "CONSENT=YES+1; SOCS=CAI"})
        except Exception:
            continue
        for chunk in page.split('"lockupViewModel":{"contentImage"')[1:]:
            vid = re.search(r'"contentId":"([\w-]{11})"', chunk)
            title = re.search(r'"title":\{"content":"((?:[^"\\]|\\.)*)"', chunk)
            if vid and title:
                vids[vid.group(1)] = json.loads('"' + title.group(1) + '"')
    return vids


def do_videos():
    """O agente arruma em programas os vídeos que as regras fixas não reconhecem."""
    known = load("videos.json", {})
    titles = channel_titles()
    todo = {v: t for v, t in titles.items()
            if v not in known and not any(w in up(t) for w in RULE_WORDS) and not re.search(r"\bEP\.?\s*\d+", up(t))}
    if not todo or not GEMINI_KEY:
        return
    todo = dict(list(todo.items())[:40])  # aos poucos, para não esgotar nem demorar
    res = gemini(
        "Estes são títulos de vídeos do canal de YouTube 'Benfica Independente'. Para cada um, diz a que programa pertence. "
        "Usa um destes nomes se servir: " + ", ".join(PROGRAMS) + ". Se for claramente uma série nova, usa um nome curto "
        "para ela; se não pertencer a nenhum programa, usa \"Outros vídeos\". Devolve um objeto JSON {id: programa}.\n\n"
        + json.dumps(todo, ensure_ascii=False)
    )
    if isinstance(res, dict):
        for k, v in res.items():
            if k in todo and isinstance(v, str) and 0 < len(v) < 40:
                known[k] = v
        save("videos.json", known)
        status["agente"]["videos"] = len(res)


def main():
    os.makedirs(DATA, exist_ok=True)
    status["agente"]["ativo"] = bool(GEMINI_KEY)
    for step in (do_agenda, do_results, do_videos):
        try:
            step()
        except Exception as e:
            log(step.__name__, "falhou:", type(e).__name__, e)
    status["atualizado"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(os.path.join(DATA, "status.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(status, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    sys.exit(main())
