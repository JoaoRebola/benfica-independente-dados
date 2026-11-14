"""
Robô do Benfica Independente.

Corre sozinho no GitHub (de 30 em 30 minutos) e prepara para a app:
  data/agenda.json    - próximos jogos de todas as modalidades
  data/results.json   - últimos resultados das equipas principais
  data/videos.json    - programa de cada vídeo do canal que as regras não reconhecem
  data/highlights.json - vídeos oficiais de resumos (todas as modalidades)
  data/summaries.json - crónica de cada jogo de futebol já terminado
  data/club.json      - ficha do clube e do estádio (TheSportsDB)
  data/history.json   - história e palmarés de todas as modalidades (Wikipédia)
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
import urllib.error
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
            _model = [n[3] for n in sorted(names, reverse=True)][:4]
    except Exception as e:
        log("não consegui ver os modelos:", type(e).__name__)
    _model = _model or [GEMINI_MODEL]
    log("agente: modelos", ", ".join(_model))
    return _model


def gemini(prompt, timeout=90):
    """Pergunta ao Gemini e devolve JSON. Devolve None se não houver chave ou falhar."""
    if not GEMINI_KEY:
        return None
    for model in pick_model():
        result = _ask_model(model, prompt, timeout)
        if result is not _BUSY:
            return result
    return None


_BUSY = object()


def _ask_model(model, prompt, timeout):
    """Um modelo. Devolve _BUSY se estiver ocupado/lento (tenta-se o seguinte)."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    def ask(fast):
        config = {"responseMimeType": "application/json", "temperature": 0}
        if fast:
            # Pensar pouco: estas tarefas são simples e assim responde em segundos.
            config["thinkingConfig"] = {"thinkingLevel": "low"}
        body = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": config}
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json", "x-goog-api-key": GEMINI_KEY},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())

    started = time.time()
    try:
        try:
            d = ask(True)
        except urllib.error.HTTPError as e:
            if e.code != 400:
                raise
            d = ask(False)  # modelo que não aceita a opção "pensar pouco"
        log("agente (%s) respondeu em %.0f s" % (model, time.time() - started))
        status["agente"]["modelo"] = model
        text = d["candidates"][0]["content"]["parts"][0]["text"]
        status["agente"]["ok"] = True
        return json.loads(text)
    except urllib.error.HTTPError as e:
        # 429 = acabaram os pedidos grátis do dia deste modelo; 503 = ocupado: tenta o seguinte.
        log("agente (%s) indisponível: %s" % (model, e.code))
        status["agente"]["erro"] = e.code
        return _BUSY if e.code in (404, 429, 500, 503) else None
    except Exception as e:
        log("agente (%s) falhou: %s" % (model, type(e).__name__))
        status["agente"]["erro"] = type(e).__name__
        return _BUSY


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


# ================================================================ separador Benfica
# Tudo de fontes públicas e gratuitas, sem contas:
#   highlights.json  - vídeos de resumos/golos (canal oficial do clube e canais oficiais das ligas)
#   summaries.json   - crónica de cada jogo de futebol já terminado (dados da ESPN), escrita pelo agente
import xml.etree.ElementTree as ET

HIGHLIGHT_CHANNELS = [
    # (canal, nome, o que se aceita). Só canais oficiais cujos vídeos tocam fora do YouTube (testado).
    # Os resumos do futebol masculino da Liga e das Taças estão bloqueados pelos donos dos direitos.
    ("UC8zrah5cNf2c3jKKeD_Z3fw", "clube"),   # Sport Lisboa e Benfica (todas as modalidades)
    ("UC1pS02BkdhdCAlzKjcZTksQ", "dazn"),    # DAZN Portugal (Liga Europa)
    ("UCsIoK3XP-cVcpoCC_SdjZdg", "fpf"),     # Federação Portuguesa de Futebol (feminino, futsal, formação)
    ("UC5F7T3y7PF_1A3JvHfgm5rQ", "ehf"),     # EHF (andebol europeu)
]
ESPN = "https://site.api.espn.com/apis/site/v2/sports/soccer"
COMPETITIONS = {
    "por.1": "Liga Portugal", "uefa.europa": "Liga Europa", "uefa.europa_qual": "Liga Europa (pré-eliminatória)",
    "uefa.champions": "Liga dos Campeões", "uefa.champions_qual": "Liga dos Campeões (pré-eliminatória)",
    "por.taca.portugal": "Taça de Portugal", "por.league_cup": "Taça da Liga", "por.super_cup": "Supertaça",
    "club.friendly": "Particular",
}


