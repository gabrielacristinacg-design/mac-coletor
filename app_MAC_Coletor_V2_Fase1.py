import json
import re
import unicodedata
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from difflib import SequenceMatcher
from urllib.parse import urljoin, urlparse

import requests
import streamlit as st
from bs4 import BeautifulSoup, Tag

API_BASE = "https://api.cartola.globo.com"
CARTOLA_BRASIL_BASE = "https://www.cartolafcbrasil.com.br"
STATZ_BASE = "https://statz.ai"
STATZ_COMPETITION = f"{STATZ_BASE}/competitions/campeonato-brasileiro"
STATZ_FIXTURES = f"{STATZ_BASE}/competitions/campeonato-brasileiro/fixtures"

POS_MAP = {1: "GOL", 2: "LAT", 3: "ZAG", 4: "MEI", 5: "ATA", 6: "TEC"}
STATUS_MAP = {
    2: "Dúvida",
    3: "Suspenso",
    5: "Contundido",
    6: "Nulo",
    7: "Provável",
}

CARTOLA_CLUBE_SLUG = {
    "FLA": "flamengo", "PAL": "palmeiras", "SAN": "santos",
    "COR": "corinthians", "SAO": "sao-paulo", "BOT": "botafogo",
    "FLU": "fluminense", "VAS": "vasco", "CAM": "atletico-mg",
    "CRU": "cruzeiro", "GRE": "gremio", "INT": "internacional",
    "BAH": "bahia", "VIT": "vitoria", "CAP": "athletico-pr",
    "RBB": "bragantino", "CFC": "coritiba", "CHA": "chapecoense",
    "MIR": "mirassol", "REM": "remo",
}

STATZ_CLUBE_SLUG = {
    "FLA": "flamengo", "PAL": "palmeiras", "SAN": "santos",
    "COR": "corinthians", "SAO": "sao-paulo", "BOT": "botafogo",
    "FLU": "fluminense", "VAS": "vasco-da-gama", "CAM": "atletico-mineiro",
    "CRU": "cruzeiro", "GRE": "gremio", "INT": "internacional",
    "BAH": "bahia", "VIT": "vitoria", "CAP": "athletico-pr",
    "RBB": "bragantino", "CFC": "coritiba", "CHA": "chapecoense",
    "MIR": "mirassol", "REM": "remo",
}

STATZ_TEAM_TO_ABBR = {
    "flamengo": "FLA",
    "palmeiras": "PAL",
    "santos": "SAN",
    "corinthians": "COR",
    "sao paulo": "SAO",
    "botafogo": "BOT",
    "fluminense": "FLU",
    "vasco": "VAS",
    "vasco da gama": "VAS",
    "atletico mg": "CAM",
    "atletico mineiro": "CAM",
    "cruzeiro": "CRU",
    "gremio": "GRE",
    "internacional": "INT",
    "bahia": "BAH",
    "vitoria": "VIT",
    "athletico pr": "CAP",
    "athletico paranaense": "CAP",
    "bragantino": "RBB",
    "red bull bragantino": "RBB",
    "coritiba": "CFC",
    "chapecoense": "CHA",
    "mirassol": "MIR",
    "remo": "REM",
}

SCOUT_CODES = {
    "DS", "G", "A", "SG", "FS", "FF", "FD", "FT", "PS", "DE", "DP",
    "GC", "CV", "CA", "GS", "PP", "PC", "FC", "I", "V"
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
}

STATZ_FIELD_ALIASES = {
    "date": {"date"},
    "competition": {"competition"},
    "opponent": {"opponent", "opposition"},
    "venue": {"venue"},
    "score": {"score"},
    "result": {"result"},
    "position": {"position"},
    "minutes": {"minutes", "mins"},
    "goals": {"goals", "goal"},
    "assists": {"assists", "assist"},
    "shots": {"shots"},
    "shots_on_target": {"shots on target", "sot"},
    "crosses": {"crosses", "total crosses"},
    "tackles": {"tackles", "tackles won"},
    "interceptions": {"interceptions"},
    "aerials_won": {"aerials won", "aerial duels won"},
    "saves": {"saves"},
}

PRODUCTION_RULES = {
    "GOL": [
        ("defesas", "saves", True),
    ],
    "LAT": [
        ("desarmes", "tackles", True),
        ("cruzamentos", "crosses", True),
        ("chutes", "shots", True),
        ("chutes_no_alvo", "shots_on_target", True),
        ("gols", "goals", False),
        ("assistencias", "assists", False),
    ],
    "ZAG": [
        ("desarmes", "tackles", True),
        ("interceptacoes", "interceptions", True),
        ("duelos_aereos_ganhos", "aerials_won", True),
        ("chutes", "shots", True),
        ("chutes_no_alvo", "shots_on_target", True),
        ("gols", "goals", False),
        ("assistencias", "assists", False),
    ],
    "MEI": [
        ("gols", "goals", False),
        ("assistencias", "assists", False),
        ("finalizacoes_no_alvo", "shots_on_target", True),
    ],
    "ATA": [
        ("gols", "goals", False),
        ("assistencias", "assists", False),
        ("finalizacoes_no_alvo", "shots_on_target", True),
    ],
}


def clean(s):
    return re.sub(r"\s+", " ", s or "").strip()


