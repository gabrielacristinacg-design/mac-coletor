
import json
import re
from datetime import datetime, timezone

import requests
import streamlit as st
from bs4 import BeautifulSoup, Tag

BASE = "https://www.cartolafcbrasil.com.br"

# Trava de identidade do protótipo:
# ID + nome + clube + posição. O nome sozinho nunca identifica um atleta.
PROTOTIPO = {
    "Samuel Lino": {
        "id": "98873",
        "clube": "Flamengo",
        "posicao": "ATA",
        "url": f"{BASE}/scouts/cartola-fc-2026/atleta/flamengo/98873/samuel-lino",
    },
    "Luciano Juba": {
        "id": "107093",
        "clube": "Bahia",
        "posicao": "LAT",
        "url": f"{BASE}/scouts/cartola-fc-2026/atleta/bahia/107093/luciano-juba",
    },
    "Canobbio": {
        "id": "102928",
        "clube": "Fluminense",
        "posicao": "ATA",
        "url": f"{BASE}/scouts/cartola-fc-2026/atleta/fluminense/102928/canobbio",
    },
    "Gabriel": {
        "id": "83257",
        "clube": "Santos",
        "posicao": "ATA",
        "url": f"{BASE}/scouts/cartola-fc-2026/atleta/santos/83257/gabriel",
    },
    "Marcelinho": {
        "id": "97795",
        "clube": "Remo",
        "posicao": "LAT",
        "url": f"{BASE}/scouts/cartola-fc-2026/atleta/remo/97795/marcelinho",
    },
}

SCOUT_CODES = {
    "DS","G","A","SG","FS","FF","FD","FT","PS","DE","DP",
    "GC","CV","CA","GS","PP","PC","FC","I","V"
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
}

def clean(s):
    return re.sub(r"\s+", " ", s or "").strip()

def parse_entry(text, contexto):
    text = clean(text)
    m = re.search(
        r"(\d+)\s*x\s*(\d+)\s*\(Rodada\s*#\s*(\d+)\)\s*(-?\d+(?:[.,]\d+)?)\s*(.*)$",
        text,
        re.I,
    )
    if not m:
        return None

    gols_a, gols_b, rodada, pontos, resto = m.groups()
    tokens = resto.split()
    scouts = {}
    i = 0

    while i + 1 < len(tokens):
        if re.fullmatch(r"-?\d+(?:[.,]\d+)?", tokens[i]) and tokens[i + 1].upper() in SCOUT_CODES:
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

    entries = []
    seen = set()

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
    m = re.search(re.escape(label) + r"\s*:\s*(-?\d+(?:[.,]\d+)?)", text, re.I)
    return float(m.group(1).replace(",", ".")) if m else None

def athlete_id_from_url(url):
    m = re.search(r"/atleta/[^/]+/(\d+)/", url)
    return m.group(1) if m else None

def fetch_player(target, timeout=20):
    url = target["url"]
    r = requests.get(url, headers=HEADERS, timeout=timeout)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")
    text = clean(soup.get_text(" ", strip=True))

    h2 = soup.find("h2")
    nome = clean(h2.get_text(" ", strip=True)) if h2 else None

    pos_clube = re.search(r"\((GOL|LAT|ZAG|MEI|ATA|TEC)\s*-\s*([^)]+)\)", text, re.I)
    posicao = pos_clube.group(1).upper() if pos_clube else None
    clube = clean(pos_clube.group(2)) if pos_clube else None
    atleta_id = athlete_id_from_url(url)

    # Trava: se a página não corresponder ao atleta esperado, a coleta falha.
    inconsistencias = []
    if atleta_id != target["id"]:
        inconsistencias.append(f"ID esperado {target['id']}, recebido {atleta_id}")
    if posicao != target["posicao"]:
        inconsistencias.append(f"posição esperada {target['posicao']}, recebida {posicao}")
    if (clube or "").casefold() != target["clube"].casefold():
        inconsistencias.append(f"clube esperado {target['clube']}, recebido {clube}")

    if inconsistencias:
        raise ValueError("TRAVA DE IDENTIDADE: " + "; ".join(inconsistencias))

    media_geral = metric(text, "Média")
    media_casa = metric(text, "Média em Casa")
    media_fora = metric(text, "Média Fora")

    casa = section_entries(soup, "Pontuações em Casa", "casa")
    fora = section_entries(soup, "Pontuações Fora", "fora")

    hist = casa + fora
    hist.sort(key=lambda x: x["rodada"])

    return {
        "atleta_id": atleta_id,
        "nome": nome,
        "clube": clube,
        "posicao": posicao,
        "identidade_validada": True,
        "medias": {
            "geral": media_geral,
            "casa": media_casa,
            "fora": media_fora,
        },
        "historico": hist,
        "fonte_url": url,
    }