HL_SPORTS = [
    ("HOQUEIBENFICAFEM", "Hóquei em Patins Feminino"), ("HOQUEI", "Hóquei em Patins"),
    ("BASKETBENFICAFEM", "Basquetebol Feminino"), ("BASKET", "Basquetebol"),
    ("LIGAFEMININAPLACARD", "Futsal Feminino"), ("LIGANEXTGEN", "Futebol Formação"), ("LIGAREVELACAO", "Futebol Formação"),
    ("FUTSALBENFICAFEM", "Futsal Feminino"), ("FUTSALFEM", "Futsal Feminino"), ("FUTSAL", "Futsal"),
    ("LIGAPLACARD", "Futsal"),
    ("ANDEBOLBENFICAFEM", "Andebol Feminino"), ("ANDEBOL", "Andebol"), ("ANDEBENFICA", "Andebol"),
    ("VOLEIBENFICAFEM", "Voleibol Feminino"), ("VOLEI", "Voleibol"),
    ("UWCL", "Futebol Feminino"), ("BENFICAFEM", "Futebol Feminino"), ("FEMININ", "Futebol Feminino"),
    ("LIGABPI", "Futebol Feminino"), ("TACAREVELACAO", "Futebol Formação"), ("SUB-23", "Futebol Formação"),
    ("SUB-19", "Futebol Formação"), ("JUNIORES", "Futebol Formação"), ("YOUTHLEAGUE", "Futebol Formação"),
]


def hl_sport(title, kind="clube"):
    t = up(title).replace(" ", "")
    if kind == "ehf":
        return "Andebol Feminino" if "WOMEN" in t else "Andebol"
    for word, sport in HL_SPORTS:
        if word in t:
            return sport
    return "Futebol"


def hl_accept(kind, title):
    U = up(title)
    if kind == "clube":
        # Futebol masculino do canal do clube: bloqueado fora do YouTube.
        return (U.startswith("HIGHLIGHTS") or U.startswith("RESUMO")) and hl_sport(title) != "Futebol"
    if "BENFICA" not in U:
        return False
    if kind == "ehf":
        return "HIGHLIGHTS" in U
    return "RESUMO" in U


REL = [("SEGUNDO", 1), ("MINUTO", 60), ("HORA", 3600), (" H", 3600), ("DIA", 86400), ("SEM", 7 * 86400),
       ("MES", 30 * 86400), ("ANO", 365 * 86400)]


def rel_time(text):
    """'há 3 sem.' -> milissegundos aproximados."""
    U = up(text or "")
    n = re.search(r"(\d+)", U)
    if not n:
        return None
    for word, secs in REL:
        if word in U:
            return int((time.time() - int(n.group(1)) * secs) * 1000)
    return None


def channel_search(channel, query="Benfica"):
    """Vídeos de um canal que falam do Benfica (inclui os mais antigos que já não estão no feed)."""
    page = get(f"https://www.youtube.com/channel/{channel}/search?query={query}",
               headers={"Cookie": "CONSENT=YES+1; SOCS=CAI"}, timeout=30)
    m = re.search(r"var ytInitialData = (\{.*?\});</script>", page)
    out = []
    if not m:
        return out

    def walk(o):
        if isinstance(o, dict):
            v = o.get("videoRenderer")
            if v:
                title = "".join(r.get("text", "") for r in v.get("title", {}).get("runs", []))
                t = rel_time(v.get("publishedTimeText", {}).get("simpleText"))
                if v.get("videoId") and title and t:
                    out.append((v["videoId"], title, t))
            for x in o.values():
                walk(x)
        elif isinstance(o, list):
            for x in o:
                walk(x)
    walk(json.loads(m.group(1)))
    return out