def norm(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = clean(s).casefold()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def slugify(s):
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def parse_number(value):
    value = clean(value)
    if not value or value in {"-", "—", "–", "N/A", "n/a"}:
        return None
    value = value.replace("%", "").replace(",", "")
    m = re.search(r"-?\d+(?:\.\d+)?", value)
    if not m:
        return None
    x = float(m.group(0))
    return int(x) if x.is_integer() else x


def get_html(url, timeout=25):
    r = requests.get(url, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    return r


def get_json(url, timeout=25):
    r = requests.get(url, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    return r.json()


def carregar_mercado():
    status = get_json(f"{API_BASE}/mercado/status")
    mercado = get_json(f"{API_BASE}/atletas/mercado")
    clubes = get_json(f"{API_BASE}/clubes")

    club_map = {}
    if isinstance(clubes, dict):
        for k, v in clubes.items():
            if isinstance(v, dict):
                try:
                    cid = int(v.get("id", k))
                except Exception:
                    continue
                club_map[cid] = v

    atletas = mercado.get("atletas", mercado if isinstance(mercado, list) else [])
    return status, atletas, club_map


def atleta_para_alvo(a, club_map):
    clube = club_map.get(int(a["clube_id"]), {})
    abreviacao = clube.get("abreviacao") or clube.get("abreviacao_nome") or ""
    nome_clube = clube.get("nome") or ""
    apelido = clean(a.get("apelido") or a.get("nome") or "")
    posicao = POS_MAP.get(int(a.get("posicao_id", 0)))
    status_id = int(a.get("status_id", 0))
    status_nome = STATUS_MAP.get(status_id, f"Status {status_id}")

    club_slug = CARTOLA_CLUBE_SLUG.get(abreviacao) or slugify(nome_clube)
    atleta_slug = a.get("slug") or slugify(apelido)
    atleta_id = str(a["atleta_id"])

    return {
        "atleta_id": atleta_id,
        "nome_mercado": apelido,
        "nome_completo": clean(a.get("nome") or ""),
        "clube": nome_clube,
        "clube_abreviacao": abreviacao,
        "posicao": posicao,
        "status": status_nome,
        "status_id": status_id,
        "preco_mercado": a.get("preco_num"),
        "media_mercado": a.get("media_num"),
        "jogos_mercado": a.get("jogos_num"),
        "url_cartola_brasil": (
            f"{CARTOLA_BRASIL_BASE}/scouts/cartola-fc-2026/"
            f"atleta/{club_slug}/{atleta_id}/{atleta_slug}"
        ),
    }


# -----------------------------
# CARTOLA FC BRASIL / HISTÓRICO
# -----------------------------

def parse_entry(text, contexto):
    text = clean(text)
    m = re.search(
        r"(\d+)\s*x\s*(\d+)\s*\(Rodada\s*#\s*(\d+)\)\s*(-?\d+(?:[.,]\d+)?)\s*(.*)$",
        text, re.I
    )
    if not m:
        return None

    gols_a, gols_b, rodada, pontos, resto = m.groups()
    tokens = resto.split()
    scouts = {}
    i = 0

    while i + 1 < len(tokens):
        if (
            re.fullmatch(r"-?\d+(?:[.,]\d+)?", tokens[i])
            and tokens[i + 1].upper() in SCOUT_CODES
        ):
            qtd = float(tokens[i].replace(",", "."))
            if qtd.is_integer():
                qtd = int(qtd)
            scouts[tokens[i + 1].upper()] = qtd
            i += 2
        else:
            i += 1

    return {
        "rodada": int(rodada),
        "pontos": float(pontos.replace(",", ".")),
        "status": "pontuou",
        "contexto": contexto,
        "placar_exibido": f"{gols_a} x {gols_b}",
        "scouts": scouts,
    }


def section_entries(soup, titulo, contexto):
    heading = None
    for h in soup.find_all(["h2", "h3", "h4"]):
        if titulo.lower() in clean(h.get_text(" ", strip=True)).lower():
            heading = h
            break
    if not heading:
        return []

    entries, seen = [], set()
    for el in heading.next_elements:
        if isinstance(el, Tag) and el is not heading and el.name in ["h2", "h3", "h4"]:
            break
        if isinstance(el, Tag) and el.name == "a":
            txt = clean(el.get_text(" ", strip=True))
            if "(Rodada" in txt:
                parsed = parse_entry(txt, contexto)
                if parsed and parsed["rodada"] not in seen:
                    entries.append(parsed)
                    seen.add(parsed["rodada"])
    return entries


def metric(text, label):
    m = re.search(
        re.escape(label) + r"\s*:\s*(-?\d+(?:[.,]\d+)?)",
        text, re.I
    )
    return float(m.group(1).replace(",", ".")) if m else None


def total_games(text):
    m = re.search(r"Total:\s*(\d+)\s+J\b", text, re.I)
    return int(m.group(1)) if m else None


def fetch_cartola_history(target, rodada_referencia, janela, timeout=25):
    r = get_html(target["url_cartola_brasil"], timeout=timeout)
    final_url = r.url
    soup = BeautifulSoup(r.text, "html.parser")
    text = clean(soup.get_text(" ", strip=True))

    h2 = soup.find("h2")
    nome_site = clean(h2.get_text(" ", strip=True)) if h2 else None

    pos_clube = re.search(r"\((GOL|LAT|ZAG|MEI|ATA|TEC)\s*-\s*([^)]+)\)", text, re.I)
    posicao_site = pos_clube.group(1).upper() if pos_clube else None
    clube_site = clean(pos_clube.group(2)) if pos_clube else None

    m = re.search(r"/atleta/[^/]+/(\d+)/", final_url)
    id_url = m.group(1) if m else None

    inconsistencias = []
    if id_url != target["atleta_id"]:
        inconsistencias.append(
            f"ID mercado {target['atleta_id']} / site {id_url}"
        )
    if posicao_site != target["posicao"]:
        inconsistencias.append(
            f"posição mercado {target['posicao']} / site {posicao_site}"
        )

    n1, n2 = norm(target["nome_mercado"]), norm(nome_site)
    if n1 and n2 and not (n1 == n2 or n1 in n2 or n2 in n1):
        inconsistencias.append(
            f"nome mercado '{target['nome_mercado']}' / site '{nome_site}'"
        )

    if inconsistencias:
        raise ValueError("TRAVA DE IDENTIDADE: " + "; ".join(inconsistencias))

    casa = section_entries(soup, "Pontuações em Casa", "casa")
    fora = section_entries(soup, "Pontuações Fora", "fora")
    hist = sorted(casa + fora, key=lambda x: x["rodada"])

    inicio = max(1, int(rodada_referencia) - int(janela))
    fim = max(0, int(rodada_referencia) - 1)
    by_round = {x["rodada"]: x for x in hist}

    historico = []
    for rodada in range(inicio, fim + 1):
        if rodada in by_round:
            historico.append(by_round[rodada])
        else:
            historico.append({
                "rodada": rodada,
                "pontos": None,
                "status": "sem_registro",
                "contexto": None,
                "placar_exibido": None,
                "scouts": {},
            })

    return {
        "nome_site": nome_site or target["nome_mercado"],
        "clube_site": clube_site or target["clube"],
        "posicao_site": posicao_site or target["posicao"],
        "jogos_temporada": total_games(text) or target["jogos_mercado"],
        "preco": metric(text, "Preço") or target["preco_mercado"],
        "medias": {
            "geral": metric(text, "Média"),
            "casa": metric(text, "Média em Casa"),
            "fora": metric(text, "Média Fora"),
        },
        "mercado_oficial": {
            "media": target["media_mercado"],
            "jogos": target["jogos_mercado"],
            "preco": target["preco_mercado"],
        },
        "historico": historico,
        "fonte_url": final_url,
    }


# -----------------------------
# STATZ / PRODUÇÃO INDIVIDUAL
# -----------------------------

def parse_statz_club_links(html):
    """Descobre endereços atuais apenas na tabela de clubes da competição."""
    soup = BeautifulSoup(html, "html.parser")
    links = {}
    ambiguous = set()
    for table in soup.find_all("table"):
        headers = {norm(th.get_text(" ", strip=True)) for th in table.find_all("th")}
        if not {"team", "played"}.issubset(headers):
            continue
        for row in table.find_all("tr"):
            for anchor in row.find_all("a", href=True):
                abbr = STATZ_TEAM_TO_ABBR.get(norm(anchor.get_text(" ", strip=True)))
                address = urlparse(urljoin(STATZ_BASE, anchor["href"]))
                if (not abbr or address.scheme != "https"
                        or address.netloc != urlparse(STATZ_BASE).netloc
                        or not re.fullmatch(r"/team/[^/]+/?", address.path)):
                    continue
                url = f"{STATZ_BASE}{address.path.rstrip('/')}/squad"
                if abbr in links and links[abbr] != url:
                    ambiguous.add(abbr)
                links[abbr] = url
    for abbr in ambiguous:
        links.pop(abbr, None)
    return links


def discover_statz_clubs():
    try:
        response = get_html(STATZ_COMPETITION)
        links = parse_statz_club_links(response.text)
        return {
            "urls": links,
            "fonte_url": response.url,
            "status": "ok" if len(links) == 20 else "parcial",
            "clubes_descobertos": len(links),
            "erro": None if links else "tabela_clubes_ou_links_nao_encontrados",
        }
    except Exception as exc:
        return {"urls": {}, "fonte_url": STATZ_COMPETITION,
                "status": "ausente", "clubes_descobertos": 0, "erro": str(exc)}


def squad_url(sigla, cache):
    discovery = cache.get("_descoberta_clubes", {})
    return discovery.get("urls", {}).get(sigla)


def fetch_statz_squad(sigla, cache):
    if sigla in cache:
        return cache[sigla]

    url = squad_url(sigla, cache)
    if not url:
        cache[sigla] = {"url": None, "players": [],
                       "erro": "link_clube_nao_descoberto_na_competicao"}
        return cache[sigla]

    try:
        r = get_html(url)
        soup = BeautifulSoup(r.text, "html.parser")
        players = []
        seen = set()

        for a in soup.find_all("a", href=True):
            href = a.get("href") or ""
            if "/player/" not in href:
                continue

            name = clean(a.get_text(" ", strip=True))
            if not name:
                continue

            full_url = urljoin(STATZ_BASE, href)
            key = (norm(name), full_url)
            if key in seen:
                continue
            seen.add(key)

            position = None
            tr = a.find_parent("tr")
            if tr:
                cells = [clean(x.get_text(" ", strip=True)) for x in tr.find_all(["td", "th"])]
                if len(cells) >= 2:
                    position = cells[1]

            players.append({
                "name": name,
                "name_norm": norm(name),
                "url": full_url,
                "position": position,
            })

        cache[sigla] = {"url": r.url, "players": players,
                       "erro": None if players else "elenco_sem_jogadores_na_fonte"}
    except Exception as e:
        cache[sigla] = {"url": url, "players": [], "erro": str(e)}

    return cache[sigla]


def resolve_statz_player(target, cache):
    squad = fetch_statz_squad(target["clube_abreviacao"], cache)
    players = squad["players"]

    if not players:
        return None, {
            "status": "ausente_fonte_prioritaria",
            "motivo": squad.get("erro") or "elenco_statz_vazio",
            "fonte_url": squad.get("url"),
        }

    q = norm(target["nome_mercado"])

    exact = [p for p in players if p["name_norm"] == q]
    if len(exact) == 1:
        return exact[0], {"status": "ok", "metodo": "nome_exato"}

    contains = [
        p for p in players
        if len(q) >= 4 and (q in p["name_norm"] or p["name_norm"] in q)
    ]
    if len(contains) == 1:
        return contains[0], {"status": "ok", "metodo": "nome_contido"}

    q_tokens = {x for x in q.split() if len(x) >= 3}
    token_matches = []
    for p in players:
        p_tokens = {x for x in p["name_norm"].split() if len(x) >= 3}
        if q_tokens and q_tokens.issubset(p_tokens):
            token_matches.append(p)
    if len(token_matches) == 1:
        return token_matches[0], {"status": "ok", "metodo": "tokens_unicos"}

    scored = sorted(
        ((
            SequenceMatcher(None, q, p["name_norm"]).ratio(),
            p
        ) for p in players),
        key=lambda item: item[0],
    )
    if scored:
        best_score, best = scored[-1]
        second_score = scored[-2][0] if len(scored) > 1 else 0
        if best_score >= 0.78 and (best_score - second_score) >= 0.08:
            return best, {
                "status": "ok",
                "metodo": "similaridade_controlada",
                "score": round(best_score, 3),
            }

    return None, {
        "status": "inconsistente",
        "motivo": "jogador_nao_resolvido_com_seguranca_no_elenco_statz",
        "fonte_url": squad.get("url"),
    }


def normalized_header_map(headers):
    result = {}
    for idx, h in enumerate(headers):
        hn = norm(h)
        for canonical, aliases in STATZ_FIELD_ALIASES.items():
            if hn in {norm(x) for x in aliases}:
                result[canonical] = idx
                break
    return result


def find_match_table(soup):
    for table in soup.find_all("table"):
        headers = [clean(th.get_text(" ", strip=True)) for th in table.find_all("th")]
        hmap = normalized_header_map(headers)
        if (
            "competition" in hmap
            and "minutes" in hmap
            and "goals" in hmap
            and "assists" in hmap
        ):
            return table, headers, hmap
    return None, [], {}


def cell_text(cells, hmap, key):
    idx = hmap.get(key)
    if idx is None or idx >= len(cells):
        return None
    return clean(cells[idx].get_text(" ", strip=True))


def parse_statz_matches(html):
    soup = BeautifulSoup(html, "html.parser")
    table, headers, hmap = find_match_table(soup)
    if table is None:
        return [], {"status": "ausente_fonte_prioritaria", "motivo": "tabela_partidas_nao_encontrada"}

    rows = []
    body_rows = table.find_all("tr")
    for tr in body_rows:
        cells = tr.find_all("td")
        if not cells:
            continue

        competition = cell_text(cells, hmap, "competition")
        minutes = parse_number(cell_text(cells, hmap, "minutes"))

        if not competition or "campeonato brasileiro" not in norm(competition):
            continue
        if minutes is None or minutes <= 0:
            continue

        row = {
            "date": cell_text(cells, hmap, "date"),
            "competition": competition,
            "opponent": cell_text(cells, hmap, "opponent"),
            "venue": cell_text(cells, hmap, "venue"),
            "score": cell_text(cells, hmap, "score"),
            "result": cell_text(cells, hmap, "result"),
            "position": cell_text(cells, hmap, "position"),
            "minutes": minutes,
        }

        for key in [
            "goals", "assists", "shots", "shots_on_target",
            "crosses", "tackles", "interceptions", "aerials_won", "saves"
        ]:
            if key in hmap:
                row[key] = parse_number(cell_text(cells, hmap, key))

        rows.append(row)

    # Statz apresenta as partidas da mais recente para a mais antiga.
    return rows[:5], {
        "status": "ok" if rows else "sem_atuacoes_suficientes",
        "cabecalhos": headers,
    }


def metric_payload(total, minutes_total, source_key, calc_por90):
    if total is None:
        return {
            "total": None,
            "por_90": None if calc_por90 else None,
            "unidade_base": "contagem",
            "fonte_id": "statz",
            "status": "ausente_fonte_prioritaria",
            "campo_fonte": source_key,
        }

    result = {
        "total": total,
        "unidade_base": "contagem",
        "fonte_id": "statz",
        "status": "ok",
        "campo_fonte": source_key,
    }
    if calc_por90:
        result["por_90"] = (
            round((float(total) / float(minutes_total)) * 90, 4)
            if minutes_total and minutes_total > 0
            else None
        )
        if result["por_90"] is None:
            result["status"] = "inconsistente"
    return result


def aggregate_field(matches, field):
    if not matches:
        return None

    values = []
    for m in matches:
        if field not in m or m[field] is None:
            return None
        values.append(float(m[field]))
    total = sum(values)
    return int(total) if float(total).is_integer() else round(total, 4)


def build_production(target, matches, statz_url):
    pos = target["posicao"]
    minutes_total = sum(float(m["minutes"]) for m in matches if m.get("minutes") is not None)
    n = len(matches)

    if not matches:
        return {
            "status": "ausente",
            "fonte_url": statz_url,
            "amostra": {
                "criterio": "ultimas_5_atuacoes_brasileirao",
                "n_atuacoes": 0,
                "minutos_totais": 0,
                "partidas_ids": [],
            },
            "metricas": {},
        }

    metrics = {}
    for output_name, source_key, calc_por90 in PRODUCTION_RULES[pos]:
        total = aggregate_field(matches, source_key)
        metrics[output_name] = metric_payload(
            total,
            minutes_total,
            source_key,
            calc_por90,
        )

    goals = aggregate_field(matches, "goals")
    assists = aggregate_field(matches, "assists")
    if pos in {"LAT", "ZAG", "MEI", "ATA"}:
        if goals is not None and assists is not None:
            ga = goals + assists
            if float(ga).is_integer():
                ga = int(ga)
            metrics["gols_assistencias"] = {
                "total": ga,
                "por_90": round((float(ga) / minutes_total) * 90, 4) if minutes_total > 0 else None,
                "unidade_base": "contagem",
                "fonte_id": "statz",
                "status": "ok" if minutes_total > 0 else "inconsistente",
                "definicao": "gols + assistencias",
            }
        else:
            metrics["gols_assistencias"] = {
                "total": None,
                "por_90": None,
                "unidade_base": "contagem",
                "fonte_id": "statz",
                "status": "ausente_fonte_prioritaria",
                "definicao": "gols + assistencias",
            }

    metrics["minutos"] = {
        "total": int(minutes_total) if float(minutes_total).is_integer() else round(minutes_total, 2),
        "media_por_participacao": round(minutes_total / n, 4) if n else None,
        "unidade_base": "minutos",
        "fonte_id": "statz",
        "status": "ok" if n else "sem_atuacoes_suficientes",
    }

    required = [
        v for k, v in metrics.items()
        if k not in {"gols", "assistencias"}
    ]
    statuses = [x.get("status") for x in required]

    if statuses and all(s == "ok" for s in statuses):
        overall = "completo"
    elif any(s == "ok" for s in statuses):
        overall = "parcial"
    else:
        overall = "ausente"

    return {
        "status": overall,
        "fonte_url": statz_url,
        "amostra": {
            "criterio": "ultimas_5_atuacoes_brasileirao",
            "n_atuacoes": n,
            "status_amostra": "completa_l5" if n == 5 else "amostra_reduzida",
            "minutos_totais": int(minutes_total) if float(minutes_total).is_integer() else round(minutes_total, 2),
            "partidas_ids": [
                f"{m.get('date') or '?'}|{m.get('opponent') or '?'}|{m.get('score') or '?'}"
                for m in matches
            ],
            "partidas": matches,
        },
        "metricas": metrics,
    }


def fetch_statz_production(target, squad_cache):
    player, resolution = resolve_statz_player(target, squad_cache)

    if not player:
        return {
            "status": "ausente",
            "resolucao_identidade_statz": resolution,
            "amostra": {
                "criterio": "ultimas_5_atuacoes_brasileirao",
                "n_atuacoes": 0,
                "minutos_totais": 0,
                "partidas_ids": [],
            },
            "metricas": {},
        }

    r = get_html(player["url"])
    matches, parse_status = parse_statz_matches(r.text)
    production = build_production(target, matches, r.url)
    production["resolucao_identidade_statz"] = {
        **resolution,
        "nome_statz": player["name"],
        "url": player["url"],
        "parse_status": parse_status,
    }
    return production


# -----------------------------
# STATZ / COLETIVOS L5
# -----------------------------

def find_fixtures_table(soup):
    for table in soup.find_all("table"):
        headers = [norm(th.get_text(" ", strip=True)) for th in table.find_all("th")]
        if "match" in headers and "score" in headers and "date" in headers:
            return table, headers
    return None, []


def parse_statz_fixtures():
    r = get_html(STATZ_FIXTURES, timeout=35)
    soup = BeautifulSoup(r.text, "html.parser")
    table, headers = find_fixtures_table(soup)
    if table is None:
        raise ValueError("Tabela de fixtures/resultados do Brasileirão não encontrada no Statz.")

    hmap = {h: i for i, h in enumerate(headers)}
    out = []

    for tr in table.find_all("tr"):
        cells = tr.find_all("td")
        if not cells:
            continue

        def col(name):
            idx = hmap.get(name)
            if idx is None or idx >= len(cells):
                return None
            return clean(cells[idx].get_text(" ", strip=True))

        date_txt = col("date")
        match_txt = col("match")
        score_txt = col("score")

        if not date_txt or not match_txt or not score_txt:
            continue
        if "upcoming" in norm(score_txt):
            continue

        mm = re.match(r"(.+?)\s+vs\s+(.+)$", match_txt, re.I)
        ss = re.match(r"(\d+)\s*-\s*(\d+)", score_txt)
        if not mm or not ss:
            continue

        home, away = clean(mm.group(1)), clean(mm.group(2))
        hg, ag = int(ss.group(1)), int(ss.group(2))

        ordinal_removed = re.sub(r"(\d+)(st|nd|rd|th)", r"\1", date_txt, flags=re.I)
        try:
            dt = datetime.strptime(ordinal_removed, "%d %b %Y")
        except Exception:
            dt = None

        out.append({
            "date": date_txt,
            "date_obj": dt,
            "home": home,
            "away": away,
            "home_abbr": STATZ_TEAM_TO_ABBR.get(norm(home)),
            "away_abbr": STATZ_TEAM_TO_ABBR.get(norm(away)),
            "home_goals": hg,
            "away_goals": ag,
        })

    out.sort(key=lambda x: x["date_obj"] or datetime.min)
    return out, r.url


def build_club_collectives(fixtures, selected_abbrs, source_url):
    club_matches = defaultdict(list)

    for m in fixtures:
        if m["home_abbr"]:
            club_matches[m["home_abbr"]].append({
                "date": m["date"],
                "opponent": m["away_abbr"],
                "venue": "casa",
                "gf": m["home_goals"],
                "ga": m["away_goals"],
            })
        if m["away_abbr"]:
            club_matches[m["away_abbr"]].append({
                "date": m["date"],
                "opponent": m["home_abbr"],
                "venue": "fora",
                "gf": m["away_goals"],
                "ga": m["home_goals"],
            })

    result = {}
    for abbr in sorted(selected_abbrs):
        last5 = club_matches.get(abbr, [])[-5:]
        if not last5:
            result[abbr] = {
                "sigla": abbr,
                "gols_marcados_l5": None,
                "gols_sofridos_l5": None,
                "sg_l5": None,
                "n_partidas_l5": 0,
                "fonte_id": "statz",
                "fonte_url": source_url,
                "status": "ausente_fonte_prioritaria",
                "partidas": [],
            }
            continue

        gf = sum(x["gf"] for x in last5)
        ga = sum(x["ga"] for x in last5)
        sg = sum(1 for x in last5 if x["ga"] == 0)

        result[abbr] = {
            "sigla": abbr,
            "gols_marcados_l5": gf,
            "gols_sofridos_l5": ga,
            "sg_l5": sg,
            "n_partidas_l5": len(last5),
            "fonte_id": "statz",
            "fonte_url": source_url,
            "status": "ok" if len(last5) == 5 else "sem_atuacoes_suficientes",
            "partidas": last5,
        }

    return result


# -----------------------------
# REFERÊNCIAS DA COMPETIÇÃO
# -----------------------------

def parse_competition_player_base(html, season):
    soup = BeautifulSoup(html, "html.parser")
    node = soup.find(attrs={"data-page": True})
    if node is None:
        raise ValueError("base_estruturada_nao_encontrada")
    props = json.loads(node["data-page"]).get("props", {})
    if props.get("competition", {}).get("slug") != "campeonato-brasileiro":
        raise ValueError("competicao_incompativel")
    if str(props.get("hero", {}).get("season")) != str(season):
        raise ValueError("temporada_incompativel")
    if props.get("venue") != "overall" or props.get("initialMode") != "total":
        raise ValueError("base_nao_representa_totais_gerais")
    rows = props.get("playerRows")
    if not isinstance(rows, list) or not rows:
        raise ValueError("base_jogadores_vazia")
    if len({r.get("id") for r in rows}) != len(rows):
        raise ValueError("jogadores_duplicados_na_base")
    if len({r.get("team_id") for r in rows}) != 20:
        raise ValueError("base_nao_cobre_20_clubes")
    return rows


def classify_reference_players(rows, cartola_players, collected_players=None):
    players = {str(p["atleta_id"]): p for p in cartola_players
               if p.get("posicao") in PRODUCTION_RULES}
    clubs = defaultdict(list)
    for player in players.values():
        clubs[player.get("clube_abreviacao")].append(player)
    confirmed = defaultdict(set)
    for collected in collected_players or []:
        resolution = collected.get("producao", {}).get("resolucao_identidade_statz", {})
        url = resolution.get("url", "")
        match = re.search(r"/player/[^/]+/(\d+)(?:[/?#]|$)", url)
        player_id = str(collected.get("atleta_id"))
        if resolution.get("status") == "ok" and match and player_id in players:
            confirmed[match.group(1)].add(player_id)
    eligible = [r for r in rows if isinstance(r.get("mins"), (int, float)) and r["mins"] > 0]
    candidates = {}
    methods = {}
    for row in eligible:
        club = next((STATZ_TEAM_TO_ABBR[norm(label)] for label in
                     (row.get("team_full") or "", row.get("team") or "")
                     if norm(label) in STATZ_TEAM_TO_ABBR), None)
        pool = clubs.get(club, [])
        name = norm(row.get("name") or "")
        linked = {pid for pid in confirmed.get(str(row.get("id")), set())
                  if players[pid].get("clube_abreviacao") == club}
        method = "vinculo_confirmado_na_coleta"
        if not linked:
            linked = {str(p["atleta_id"]) for p in pool if name and name in
                      {norm(p.get("nome_mercado")), norm(p.get("nome_completo"))}}
            method = "nome_exato_e_clube"
        if not linked:
            # Contenção de palavras inteiras; nada de similaridade aproximada.
            linked = {str(p["atleta_id"]) for p in pool
                      for alias in (norm(p.get("nome_mercado")), norm(p.get("nome_completo")))
                      if name and alias and min(len(name), len(alias)) >= 4 and
                      (f" {name} " in f" {alias} " or f" {alias} " in f" {name} ")}
            method = "nome_contido_unico_e_clube"
        candidates[row["id"]] = linked
        methods[row["id"]] = method
    # Reciprocidade evita vincular dois nomes/linhas ao mesmo atleta.
    reverse = defaultdict(set)
    for statz_id, linked in candidates.items():
        for player_id in linked:
            reverse[player_id].add(statz_id)
    classified, excluded = [], []
    for row in eligible:
        linked = candidates[row["id"]]
        player_id = next(iter(linked)) if len(linked) == 1 else None
        if player_id and len(reverse[player_id]) == 1:
            player = players[player_id]
            classified.append(dict(row, posicao_cartola=player["posicao"],
                                   atleta_id_cartola=player_id,
                                   metodo_vinculo_cartola=methods[row["id"]]))
        else:
            excluded.append({"id": row.get("id"), "jogador": row.get("name"),
                             "clube": row.get("team_full") or row.get("team"),
                             "minutos": row["mins"], "posicao_fonte": row.get("pd"),
                             "candidatos_cartola": sorted(linked),
                             "motivo": "identidade_cartola_ambigua" if linked else
                             "sem_correspondencia_segura_no_elenco_cartola"})
    return classified, excluded


def build_individual_references(rows, season, source_url, cartola_players, collected_players=None):
    fields = {"saves": "sav", "tackles": "tkl", "crosses": "crs",
              "shots": "sh", "shots_on_target": "sot",
              "interceptions": "int", "aerials_won": "aer",
              "goals": "g", "assists": "a"}
    eligible, unclassified = classify_reference_players(rows, cartola_players, collected_players)
    result = {}
    for position, rules in PRODUCTION_RULES.items():
        group = [r for r in eligible if r["posicao_cartola"] == position]
        metrics = [(name, fields[field]) for name, field, _ in rules]
        if position != "GOL":
            metrics.append(("gols_assistencias", "ga"))
            metrics.append(("minutos", "minutes_per_appearance"))
        for metric, field in metrics:
            values = []
            for row in group:
                if field == "minutes_per_appearance":
                    appearances = row.get("apps")
                    if isinstance(appearances, (int, float)) and appearances > 0:
                        values.append(row["mins"] / appearances)
                    continue
                value = (row.get("g") + row.get("a")
                         if field == "ga" and isinstance(row.get("g"), (int, float))
                         and isinstance(row.get("a"), (int, float))
                         else row.get(field))
                if isinstance(value, (int, float)) and value >= 0:
                    values.append(value * 90 / row["mins"])
            complete = bool(group) and len(values) == len(group)
            result[f"{position}_{metric}"] = {
                "competicao": "Campeonato Brasileiro", "temporada": season,
                "universo": "jogadores identificados no elenco completo do Cartola, agrupados pela posicao oficial do Cartola, com minutos > 0 no Statz",
                "posicao": position, "metrica": metric,
                "unidade": "minutos por participacao" if field == "minutes_per_appearance" else "por 90 minutos",
                "definicao": (
                    "media aritmetica de minutos/apps de cada jogador da posicao na temporada"
                    if field == "minutes_per_appearance" else
                    "media aritmetica das taxas individuais por 90 da temporada; producao L5 deve ser comparada por 90"
                ),
                "valor": round(sum(values) / len(values), 4) if complete else None,
                "n_elementos": len(group), "n_com_metrica": len(values),
                "jogadores_sem_vinculo_cartola": unclassified,
                "excluidos_da_base_referencia": unclassified,
                "n_excluidos_sem_vinculo_cartola": len(unclassified),
                "n_jogadores_statz_com_minutos": len(eligible) + len(unclassified),
                "n_jogadores_vinculados_cartola": len(eligible),
                "criterio_exclusao": "excluir correspondencia ausente ou ambigua no Cartola; nao inferir posicao pelo Statz",
                "fonte_id": "statz", "fonte_url": source_url,
                "metodo": (
                    "base_em_lote_media_minutos_por_participacao_individual"
                    if field == "minutes_per_appearance" else
                    "base_completa_em_lote_media_taxas_individuais"
                ),
                "criterio_minutos": "> 0; sem filtro arbitrario de titularidade",
                "fonte_posicao": "cartola_api_oficial",
                "fonte_posicao_url": f"{API_BASE}/atletas/mercado",
                "jogadores_base": [{"id_statz": r["id"], "atleta_id_cartola": r["atleta_id_cartola"], "jogador": r["name"], "posicao_cartola": position, "posicao_statz": r.get("pd"), "metodo_vinculo": r["metodo_vinculo_cartola"]} for r in group],
                "status": "ok" if complete else "ausente_fonte_prioritaria",
            }
    return result


def fetch_individual_references(season, cartola_players, collected_players=None):
    url = f"{STATZ_COMPETITION}/stats/players"
    response = get_html(url, timeout=35)
    rows = parse_competition_player_base(response.text, season)
    return build_individual_references(rows, season, response.url, cartola_players, collected_players)


def build_competition_references(collectives, season):
    universe = set(STATZ_CLUBE_SLUG)
    valid = (
        set(collectives) == universe
        and all(c.get("status") == "ok" and c.get("n_partidas_l5") == 5
                for c in collectives.values())
    )
    references = {}
    for metric in ("gols_marcados_l5", "gols_sofridos_l5"):
        values = [c.get(metric) for c in collectives.values()]
        complete = valid and all(isinstance(v, (int, float)) for v in values)
        references[metric] = {
            "competicao": "Campeonato Brasileiro",
            "temporada": season,
            "universo": "20 clubes da competicao",
            "posicao": None,
            "metrica": metric,
            "unidade": "gols por clube em 5 partidas",
            "definicao": "media aritmetica dos totais L5 dos 20 clubes",
            "valor": round(sum(values) / 20, 4) if complete else None,
            "n_elementos": 20 if complete else 0,
            "fonte_id": "statz",
            "fonte_url": STATZ_FIXTURES,
            "metodo": "base_coletiva_em_lote",
            "status": "ok" if complete else "ausente_fonte_prioritaria",
        }
    for position, rules in PRODUCTION_RULES.items():
        for metric, _, per90 in rules:
            references[f"{position}_{metric}"] = {
                "competicao": "Campeonato Brasileiro",
                "temporada": season,
                "universo": "jogadores da competicao na mesma posicao",
                "posicao": position,
                "metrica": metric,
                "unidade": "por 90 minutos" if per90 else "contagem",
                "definicao": "media da competicao por posicao",
                "valor": None,
                "n_elementos": 0,
                "fonte_id": "statz",
                "metodo": None,
                "status": "ausente_fonte_prioritaria",
                "motivo": "base completa por posicao nao disponivel nesta coleta; ranking parcial nao representa o universo",
            }
        if position != "GOL":
            references[f"{position}_minutos"] = {
                "competicao": "Campeonato Brasileiro", "temporada": season,
                "universo": "jogadores da posicao com minutos > 0 e posicao conhecida",
                "posicao": position, "metrica": "minutos",
                "unidade": "minutos por participacao",
                "definicao": "media aritmetica de minutos/apps de cada jogador da posicao na temporada",
                "valor": None, "n_elementos": 0, "fonte_id": "statz",
                "metodo": None, "status": "ausente_fonte_prioritaria",
            }
    return references


# COLETA DE UM JOGADOR

def collect_one_player(target, rodada, janela, squad_cache):
    base = {
        "id": target["atleta_id"],
        "atleta_id": target["atleta_id"],
        "jogador": target["nome_mercado"],
        "clube": target["clube"],
        "sigla": target["clube_abreviacao"],
        "posicao": target["posicao"],
        "status_mercado": target["status"],
        "identidade_validada": True,
    }

    cartola = fetch_cartola_history(target, rodada, janela)
    base.update({
        "jogador": cartola["nome_site"] or target["nome_mercado"],
        "clube": cartola["clube_site"] or target["clube"],
        "posicao": cartola["posicao_site"] or target["posicao"],
        "jogos_temporada": cartola["jogos_temporada"],
        "preco": cartola["preco"],
        "medias": cartola["medias"],
        "mercado_oficial": cartola["mercado_oficial"],
        "historico_individual": cartola["historico"],
        # Compatibilidade com V1:
        "historico": cartola["historico"],
        "fonte_historico_url": cartola["fonte_url"],
    })

    try:
        base["producao"] = fetch_statz_production(target, squad_cache)
    except Exception as e:
        base["producao"] = {
            "status": "ausente",
            "erro": str(e),
            "amostra": {
                "criterio": "ultimas_5_atuacoes_brasileirao",
                "n_atuacoes": 0,
                "minutos_totais": 0,
                "partidas_ids": [],
            },
            "metricas": {},
        }

    return base


def label_player(p):
    return (
        f"{p['nome_mercado']} — {p['posicao']} / "
        f"{p['clube_abreviacao']} — {p['status']} — ID {p['atleta_id']}"
    )


# -----------------------------
# STREAMLIT
# -----------------------------

st.set_page_config(page_title="MAC-Coletor V2", page_icon="⚽", layout="centered")
st.title("⚽ MAC-Coletor V2")
st.caption("Produção L5 + histórico individual + coletivos L5 | Fase operacional inicial")

st.info(
    "Nesta fase V2 já entram Produção individual via Statz e coletivos L5. "
    "As referências coletivas usam os 20 clubes. As individuais usam a base "
    "da competição agrupada pela posição oficial do Cartola, com taxas por 90 "
    "e minutos por participação. Jogadores sem vínculo seguro com o Cartola "
    "são excluídos das referências e registrados no JSON."
)

if "mercado" not in st.session_state:
    st.session_state["mercado"] = None
if "mac_resultado" not in st.session_state:
    st.session_state["mac_resultado"] = None

rodada = st.number_input(
    "Rodada de referência",
    min_value=2,
    max_value=38,
    value=29,
    step=1,
)
janela = st.slider(
    "Quantas rodadas anteriores guardar no histórico Cartola",
    min_value=5,
    max_value=24,
    value=12,
)

st.write("### 1. Carregar elenco completo")
if st.button("CARREGAR JOGADORES DO CARTOLA", type="primary", use_container_width=True):
    try:
        with st.spinner("Buscando o mercado oficial completo..."):
            status, atletas_api, clubes = carregar_mercado()
            alvos = []
            for a in atletas_api:
                pos = POS_MAP.get(int(a.get("posicao_id", 0)))
                if pos in {"GOL", "LAT", "ZAG", "MEI", "ATA"}:
                    alvos.append(atleta_para_alvo(a, clubes))

        st.session_state["mercado"] = {
            "rodada_atual": int(status.get("rodada_atual", 0)),
            "status_mercado": int(status.get("status_mercado", 0)),
            "temporada": int(status.get("temporada", 2026)),
            "atletas": alvos,
        }
        st.session_state["mac_resultado"] = None
    except Exception as e:
        st.error(f"Falha ao carregar mercado oficial: {e}")

mercado = st.session_state.get("mercado")

if mercado:
    atletas = mercado["atletas"]
    rodada_oficial = mercado["rodada_atual"]

    st.success(
        f"{len(atletas)} jogadores carregados do mercado oficial "
        f"da rodada {rodada_oficial}."
    )

    if rodada_oficial != int(rodada):
        st.warning(
            f"O Cartola oficial está na rodada {rodada_oficial}, "
            f"mas o app está configurado para {int(rodada)}."
        )
    st.write("### 2. Escolher quem pesquisar")

    modo = st.radio(
        "Como você quer pesquisar?",
        [
            "Só prováveis",
            "Buscar qualquer jogador do elenco",
            "Todos os jogadores filtrados",
        ],
    )

    if modo == "Só prováveis":
        candidatos = [p for p in atletas if p["status"] == "Provável"]
        posicoes = st.multiselect(
            "Posições",
            ["GOL", "LAT", "ZAG", "MEI", "ATA"],
            default=["GOL", "LAT", "ZAG", "MEI", "ATA"],
        )
        candidatos = [p for p in candidatos if p["posicao"] in posicoes]

        mapa = {label_player(p): p for p in candidatos}
        labels = st.multiselect(
            "Prováveis disponíveis",
            list(mapa.keys()),
            default=[],
        )
        escolhidos = [mapa[x] for x in labels]

    elif modo == "Buscar qualquer jogador do elenco":
        clubes_disponiveis = sorted(
            {p["clube"] for p in atletas if p["clube"]},
            key=norm
        )
        clube_filtro = st.selectbox("Clube", ["Todos"] + clubes_disponiveis)
        posicao_filtro = st.selectbox(
            "Posição",
            ["Todas", "GOL", "LAT", "ZAG", "MEI", "ATA"],
        )
        busca = st.text_input(
            "Digite parte do nome do jogador",
            placeholder="Ex.: Neymar, Marcelinho, Gabriel..."
        )

        candidatos = atletas
        if clube_filtro != "Todos":
            candidatos = [p for p in candidatos if p["clube"] == clube_filtro]
        if posicao_filtro != "Todas":
            candidatos = [p for p in candidatos if p["posicao"] == posicao_filtro]
        if busca.strip():
            q = norm(busca)
            candidatos = [p for p in candidatos if q in norm(p["nome_mercado"])]

        mapa = {label_player(p): p for p in candidatos}
        labels = st.multiselect(
            f"Resultados ({len(candidatos)})",
            list(mapa.keys()),
            default=[],
        )
        escolhidos = [mapa[x] for x in labels]

    else:
        status_disponiveis = sorted({p["status"] for p in atletas})
        status_filtro = st.multiselect(
            "Status",
            status_disponiveis,
            default=status_disponiveis,
        )
        posicoes = st.multiselect(
            "Posições",
            ["GOL", "LAT", "ZAG", "MEI", "ATA"],
            default=["GOL", "LAT", "ZAG", "MEI", "ATA"],
        )
        clubes_disponiveis = sorted(
            {p["clube"] for p in atletas if p["clube"]},
            key=norm
        )
        clubes_filtro = st.multiselect(
            "Clubes",
            clubes_disponiveis,
            default=[],
            help="Vazio = todos os clubes",
        )

        escolhidos = [
            p for p in atletas
            if p["status"] in status_filtro
            and p["posicao"] in posicoes
            and (not clubes_filtro or p["clube"] in clubes_filtro)
        ]

    st.info(f"{len(escolhidos)} jogador(es) selecionado(s) para coleta.")

    st.write("### 3. Gerar JSON V2")
    pode = bool(escolhidos) and rodada_oficial == int(rodada)

    if st.button(
        "GERAR ARQUIVO V2 PARA O MAC",
        type="primary",
        use_container_width=True,
        disabled=not pode,
    ):
        resultados, erros = [], []
        progresso = st.progress(0)
        status_box = st.empty()
        status_box.write("Descobrindo os endereços atuais dos clubes no Statz...")
        descoberta_clubes = discover_statz_clubs()
        squad_cache = {"_descoberta_clubes": descoberta_clubes}

        # Coleta de jogadores em paralelo; o cache de elenco é compartilhado
        # apenas como otimização. Em caso de corrida, a pior consequência é uma
        # repetição de request, nunca alteração de dado.
        max_workers = min(5, max(1, len(escolhidos)))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(
                    collect_one_player,
                    p,
                    int(rodada),
                    int(janela),
                    squad_cache,
                ): p
                for p in escolhidos
            }

            concluidos = 0
            for future in as_completed(futures):
                p = futures[future]
                try:
                    resultados.append(future.result())
                except Exception as e:
                    erros.append({
                        "atleta_id": p["atleta_id"],
                        "nome": p["nome_mercado"],
                        "clube": p["clube"],
                        "posicao": p["posicao"],
                        "status": p["status"],
                        "erro": str(e),
                    })
                concluidos += 1
                progresso.progress(concluidos / len(escolhidos))
                status_box.write(
                    f"Jogadores processados {concluidos}/{len(escolhidos)}..."
                )

        resultados.sort(key=lambda x: (x["posicao"], norm(x["jogador"])))

        # Coletivos L5 em uma única passagem pela página de fixtures do Statz.
        coletivos = {}
        coletivo_erro = None
        try:
            status_box.write("Fechando coletivos L5 dos clubes...")
            fixtures, fixtures_url = parse_statz_fixtures()
            selected_abbrs = set(STATZ_CLUBE_SLUG)
            coletivos = build_club_collectives(
                fixtures,
                selected_abbrs,
                fixtures_url,
            )
        except Exception as e:
            coletivo_erro = str(e)
            erros.append({
                "tipo": "coletivos_clubes",
                "fonte": "statz",
                "erro": coletivo_erro,
            })

        referencias = build_competition_references(coletivos, mercado["temporada"])
        referencia_erro = None
        try:
            status_box.write("Calculando referências individuais da competição em lote...")
            referencias.update(fetch_individual_references(mercado["temporada"], atletas, resultados))
        except Exception as exc:
            referencia_erro = str(exc)
        pendencias = [
            {"atleta_id": p["atleta_id"], "jogador": p["jogador"],
             "status": p.get("producao", {}).get("status")}
            for p in resultados
            if p.get("producao", {}).get("status") != "completo"
        ]
        amostras_reduzidas = sum(
            1 for p in resultados
            if 0 < p.get("producao", {}).get("amostra", {}).get("n_atuacoes", 0) < 5
        )

        completos = sum(
            1 for p in resultados
            if p.get("producao", {}).get("status") == "completo"
        )
        parciais = sum(
            1 for p in resultados
            if p.get("producao", {}).get("status") == "parcial"
        )
        ausentes = sum(
            1 for p in resultados
            if p.get("producao", {}).get("status") == "ausente"
        )

        payload = {
            "schema_version": "MAC_COLETOR_V2",
            "rodada": int(rodada),
            "rodada_referencia": int(rodada),
            "rodada_oficial": rodada_oficial,
            "temporada": mercado["temporada"],
            "competicao": "Campeonato Brasileiro",
            "gerado_em": datetime.now(timezone.utc).isoformat(),
            "modo": "v2_producao_e_coletivos_fase1",
            "fontes_coleta": {
                "cartola_api": {
                    "tipo": "identidade_mercado",
                    "base": API_BASE,
                    "status": "ok",
                },
                "cartola_fc_brasil": {
                    "tipo": "historico_individual",
                    "base": CARTOLA_BRASIL_BASE,
                    "status": "ok",
                },
                "statz": {
                    "tipo": "prioritaria_producao_e_coletivos",
                    "base": STATZ_BASE,
                    "competicao": "Campeonato Brasileiro",
                    "temporada": mercado["temporada"],
                    "status": "ok",
                },
            },
            "regra_ausencia": "ausencia_na_fonte_nao_e_zero",
            "regra_identidade": "ID oficial + nome + clube + posição",
            "jogadores": resultados,
            "coletivos_clubes": coletivos,
            "referencias_producao": referencias,
            "checkpoint": {
                "fase": "V2_FASE_1",
                "jogadores_concluidos": completos,
                "jogadores_parciais": parciais,
                "jogadores_ausentes": ausentes,
                "coletivos_concluidos": sum(
                    1 for x in coletivos.values()
                    if x.get("status") == "ok"
                ),
                "referencias_concluidas": sum(r["status"] == "ok" for r in referencias.values()),
                "referencias_pendentes": sum(r["status"] != "ok" for r in referencias.values()),
                "referencias_parciais": sum(r["status"] == "parcial" for r in referencias.values()),
                "amostras_reduzidas": amostras_reduzidas,
                "descoberta_clubes_statz": descoberta_clubes,
                "erro_referencias_individuais": referencia_erro,
                "protocolo_fallback": "pendente_fase_2",
                "proxima_acao": (
                    "implementar fonte alternativa finita; revisar referencias ausentes, se houver"
                ),
            },
            "total_mercado": len(atletas),
            "total_solicitado": len(escolhidos),
            "total_coletado": len(resultados),
            "total_erros": len(erros),
            "total_pendencias_producao": len(pendencias),
            "pendencias_producao": pendencias,
            "erros": erros,
        }

        st.session_state["mac_resultado"] = payload
        status_box.write("Coleta encerrada. JSON V2 gerado.")

payload = st.session_state.get("mac_resultado")

if payload:
    st.divider()
    st.write("### Resultado V2")
    st.success(
        f"{payload['total_coletado']} coletados | "
        f"{payload['total_erros']} erro(s)"
    )

    cp = payload.get("checkpoint", {})
    if payload.get("total_pendencias_producao", 0) or cp.get("referencias_pendentes", 0):
        st.warning(
            f"Coleta parcial: {payload.get('total_pendencias_producao', 0)} "
            f"pendência(s) de produção e {cp.get('referencias_pendentes', 0)} "
            "referência(s) pendente(s), incluindo cobertura parcial. "
            "Erros técnicos e dados ausentes são contados separadamente."
        )
    if cp.get("amostras_reduzidas", 0):
        st.info(f"{cp['amostras_reduzidas']} jogador(es) com menos de 5 atuações disponíveis.")
    excluded = {
        item["id"]: item
        for reference in payload.get("referencias_producao", {}).values()
        for item in reference.get("excluidos_da_base_referencia", [])
    }
    if excluded:
        with st.expander(f"{len(excluded)} jogador(es) excluído(s) das referências sem vínculo seguro com o Cartola"):
            st.json(list(excluded.values()))
    st.write(
        f"Produção: **{cp.get('jogadores_concluidos', 0)} completa(s)** | "
        f"**{cp.get('jogadores_parciais', 0)} parcial(is)** | "
        f"**{cp.get('jogadores_ausentes', 0)} ausente(s)**"
    )

    if payload["erros"]:
        with st.expander("Ver erros"):
            st.json(payload["erros"])

    json_bytes = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
        default=str,
    ).encode("utf-8")

    st.download_button(
        "BAIXAR MAC_DADOS V2",
        data=json_bytes,
        file_name=f"MAC_DADOS_R{payload['rodada']}_V2.json",
        mime="application/json",
        use_container_width=True,
    )

    with st.expander("Ver jogadores coletados"):
        for p in payload["jogadores"]:
            prod = p.get("producao", {})
            st.write(
                f"**{p['jogador']}** — {p['posicao']} / {p['clube']} "
                f"— Produção: **{prod.get('status', 'ausente')}**"
            )

    with st.expander("Ver checkpoint"):
        st.json(payload.get("checkpoint", {}))