def fill_missing_rounds(player, rodada_referencia, janela):
    if janela <= 0:
        return player

    inicio = max(1, rodada_referencia - janela)
    fim = max(0, rodada_referencia - 1)
    by_round = {x["rodada"]: x for x in player["historico"]}

    out = []
    for rodada in range(inicio, fim + 1):
        if rodada in by_round:
            out.append(by_round[rodada])
        else:
            out.append({
                "rodada": rodada,
                "pontos": None,
                "status": "sem_registro",
                "contexto": None,
                "placar_exibido": None,
                "scouts": {},
            })

    player = dict(player)
    player["historico"] = out
    return player

def executar_coleta(selecionados, rodada, janela):
    jogadores = []
    erros = []

    barra = st.progress(0)
    for i, nome in enumerate(selecionados, start=1):
        try:
            atleta = fetch_player(PROTOTIPO[nome])
            atleta = fill_missing_rounds(atleta, int(rodada), int(janela))
            jogadores.append(atleta)
        except Exception as e:
            erros.append({"jogador": nome, "erro": str(e)})
        barra.progress(i / len(selecionados))

    return {
        "schema": "MAC_COLETOR_1.1",
        "modo": "prototipo_identidade_travada",
        "rodada_referencia": int(rodada),
        "fonte": "Cartola FC Brasil",
        "coletado_em_utc": datetime.now(timezone.utc).isoformat(),
        "regra_ausencia": "ausencia_na_fonte_nao_e_zero",
        "regra_identidade": "ID+nome+clube+posicao",
        "jogadores": jogadores,
        "erros": erros,
    }

st.set_page_config(page_title="MAC-Coletor 1.1", page_icon="⚽", layout="centered")

st.title("⚽ MAC-Coletor 1.1")
st.caption("Protótipo mobile — agora com trava ID + clube + posição")

rodada = st.number_input("Rodada de referência", min_value=2, max_value=38, value=25, step=1)
janela = st.slider("Quantas rodadas anteriores guardar", min_value=5, max_value=24, value=12)

selecionados = st.multiselect(
    "Jogadores do teste",
    list(PROTOTIPO.keys()),
    default=list(PROTOTIPO.keys()),
)

st.info(
    "Ausência nunca vira 0. E nenhum atleta é identificado apenas pelo nome: "
    "a coleta valida ID, clube e posição antes de aceitar os dados."
)

if st.button("COLETAR DADOS", type="primary", use_container_width=True):
    if not selecionados:
        st.warning("Selecione ao menos um jogador.")
    else:
        st.session_state["mac_resultado"] = executar_coleta(selecionados, rodada, janela)

# Mantém o resultado na tela mesmo após o rerender do Streamlit.
payload = st.session_state.get("mac_resultado")

if payload:
    jogadores = payload["jogadores"]
    erros = payload["erros"]

    st.success(f"Coleta concluída: {len(jogadores)} jogador(es); {len(erros)} erro(s).")

    for p in jogadores:
        with st.expander(
            f"{p['nome']} — {p['posicao']} / {p['clube']} — ID {p['atleta_id']}"
        ):
            st.write("✅ Identidade validada")
            st.write(
                f"Média geral: **{p['medias']['geral']}** | "
                f"Casa: **{p['medias']['casa']}** | Fora: **{p['medias']['fora']}**"
            )
            st.dataframe(
                [
                    {
                        "Rodada": h["rodada"],
                        "Pontos": h["pontos"],
                        "Status": h["status"],
                        "Contexto": h["contexto"],
                    }
                    for h in p["historico"]
                ],
                use_container_width=True,
                hide_index=True,
            )

    if erros:
        st.error("Algum atleta falhou na trava de identidade ou na coleta.")
        st.json(erros)

    json_bytes = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")

    st.download_button(
        "BAIXAR ARQUIVO PARA O MAC",
        data=json_bytes,
        file_name=f"MAC_DADOS_R{payload['rodada_referencia']}_V1_1.json",
        mime="application/json",
        use_container_width=True,
    )

    st.subheader("Prévia do JSON")
    st.json(payload)