def do_highlights():
    known = {h["id"]: h for h in load("highlights.json", [])}
    ns = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
    for channel, kind in HIGHLIGHT_CHANNELS:
        found = []
        try:
            root = ET.fromstring(get("https://www.youtube.com/feeds/videos.xml?channel_id=" + channel, timeout=30))
            for e in root.findall("a:entry", ns):
                try:
                    t = int(datetime.fromisoformat(e.findtext("a:published", namespaces=ns)).timestamp() * 1000)
                except Exception:
                    continue
                found.append((e.findtext("yt:videoId", namespaces=ns), (e.findtext("a:title", namespaces=ns) or "").strip(), t))
        except Exception as ex:
            log("resumos", kind, "feed falhou:", type(ex).__name__)
        try:
            found += channel_search(channel)
        except Exception as ex:
            log("resumos", kind, "pesquisa falhou:", type(ex).__name__)
        year_ago = (time.time() - 300 * 86400) * 1000
        for vid, title, t in found:
            if not vid or vid in known or t < year_ago or not hl_accept(kind, title):
                continue
            known[vid] = {"id": vid, "title": title, "t": t, "sport": hl_sport(title, kind), "src": kind}
    # Retira o que entretanto ficou proibido (futebol masculino do canal do clube).
    for h in known.values():
        h["sport"] = hl_sport(h["title"], h.get("src", "clube"))
    out = sorted((h for h in known.values() if hl_accept(h.get("src", "clube"), h["title"])), key=lambda h: -h["t"])[:200]
    save("highlights.json", out)
    log("resumos em vídeo:", len(out))


def _team_words(name):
    return [w for w in re.findall(r"[A-Z]{3,}", up(name)) if w not in ("BENFICA", "SPORT", "LISBOA", "CLUBE", "FUTEBOL")]


def match_video(home, away, t, highlights):
    """O vídeo de resumo de um jogo: canal oficial, com o adversário no título, publicado até 3 dias depois."""
    other = away if "BENFICA" in up(home) else home
    words = _team_words(other)
    best = None
    for h in highlights:
        if h["sport"] != "Futebol" or not (t - 3 * 3600_000 <= h["t"] <= t + 3 * 86400_000):
            continue
        if words and any(w[:5] in up(h["title"]) for w in words):
            if best is None or (h["src"] == "clube") > (best["src"] == "clube"):
                best = h
    return best["id"] if best else ""


STAT_NAMES = [
    ("Possession", "Posse de bola (%)"), ("SHOTS", "Remates"), ("ON GOAL", "Remates à baliza"),
    ("Corner Kicks", "Cantos"), ("Fouls", "Faltas"), ("Offsides", "Foras de jogo"), ("Saves", "Defesas"),
    ("Yellow Cards", "Cartões amarelos"), ("Red Cards", "Cartões vermelhos"), ("Passes", "Passes"),
    ("Accurate Passes", "Passes certos"), ("Tackles", "Desarmes"), ("Interceptions", "Interceções"),
]
EVENT_TYPES = {"Goal": "golo", "Own Goal": "autogolo", "Penalty - Scored": "golo", "Yellow Card": "amarelo",
               "Red Card": "vermelho", "Substitution": "troca"}


