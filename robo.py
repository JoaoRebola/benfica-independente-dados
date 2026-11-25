"""
Robô do Benfica Independente.

Corre sozinho no GitHub (de 30 em 30 minutos) e prepara para a app:
  data/agenda.json    - próximos jogos de todas as modalidades
  data/results.json   - últimos resultados das equipas principais
  data/videos.json    - programa de cada vídeo do canal que as regras não reconhecem
  data/summaries.json - crónica de cada jogo de futebol já terminado
  data/club.json      - ficha do clube e do estádio (TheSportsDB)
  data/history.json   - história e palmarés de todas as modalidades (Wikipédia)
  data/podcast_videos.json - episódio do podcast -> vídeo do YouTube (API oficial)
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
    ("Andebol Feminino", "{698B2A89-1519-40B6-85E6-DD80B3325693}", False),
    ("Voleibol", "{61FF7D89-5327-4A5E-82C8-6E4CD35D8314}", True),
    ("Voleibol Feminino", "{61FF7D89-5327-4A5E-82C8-6E4CD35D8314}", False),
    # O site do clube chama-lhe "Râguebi"; a agenda e a app dizem "Rugby". O feminino usa o MESMO identificador com IsMaleTeam=false
    # (o identificador próprio "Râguebi feminino" não filtra: devolve jogos de todas as modalidades).
    ("Rugby", "{4F2A2B71-D436-4BF2-AB38-4D191F48862C}", True),
    ("Rugby Feminino", "{4F2A2B71-D436-4BF2-AB38-4D191F48862C}", False),
]

# Modalidades em que um marcador "0 - 0" é um jogo sem resultado registado (no râguebi não há 0-0): não entra na tabela.
SEM_ZEROS = {"Rugby", "Rugby Feminino"}

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
        try:
            fem = fpa_agenda(fpa_andebol("F"), "F")
            if fem:
                events = [e for e in events if not (e.get("sport") == "Andebol" and e.get("detail", "").rstrip().endswith("(F)"))] + fem
                status["robo"]["agenda_andebol_feminino"] = len(fem)
        except Exception as e:
            log("agenda: andebol feminino (federação) falhou:", e)
        try:
            masc = [e for e in fpa_agenda(fpa_andebol("M"), "M") if not ja_tem(events, e)]
            if masc:
                events += masc
                status["robo"]["agenda_andebol_masculino"] = len(masc)
        except Exception as e:
            log("agenda: andebol masculino (federação) falhou:", e)
        try:
            form = [e for e in fpa_agenda_formacao() if not ja_tem(events, e)]
            if form:
                events += form
                status["robo"]["agenda_andebol_formacao"] = len(form)
        except Exception as e:
            log("agenda: andebol camadas jovens (federação) falhou:", e)
        for chave, desporto, fonte in (("hoquei", "Hóquei em Patins", fpp_hoquei), ("basquetebol", "Basquetebol", fpb_basquetebol)):
            try:
                novos = [e for e in modalidade_agenda(fonte(), desporto) if not ja_tem(events, e)]
                if novos:
                    events += novos
                    status["robo"]["agenda_" + chave] = len(novos)
            except Exception as e:
                log("agenda: %s (federação) falhou:" % chave, e)
        events.sort(key=lambda e: e["t"])
        save("agenda.json", events)
        status[who]["agenda"] = len(events)
    log("agenda:", len(events), "por", who)


# ---------------------------------------------------------------- Federação de Andebol (andebol feminino)
# O site do clube nem sempre tem os jogos do andebol feminino. A federação publica tudo em ws.fpa.pt.
FPA = "https://ws.fpa.pt/index.php/"
FPA_EPOCA_PAGINA = "https://portal.fpa.pt/competicoes-nacionais-andebol-feminino/"
SEMANA = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"]
MESES_PT = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro", "novembro", "dezembro"]
FPA_TROCAS = [("Taca Portugal", "Taça de Portugal"), ("Taca da Liga", "Taça da Liga"), ("Supertaca", "Supertaça"), ("Divisao", "Divisão"), ("Eliminatoria", "Eliminatória")]


def fpa_get(caminho):
    return json.loads(get(FPA + caminho, timeout=60))


def fpa_epoca():
    """Número da época em curso na federação (a primeira da lista do site); 374 = 2026/27."""
    try:
        m = re.search(r'"id":"season","label":"[^"]*","options":\[\{"term_id":"(\d+)"', get(FPA_EPOCA_PAGINA, timeout=60))
        if m:
            return m.group(1)
    except Exception:
        pass
    return "374"


def fpa_nome(texto):
    """'SL BENFICA' -> 'SL Benfica', 'ABC DE BRAGA /OBO' -> 'ABC de Braga /OBO'."""
    pequenas = {"de", "da", "do", "dos", "das", "e"}

    def troca(m):
        w = m.group(0)
        if w.lower() in pequenas:
            return w.lower()
        return w.upper() if len(w) <= 3 else w.capitalize()
    return re.sub(r"\s+", " ", re.sub(r"[A-Za-zÀ-ÿ]+", troca, texto.strip())).strip()


def nome_competicao(nome):
    """O mesmo campeonato com o mesmo nome venha de onde vier (clube ou federação): assim os menus da app não o repetem."""
    n = re.sub(r"\s+", " ", nome).strip()
    trocas = [
        (r"^Campeonato Betclic (Masculino|Feminino)$", r"Campeonato Nacional \1"),
        (r"^Campeonato Placard HP.*$", "Campeonato Nacional Masculino"),
        (r"^CN Feminino (Norte|Centro|Sul)$", "Campeonato Nacional Feminino"),
        (r"^Super ?Ta[çc]a Feminina.*$", "Supertaça Feminina"),
        (r"^Super ?Ta[çc]a Ant[óo]nio Livramento.*$", "Supertaça Masculina"),
        (r"^Super ?Ta[çc]a Kempa (Masculina|Feminina)$", r"Supertaça \1"),
        (r"^Ta[çc]a de Portugal Betclic (Masculina|Feminina)$", r"Taça de Portugal \1"),
    ]
    for rx, novo in trocas:
        if re.match(rx, n, re.I):
            return re.sub(rx, novo, n, flags=re.I)
    return n


def fpa_prova(descricao):
    nome = re.sub(r"^PO\.?\d+[A-Z]?\s*-\s*", "", descricao).strip()
    for a, b in FPA_TROCAS:
        nome = nome.replace(a, b)
    return nome_competicao(re.sub(r"\s+", " ", nome))


def fpa_andebol_feminino():
    return fpa_andebol("F")


def fpa_andebol(genero="F"):
    """Jogos da equipa principal do Benfica (campeonato, taça e supertaça), jogados e por jogar. genero: "F" ou "M"."""
    epoca = fpa_epoca()
    jogos = []
    for c in fpa_get("getCompeticoes/%s/8/%s/" % (epoca, genero)):
        if c.get("ESC_DESCRICAO") != "Seniores " + genero or "Honra" in c.get("PRO_DESCRICAO", ""):
            continue                      # só a equipa principal (a Divisão de Honra é da equipa B)
        prova = fpa_prova(c["PRO_DESCRICAO"])
        for f in fpa_get("getFases/" + c["ID_PROVA"]):
            for g in fpa_get("getResultadosCalendario/%s/%s" % (c["ID_PROVA"], f["ID_FASE"])):
                casa, fora = (g.get("EQUIPA_CASA") or "").strip().upper(), (g.get("EQUIPA_FORA") or "").strip().upper()
                if "SL BENFICA" in (casa, fora):
                    g["_prova"] = prova
                    jogos.append(g)
    return jogos


def _fpa_jogado(g):
    return str(g.get("JOG_GOLOS_CASA") or "").strip() != "" and str(g.get("JOG_GOLOS_FORA") or "").strip() != ""


def fpa_resultados(jogos):
    """No mesmo formato dos resultados do site do clube (os 10 mais recentes)."""
    feitos = sorted([g for g in jogos if _fpa_jogado(g)], key=lambda g: (g["JOG_DATA"], g.get("JOG_HORA") or ""), reverse=True)[:10]
    saida = []
    for g in feitos:
        d = datetime.strptime(g["JOG_DATA"], "%Y-%m-%d")
        saida.append({
            "s": "Andebol Feminino", "c": g["_prova"], "h": fpa_nome(g["EQUIPA_CASA"]), "a": fpa_nome(g["EQUIPA_FORA"]),
            "hg": int(g["JOG_GOLOS_CASA"]), "ag": int(g["JOG_GOLOS_FORA"]), "v": fpa_nome(g.get("REC_NOME") or ""),
            "d": "%s %02d %s" % (SEMANA[d.weekday()], d.day, MESES_PT[d.month - 1]),
        })
    return saida


def _palavras(t):
    return {w for w in re.findall(r"[a-z]{4,}", unicodedata.normalize("NFD", t.lower()).encode("ascii", "ignore").decode()) if w not in ("benfica", "vs")}


def ja_tem(eventos, novo):
    """Já há na agenda um jogo do mesmo desporto, no mesmo dia, com um adversário parecido?"""
    dia = datetime.fromtimestamp(novo["t"] / 1000, LISBON).date()
    for e in eventos:
        if e.get("sport") != novo.get("sport"):
            continue
        if datetime.fromtimestamp(e["t"] / 1000, LISBON).date() == dia and _palavras(e.get("title", "")) & _palavras(novo["title"]):
            return True
    return False


def fpa_agenda_formacao():
    """Todos os jogos por jogar das outras equipas do Benfica no andebol (camadas jovens e equipa B), da agenda geral da federação."""
    agora = time.time() - 3 * 3600
    saida = []
    for g in fpa_get("getAgendaJogosFAP/"):
        casa, fora = (g.get("EQUIPA_CASA") or "").upper(), (g.get("EQUIPA_FORA") or "").upper()
        if "SL BENFICA" not in casa and "SL BENFICA" not in fora:
            continue
        try:
            quando = datetime.strptime(g["JOG_DATA"] + " " + (g.get("JOG_HORA") or "00:00")[:5], "%Y-%m-%d %H:%M").replace(tzinfo=LISBON)
        except Exception:
            continue
        if quando.timestamp() < agora:
            continue
        prova = fpa_prova(g.get("PROVA") or "")
        fase = (g.get("FASE") or "").strip()
        saida.append({
            "t": int(quando.timestamp() * 1000),
            "title": "%s vs %s" % (fpa_nome(g["EQUIPA_CASA"]), fpa_nome(g["EQUIPA_FORA"])),
            "detail": " - ".join(x for x in (prova, fase) if x) + " (%s)" % (g.get("PRO_SEXO") or "M"),
            "loc": fpa_nome(g.get("LOCAL") or ""),
            "sport": "Andebol",
        })
    return saida


def fpa_agenda(jogos, genero="F"):
    """Jogos por jogar, no formato da agenda do clube."""
    agora = time.time() - 3 * 3600
    saida = []
    for g in jogos:
        if _fpa_jogado(g):
            continue
        try:
            quando = datetime.strptime(g["JOG_DATA"] + " " + (g.get("JOG_HORA") or "00:00")[:5], "%Y-%m-%d %H:%M").replace(tzinfo=LISBON)
        except Exception:
            continue
        if quando.timestamp() < agora:
            continue
        jornada = ("Jornada %s" % g["PJO_NUM_JORNADA"]) if g.get("PJO_NUM_JORNADA") else ""
        saida.append({
            "t": int(quando.timestamp() * 1000),
            "title": "%s vs %s" % (fpa_nome(g["EQUIPA_CASA"]), fpa_nome(g["EQUIPA_FORA"])),
            "detail": " - ".join(x for x in (g["_prova"], jornada) if x) + " (%s)" % genero,
            "loc": fpa_nome(g.get("REC_NOME") or ""),
            "sport": "Andebol",
        })
    return saida


# ---------------------------------------------------------------- Federação de Hóquei em Patins (hp.fpp.pt)
HP = "https://hp.fpp.pt"


def _limpa(t):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", t))).strip()


def fpp_competicoes():
    """(id, nome) de todas as competições da época em curso, lidas da página inicial (muda sozinha de época)."""
    comps = []
    for caminho in sorted(set(re.findall(r'href="(/\d+/\d+)"', get(HP + "/", timeout=60)))):
        pag = get(HP + caminho, timeout=60)
        for cid, nome in re.findall(r'href="/Competicao/(\d+)"[^>]*>.*?<p>(.*?)</p>', pag, re.S):
            comps.append((cid, _limpa(nome)))
    return comps


def fpp_hoquei():
    """Jogos do SL Benfica (equipas "SL Benfica" exatas, sem a B) em todas as competições de hóquei da época."""
    jogos = []
    for cid, nome in fpp_competicoes():
        try:
            pag = get(HP + "/Competicao/" + cid, timeout=60)
        except Exception:
            continue
        jornada = ""
        for tr in pag.split("<tr")[1:]:
            mj = re.search(r"(\d+)\s*[ªa]?\s*Jornada", _limpa(tr)) if "coluna-data" not in tr else None
            if mj:
                jornada = "Jornada " + mj.group(1)
                continue
            quando = re.search(r"(\d{4}-\d{2}-\d{2})\s*<br>\s*(\d{2}:\d{2})", tr)
            equipas = [_limpa(x) for x in re.findall(r'<p class="visit[^"]*">(.*?)</p>', tr, re.S)]
            if not quando or len(equipas) != 2 or "SL Benfica" not in equipas:
                continue
            res = re.search(r'class="resultado"[^>]*>\s*(\d+)\s*-\s*(\d+)', tr)
            jogos.append({"data": quando.group(1), "hora": quando.group(2), "casa": equipas[0], "fora": equipas[1], "prova": nome_competicao(nome),
                          "jornada": jornada, "gc": int(res.group(1)) if res else None, "gf": int(res.group(2)) if res else None})
    return jogos


def _genero_prova(nome):
    return "F" if re.search(r"feminin", nome, re.I) else "M"


def modalidade_resultados(jogos, desporto, limite=10):
    """No formato dos resultados do clube; o feminino vai para "<desporto> Feminino"."""
    saida = []
    for g in sorted([g for g in jogos if g["gc"] is not None], key=lambda g: (g["data"], g["hora"]), reverse=True):
        fem = _genero_prova(g["prova"]) == "F"
        if re.search(r"sub[\s-]?\d|juniores|juvenis|iniciados", g["prova"], re.I):
            continue                           # formação não tem resultados na lista
        d = datetime.strptime(g["data"], "%Y-%m-%d")
        saida.append({"s": desporto + (" Feminino" if fem else ""), "c": g["prova"], "h": g["casa"], "a": g["fora"], "hg": g["gc"], "ag": g["gf"],
                      "v": "", "d": "%s %02d %s" % (SEMANA[d.weekday()], d.day, MESES_PT[d.month - 1])})
    por = {}
    for r in saida:
        por.setdefault(r["s"], []).append(r)
    return [r for lista in por.values() for r in lista[:limite]]


def modalidade_agenda(jogos, desporto):
    agora = time.time() - 3 * 3600
    saida = []
    for g in jogos:
        if g["gc"] is not None:
            continue
        try:
            quando = datetime.strptime(g["data"] + " " + g["hora"], "%Y-%m-%d %H:%M").replace(tzinfo=LISBON)
        except Exception:
            continue
        if quando.timestamp() < agora:
            continue
        saida.append({"t": int(quando.timestamp() * 1000), "title": "%s vs %s" % (g["casa"], g["fora"]),
                      "detail": " - ".join(x for x in (g["prova"], g.get("jornada", "")) if x) + " (%s)" % _genero_prova(g["prova"]),
                      "loc": g.get("local", ""), "sport": desporto})
    return saida


# ---------------------------------------------------------------- Federação de Basquetebol (fpb.pt)
FPB = "https://www.fpb.pt"
MES3 = {"JAN": 1, "FEV": 2, "MAR": 3, "ABR": 4, "MAI": 5, "JUN": 6, "JUL": 7, "AGO": 8, "SET": 9, "OUT": 10, "NOV": 11, "DEZ": 12}


def fpb_basquetebol():
    """Próximos jogos do SL Benfica nas competições da federação (a página do calendário mostra as próximas jornadas)."""
    base = get(FPB + "/competicao/11440/", timeout=60)
    ids = sorted(set(re.findall(r'href="/calendario/(\d+)"', base)) | set(re.findall(r'href="/competicao/(\d+)"', base)) | {"11440"})
    jogos = []
    for cid in ids:
        try:
            pag = get(FPB + "/calendario/" + cid, timeout=60)
        except Exception:
            continue
        datas = [(m.start(), m.group(1), m.group(2), m.group(3)) for m in re.finditer(r"(\d{1,2})\s+(JAN|FEV|MAR|ABR|MAI|JUN|JUL|AGO|SET|OUT|NOV|DEZ)\s+(\d{4})", pag)]
        for m in re.finditer(r'<a href="/ficha-de-jogo\?internalID=(\d+)"(.*?)(?=<a href="/ficha-de-jogo|<footer|$)', pag, re.S):
            bloco = m.group(2)
            siglas = [_limpa(x) for x in re.findall(r'<span class="sigla">(.*?)</span>', bloco, re.S)]
            if len(siglas) < 2 or "SL Benfica" not in siglas[:2]:
                continue
            hora = re.search(r"(\d{2})H(\d{2})", bloco)
            anteriores = [d for d in datas if d[0] < m.start()]
            if not hora or not anteriores:
                continue
            _p, dia, mes, ano = anteriores[-1]
            local = re.search(r"<b>\s*(.*?)\s*</b>", bloco, re.S)
            comp = re.search(r'<div class="competition">\s*<span>(.*?)</span>', bloco, re.S)
            prova = _limpa(comp.group(1)) if comp else ""
            if not prova or "<?" in (comp.group(1) if comp else ""):
                prova = re.search(r"(Liga[^<]*|Ta[çc]a[^<]*|Supertaça[^<]*)", _limpa(bloco)).group(1).strip() if re.search(r"(Liga[^<]*|Ta[çc]a[^<]*|Supertaça[^<]*)", _limpa(bloco)) else ""
            jogos.append({"data": "%04d-%02d-%02d" % (int(ano), MES3[mes], int(dia)), "hora": "%s:%s" % hora.groups(), "casa": siglas[0], "fora": siglas[1],
                          "prova": prova, "jornada": "", "local": _limpa(local.group(1)) if local else "", "gc": None, "gf": None, "fpb": m.group(1)})
    unicos = {j["fpb"]: j for j in jogos}
    return list(unicos.values())


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


YOUTH_RX = re.compile(r"\bsub[\s-]?\d|juniores|juvenis|iniciados|infantis|benjamins|traquinas|liga jovem|youth|next gen|revela[çc][ãa]o|\bB\b", re.I)


def genero_evento(e):
    """'F', 'M' ou None, pelo que o próprio jogo da agenda diz (ex.: "... Jornada 12 (F)")."""
    d = (e.get("detail") or "").strip()
    if re.search(r"\(F\)\s*$", d) or re.search(r"femin|women", d, re.I):
        return "F"
    if re.search(r"\(M\)\s*$", d) or re.search(r"masculin", d, re.I):
        return "M"
    return None


def modalidades_da_agenda():
    """Modalidades e géneros que têm jogos da equipa principal na agenda mas não estão na lista fixa dos resultados.

    Devolve (extras, sem_resultados): extras = modalidades novas que se sabe obter (mesmo identificador da modalidade);
    sem_resultados = nomes que a agenda tem mas que a origem não permite obter. Assim, se o clube passar a ter uma
    equipa nova (como aconteceu com o andebol feminino), os resultados aparecem sem ser preciso mexer no código."""
    ids = {n: sid for n, sid, _m in SPORTS}
    conhecidos = set(ids)
    extras, sem, vistos = [], [], set()
    for e in load("agenda.json", []):
        sp = (e.get("sport") or "").strip()
        if not sp or not re.search(r"\bvs\b", e.get("title", ""), re.I):
            continue
        if YOUTH_RX.search((e.get("title") or "") + " " + (e.get("detail") or "")):
            continue                       # formação e equipa B não têm resultados na lista
        g = genero_evento(e)
        base = sp[:-len(" Feminino")] if sp.endswith(" Feminino") else sp
        nome = sp if sp.endswith(" Feminino") else (base + " Feminino" if g == "F" else base)
        if nome in conhecidos or nome in vistos:
            continue
        vistos.add(nome)
        sid = ids.get(base) or ids.get(sp)
        if sid:
            extras.append((nome, sid, not nome.endswith(" Feminino")))
        else:
            sem.append(nome)
    return extras, sem


def filtrar_incoerencias(lista):
    """Rede de segurança: um resultado cuja competição diz o contrário do género da modalidade não entra
    (ex.: "Campeonato Nacional Masculino" na lista do andebol feminino). Devolve (limpa, tirados por género, tirados por 0-0)."""
    fem = re.compile(r"femin|women", re.I)
    mas = re.compile(r"masculin|\bmen\b", re.I)
    boa, tirados, zeros = [], [], []
    for r in lista:
        c = r.get("c", "")
        if r["s"] in SEM_ZEROS and r.get("hg") == 0 and r.get("ag") == 0:
            zeros.append(r)
            continue
        feminina = r["s"].endswith(" Feminino")
        if (feminina and mas.search(c) and not fem.search(c)) or (not feminina and fem.search(c) and not mas.search(c)):
            tirados.append(r)
        else:
            boa.append(r)
    return boa, tirados, zeros


# ---------------------------------------------------------------- futebol: equipa B e camadas jovens (resultados)
FUT_MASC = "{ECCFEB41-A0FD-4830-A3BB-7E57A0A15D00}"
FUT_FEM = "{37A610A0-2CCD-4F89-A589-A1F995F8FCB5}"
# (escalão, identificador da equipa no site do clube, masculino?)
FUT_CATEGORIAS = [
    ("Equipa B", "72660737-f4f6-4c76-864f-d1ca2f545913", True),
    ("Sub-23", "62d5a1a6-fae4-4a4a-871d-bd92831f9dbb", True),
    ("Sub-19", "808fa96a-376b-4402-9686-813fcd769edd", True),
    ("Sub-17", "37200436-af91-424d-8e2c-8659a0dc71d8", True),
    ("Sub-16", "ea04962e-4b80-41e6-aa64-731f111b175d", True),
    ("Sub-15", "a8f08244-7fa6-4e6d-99ae-faab1b2c136c", True),
    ("Equipa B", "6c7c8f78-4d64-4ca2-a3a4-392cd8ac3f03", False),
    ("Sub-19", "872be730-26ca-427d-ada4-a09919fb9487", False),
]


def epoca_atual():
    """Ex.: ("26/27", "2026/27") em novembro de 2026."""
    h = datetime.now(LISBON)
    a = h.year if h.month >= 7 else h.year - 1
    return "%02d/%02d" % (a % 100, (a + 1) % 100), "%d/%02d" % (a, (a + 1) % 100)


def one_categoria(cat):
    """Resultados de um escalão (só os desta época: os antigos não interessam). Devolve (cat, lista ou None)."""
    k, rank, male = cat
    sport = "Futebol" if male else "Futebol Feminino"
    body = {"filters": {"Menu": "last", "Modality": FUT_MASC if male else FUT_FEM, "IsMaleTeam": male, "Rank": rank,
                        "Tournaments": [], "Seasons": [], "PageNumber": 0}}
    curta, longa = epoca_atual()
    for _ in range(2):
        try:
            page = post_json(RESULTS_URL, body, timeout=150)
            out = []
            for r in parse_results(page, sport):
                if curta in r["c"] or longa in r["c"]:
                    r["k"] = k
                    out.append(r)
            return cat, out
        except Exception as e:
            log("resultados", sport, k, "falhou:", type(e).__name__)
            time.sleep(5)
    return cat, None



def do_results():
    old = load("results.json", [])
    extras, sem = modalidades_da_agenda()
    if extras:
        log("resultados: modalidades novas na agenda:", ", ".join(n for n, _s, _m in extras))
    status["robo"]["sem_resultados"] = sem        # a plataforma avisa quando há jogos sem resultados possíveis
    sports = SPORTS + extras
    with ThreadPoolExecutor(max_workers=len(sports)) as pool:
        parts = list(pool.map(one_sport, sports))
    if all(p[1] is None for p in parts):
        log("resultados: nada (mantém os anteriores)")
        return
    merged = []
    for name, got, who in parts:
        if got is None or (not got and any(r["s"] == name and not r.get("k") for r in old)):
            merged += [r for r in old if r["s"] == name and not r.get("k")]  # falhou: mantém o que havia
        else:
            merged += got
            if got:
                status[who]["resultados_" + name] = len(got)
    if not any(r["s"] == "Andebol Feminino" for r in merged):
        try:
            fem = fpa_resultados(fpa_andebol_feminino())
            if fem:
                merged += fem
                status["robo"]["resultados_Andebol Feminino"] = len(fem)
                log("resultados: andebol feminino vindo da federação:", len(fem))
        except Exception as e:
            log("resultados: andebol feminino (federação) falhou:", e)
    try:
        ja = {x["s"] for x in merged}
        for r in modalidade_resultados(fpp_hoquei(), "Hóquei em Patins"):
            if r["s"] not in ja:
                merged.append(r)
                status["robo"]["resultados_" + r["s"]] = status["robo"].get("resultados_" + r["s"], 0) + 1
    except Exception as e:
        log("resultados: hóquei (federação) falhou:", e)
    try:
        with ThreadPoolExecutor(max_workers=len(FUT_CATEGORIAS)) as pool2:
            cats = list(pool2.map(one_categoria, FUT_CATEGORIAS))
        for (k, rank, male), got in cats:
            sport = "Futebol" if male else "Futebol Feminino"
            if got is None:
                merged += [r for r in old if r.get("k") == k and r["s"] == sport]      # falhou: mantém o que havia
            else:
                merged += got
                if got:
                    status["robo"]["resultados_%s_%s" % (sport, k)] = len(got)
    except Exception as e:
        log("resultados: escalões do futebol falharam:", e)
    merged, tirados, zeros = filtrar_incoerencias(merged)
    if zeros:
        log("resultados: jogos sem resultado registado (0-0) deixados de fora:", len(zeros))
    if tirados:
        log("resultados: tirados por género incoerente:", [(r["s"], r["c"], r["h"], r["a"]) for r in tirados])
    status["robo"]["resultados_incoerentes"] = len(tirados)
    save("results.json", merged)
    log("resultados:", len(merged))


# ---------------------------------------------------------------- classificações (federações)
# Cada entrada: {"s": modalidade, "k": escalão ("" = equipa principal), "c": competição, "t": [[pos, equipa, pts, j, v, e, d, gm, gs], ...]}
FPF = "https://resultados.fpf.pt"
# (competição na FPF, modalidade, escalão, nome mostrado). Só entram as tabelas em que o Benfica aparece.
FPF_COMPETICOES = [
    ("29809", "Futebol", "Equipa B", "Segunda Liga"),
    ("29536", "Futebol", "Sub-23", "Liga Next Gen"),
    ("29534", "Futebol", "Sub-19", "Campeonato Nacional Sub-19"),
    ("29866", "Futebol", "Sub-19", "Campeonato Nacional Sub-19 II Divisão"),
    ("29535", "Futebol", "Sub-17", "Campeonato Nacional Sub-17"),
    ("29858", "Futebol", "Sub-17", "Campeonato Nacional Sub-17 II Divisão"),
    ("29836", "Futebol", "Sub-15", "Campeonato Nacional Sub-15"),
    ("29990", "Futebol", "Sub-15", "Campeonato Nacional Sub-15 II Divisão"),
    ("29839", "Futebol Feminino", "", "Liga BPI"),
    ("29910", "Futebol Feminino", "Equipa B", "Campeonato Nacional Feminino II Divisão"),
    ("29872", "Futebol Feminino", "Sub-19", "Liga Feminina Sub-19"),
    ("29873", "Futebol Feminino", "Sub-17", "Liga Feminina Sub-17"),
    ("29817", "Futsal", "", "Liga Placard"),
    ("29897", "Futsal Feminino", "", "Liga Feminina Placard"),
]
SIGLAS = {"sl", "fc", "sc", "cd", "gd", "ud", "cf", "ad", "cs", "cp", "ac", "af", "aj", "gc", "cdr", "ccd", "scp", "sad", "ca", "cr", "ccr", "gdc", "gdr", "gs"}


def nome_equipa(n):
    """"Sl Benfica, Sad" -> "SL Benfica"; "Fc Porto Sad" -> "FC Porto"."""
    n = re.sub(r"[,\s]+SAD\s*$", "", html.unescape(n).strip(), flags=re.I)
    return " ".join(w.upper() if w.lower() in SIGLAS else w for w in n.split())


def _num(x):
    try:
        return int(str(x).strip().replace("+", ""))
    except Exception:
        return 0


def fpf_classificacoes():
    """Tabelas das competições da FPF onde o Benfica joga (a página mostra a classificação do grupo de cada fase)."""
    saida = []
    lista = get(FPF + "/Competition", timeout=60)
    epoca = dict(re.findall(r"competitionId=(\d+)&amp;seasonId=(\d+)", lista))
    for cid, desporto, k, nome in FPF_COMPETICOES:
        if cid not in epoca:
            continue                                  # a competição ainda não está na época em curso
        pag = None
        time.sleep(4)                                  # o site da federação recusa pedidos seguidos demais
        for _tentativa in range(4):
            try:
                pag = get("%s/Competition/Details?competitionId=%s&seasonId=%s" % (FPF, cid, epoca[cid]), timeout=60)
                break
            except Exception as e:
                log("classificações FPF", cid, "falhou:", type(e).__name__, getattr(e, "code", ""))
                time.sleep(15 * (_tentativa + 1))
        if pag is None:
            continue
        for bloco_ in pag.split('<div id="classification">')[1:]:
            linhas = []
            for row in bloco_.split("game classification")[1:]:
                row = row.split("</div></div>")[0] if False else row
                cel = [_limpa(x) for x in re.findall(r'<div class="col[^>]*>(.*?)</div>', row, re.S)]
                if len(cel) < 9:
                    continue
                linhas.append([_num(cel[0]), nome_equipa(cel[1]), _num(cel[8]), _num(cel[2]), _num(cel[3]), _num(cel[4]), _num(cel[5]), _num(cel[6]), _num(cel[7])])
            if linhas and any(l[1] == "SL Benfica" for l in linhas):
                saida.append({"s": desporto, "k": k, "c": nome, "t": linhas, "src": "fpf"})
                break                                  # o grupo do Benfica
    return saida


def fpa_classificacoes():
    """Andebol (seniores masculino e feminino): campeonatos onde o SL Benfica joga."""
    saida = []
    epoca = fpa_epoca()
    for genero, desporto in (("F", "Andebol Feminino"), ("M", "Andebol")):
        for c in fpa_get("getCompeticoes/%s/8/%s/" % (epoca, genero)):
            if c.get("ESC_DESCRICAO") != "Seniores " + genero or "Honra" in c.get("PRO_DESCRICAO", ""):
                continue
            prova = fpa_prova(c["PRO_DESCRICAO"])
            if re.search(r"Ta[çc]a|Super", prova, re.I):
                continue
            for f in fpa_get("getFases/" + c["ID_PROVA"]):
                try:
                    tab = fpa_get("getClassificacoes/%s/%s" % (c["ID_PROVA"], f["ID_FASE"]))
                except Exception:
                    continue
                linhas = [[_num(r.get("PEQ_CLASS_CLASSIFICACAO")), fpa_nome(r.get("CLU_NOME") or ""), _num(r.get("PEQ_CLASS_PONTOS")), _num(r.get("PEQ_CLASS_NUMERO_JOGOS")),
                           _num(r.get("PEQ_CLASS_NUMERO_VITORIAS")), _num(r.get("PEQ_CLASS_NUMERO_EMPATES")), _num(r.get("PEQ_CLASS_NUMERO_DERROTAS")),
                           _num(r.get("PEQ_CLASS_GOLOS_MARCADOS")), _num(r.get("PEQ_CLASS_GOLOS_SOFRIDOS"))] for r in tab]
                if linhas and any(l[1] == "SL Benfica" for l in linhas):
                    saida.append({"s": desporto, "k": "", "c": prova, "t": sorted(linhas, key=lambda l: l[0]), "src": "fpa"})
    return saida


def fpp_classificacoes():
    """Hóquei em patins (seniores): classificação de cada competição onde o SL Benfica joga."""
    saida = []
    for cid, nome in fpp_competicoes():
        if re.search(r"sub[\s-]?\d|juniores|juvenis|iniciados|ta[çc]a|cup|super", nome, re.I):
            continue
        try:
            pag = get(HP + "/Classificacao/" + cid, timeout=60)
        except Exception:
            continue
        linhas = []
        for tr in pag.split("<tr")[1:]:
            pos = re.search(r'class="classificacao">\s*<p>(\d+)</p>', tr)
            if not pos:
                continue
            clube = re.search(r'<p>([^<]+?)\s*</p>\s*</a>', tr)
            nums = [_num(x) for x in re.findall(r"<td>\s*(-?\d+)\s*</td>", tr)]
            if not clube or len(nums) < 7:
                continue
            p_, j, v, e, d, gm, gs = nums[:7]
            linhas.append([int(pos.group(1)), _limpa(clube.group(1)), p_, j, v, e, d, gm, gs])
        if linhas and any(l[1] == "SL Benfica" for l in linhas):
            fem = _genero_prova(nome) == "F"
            saida.append({"s": "Hóquei em Patins" + (" Feminino" if fem else ""), "k": "", "c": nome_competicao(nome), "t": linhas, "src": "fpp"})
    return saida


# Escalões do futebol de formação: o próprio site do clube publica a classificação de cada um.
CLUBE_ESCALOES = [("Sub-23", "sub-23"), ("Sub-19", "juniores"), ("Sub-17", "juvenis"), ("Sub-15", "iniciados")]


def clube_classificacoes():
    saida = []
    for k, slug in CLUBE_ESCALOES:
        try:
            pag = get("https://www.slbenfica.pt/pt-pt/futebol-formacao/%s/classificacao" % slug, timeout=60)
        except Exception as e:
            log("classificações clube", slug, "falhou:", type(e).__name__)
            continue
        for tab in pag.split("<table")[1:]:
            tab = tab.split("</table>")[0]
            cap = re.search(r"<caption>(.*?)</caption>", tab, re.S)
            nome = _limpa(cap.group(1)) if cap else "Campeonato"
            nome = re.sub(r"^Classifica[çc][ãa]o d[oa]s? ", "", nome)
            nome = re.sub(r"\s+da [ée]poca \d{4}/\d{2}", "", nome).strip()
            linhas = []
            for tr in tab.split("<tr")[1:]:
                tds = [_limpa(x) for x in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
                if len(tds) < 9:
                    continue
                equipa = "SL Benfica" if tds[1].strip().upper() == "BENFICA" else tds[1]
                linhas.append([_num(tds[0]), equipa, _num(tds[8]), _num(tds[2]), _num(tds[3]), _num(tds[4]), _num(tds[5]), _num(tds[6]), _num(tds[7])])
            if linhas and any(l[1] == "SL Benfica" for l in linhas):
                saida.append({"s": "Futebol", "k": k, "c": nome, "t": linhas, "src": "clube"})
    return saida


def do_standings():
    old = load("classificacoes.json", [])
    novo, falhou = [], set()
    for fonte, fn in (("clube", clube_classificacoes), ("fpa", fpa_classificacoes), ("fpp", fpp_classificacoes)):
        try:
            got = fn()
            if got:
                novo += got
                log("classificações", fonte, len(got))
            else:
                falhou.add(fonte)
        except Exception as e:
            falhou.add(fonte)
            log("classificações", fonte, "falhou:", type(e).__name__, e)
    chaves = {(e["s"], e["k"], e["c"]) for e in novo}
    for e in old:
        if e.get("src") in falhou and (e["s"], e["k"], e["c"]) not in chaves:
            novo.append(e)          # a fonte falhou: mantém o que havia
    if novo:
        save("classificacoes.json", novo)
        status["robo"]["classificacoes"] = len(novo)
    log("classificações:", len(novo))



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
    highlights = []  # sem resumos em vídeo (decisão do utilizador)
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
            old["video"] = ""
            old["title"], old["text"], old["agent"] = "", "", False
        need_facts = not old or old.get("v") != 2
        need_text = False  # sem crónicas escritas pelo agente (decisão do utilizador): só os factos do jogo
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
            "title": "", "text": "", "agent": False, "v": 2,
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


# ================================================================ podcasts -> vídeos
# Liga cada episódio do podcast ao vídeo do YouTube correspondente, com a lista
# COMPLETA do canal (API oficial do YouTube, chave gratuita no segredo
# YOUTUBE_API_KEY). A app usa isto para "ouvir em podcast" tocando o vídeo com a
# imagem escondida (conta como visualização). Sem chave, não faz nada.
YOUTUBE_KEY = os.environ.get("YOUTUBE_API_KEY", "").strip()
PODCAST_FEED = "https://anchor.fm/s/12b150a0/podcast/rss"
_LINK_STOP = set("o a os as de do da dos das e em no na nos nas com por para um uma ao vs x the of and ep episodio parte live direto sl s".split())


def _tokens(text):
    plain = unicodedata.normalize("NFD", text.lower())
    plain = "".join(c for c in plain if unicodedata.category(c) != "Mn")
    return {t for t in re.findall(r"[a-z0-9]+", plain) if len(t) > 1 and t not in _LINK_STOP}


def _yt_api(path, **params):
    params["key"] = YOUTUBE_KEY
    q = "&".join(f"{k}={urllib.request.quote(str(v))}" for k, v in params.items())
    return json.loads(get(f"https://www.googleapis.com/youtube/v3/{path}?{q}", timeout=30))


_CANAL_VIDEOS = None


def _channel_videos():
    global _CANAL_VIDEOS
    if _CANAL_VIDEOS is not None:
        return _CANAL_VIDEOS
    """Todos os vídeos do canal: {id: (título, data em segundos)}."""
    uploads = "UU" + CHANNEL[2:]
    vids, token = {}, ""
    for _ in range(80):  # até 4000 vídeos
        params = {"part": "snippet,contentDetails", "playlistId": uploads, "maxResults": 50}
        if token:
            params["pageToken"] = token
        page = _yt_api("playlistItems", **params)
        for it in page.get("items", []):
            vid = it.get("contentDetails", {}).get("videoId")
            title = it.get("snippet", {}).get("title", "")
            when = it.get("contentDetails", {}).get("videoPublishedAt") or it.get("snippet", {}).get("publishedAt", "")
            if vid and when:
                vids[vid] = (title, datetime.fromisoformat(when.replace("Z", "+00:00")).timestamp())
        token = page.get("nextPageToken")
        if not token:
            break
    _CANAL_VIDEOS = vids
    return vids


def _episodes():
    from email.utils import parsedate_to_datetime
    root = ET.fromstring(get(PODCAST_FEED, timeout=60))
    eps = []
    for item in root.iter("item"):
        guid = (item.findtext("guid") or "").strip()
        title = (item.findtext("title") or "").strip()
        desc = item.findtext("description") or ""
        try:
            when = parsedate_to_datetime(item.findtext("pubDate")).timestamp()
        except Exception:
            continue
        if guid and title:
            eps.append((guid, title, when, desc))
    return eps


def do_podcast_links():
    if not YOUTUBE_KEY:
        return
    vids = _channel_videos()
    eps = _episodes()
    day = 86400
    used, links = set(), {}
    # 1) O episódio que tem o link do vídeo na descrição.
    for guid, title, when, desc in eps:
        for vid in re.findall(r"(?:youtu\.be/|v=)([\w-]{11})", desc):
            if vid in vids:
                links[guid] = vid
                used.add(vid)
                break
    # 2) Títulos parecidos, mesmos números e o episódio a sair perto do vídeo.
    vt = {v: (_tokens(t), d) for v, (t, d) in vids.items()}
    for guid, title, when, desc in eps:
        if guid in links:
            continue
        et = _tokens(title)
        if not et:
            continue
        best, score = None, 0.0
        for v, (tok, d) in vt.items():
            if v in used or not tok:
                continue
            days = (when - d) / day
            if days < -2 or days > 14:
                continue
            nums = {t for t in tok if any(c.isdigit() for c in t)}
            if not nums <= et:
                continue
            s = len(tok & et) / len(tok | et)
            if s > score:
                best, score = v, s
        if best and score >= 0.6:
            links[guid] = best
            used.add(best)
    if links:
        save("podcast_videos.json", links)
    status["robo"]["podcasts_com_video"] = len(links)
    status["robo"]["podcasts_total"] = len(eps)


# ================================================================ chat (Supabase)
# Confirma que o chat e o formulário de contacto da app estão a funcionar (uma
# consulta normal à função pública bi_ping). Também mantém o projeto ativo no
# plano grátis do Supabase, que pausa projetos sem uso. A chave é a pública.
SUPABASE_URL = "https://rpqqucqxgjkisoimstfh.supabase.co"
SUPABASE_PUBLICA = "sb_publishable_InvgB931-Wc8vFs65WWXxw_KVpMLGhH"


def do_chat_check():
    req = urllib.request.Request(
        SUPABASE_URL + "/rest/v1/rpc/bi_ping", data=b"{}", method="POST",
        headers={"apikey": SUPABASE_PUBLICA, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        info = json.loads(r.read().decode("utf-8"))
    status["robo"]["chat"] = {"ok": bool(info.get("ok")), "conversas": info.get("conversas", 0)}
    log("chat:", status["robo"]["chat"])


# ---------------------------------------------------------------- Comunidade BI (posts)
# Posts da comunidade do canal do YouTube que NÃO são vídeos nem avisos de diretos/episódios
# (esses já chegam pelas notificações da app). O mesmo texto noutra rede aparece só uma vez.
AVISO_DIRETO = re.compile(
    r"\bem direto\b|\bdireto\b|\bdirecto\b|\blive\b|estaremos|entramos|vamos estar|watchalong|rescaldo|antevis|"
    r"\bep\.?\s*\d|epis[oó]dio|podcast|noite de megafone|youtu\.?be|\bjá dispon[ií]vel\b|\bnovo v[ií]deo\b",
    re.IGNORECASE)


def _posts_youtube():
    page = get(f"https://www.youtube.com/channel/{CHANNEL}/posts", timeout=60)
    m = re.search(r"var ytInitialData = (\{.*?\});</script>", page)
    if not m:
        raise ValueError("sem ytInitialData")
    found = []

    def walk(o):
        if isinstance(o, dict):
            if "backstagePostRenderer" in o:
                found.append(o["backstagePostRenderer"])
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(json.loads(m.group(1)))
    posts = []
    for p in found:
        texto = "".join(r.get("text", "") for r in p.get("contentText", {}).get("runs", [])).strip()
        att = p.get("backstageAttachment", {}) or {}
        imgs = att.get("backstageImageRenderer", {}).get("image", {}).get("thumbnails") or []
        if not imgs:
            multi = att.get("postMultiImageRenderer", {}).get("images") or []
            if multi:
                imgs = multi[0].get("backstageImageRenderer", {}).get("image", {}).get("thumbnails") or []
        video = "videoRenderer" in att or "pollRenderer" in att
        img = max(imgs, key=lambda t: t.get("width", 0)).get("url", "") if imgs else ""
        if img.startswith("//"):
            img = "https:" + img
        quando = rel_time((p.get("publishedTimeText", {}).get("runs") or [{}])[0].get("text", ""))
        posts.append({"id": "yt:" + p["postId"], "rede": "youtube", "texto": texto, "imagem": img,
                      "url": "https://www.youtube.com/post/" + p["postId"], "quando": quando or 0, "video": video})
    return posts


def _chave_texto(t):
    t = unicodedata.normalize("NFD", t.lower())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return " ".join(re.findall(r"[a-z0-9]{3,}", t))[:160]


def do_comunidade():
    antigos = {p["id"]: p for p in load("redes.json", [])}
    decisoes = load("redes_agente.json", {})          # id -> True (mostra) / False (é aviso de vídeo/direto)
    novos = _posts_youtube()
    log("comunidade: %d posts lidos" % len(novos))
    candidatos = []
    for p in novos:
        if not p["imagem"].startswith("https://") or p.pop("video"):
            decisoes[p["id"]] = False
            continue
        if p["id"] in antigos:                         # mantém a data da primeira vez que foi visto
            p["quando"] = antigos[p["id"]].get("quando") or p["quando"]
        if AVISO_DIRETO.search(p["texto"]):
            decisoes[p["id"]] = False
        candidatos.append(p)
    # O agente decide os que as regras deixaram passar (só uma vez por post).
    duvida = [p for p in candidatos if p["id"] not in decisoes]
    if duvida and GEMINI_KEY:
        r = gemini(
            "És o agente da app do Benfica Independente (canal de YouTube de adeptos do Benfica). "
            "Para cada post da comunidade abaixo responde se deve aparecer na secção 'Comunidade BI'. "
            "Fica FORA (false) se for um aviso/anúncio de um vídeo, de um direto, de um episódio de podcast ou programa "
            "(ex.: 'hoje às 22h estamos em direto', 'novo episódio', rescaldo, antevisão, watchalong). "
            "Fica DENTRO (true) tudo o resto: efemérides, fotos, opiniões, perguntas aos adeptos, curiosidades, homenagens. "
            'Responde só JSON: {"<id>": true|false, ...}\n\n'
            + json.dumps([{"id": p["id"], "texto": p["texto"][:600]} for p in duvida], ensure_ascii=False))
        if isinstance(r, dict):
            for p in duvida:
                if isinstance(r.get(p["id"]), bool):
                    decisoes[p["id"]] = r[p["id"]]
    # Sem agente (ou sem resposta): passa o que as regras deixaram passar.
    mostrar = [p for p in candidatos if decisoes.get(p["id"], True)]
    # Também guarda os que já tinham sido mostrados e saíram da página do canal (até 20 no total).
    ids = {p["id"] for p in mostrar}
    mostrar += [p for p in antigos.values() if p["id"] not in ids and decisoes.get(p["id"], True)]
    # O mesmo post em redes diferentes (mesmo texto): fica só o primeiro.
    vistos, final = set(), []
    for p in sorted(mostrar, key=lambda x: x.get("quando") or 0, reverse=True):
        k = _chave_texto(p["texto"]) or p["imagem"]
        if k in vistos:
            continue
        vistos.add(k)
        final.append({c: p[c] for c in ("id", "rede", "texto", "imagem", "url", "quando")})
    final = final[:20]
    save("redes.json", final)
    save("redes_agente.json", {k: v for k, v in decisoes.items() if k in {p["id"] for p in novos} or k in ids})
    status["robo"]["comunidade"] = len(final)
    log("comunidade: %d posts na app" % len(final))


# ---------------------------------------------------------------- Quiz do BI
# Duas perguntas por semana (terça e sexta) sobre a história gloriosa do Benfica e os seus grandes
# jogadores. O agente escreve como um benfiquista fanático e conhecedor; não precisa de aprovação.
def do_quiz():
    import random
    from zoneinfo import ZoneInfo
    banco = load("quiz.json", [])
    agora = datetime.now(timezone.utc)
    lisboa = agora.astimezone(ZoneInfo("Europe/Lisbon"))
    ultimo = max((q.get("quando", 0) for q in banco), default=0)
    dias = (agora.timestamp() * 1000 - ultimo) / 86400000
    # Primeira vez: logo uma pergunta. Depois: terça e sexta a partir das 10h (ou se passaram 5 dias).
    devida = (not banco) or (dias >= 3 and lisboa.weekday() in (1, 4) and lisboa.hour >= 10) or dias >= 5
    if not devida:
        log("quiz: ainda não é altura da próxima pergunta")
        return
    if not GEMINI_KEY:
        log("quiz: sem agente")
        return
    recentes = [q["pergunta"] for q in banco[-60:]]
    r = gemini(
        "És um benfiquista fanático e enorme conhecedor da história do Sport Lisboa e Benfica, e escreves o quiz do "
        "Benfica Independente (canal de adeptos). Cria UMA pergunta de escolha múltipla sobre a história gloriosa do "
        "Benfica e dos seus grandes jogadores e treinadores (Eusébio, Águias de Ouro, Taça dos Campeões Europeus de 1961 e 1962, "
        "Béla Guttmann, Coluna, José Águas, Humberto Coelho, Chalana, Rui Costa, Nuno Gomes, Oliveira, Cardozo, Jonas, etc.; "
        "títulos, finais, golos míticos, recordes, estádios, curiosidades). "
        "REGRAS: só factos de que tens certeza absoluta e que são fáceis de confirmar; se tiveres dúvidas, escolhe outro facto. "
        "Português de Portugal, tom de adepto apaixonado mas rigoroso. Nunca fales de Sporting nem de FC Porto. "
        "A pergunta deve ser curta e clara; as 4 opções plausíveis e curtas, só uma certa. "
        "A explicação conta em 3 a 5 frases a história ou o momento por trás da resposta, com emoção e com factos exatos (datas, números). "
        "Muda de tema em relação às perguntas recentes e não repitas nenhuma. "
        'Responde só JSON: {"pergunta": "...", "opcoes": ["...", "...", "...", "..."], "certa": 0, "explicacao": "..."} '
        "(certa é o índice 0 a 3 da opção certa).\n\nPerguntas recentes (não repetir):\n" + json.dumps(recentes, ensure_ascii=False))
    if not isinstance(r, dict):
        log("quiz: o agente não respondeu")
        return
    op = r.get("opcoes")
    if (not isinstance(op, list) or len(op) != 4 or not isinstance(r.get("certa"), int) or not 0 <= r["certa"] <= 3
            or not str(r.get("pergunta", "")).strip() or len(str(r.get("explicacao", "")).strip()) < 40
            or len({str(o).strip().lower() for o in op}) != 4):
        log("quiz: resposta do agente inválida")
        return
    if str(r["pergunta"]).strip() in recentes:
        log("quiz: pergunta repetida")
        return
    ops = [str(o).strip() for o in op]
    certa_txt = ops[r["certa"]]
    random.shuffle(ops)                       # a certa não fica sempre na mesma posição
    banco.append({"id": "q" + agora.strftime("%Y%m%d%H%M"), "quando": int(agora.timestamp() * 1000),
                  "pergunta": str(r["pergunta"]).strip(), "opcoes": ops, "certa": ops.index(certa_txt),
                  "explicacao": str(r["explicacao"]).strip()})
    save("quiz.json", banco[-80:])
    status["robo"]["quiz"] = len(banco)
    log("quiz: nova pergunta (%d no banco)" % len(banco))


# ---------------------------------------------------------------- Resultado e onze reais dos jogos
# O Supabase lê este ficheiro sozinho para contar quem acertou nos prognósticos e no "Quem vai jogar?".
# Os nomes do onze são convertidos para os nomes do plantel (os mesmos que os adeptos escolhem na app).
ESPN = "https://site.api.espn.com/apis/site/v2/sports/soccer"
BENFICA_ID = "1929"


def _plano(t):
    t = unicodedata.normalize("NFD", t.lower())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return [x for x in re.split(r"[^a-z0-9]+", t) if len(x) >= 3]


def _nome_plantel(nome, plantel):
    """Nome ESPN (ex.: 'Vangelis Pavlidis') -> nome do plantel ('Pavlidis'). Só se houver um único."""
    t = _plano(nome)
    if not t:
        return nome
    por_apelido = [p for p in plantel if t[-1] in _plano(p)]
    if len(por_apelido) == 1:
        return por_apelido[0]
    por_nome = [p for p in plantel if _plano(p) and all(x in t for x in _plano(p))]
    if len(por_nome) == 1:
        return por_nome[0]
    return nome


def do_jogos_reais():
    plantel = []
    try:
        for j in json.loads(get("https://benficaindependente.github.io/jogadores.json", timeout=60)):
            if j.get("equipa") == "Equipa principal":
                plantel.append(j["nome"])
    except Exception as e:
        log("jogos reais: sem plantel (%s)" % e)
    agora = datetime.now(timezone.utc)
    ids = {}
    for url in (f"{ESPN}/all/teams/{BENFICA_ID}/schedule?fixture=true", f"{ESPN}/por.1/teams/{BENFICA_ID}/schedule"):
        try:
            for e in json.loads(get(url, timeout=60)).get("events", []):
                quando = datetime.strptime(e["date"].replace("Z", "")[:16], "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc)
                if 0 < (agora - quando).total_seconds() < 6 * 86400:
                    ids[e["id"]] = quando
        except Exception as e:
            log("jogos reais: agenda falhou (%s)" % e)
    antigos = {x["jogo"]: x for x in load("jogos_reais.json", [])}
    lista = []
    for eid in ids:
        try:
            d = json.loads(get(f"{ESPN}/all/summary?event={eid}", timeout=60))
            comp = d["header"]["competitions"][0]
            if comp["status"]["type"]["state"] != "post":
                continue
            eq = {c["team"]["id"]: c for c in comp["competitors"]}
            if BENFICA_ID not in eq or len(eq) != 2:
                continue
            rival = next(i for i in eq if i != BENFICA_ID)
            casa = next(c for c in comp["competitors"] if c.get("homeAway") == "home")
            fora = next(c for c in comp["competitors"] if c.get("homeAway") == "away")
            onze = []
            for r in d.get("rosters", []):
                if r.get("team", {}).get("id") == BENFICA_ID:
                    onze = [_nome_plantel(p["athlete"]["displayName"], plantel) for p in r.get("roster", []) if p.get("starter")]
            if len(onze) != 11:
                continue
            lista.append({"jogo": eid, "titulo": "%s - %s" % (casa["team"]["displayName"], fora["team"]["displayName"]),
                          "golos_b": int(eq[BENFICA_ID]["score"]), "golos_r": int(eq[rival]["score"]), "onze": onze})
        except Exception as e:
            log("jogos reais: %s falhou (%s)" % (eid, e))
    for x in lista:
        antigos[x["jogo"]] = x
    final = sorted(antigos.values(), key=lambda x: x["jogo"])[-10:]
    save("jogos_reais.json", final)
    status["robo"]["jogos_reais"] = len(final)
    log("jogos reais: %d" % len(final))


def _ms_iso(texto):
    """ISO 8601 do YouTube -> milissegundos (0 se não houver)."""
    if not texto:
        return 0
    try:
        return int(datetime.fromisoformat(str(texto).replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return 0


def do_live():
    """Estado do direto para a app: 'live', 'upcoming' ou 'none'.

    Vê os vídeos recentes do canal (API oficial) e a página dos diretos.
    A app lê data/live.json e avisa quando o direto começa de verdade.
    """
    if not YOUTUBE_KEY:
        return
    ids = list(channel_titles().keys())          # inclui diretos marcados
    try:
        for vid in list(_channel_videos().keys())[:30]:
            if vid not in ids:
                ids.append(vid)
    except Exception as e:
        log("live: lista de videos falhou:", e)
    ids = ids[:50]
    if not ids:
        return
    dados = _yt_api("videos", part="snippet,liveStreamingDetails", id=",".join(ids), maxResults=50)
    vivo, marcado = None, None
    for it in dados.get("items", []):
        lsd = it.get("liveStreamingDetails") or {}
        sn = it.get("snippet") or {}
        estado = sn.get("liveBroadcastContent")
        info = {
            "id": it.get("id", ""),
            "titulo": sn.get("title", ""),
            "agendado": _ms_iso(lsd.get("scheduledStartTime")),
            "inicio": _ms_iso(lsd.get("actualStartTime")),
        }
        if estado == "live":
            vivo = info
        elif estado == "upcoming":
            if marcado is None or (info["agendado"] and info["agendado"] < (marcado["agendado"] or 10 ** 15)):
                marcado = info
    escolhido = vivo or marcado or {}
    estado = "live" if vivo else ("upcoming" if marcado else "none")
    novo = {
        "estado": estado,
        "id": escolhido.get("id", ""),
        "titulo": escolhido.get("titulo", ""),
        "agendado": int(escolhido.get("agendado") or 0),
        "inicio": int(escolhido.get("inicio") or 0),
    }
    # Só grava quando o estado muda (ou de tempos a tempos, para a app saber que está em dia):
    # assim não há uma gravação nova a cada execução do robô.
    antigo = load("live.json", {})
    try:
        idade = (datetime.now(timezone.utc) - datetime.strptime(antigo.get("atualizado", ""), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)).total_seconds()
    except Exception:
        idade = 10 ** 9
    if not (all(antigo.get(k) == v for k, v in novo.items()) and idade < 100 * 60):
        novo["atualizado"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        save("live.json", novo)
    status["robo"]["live"] = estado
    log("live: %s %s" % (estado, escolhido.get("titulo", "")))


def main():
    os.makedirs(DATA, exist_ok=True)
    status["agente"]["ativo"] = bool(GEMINI_KEY)
    for step in (do_agenda, do_results, do_standings, do_videos, do_live, do_summaries, do_club, do_history, do_podcast_links, do_comunidade, do_quiz, do_jogos_reais, do_chat_check):
        try:
            step()
        except Exception as e:
            log(step.__name__, "falhou:", type(e).__name__, e)
    status["atualizado"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(os.path.join(DATA, "status.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(status, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    sys.exit(main())
