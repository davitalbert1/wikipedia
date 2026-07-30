import os
from pathlib import Path
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup
from tqdm import tqdm

BASE_URLS = [
    "https://dumps.wikimedia.org/other/mediawiki_content_current/enwiki/2026-07-01/xml/bzip2/",
    # "https://dumps.wikimedia.org/other/mediawiki_content_current/ptwiki/2026-07-01/xml/bzip2/",
]

CHUNK_SIZE = 10 * 1024 * 1024  # 10 MB

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}

def get_language(url: str) -> str:
    parts = url.rstrip("/").split("/")
    idx = parts.index("mediawiki_content_current")
    return parts[idx + 1]

def limpar_temp(destino: Path):
    if not destino.exists(): return

    for arq in destino.glob("*.temp"):
        try:
            arq.unlink()
            print(f"Removido {arq.name}")
        except Exception as e:
            print(f"Erro removendo {arq}: {e}")

def listar_arquivos(base_url: str):
    r = requests.get(base_url, headers=HEADERS, timeout=60)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")

    arquivos = sorted(a["href"] for a in soup.find_all("a", href=True) if a["href"].endswith(".xml.bz2"))

    return arquivos

def baixar(base_url: str, destino: Path, nome: str):
    final = destino / nome
    temp = destino / f"{nome}.temp"

    if final.exists():
        print(f"✓ {nome}")
        return

    url = urljoin(base_url, nome)

    with requests.get(url, headers=HEADERS, stream=True, timeout=60) as r:
        r.raise_for_status()

        total = int(r.headers.get("Content-Length", 0))

        with open(temp, "wb") as f, tqdm(
            total=total,
            unit="B",
            unit_scale=True,
            unit_divisor=1024,
            desc=nome,
            leave=True,
        ) as pbar:
            for chunk in r.iter_content(CHUNK_SIZE):
                if not chunk: continue

                f.write(chunk)
                pbar.update(len(chunk))

    temp.rename(final)

def processar(base_url: str):
    language = get_language(base_url)

    destino = Path("dump") / language
    destino.mkdir(parents=True, exist_ok=True)

    print(f"\n=== {language} ===")

    limpar_temp(destino)

    arquivos = listar_arquivos(base_url)

    print(f"{len(arquivos)} arquivos encontrados.\n")

    for nome in arquivos: baixar(base_url, destino, nome)

def main():
    for url in BASE_URLS: processar(url)

if __name__ == "__main__":
    main()