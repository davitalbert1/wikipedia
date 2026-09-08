import os
import re
import html
import requests
from urllib.parse import urlparse, unquote
from bs4 import BeautifulSoup

URLS = [
    "https://en.wikipedia.org/wiki/Trimmer_(electronics)",
    "https://pt.wikipedia.org/wiki/Capacitor_de_t%C3%A2ntalo",
    "https://en.wikipedia.org/wiki/MIS_capacitor",
    "https://en.wikipedia.org/wiki/Polymer_capacitor",
    "https://en.wikipedia.org/wiki/Niobium_capacitor",
    "https://en.wikipedia.org/wiki/Silver_mica_capacitor",
    "https://pt.wikipedia.org/wiki/Capacitor_eletrol%C3%ADtico",
    "https://pt.wikipedia.org/wiki/Capacitor",
    "https://pt.wikipedia.org/wiki/Supercapacitor",
    "https://pt.wikipedia.org/wiki/Capacitor_de_filme",
    "https://pt.wikipedia.org/wiki/EEPROM",
    "https://pt.wikipedia.org/wiki/FIFO",
    "https://pt.wikipedia.org/wiki/Mem%C3%B3ria_flash",
    "https://pt.wikipedia.org/wiki/Cart%C3%A3o_de_mem%C3%B3ria",
    "https://pt.wikipedia.org/wiki/Mem%C3%B3ria_de_acesso_aleat%C3%B3rio",
    "https://pt.wikipedia.org/wiki/Ponte_retificadora",
    "https://pt.wikipedia.org/wiki/Diodo",
    "https://pt.wikipedia.org/wiki/Diodo",
    "https://pt.wikipedia.org/wiki/Diodo_retificador",
    "https://pt.wikipedia.org/wiki/Diodo_Schottky",
    "https://pt.wikipedia.org/wiki/Diodo_varicap",
    "https://pt.wikipedia.org/wiki/Diodo_Zener",
    "https://pt.wikipedia.org/wiki/Trans%C3%ADstor_bipolar_de_jun%C3%A7%C3%A3o",
    "https://pt.wikipedia.org/wiki/Trans%C3%ADstor_bipolar_de_porta_isolada",
    "https://pt.wikipedia.org/wiki/JFET",
    "https://pt.wikipedia.org/wiki/MOSFET",
    "https://pt.wikipedia.org/wiki/Trans%C3%ADstor_de_radiofrequ%C3%AAncia",
    "https://pt.wikipedia.org/wiki/MOSFET",
    "https://pt.wikipedia.org/wiki/DIAC",
    "https://pt.wikipedia.org/wiki/Retificador_controlado_de_sil%C3%ADcio",
    "https://pt.wikipedia.org/wiki/TRIAC",
    "https://pt.wikipedia.org/wiki/Transistor_de_jun%C3%A7%C3%A3o_bipolar",
    "https://pt.wikipedia.org/wiki/IGBT",
    "https://pt.wikipedia.org/wiki/JFET",
    "https://pt.wikipedia.org/wiki/MOSFET",
    "https://en.wikipedia.org/wiki/Heterojunction_bipolar_transistor",
    "https://pt.wikipedia.org/wiki/MOSFET_de_pot%C3%AAncia",
]

PASTA_SAIDA = "wikipedia_downloads"
HEADERS = {"User-Agent": "WikipediaOfflineDownloader/1.0"}

def nome_seguro(texto):
    texto = re.sub(r'[<>:"/\\|?*]', "_", texto)
    texto = re.sub(r"\s+", "_", texto)
    return texto.strip("._ ")[:150]

def obter_artigo(url):
    print(f"\nObtendo: {url}")

    response = requests.get(url, headers=HEADERS, timeout=30)

    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    # Título
    titulo = soup.select_one("#firstHeading")

    if titulo: titulo = titulo.get_text(" ", strip=True)
    else: titulo = "Wikipedia"

    # Conteúdo principal
    content = soup.select_one("#mw-content-text .mw-parser-output")

    if content is None: raise RuntimeError("Conteúdo principal não encontrado.")

    # Remove elementos que não queremos
    seletores_remover = [
        ".navbox",
        ".vertical-navbox",
        ".metadata",
        ".ambox",
        ".mbox-small",
        ".sistersitebox",
        ".noprint",
        ".mw-editsection",
        ".reference",
        ".reflist",
        ".mw-references-wrap",
        ".catlinks",
        ".portal",
        ".shortdescription",
        ".magnify",
        ".mw-empty-elt",
    ]

    for seletor in seletores_remover:
        for elemento in content.select(seletor): elemento.decompose()

    return titulo, content