def event_facts(event_id, league):
    s = json.loads(get(f"{ESPN}/{league}/summary?event={event_id}", timeout=40))
    comp = s["header"]["competitions"][0]
    teams = {c["homeAway"]: c for c in comp["competitors"]}
    home, away = teams["home"], teams["away"]
    facts = {
        "home": home["team"]["displayName"], "away": away["team"]["displayName"],
        "hs": int(home.get("score") or 0), "as": int(away.get("score") or 0),
        "homeLogo": (home["team"].get("logos") or [{}])[0].get("href", ""),
        "awayLogo": (away["team"].get("logos") or [{}])[0].get("href", ""),
        "venue": (s.get("gameInfo", {}).get("venue", {}) or {}).get("fullName", ""),
        "goals": [], "events": [], "stats": [], "lineups": [],
    }
    home_name = facts["home"]
    for k in s.get("keyEvents", []):
        typ = (k.get("type", {}) or {}).get("text", "")
        minute = (k.get("clock", {}) or {}).get("displayValue", "")
        people = [p.get("athlete", {}).get("displayName", "") for p in k.get("participants", [])]
        team = (k.get("team", {}) or {}).get("displayName", "")
        kind = "golo" if k.get("scoringPlay") and "Own" not in typ else EVENT_TYPES.get(typ.split(" - ")[0], "")
        if "Own" in typ:
            kind = "autogolo"
        if not kind:
            continue
        side = "h" if team == home_name else "a"
        ev = {"min": minute, "k": kind, "who": people[0] if people else "", "side": side}
        if kind == "troca" and len(people) > 1:
            ev["out"] = people[1]
        if kind == "golo" and len(people) > 1 and people[1]:
            ev["ast"] = people[1]
        if "Penalty" in typ:
            ev["pen"] = True
        facts["events"].append(ev)
        if kind in ("golo", "autogolo"):
            facts["goals"].append({"min": minute, "who": ev["who"], "team": team, "how": typ})
    box = {t.get("team", {}).get("displayName", ""): {st.get("label"): st.get("displayValue") for st in t.get("statistics", [])}
           for t in s.get("boxscore", {}).get("teams", [])}
    hb, ab = box.get(facts["home"], {}), box.get(facts["away"], {})
    for key, label in STAT_NAMES:
        if key in hb or key in ab:
            facts["stats"].append({"n": label, "h": hb.get(key, "0"), "a": ab.get(key, "0")})
    for r in s.get("rosters", []):
        side = "h" if r.get("homeAway") == "home" or r.get("team", {}).get("displayName") == home_name else "a"
        players = []
        for p in r.get("roster", []):
            players.append({
                "n": p.get("athlete", {}).get("displayName", ""), "no": p.get("jersey", ""),
                "pos": (p.get("position") or {}).get("abbreviation", ""), "s": bool(p.get("starter")),
                "in": bool(p.get("subbedIn")), "out": bool(p.get("subbedOut")),
            })
        facts["lineups"].append({"side": side, "formation": r.get("formation", ""), "players": players})
    return facts


def plain_summary(f):
    """Crónica simples quando o agente não está disponível."""
    txt = f"{f['home']} {f['hs']}-{f['as']} {f['away']}."
    if f["goals"]:
        txt += " Golos: " + "; ".join(f"{g['who']} ({g['team']}) {g['min']}" for g in f["goals"]) + "."
    return txt


