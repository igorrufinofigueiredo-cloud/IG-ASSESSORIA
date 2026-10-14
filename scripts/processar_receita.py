"""Base de prospecção da IG Assessoria a partir dos dados abertos do CNPJ (Receita Federal).

Baixa o mês mais recente do compartilhamento público da Receita, separa as empresas ATIVAS
dos nichos da IG (odontologia, clínicas, estética, advocacia, contabilidade, fisioterapia,
psicologia, nutrição e barbearias), junta razão social, porte e MEI, e grava um arquivo por
estado e nicho, compactado e cifrado (só o CRM tem a chave para abrir).

Uso: python scripts/processar_receita.py --saida saida/
Teste local: python scripts/processar_receita.py --local pasta_com_zips --mes 2026-09 --saida saida/
"""
import argparse, base64, csv, gzip, io, json, os, re, struct, sys, time, unicodedata, urllib.parse, urllib.request, zipfile
from concurrent.futures import ThreadPoolExecutor
import xml.etree.ElementTree as ET
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

WEBDAV = os.environ.get("RFB_WEBDAV_URL", "https://arquivos.receitafederal.gov.br/public.php/webdav/")
TOKEN = os.environ.get("RFB_SHARE_TOKEN", "YggdBLfdninEJX9")  # link público da Receita, não é segredo
MUNICIPIOS_IBGE = "https://raw.githubusercontent.com/kelvins/municipios-brasileiros/main/csv/municipios.csv"
ESTADOS_IBGE = "https://raw.githubusercontent.com/kelvins/municipios-brasileiros/main/csv/estados.csv"

# Nicho da IG -> CNAEs (atividade principal)
NICHOS = {
    "odontologia": {"8630504"},
    "clinica": {"8630501", "8630502", "8630503"},
    "estetica": {"9602502"},
    "advocacia": {"6911701"},
    "contabilidade": {"6920601", "6920602"},
    "fisioterapia": {"8650004"},
    "psicologia": {"8650003"},
    "nutricao": {"8650002"},
    "barbearia": {"9602501"},  # cabeleireiros: só entra quem tem BARB no nome
}
CNAE_NICHO = {c: n for n, cs in NICHOS.items() for c in cs}
CNAE_DESC = {
    "8630504": "Atividade odontológica", "8630501": "Atividade médica ambulatorial com recursos para procedimentos cirúrgicos",
    "8630502": "Atividade médica ambulatorial com recursos para exames complementares", "8630503": "Atividade médica ambulatorial restrita a consultas",
    "9602502": "Atividades de estética e outros serviços de cuidados com a beleza", "6911701": "Serviços advocatícios",
    "6920601": "Atividades de contabilidade", "6920602": "Atividades de consultoria e auditoria contábil e tributária",
    "8650004": "Atividades de fisioterapia", "8650003": "Atividades de psicologia e psicanálise", "8650002": "Atividades de profissionais da nutrição",
    "9602501": "Cabeleireiros, manicure e pedicure",
}
PORTE = {"01": "MICRO EMPRESA", "03": "EMPRESA DE PEQUENO PORTE", "05": "DEMAIS"}


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def req(url, method="GET", headers=None, auth=True):
    h = dict(headers or {})
    if auth:
        h["Authorization"] = "Basic " + base64.b64encode(f"{TOKEN}:".encode()).decode()
    return urllib.request.Request(url, method=method, headers=h)


def listar(caminho=""):
    with urllib.request.urlopen(req(WEBDAV + caminho, "PROPFIND", {"Depth": "1"}), timeout=120) as r:
        arvore = ET.fromstring(r.read())
    ns = {"d": "DAV:"}
    itens = []
    for resp in arvore.findall("d:response", ns):
        nome = resp.findtext("d:href", "", ns).rstrip("/").rsplit("/", 1)[-1]
        tam = resp.findtext(".//d:getcontentlength", "0", ns)
        if nome and nome != caminho.strip("/"):
            itens.append((urllib.parse.unquote(nome), int(tam or 0)))
    return itens


def baixar(url, destino, tamanho=0, auth=True, tentativas=8):
    destino.parent.mkdir(parents=True, exist_ok=True)
    for n in range(1, tentativas + 1):
        ja = destino.stat().st_size if destino.exists() else 0
        if tamanho and ja == tamanho:
            return destino
        hdr = {"Range": f"bytes={ja}-"} if ja else {}
        try:
            with urllib.request.urlopen(req(url, headers=hdr, auth=auth), timeout=180) as r, \
                    open(destino, "ab" if ja and r.status == 206 else "wb") as f:
                while bloco := r.read(1 << 20):
                    f.write(bloco)
            if not tamanho or destino.stat().st_size == tamanho:
                return destino
            raise IOError(f"tamanho {destino.stat().st_size} != {tamanho}")
        except Exception as e:  # a rede da Receita oscila: retoma de onde parou
            log(f"  {destino.name}: tentativa {n}/{tentativas} falhou ({e})")
            time.sleep(min(120, 15 * n))
    raise RuntimeError(f"não consegui baixar {url}")


