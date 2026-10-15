"""Base de prospecção da IG Assessoria a partir dos dados abertos do CNPJ (Receita Federal).

Baixa o mês mais recente do compartilhamento público da Receita, separa as empresas ATIVAS
dos nichos listados em scripts/nichos.json (atividade principal), junta razão social, porte
e MEI, limpa os telefones e grava um arquivo por estado e nicho, compactado e cifrado (só o
CRM tem a chave para abrir).

Os dados intermediários ficam num SQLite em disco: o Brasil inteiro com dezenas de nichos não
cabe com folga na memória do runner.

Uso: python scripts/processar_receita.py --saida saida/
Teste local: python scripts/processar_receita.py --local pasta_com_zips --mes 2026-09 --saida saida/
"""
import argparse, base64, csv, gzip, io, json, os, re, sqlite3, struct, threading, time, unicodedata, urllib.parse, urllib.request, zipfile
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
PORTE = {"01": "MICRO EMPRESA", "03": "EMPRESA DE PEQUENO PORTE", "05": "DEMAIS"}
POR_RELEASE = 900
CAMPOS = ["cnpj", "fantasia", "razao", "ibge", "bairro", "endereco", "cep", "tel1", "tel2", "email",
          "abertura", "cnae", "porte", "mei", "capital", "matriz"]

BAIXADO = {"bytes": 0}
_trava = threading.Lock()


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def carregar_nichos(caminho):
    nichos = json.loads(Path(caminho).read_text(encoding="utf-8"))
    por_cnae = {}
    for n in nichos:
        for c in n["cnaes"]:
            por_cnae.setdefault(c, []).append(n)
    return nichos, por_cnae


def nicho_de(cnae, fantasia, razao, por_cnae):
    """CNAE principal -> nicho. Quando dois nichos dividem o CNAE (barbearia e salão), decide pelo nome."""
    nome = (fantasia + " " + razao).upper()
    for n in por_cnae.get(cnae, ()):
        if n.get("nome_tem") and n["nome_tem"] not in nome:
            continue
        if n.get("nome_sem") and n["nome_sem"] in nome:
            continue
        return n["id"]
    return None


def req(url, method="GET", headers=None, auth=True):
    h = dict(headers or {})
    if auth:
        h["Authorization"] = "Basic " + base64.b64encode(f"{TOKEN}:".encode()).decode()
    return urllib.request.Request(url, method=method, headers=h)


def listar(caminho="", tentativas=8):
    for n in range(1, tentativas + 1):
        try:
            with urllib.request.urlopen(req(WEBDAV + caminho, "PROPFIND", {"Depth": "1"}), timeout=180) as r:
                arvore = ET.fromstring(r.read())
            break
        except Exception as e:
            log(f"  listagem de '{caminho or '/'}': tentativa {n}/{tentativas} falhou ({e})")
            if n == tentativas:
                raise
            time.sleep(min(120, 20 * n))
    ns = {"d": "DAV:"}
    itens = []
    for resp in arvore.findall("d:response", ns):
        nome = resp.findtext("d:href", "", ns).rstrip("/").rsplit("/", 1)[-1]
        tam = resp.findtext(".//d:getcontentlength", "0", ns)
        if nome and nome != caminho.strip("/"):
            itens.append((urllib.parse.unquote(nome), int(tam or 0)))
    return itens


def baixar(url, destino, tamanho=0, auth=True, tentativas=14):
    """Baixa com retomada. Se a retomada (Range) falhar duas vezes seguidas sem receber nada,
    o servidor provavelmente não aceita continuar aquele arquivo: apaga e baixa do zero."""
    destino.parent.mkdir(parents=True, exist_ok=True)
    retomadas_vazias = 0
    for n in range(1, tentativas + 1):
        ja = destino.stat().st_size if destino.exists() else 0
        if tamanho and ja == tamanho:
            return destino
        if tamanho and ja > tamanho:
            destino.unlink(); ja = 0
        if ja and retomadas_vazias >= 2:
            log(f"  {destino.name}: a retomada não funciona, baixando do zero")
            destino.unlink(); ja = 0; retomadas_vazias = 0
        hdr = {"Range": f"bytes={ja}-"} if ja else {}
        recebido = 0
        try:
            with urllib.request.urlopen(req(url, headers=hdr, auth=auth), timeout=300) as r, \
                    open(destino, "ab" if ja and r.status == 206 else "wb") as f:
                while bloco := r.read(1 << 20):
                    f.write(bloco)
                    recebido += len(bloco)
                    with _trava:
                        BAIXADO["bytes"] += len(bloco)
            if not tamanho or destino.stat().st_size == tamanho:
                log(f"  baixado: {destino.name} ({destino.stat().st_size / 1e6:,.0f} MB)")
                return destino
            raise IOError(f"tamanho {destino.stat().st_size} != {tamanho}")
        except Exception as e:  # a rede da Receita oscila: retoma de onde parou
            retomadas_vazias = retomadas_vazias + 1 if (ja and recebido == 0) else 0
            log(f"  {destino.name}: tentativa {n}/{tentativas} falhou depois de {recebido / 1e6:,.0f} MB ({e})")
            time.sleep(min(90, 10 * n))
    raise RuntimeError(f"não consegui baixar {url}")