def do_summaries():
    done = {m["id"]: m for m in load("summaries.json", [])}
    highlights = load("highlights.json", [])
    sched = json.loads(get(f"{ESPN}/all/teams/1929/schedule", timeout=40))
    finished = []
    for e in sched.get("events", []):
        st = e["competitions"][0].get("status", {}).get("type", {})
        if not st.get("completed"):
            continue
        league = (e.get("league", {}) or {}).get("slug") or ""
        t = int(datetime.fromisoformat(e["date"].replace("Z", "+00:00")).timestamp() * 1000)
        finished.append((t, e["id"], league))
    finished.sort(reverse=True)
    fetched = written = 0
    for t, eid, league in finished[:40]:
        old = done.get(eid)
        if old:
            old["video"] = match_video(old["home"], old["away"], t, highlights)
        need_facts = not old or old.get("v") != 2
        need_text = GEMINI_KEY and (not old or not old.get("agent")) and written < 4 and finished.index((t, eid, league)) < 15
        if not need_facts and not need_text:
            continue
        if need_facts and fetched >= 12:
            continue
        try:
            f = event_facts(eid, league or "por.1")
            fetched += 1
        except Exception as ex:
            log("crónica", eid, "falhou:", type(ex).__name__)
            continue
        comp = COMPETITIONS.get(league, "")
        text, title, agent = (old or {}).get("text", ""), (old or {}).get("title", ""), bool((old or {}).get("agent"))
        if need_text:
            res = gemini(
                "Escreve em português de Portugal a crónica (5 a 8 frases, tom de jornal desportivo, do ponto de vista de "
                "quem segue o Benfica mas sem exageros) deste jogo, usando APENAS estes dados: resultado, golos, "
                "cartões, substituições, estatísticas e onzes. Não inventes jogadas, nomes nem números que não estejam "
                "aqui. Devolve JSON {\"titulo\": ..., \"texto\": ...}.\n\n"
                + json.dumps({"competicao": comp, **{k: v for k, v in f.items() if k not in ("homeLogo", "awayLogo")}},
                             ensure_ascii=False)
            )
            if isinstance(res, dict) and res.get("texto"):
                text, title, agent = str(res["texto"]), str(res.get("titulo", ""))[:120], True
                written += 1
        if not text:
            text = plain_summary(f)
        done[eid] = {
            "id": eid, "t": t, "comp": comp, "home": f["home"], "away": f["away"], "hs": f["hs"], "as": f["as"],
            "homeLogo": f["homeLogo"], "awayLogo": f["awayLogo"], "venue": f["venue"],
            "goals": f["goals"], "events": f["events"], "stats": f["stats"], "lineups": f["lineups"],
            "title": title, "text": text, "agent": agent, "v": 2,
            "video": match_video(f["home"], f["away"], t, highlights),
        }
    out = sorted(done.values(), key=lambda m: -m["t"])[:40]
    save("summaries.json", out)
    status["agente" if any(m.get("agent") for m in out) else "robo"]["cronicas"] = len(out)
    log("crónicas:", len(out), "dados novos:", fetched, "textos novos:", written)


# ---------------------------------------------------------------- clube e estádio
SPORTSDB = "https://www.thesportsdb.com/api/v1/json/123"


def do_club():
    """Ficha do clube e do estádio (TheSportsDB, chave pública), uma vez por dia."""
    old = load("club.json", {})
    if old.get("dia") == datetime.now(LISBON).strftime("%Y-%m-%d"):
        return
    team = json.loads(get(SPORTSDB + "/lookupteam.php?id=134108", timeout=30))["teams"][0]
    venue = (json.loads(get(SPORTSDB + "/lookupvenue.php?id=" + (team.get("idVenue") or "17485"), timeout=30))
             .get("venues") or [{}])[0]
    club_text = (team.get("strDescriptionPT") or "").strip()
    stadium_text = (venue.get("strDescriptionPT") or "").strip() or old.get("estadioTexto", "")
    if not stadium_text and venue.get("strDescriptionEN") and GEMINI_KEY:
        res = gemini("Traduz para português de Portugal, sem acrescentar nada. Devolve JSON {\"texto\": ...}.\n\n"
                     + venue["strDescriptionEN"])
        if isinstance(res, dict) and res.get("texto"):
            stadium_text = str(res["texto"])
    club = {
        "dia": datetime.now(LISBON).strftime("%Y-%m-%d"),
        "nome": "Sport Lisboa e Benfica", "fundacao": team.get("intFormedYear") or "1904",
        "alcunhas": "Águias, Encarnados, Glorioso",
        "texto": club_text or (team.get("strDescriptionEN") or ""),
        "competicoes": [team.get("strLeague" + s) for s in ("", "2", "3", "4", "5", "6", "7") if team.get("strLeague" + s)],
        "estadio": venue.get("strVenue") or team.get("strStadium") or "Estádio da Luz",
        "estadioOficial": venue.get("strVenueAlternate", ""),
        "capacidade": venue.get("intCapacity", ""), "inauguracao": venue.get("intFormedYear", ""),
        "local": venue.get("strLocation", ""), "mapa": venue.get("strMap", ""),
        "estadioFoto": venue.get("strThumb", ""), "estadioTexto": stadium_text or (venue.get("strDescriptionEN") or ""),
        "site": team.get("strWebsite", ""), "facebook": team.get("strFacebook", ""),
        "instagram": team.get("strInstagram", ""), "twitter": team.get("strTwitter", ""),
        "youtube": team.get("strYoutube", ""),
    }
    save("club.json", club)
    log("clube: ok")