def linhas_zip(caminho):
    with zipfile.ZipFile(caminho) as z:
        for membro in z.namelist():
            with io.TextIOWrapper(z.open(membro), encoding="latin-1", newline="") as txt:
                for linha in csv.reader((l.replace("\x00", "") for l in txt), delimiter=";", quotechar='"'):
                    yield linha


def slug(s):
    s = unicodedata.normalize("NFD", (s or "").lower())
    return re.sub(r"[^a-z0-9]+", "-", "".join(c for c in s if unicodedata.category(c) != "Mn")).strip("-")


def fone(ddd, num):
    d = re.sub(r"\D", "", (ddd or "") + (num or ""))
    d = d.lstrip("0")
    return d if 10 <= len(d) <= 11 else ""


def data_iso(d):
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}" if d and len(d) == 8 and d != "00000000" else ""


def cifrar(dados, chave_publica):
    chave = AESGCM.generate_key(bit_length=256)
    nonce = os.urandom(12)
    ct = AESGCM(chave).encrypt(nonce, dados, None)
    embrulho = chave_publica.encrypt(chave, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
    return b"IGR1" + struct.pack(">H", len(embrulho)) + embrulho + nonce + ct


def mapa_municipios(pasta, local):
    """Código da Receita (SIAFI/TOM) -> (IBGE, nome, UF, DDD)."""
    arq_m, arq_e = pasta / "municipios_ibge.csv", pasta / "estados_ibge.csv"
    if not local:
        baixar(MUNICIPIOS_IBGE, arq_m, auth=False)
        baixar(ESTADOS_IBGE, arq_e, auth=False)
    uf = {}
    with open(arq_e, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            uf[r["codigo_uf"]] = r["uf"]
    m = {}
    with open(arq_m, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            m[r["siafi_id"].zfill(4)] = (r["codigo_ibge"], r["nome"], uf[r["codigo_uf"]], r["ddd"])
    return m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--saida", default="saida")
    ap.add_argument("--local", help="pasta com os .zip já baixados (teste)")
    ap.add_argument("--mes", help="AAAA-MM; padrão: o mais recente publicado")
    ap.add_argument("--chave", default=str(Path(__file__).with_name("chave_publica.pem")))
    a = ap.parse_args()

    saida = Path(a.saida); saida.mkdir(parents=True, exist_ok=True)
    trabalho = Path(a.local) if a.local else Path("dados_rf")
    chave_publica = serialization.load_pem_public_key(Path(a.chave).read_bytes())

    if a.local:
        mes = a.mes or "local"
        arquivos = {p.name: 0 for p in trabalho.glob("*.zip")}
    else:
        meses = sorted(n for n, _ in listar() if re.fullmatch(r"\d{4}-\d{2}", n))
        mes = a.mes or meses[-1]
        arquivos = dict(listar(mes + "/"))
        log(f"mês {mes}: {len(arquivos)} arquivos")

    # O servidor da Receita é lento por conexão: baixa 4 arquivos ao mesmo tempo, já na ordem em que serão lidos.
    precisos = sorted(n for n in arquivos if re.match(r"(estabelecimentos|empresas|simples)", n.lower()))
    ordem = {n: i for i, n in enumerate(sorted(precisos, key=lambda n: (0 if n.lower().startswith("estab") else 1 if n.lower().startswith("empresas") else 2, n)))}
    pool = ThreadPoolExecutor(max_workers=4)
    futuros = {} if a.local else {n: pool.submit(baixar, f"{WEBDAV}{mes}/{n}", trabalho / n, arquivos.get(n, 0)) for n in sorted(precisos, key=ordem.get)}

    def obter(nome):
        if a.local:
            return trabalho / nome
        return futuros[nome].result()

    def apagar(p):
        if not a.local:
            p.unlink(missing_ok=True)

    municipios = mapa_municipios(trabalho if a.local else trabalho, a.local)
    partes = {}      # (uf, nicho) -> lista de linhas
    precisa = {}     # cnpj_basico -> [linhas que esperam razão social / porte / MEI]
    ddd_de = {}      # código IBGE -> DDD da cidade
    uso_tel = {}     # telefone -> empresas (CNPJ básico) que usam esse número
    tel_contador = set()  # telefones de escritórios de contabilidade
    vistos = 0

    # 1. Estabelecimentos: ativos dos nichos, com telefone ou e-mail
    for nome in sorted(n for n in arquivos if n.lower().startswith("estabelecimentos")):
        caminho = obter(nome)
        log(f"lendo {nome}")
        for c in linhas_zip(caminho):
            vistos += 1
            if len(c) < 28 or c[5] != "02":
                continue
            nicho = CNAE_NICHO.get(c[11])
            if not nicho:
                continue
            fantasia = c[4].strip()
            if nicho == "barbearia" and "BARB" not in fantasia.upper():
                continue
            t1, t2 = fone(c[21], c[22]), fone(c[23], c[24])
            email = c[27].strip().lower()
            if not (t1 or t2):
                continue
            mun = municipios.get(c[20].zfill(4))
            if not mun:
                continue
            ibge, _, uf, ddd_mun = mun
            endereco = " ".join(x for x in [c[13].strip().title(), c[14].strip().title()] if x)
            if c[15].strip() and c[15].strip() not in ("S/N", "SN"):
                endereco += ", " + c[15].strip()
            linha = [c[0] + c[1] + c[2], fantasia, "", ibge, c[17].strip().title(), endereco, c[18].strip(), t1, t2,
                     email if "@" in email else "", data_iso(c[10]), c[11], "", 0, None, 1 if c[3] == "1" else 2]
            partes.setdefault((uf, nicho), []).append(linha)
            precisa.setdefault(c[0], []).append(linha)
            ddd_de[ibge] = ddd_mun
            for t in (t1, t2):
                if t:
                    donos = uso_tel.setdefault(t, set())
                    if len(donos) < 5:
                        donos.add(c[0])
                    if nicho == "contabilidade":
                        tel_contador.add(t)
        apagar(caminho)
        log(f"  {sum(len(v) for v in partes.values())} estabelecimentos dos nichos até agora ({vistos} lidos)")

    # 1b. Limpeza dos telefones: só fica número que é da própria empresa e da cidade dela.
    #     Sai: DDD de outra região, número de escritório de contabilidade (quem abriu o CNPJ)
    #     e número repetido em 3 ou mais empresas diferentes (central, contador ou despachante).
    #     Sem nenhum número bom, a empresa sai da base.
    cortes = {"ddd": 0, "contador": 0, "repetido": 0, "sem_numero": 0}
    for chave in list(partes):
        nicho = chave[1]
        boas = []
        for l in partes[chave]:
            ddd = ddd_de.get(l[3], "")
            fones = []
            for t in (l[7], l[8]):
                if not t:
                    continue
                if ddd and t[:2] != ddd:
                    cortes["ddd"] += 1; continue
                if nicho != "contabilidade" and t in tel_contador:
                    cortes["contador"] += 1; continue
                if len(uso_tel.get(t, ())) >= 3:
                    cortes["repetido"] += 1; continue
                fones.append(t)
            fones.sort(key=lambda t: 0 if len(t) == 11 and t[2] == "9" else 1)  # celular primeiro
            if not fones:
                cortes["sem_numero"] += 1
                continue
            l[7], l[8] = fones[0], (fones[1] if len(fones) > 1 else "")
            if nicho != "contabilidade" and re.search(r"contab|contador|escritorio", l[9] or ""):
                l[9] = ""
            boas.append(l)
        partes[chave] = boas
    log(f"limpeza dos telefones: {cortes}; ficaram {sum(len(v) for v in partes.values())} empresas")
    uso_tel.clear(); tel_contador.clear()

    # 2. Empresas: razão social, porte e capital
    for nome in sorted(n for n in arquivos if n.lower().startswith("empresas")):
        caminho = obter(nome)
        log(f"lendo {nome}")
        for c in linhas_zip(caminho):
            ls = precisa.get(c[0]) if c else None
            if not ls:
                continue
            cap = c[4].replace(".", "").replace(",", ".") if len(c) > 4 else ""
            for l in ls:
                l[2] = c[1].strip()
                l[12] = PORTE.get(c[5], "") if len(c) > 5 else ""
                try:
                    l[14] = float(cap) if cap else None
                except ValueError:
                    pass
        apagar(caminho)

    # 3. Simples: quem é MEI
    for nome in sorted(n for n in arquivos if n.lower().startswith("simples")):
        caminho = obter(nome)
        log(f"lendo {nome}")
        for c in linhas_zip(caminho):
            ls = precisa.get(c[0]) if c else None
            if ls and len(c) > 4 and c[4] == "S":
                for l in ls:
                    l[13] = 1
        apagar(caminho)

    # 4. Um arquivo por estado e nicho, compactado e cifrado + manifesto aberto (só contagens)
    manifesto = {"versao": 1, "mes": mes, "gerado_em": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 "campos": ["cnpj", "fantasia", "razao", "ibge", "bairro", "endereco", "cep", "tel1", "tel2", "email",
                            "abertura", "cnae", "porte", "mei", "capital", "matriz"],
                 "cnae": CNAE_DESC, "partes": {}}
    total = 0
    for (uf, nicho), linhas in sorted(partes.items()):
        if not linhas:
            continue
        for l in linhas:
            if not l[1]:
                l[1] = l[2]
        corpo = gzip.compress(json.dumps(linhas, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), 9)
        (saida / f"{uf}-{nicho}.bin").write_bytes(cifrar(corpo, chave_publica))
        cont = {}
        for l in linhas:
            cont[l[3]] = cont.get(l[3], 0) + 1
        manifesto["partes"][f"{uf}-{nicho}"] = {"linhas": len(linhas), "municipios": cont}
        total += len(linhas)
    manifesto["total"] = total
    (saida / "manifesto.json").write_text(json.dumps(manifesto, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    manifesto["limpeza"] = cortes
    (saida / "manifesto.json").write_text(json.dumps(manifesto, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    pool.shutdown(wait=False, cancel_futures=True)
    log(f"pronto: {total} empresas em {len(partes)} arquivos (mês {mes})")


if __name__ == "__main__":
    main()
