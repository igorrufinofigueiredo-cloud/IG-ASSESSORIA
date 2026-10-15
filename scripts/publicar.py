"""Publica a base da Receita nos releases do GitHub (pela API REST, com novas tentativas).

saida/manifesto.json + saida/rel0/*  -> release base-MES (o "latest", que o CRM lê)
saida/rel1/*                         -> release base-MES-2
saida/rel2/* ...                     -> base-MES-3 ...
Antes apaga os releases do mesmo mês (inclusive rascunhos que sobraram de uma falha)
e depois guarda só os dois meses mais recentes.
"""
import json, os, re, sys, time, urllib.error, urllib.parse, urllib.request
from pathlib import Path

REPO = os.environ["GITHUB_REPOSITORY"]
TOKEN = os.environ["GH_TOKEN"]
API = os.environ.get("GITHUB_API_URL", "https://api.github.com") + "/repos/" + REPO


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def chamar(metodo, url, corpo=None, tipo="application/json", tentativas=6):
    dados = json.dumps(corpo).encode() if isinstance(corpo, (dict, list)) else corpo
    for n in range(1, tentativas + 1):
        r = urllib.request.Request(url, data=dados, method=metodo, headers={
            "Authorization": "Bearer " + TOKEN, "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", **({"Content-Type": tipo} if dados is not None else {})})
        try:
            with urllib.request.urlopen(r, timeout=300) as resp:
                txt = resp.read()
                return json.loads(txt) if txt else None
        except urllib.error.HTTPError as e:
            corpo_erro = e.read().decode(errors="replace")[:300]
            if e.code in (404, 422) or n == tentativas:
                raise RuntimeError(f"{metodo} {url} -> {e.code}: {corpo_erro}")
            log(f"  {metodo} {e.code}, tentando de novo ({n}/{tentativas})")
        except Exception as e:
            if n == tentativas:
                raise
            log(f"  {metodo} falhou ({e}), tentando de novo ({n}/{tentativas})")
        time.sleep(min(60, 5 * n))


def releases():
    tudo, pg = [], 1
    while True:
        lote = chamar("GET", f"{API}/releases?per_page=100&page={pg}") or []
        tudo += lote
        if len(lote) < 100:
            return tudo
        pg += 1


def apagar(rel):
    chamar("DELETE", f"{API}/releases/{rel['id']}")
    if not rel.get("draft"):
        try:
            chamar("DELETE", f"{API}/git/refs/tags/{urllib.parse.quote(rel['tag_name'])}")
        except RuntimeError:
            pass
    log(f"  apagado {rel['tag_name']} ({'rascunho' if rel.get('draft') else 'publicado'})")


def criar(tag, titulo, notas, arquivos, latest):
    rel = chamar("POST", f"{API}/releases", {"tag_name": tag, "name": titulo, "body": notas, "draft": True})
    up = rel["upload_url"].split("{")[0]
    for i, arq in enumerate(arquivos, 1):
        tipo = "application/json" if arq.suffix == ".json" else "application/octet-stream"
        chamar("POST", up + "?name=" + urllib.parse.quote(arq.name), arq.read_bytes(), tipo)
        if i % 100 == 0:
            log(f"  {tag}: {i} de {len(arquivos)} arquivos")
    # Só publica depois de tudo enviado: o CRM nunca vê um release pela metade.
    chamar("PATCH", f"{API}/releases/{rel['id']}", {"draft": False, "make_latest": "true" if latest else "false"})
    log(f"publicado {tag} com {len(arquivos)} arquivos")


def main():
    saida = Path(sys.argv[1] if len(sys.argv) > 1 else "saida")
    m = json.loads((saida / "manifesto.json").read_text(encoding="utf-8"))
    mes, tag = m["mes"], "base-" + m["mes"]
    pastas = sorted((p for p in saida.glob("rel*") if p.is_dir()), key=lambda p: int(p.name[3:]))
    log(f"mês {mes}: {sum(len(list(p.iterdir())) for p in pastas)} arquivos em {len(pastas)} releases")

    for rel in releases():
        if re.fullmatch(re.escape(tag) + r"(-\d+)?", rel["tag_name"]):
            apagar(rel)

    for p in pastas[1:]:
        n = int(p.name[3:]) + 1
        criar(f"{tag}-{n}", f"Base da Receita {mes} (parte {n})", f"Continuação de {tag}.", sorted(p.iterdir()), False)
    criar(tag, f"Base da Receita {mes}", "Empresas ativas dos nichos da IG (arquivos cifrados). Gerado em "
          + time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + ".", [saida / "manifesto.json"] + sorted(pastas[0].iterdir()), True)

    # Guarda só os dois meses mais recentes
    todos = releases()
    meses = sorted({re.match(r"base-(\d{4}-\d{2})", r["tag_name"]).group(1) for r in todos if re.match(r"base-\d{4}-\d{2}", r["tag_name"])}, reverse=True)[:2]
    for rel in todos:
        mm = re.match(r"base-(\d{4}-\d{2})", rel["tag_name"])
        if mm and mm.group(1) not in meses:
            apagar(rel)
    log("pronto")


if __name__ == "__main__":
    main()