# ---------------------------------------------------------------- história e palmarés (Wikipédia, CC BY-SA)
WIKI = "https://pt.wikipedia.org/w/api.php?"
WIKI_AGENT = {"User-Agent": "BenficaIndependenteRobo/1.0 (https://github.com/JoaoRebola/benfica-independente-dados)"}


def wiki_html(title):
    import urllib.parse
    url = WIKI + urllib.parse.urlencode({"action": "parse", "page": title, "prop": "text", "redirects": 1,
                                         "format": "json", "formatversion": 2})
    req = urllib.request.Request(url, headers=WIKI_AGENT)
    with urllib.request.urlopen(req, timeout=60) as r:
        h = json.loads(r.read().decode())["parse"]["text"]
    h = re.sub(r"<(style|script)[^>]*>.*?</\1>", "", h, flags=re.S)
    return re.sub(r"<sup[^>]*>.*?</sup>", "", h, flags=re.S)


def cell_text(c):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", c))).strip()


def wiki_text(h):
    h = re.sub(r"<table.*?</table>", "", h, flags=re.S)
    h = re.sub(r"</p>|</li>|</h\d>|<br\s*/?>", "\n", h)
    t = html.unescape(re.sub(r"<[^>]+>", "", h))
    t = re.sub(r"\[editar[^\]]*\]", "", t)
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n\n", t)).strip()


SPORT_HEADS = {"FUTEBOL": "Futebol", "BASQUETEBOL": "Basquetebol", "HOQUEI EM PATINS": "Hóquei em Patins",
               "FUTSAL": "Futsal", "ANDEBOL": "Andebol", "VOLEIBOL": "Voleibol", "ATLETISMO": "Atletismo",
               "RAGUEBI": "Râguebi", "RUGBY": "Râguebi", "POLO AQUATICO": "Polo Aquático", "NATACAO": "Natação",
               "TENIS DE MESA": "Ténis de Mesa", "BILHAR": "Bilhar", "CICLISMO": "Ciclismo", "JUDO": "Judo"}