def baixar_imagens(content, pasta_imagens):
    os.makedirs(pasta_imagens, exist_ok=True)

    imagens = content.find_all("img")

    total = 0

    for imagem in imagens:
        src = imagem.get("src")

        if not src: continue

        # URLs relativas
        if src.startswith("//"): src = "https:" + src
        elif src.startswith("/"): src = "https://pt.wikipedia.org" + src

        try:
            response = requests.get(src, headers=HEADERS, timeout=30)

            response.raise_for_status()

            # Nome original
            parsed = urlparse(src)
            nome = os.path.basename(unquote(parsed.path))
            nome = nome_seguro(nome)

            if not nome: nome = f"imagem_{total + 1}.jpg"

            # Evita duplicidade
            caminho = os.path.join(pasta_imagens, nome)

            contador = 1

            nome_base, extensao = os.path.splitext(nome)

            while os.path.exists(caminho):
                nome = f"{nome_base}_{contador}{extensao}"
                caminho = os.path.join(pasta_imagens, nome)
                contador += 1

            # Salva imagem
            with open(caminho, "wb") as arquivo: arquivo.write(response.content)

            # Altera o HTML para apontar
            # para a imagem local
            imagem["src"] = (f"imagens/{nome}")

            # Remove atributos remotos
            imagem.attrs.pop("srcset", None)
            imagem.attrs.pop("data-src", None)
            imagem.attrs.pop("data-srcset", None)
            imagem.attrs.pop("loading", None)

            total += 1

            print(f"[OK] {nome}")
        except Exception as erro:
            print(f"[ERRO] {src}")
            print(f"{erro}")

    return total

def salvar_html(titulo, content, pasta_artigo):
    estilo = """
    <style>

        body {
            font-family:
                -apple-system,
                BlinkMacSystemFont,
                "Segoe UI",
                sans-serif;

            max-width: 1100px;

            margin:
                40px auto;

            padding:
                0 25px;

            line-height: 1.6;

            color: #202122;

            background: white;
        }

        h1 {
            font-size: 2.2em;

            border-bottom:
                1px solid #a2a9b1;

            padding-bottom: 10px;
        }

        h2 {
            border-bottom:
                1px solid #eaecf0;

            padding-bottom: 5px;
        }

        img {
            max-width: 100%;
            height: auto;
        }

        table {
            border-collapse: collapse;
            max-width: 100%;
        }

        th,
        td {
            border:
                1px solid #a2a9b1;

            padding:
                6px 10px;
        }

        th {
            background:
                #eaecf0;
        }

        .infobox {
            float: right;

            margin:
                0 0 20px 20px;

            max-width: 350px;

            border:
                1px solid #a2a9b1;

            background:
                #f8f9fa;
        }

        @media (max-width: 700px) {

            .infobox {
                float: none;

                margin: 10px 0;

                max-width: 100%;
            }
        }

    </style>
    """

    documento = f"""<!DOCTYPE html>

<html lang="pt-BR">

<head>

<meta charset="UTF-8">

<meta name="viewport"
      content="width=device-width,
               initial-scale=1.0">

<title>
{html.escape(titulo)}
</title>

{estilo}

</head>

<body>

<h1>
{html.escape(titulo)}
</h1>

{str(content)}

</body>

</html>
"""

    caminho = os.path.join(pasta_artigo, "index.html")

    with open(caminho, "w", encoding="utf-8") as arquivo: arquivo.write(documento)
    return caminho

def baixar_artigo(url):
    try:

        titulo, content = obter_artigo(url)

        nome_pasta = nome_seguro(titulo)
        pasta_artigo = os.path.join(PASTA_SAIDA, nome_pasta)
        pasta_imagens = os.path.join(pasta_artigo, "imagens")

        os.makedirs(pasta_artigo, exist_ok=True)

        print(f"Título: {titulo}")

        print("Baixando imagens...")

        total = baixar_imagens(content, pasta_imagens)
        arquivo = salvar_html(titulo, content, pasta_artigo)

        print(f"Imagens: {total}")

        print(f"HTML: {arquivo}")
        print("[CONCLUÍDO]")
    except Exception as erro:
        print(f"\n[ERRO] {url}")
        print(f"{erro}")

def main():
    os.makedirs(PASTA_SAIDA, exist_ok=True)

    print("======================================")
    print("Wikipedia Offline Downloader")
    print("======================================")
    print(f"Artigos: {len(URLS)}")

    for numero, url in enumerate(URLS, start=1):
        print(f"\n[{numero}/{len(URLS)}]")
        baixar_artigo(url)

    print("\n======================================")
    print("Download concluído.")
    print(f"Arquivos em: {PASTA_SAIDA}/")
    print("======================================")

if __name__ == "__main__":
    main()