def linhas_zip(caminho):
    with zipfile.ZipFile(caminho) as z:
        for membro in z.namelist():
            with io.TextIOWrapper(z.open(membro), encoding="latin-1", newline="") as txt:
                for linha in csv.reader((l.replace("\x00", "") for l in txt), delimiter=";", quotechar='"'):
                    yield linha


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


def abrir_banco(caminho):
    caminho.unlink(missing_ok=True)
    db = sqlite3.connect(caminho)
    db.executescript("""
        PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA temp_store=FILE; PRAGMA cache_size=-400000;
        CREATE TABLE est(basico TEXT, uf TEXT, nicho TEXT, ibge TEXT, ddd TEXT, cnpj TEXT, fantasia TEXT, bairro TEXT,
                         endereco TEXT, cep TEXT, t1 TEXT, t2 TEXT, email TEXT, abertura TEXT, cnae TEXT, matriz INT);
        CREATE TABLE tel(t TEXT, basico TEXT, contador INT);
        CREATE TABLE emp(basico TEXT PRIMARY KEY, razao TEXT, porte TEXT, capital REAL) WITHOUT ROWID;
        CREATE TABLE mei(basico TEXT PRIMARY KEY) WITHOUT ROWID;
    """)
    return db


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--saida", default="saida")
    ap.add_argument("--local", help="pasta com os .zip já baixados (teste)")
    ap.add_argument("--mes", help="AAAA-MM; padrão: o mais recente publicado")
    ap.add_argument("--chave", default=str(Path(__file__).with_name("chave_publica.pem")))
    ap.add_argument("--nichos", default=str(Path(__file__).with_name("nichos.json")))
    a = ap.parse_args()

    saida = Path(a.saida); saida.mkdir(parents=True, exist_ok=True)
    trabalho = Path(a.local) if a.local else Path("dados_rf")
    trabalho.mkdir(parents=True, exist_ok=True)
    chave_publica = serialization.load_pem_public_key(Path(a.chave).read_bytes())
    nichos, por_cnae = carregar_nichos(a.nichos)
    log(f"{len(nichos)} nichos, {len(por_cnae)} CNAEs")

    if a.local:
        mes = a.mes or "local"
        arquivos = {p.name: 0 for p in trabalho.glob("*.zip")}
    else:
        meses = sorted(n for n, _ in listar() if re.fullmatch(r"\d{4}-\d{2}", n))
        mes = a.mes or meses[-1]
        arquivos = dict(listar(mes + "/"))
        log(f"mês {mes}: {len(arquivos)} arquivos")

    # O servidor da Receita é lento por conexão: baixa vários arquivos ao mesmo tempo, na ordem em que serão lidos.
    def grupo(n):
        n = n.lower()
        return 0 if n.startswith("estab") else 1 if n.startswith("empresas") else 2 if n.startswith("simples") else 3 if n.startswith("cnae") else 9
    precisos = sorted((n for n in arquivos if grupo(n) < 9), key=lambda n: (grupo(n), n))
    total_bytes = sum(arquivos.get(n, 0) for n in precisos)
    pool = ThreadPoolExecutor(max_workers=6)
    futuros = {} if a.local else {n: pool.submit(baixar, f"{WEBDAV}{mes}/{n}", trabalho / n, arquivos.get(n, 0)) for n in precisos}

    parar = threading.Event()
    def andamento():
        inicio = time.time()
        while not parar.wait(90):
            mb = BAIXADO["bytes"] / 1e6
            vel = mb / max(1, time.time() - inicio)
            log(f"  download: {mb:,.0f} de {total_bytes / 1e6:,.0f} MB ({vel:.1f} MB/s)")
    if not a.local:
        threading.Thread(target=andamento, daemon=True).start()

    def obter(nome):
        return trabalho / nome if a.local else futuros[nome].result()

    def apagar(p):
        if not a.local:
            p.unlink(missing_ok=True)

    municipios = mapa_municipios(trabalho, a.local)
    db = abrir_banco(trabalho / "base.sqlite")
    basicos = set()   # CNPJ básico das empresas que ficaram (para casar razão social e MEI)
    vistos = 0

    # 1. Estabelecimentos: ativos dos nichos, com telefone
    for nome in [n for n in precisos if grupo(n) == 0]:
        caminho = obter(nome)
        log(f"lendo {nome}")
        lote_est, lote_tel = [], []
        for c in linhas_zip(caminho):
            vistos += 1
            if len(c) < 28 or c[5] != "02" or c[11] not in por_cnae:
                continue
            fantasia = c[4].strip()
            nicho = nicho_de(c[11], fantasia, "", por_cnae)
            if not nicho:
                continue
            t1, t2 = fone(c[21], c[22]), fone(c[23], c[24])
            if not (t1 or t2):
                continue
            mun = municipios.get(c[20].zfill(4))
            if not mun:
                continue
            ibge, _, uf, ddd_mun = mun
            endereco = " ".join(x for x in [c[13].strip().title(), c[14].strip().title()] if x)
            if c[15].strip() and c[15].strip() not in ("S/N", "SN"):
                endereco += ", " + c[15].strip()
            email = c[27].strip().lower()
            lote_est.append((c[0], uf, nicho, ibge, ddd_mun, c[0] + c[1] + c[2], fantasia, c[17].strip().title(), endereco,
                             c[18].strip(), t1, t2, email if "@" in email else "", data_iso(c[10]), c[11], 1 if c[3] == "1" else 2))
            basicos.add(c[0])
            for t in {t1, t2} - {""}:
                lote_tel.append((t, c[0], 1 if nicho == "contabilidade" else 0))
            if len(lote_est) >= 50000:
                db.executemany("INSERT INTO est VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", lote_est)
                db.executemany("INSERT INTO tel VALUES (?,?,?)", lote_tel)
                lote_est, lote_tel = [], []
        db.executemany("INSERT INTO est VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", lote_est)
        db.executemany("INSERT INTO tel VALUES (?,?,?)", lote_tel)
        db.commit()
        apagar(caminho)
        log(f"  {len(basicos):,} empresas dos nichos até agora ({vistos:,} linhas lidas)")

    # 1b. Telefones ruins: de escritório de contabilidade (quem abriu o CNPJ) ou repetidos
    #     em 3 ou mais empresas diferentes (central, contador ou despachante).
    log("procurando telefones repetidos e de contador")
    repetidos = {t for (t,) in db.execute("SELECT t FROM tel GROUP BY t HAVING COUNT(DISTINCT basico) >= 3")}
    tel_contador = {t for (t,) in db.execute("SELECT DISTINCT t FROM tel WHERE contador = 1")}
    db.execute("DROP TABLE tel"); db.commit()
    log(f"  {len(repetidos):,} números repetidos, {len(tel_contador):,} de contabilidade")

    # 2. Empresas: razão social, porte e capital
    for nome in [n for n in precisos if grupo(n) == 1]:
        caminho = obter(nome)
        log(f"lendo {nome}")
        lote = []
        for c in linhas_zip(caminho):
            if not c or c[0] not in basicos:
                continue
            cap = c[4].replace(".", "").replace(",", ".") if len(c) > 4 else ""
            try:
                cap = float(cap) if cap else None
            except ValueError:
                cap = None
            lote.append((c[0], c[1].strip(), PORTE.get(c[5], "") if len(c) > 5 else "", cap))
            if len(lote) >= 50000:
                db.executemany("INSERT OR REPLACE INTO emp VALUES (?,?,?,?)", lote); lote = []
        db.executemany("INSERT OR REPLACE INTO emp VALUES (?,?,?,?)", lote); db.commit()
        apagar(caminho)

    # 3. Simples: quem é MEI
    for nome in [n for n in precisos if grupo(n) == 2]:
        try:
            caminho = obter(nome)
        except Exception as e:
            log(f"  sem {nome} ({e}): a base sai sem a marcação de MEI"); continue
        log(f"lendo {nome}")
        lote = []
        for c in linhas_zip(caminho):
            if c and len(c) > 4 and c[4] == "S" and c[0] in basicos:
                lote.append((c[0],))
        db.executemany("INSERT OR IGNORE INTO mei VALUES (?)", lote); db.commit()
        apagar(caminho)
    basicos.clear()

    # 4. Descrição das atividades (tabela de CNAEs da própria Receita)
    cnae_desc = {}
    for nome in [n for n in precisos if grupo(n) == 3]:
        try:
            caminho = obter(nome)
        except Exception as e:
            log(f"  sem {nome} ({e})"); continue
        for c in linhas_zip(caminho):
            if len(c) >= 2 and c[0] in por_cnae:
                cnae_desc[c[0]] = c[1].strip()
        apagar(caminho)
    for n in nichos:
        for c in n["cnaes"]:
            cnae_desc.setdefault(c, n["nome"])

    # 5. Limpeza dos telefones e um arquivo por estado e nicho, compactado e cifrado.
    #    Só fica número da própria empresa e com o DDD da cidade dela; sem número bom, a empresa sai.
    log("gravando os arquivos")
    db.execute("CREATE INDEX est_parte ON est(uf, nicho)"); db.commit()
    manifesto = {"versao": 3, "mes": mes, "gerado_em": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 "campos": CAMPOS, "cnae": cnae_desc,
                 "nichos": {n["id"]: {"nome": n["nome"], "rotulo": n["rotulo"]} for n in nichos},
                 "partes": {}}
    cortes = {"ddd": 0, "contador": 0, "repetido": 0, "sem_numero": 0}
    total = 0
    sql = """SELECT e.cnpj, e.fantasia, COALESCE(m.razao, ''), e.ibge, e.bairro, e.endereco, e.cep, e.t1, e.t2, e.email,
                    e.abertura, e.cnae, COALESCE(m.porte, ''), CASE WHEN s.basico IS NULL THEN 0 ELSE 1 END, m.capital, e.matriz, e.ddd
             FROM est e LEFT JOIN emp m ON m.basico = e.basico LEFT JOIN mei s ON s.basico = e.basico
             WHERE e.uf = ? AND e.nicho = ?"""
    for uf, nicho in db.execute("SELECT DISTINCT uf, nicho FROM est ORDER BY uf, nicho").fetchall():
        linhas = []
        for r in db.execute(sql, (uf, nicho)):
            l = list(r[:16]); ddd = r[16]
            fones = []
            for t in (l[7], l[8]):
                if not t or t in fones:
                    continue
                if ddd and t[:2] != ddd:
                    cortes["ddd"] += 1; continue
                if nicho != "contabilidade" and t in tel_contador:
                    cortes["contador"] += 1; continue
                if t in repetidos:
                    cortes["repetido"] += 1; continue
                fones.append(t)
            if not fones:
                cortes["sem_numero"] += 1
                continue
            fones.sort(key=lambda t: 0 if len(t) == 11 and t[2] == "9" else 1)  # celular primeiro
            l[7], l[8] = fones[0], (fones[1] if len(fones) > 1 else "")
            if nicho != "contabilidade" and re.search(r"contab|contador|escritorio", l[9] or ""):
                l[9] = ""
            if not l[1]:
                l[1] = l[2]
            linhas.append(l)
        if not linhas:
            continue
        corpo = gzip.compress(json.dumps(linhas, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), 9)
        # Um release do GitHub aceita até 1000 arquivos: as partes vão em pastas de 900 (um release por pasta).
        rel = len(manifesto["partes"]) // POR_RELEASE
        (saida / f"rel{rel}").mkdir(exist_ok=True)
        (saida / f"rel{rel}" / f"{uf}-{nicho}.bin").write_bytes(cifrar(corpo, chave_publica))
        cont = {}
        for l in linhas:
            cont[l[3]] = cont.get(l[3], 0) + 1
        manifesto["partes"][f"{uf}-{nicho}"] = {"linhas": len(linhas), "municipios": cont, "rel": rel}
        total += len(linhas)
    manifesto["total"] = total
    manifesto["limpeza"] = cortes
    (saida / "manifesto.json").write_text(json.dumps(manifesto, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    parar.set()
    pool.shutdown(wait=False, cancel_futures=True)
    db.close()
    log(f"limpeza dos telefones: {cortes}")
    log(f"pronto: {total:,} empresas em {len(manifesto['partes'])} arquivos (mês {mes})")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"ERRO: {e!r}")
        os._exit(1)  # sem esperar os downloads que ainda estão em andamento
    os._exit(0)