def parse_honours(h):
    """Lê as tabelas do palmarés: modalidade > masculino/feminino > âmbito > (competição, n.º, épocas)."""
    sport, gender, scope = "", "Masculino", ""
    out = {}
    for m in re.finditer(r"<h([2-4])[^>]*>(.*?)</h\1>|<tr[^>]*>(.*?)</tr>|<(?:p|b|th)[^>]*>\s*(Competições[^<]*)<", h, re.S):
        if m.group(2) is not None:
            head = cell_text(m.group(2))
            U = up(head)
            if U in SPORT_HEADS:
                sport, gender, scope = SPORT_HEADS[U], "Masculino", ""
            elif "FEMININ" in U:
                gender = "Feminino"
            elif "MASCULIN" in U:
                gender = "Masculino"
            elif "COMPETICOES" in U or "INTERNACION" in U or "NACION" in U or "REGION" in U:
                scope = head
            continue
        if m.group(4) is not None:
            scope = cell_text(m.group(4))
            continue
        cells = [cell_text(c) for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", m.group(3), re.S)]
        cells = [c for c in cells if c != ""]
        if len(cells) == 1 and "COMPETICOES" in up(cells[0]):
            scope = cells[0]
            continue
        if len(cells) >= 2 and cells[1].isdigit() and sport and up(cells[0]) not in ("COMPETICAO", "TOTAL"):
            key = sport + ("" if gender == "Masculino" else " Feminino")
            out.setdefault(key, []).append({
                "nome": cells[0], "n": int(cells[1]), "epocas": cells[2] if len(cells) > 2 else "",
                "ambito": "Internacional" if "INTERNACION" in up(scope) else ("Regional" if "REGION" in up(scope) else "Nacional"),
            })
    return [{"modalidade": k, "titulos": v, "total": sum(t["n"] for t in v)} for k, v in out.items()]


def do_history():
    """História e palmarés de todas as modalidades, a partir da Wikipédia (uma vez por semana)."""
    old = load("history.json", {})
    week = datetime.now(LISBON).strftime("%G-%V")
    if old.get("semana") == week and old.get("palmares") and (old.get("agente") or not GEMINI_KEY):
        return
    honours = parse_honours(wiki_html("Palmarés do Sport Lisboa e Benfica")) or old.get("palmares", [])
    main = wiki_text(wiki_html("Sport Lisboa e Benfica"))
    long_text = wiki_text(wiki_html("História do Sport Lisboa e Benfica"))
    chapters, milestones, agent = old.get("capitulos", []), old.get("marcos", []), bool(old.get("agente"))
    if GEMINI_KEY and not (agent and old.get("semana")):
        res = gemini(
            "Com base APENAS no texto abaixo (Wikipédia), escreve em português de Portugal a história do Sport Lisboa "
            "e Benfica para uma app de adeptos: 8 a 12 capítulos por ordem cronológica, cada um com título curto e "
            "texto de 4 a 7 frases, escrito por palavras tuas (não copies frases), sem inventar nada. Junta também "
            "uma lista de 15 a 25 marcos (ano e uma frase). Devolve JSON {\"capitulos\": [{\"titulo\":...,\"texto\":...}], "
            "\"marcos\": [{\"ano\":...,\"texto\":...}]}.\n\n" + (long_text or main)[:120000],
            timeout=180,
        )
        if isinstance(res, dict) and res.get("capitulos"):
            chapters = [{"titulo": str(c.get("titulo", ""))[:80], "texto": str(c.get("texto", ""))}
                        for c in res["capitulos"] if c.get("texto")]
            milestones = [{"ano": str(x.get("ano", ""))[:12], "texto": str(x.get("texto", ""))[:300]}
                          for x in res.get("marcos", []) if x.get("texto")]
            agent = True
    if not chapters:
        # Sem agente: a secção "História" do artigo principal, tal como está.
        i = main.find("História")
        part = main[i:i + 6000] if i >= 0 else main[:6000]
        chapters = [{"titulo": "História", "texto": p.strip()} for p in part.split("\n\n")[1:8] if len(p.strip()) > 120]
    save("history.json", {
        "semana": week, "agente": agent, "capitulos": chapters, "marcos": milestones, "palmares": honours,
        "fonte": "Wikipédia (pt.wikipedia.org), texto disponível sob a licença CC BY-SA 4.0",
    })
    log("história:", len(chapters), "capítulos; palmarés:", len(honours), "modalidades")


def main():
    os.makedirs(DATA, exist_ok=True)
    status["agente"]["ativo"] = bool(GEMINI_KEY)
    for step in (do_agenda, do_results, do_videos, do_highlights, do_summaries, do_club, do_history):
        try:
            step()
        except Exception as e:
            log(step.__name__, "falhou:", type(e).__name__, e)
    status["atualizado"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(os.path.join(DATA, "status.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(status, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    sys.exit(main())
