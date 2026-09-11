import argparse
import bz2
import re
import sqlite3
import sys
import time
from pathlib import Path

from limpeza import formatar_texto, eh_redirecionamento, extrair_links
try:
    from lxml.etree import iterparse  # mais rápido, se disponível
    HAVE_LXML = True
except ImportError:
    from xml.etree.ElementTree import iterparse
    HAVE_LXML = False

limpar = False
PASTA_DUMPS = Path(r"F:\wikipedia\dump") # pasta com os .xml.bz2
DB_PATH = Path(r"F:\wikipedia\wikipedia.db") # destino do .db
BATCH_SIZE = 5000  # páginas por lote de inserção

# Configurações de filtro de páginas inúteis
IGNORAR_REDIRECIONAMENTOS = True  # Pula páginas de #REDIRECT / #REDIRECIONAMENTO
APENAS_NAMESPACE_ARTIGOS = True # Importa apenas namespace 0 (artigos de conteúdo)
TAMANHO_MINIMO_TEXTO = 20 # Descarta páginas cujo texto formatado fique menor que o limite

SCHEMA_BASE = """
CREATE TABLE IF NOT EXISTS paginas (
    page_id INTEGER PRIMARY KEY, -- id oficial da página
    titulo TEXT NOT NULL,
    namespace INTEGER NOT NULL,
    projeto TEXT NOT NULL, -- enwiki, enwikibooks, enwikivoyage...
    data TEXT NOT NULL, -- ex: 2026-07-01 (do nome do arquivo)
    origem TEXT NOT NULL, -- nome do arquivo .bz2 de onde veio
    texto TEXT -- wikitexto formatado (legível)
);

-- controle de progresso p/ retomar importação interrompida.
CREATE TABLE IF NOT EXISTS progresso (
    arquivo TEXT PRIMARY KEY, -- nome do .bz2
    completo INTEGER NOT NULL DEFAULT 0, -- 1 = importado até o fim
    ultimo_page_id INTEGER NOT NULL DEFAULT 0 -- checkpoint de retomada
) WITHOUT ROWID;

-- Tabela de categorias extraídas das páginas
CREATE TABLE IF NOT EXISTS categorias (
    page_id INTEGER NOT NULL,
    categoria TEXT NOT NULL,
    FOREIGN KEY (page_id) REFERENCES paginas(page_id)
);

-- Tabela de links internos extraídos das páginas
CREATE TABLE IF NOT EXISTS links (
    page_id INTEGER NOT NULL,
    destino TEXT NOT NULL,
    FOREIGN KEY (page_id) REFERENCES paginas(page_id)
);
"""

SCHEMA_INDICES = """
CREATE INDEX IF NOT EXISTS idx_categorias_page ON categorias(page_id);
CREATE INDEX IF NOT EXISTS idx_categorias_nome ON categorias(categoria);
CREATE INDEX IF NOT EXISTS idx_links_page ON links(page_id);
CREATE INDEX IF NOT EXISTS idx_links_destino ON links(destino);
"""

RE_DATA = re.compile(r"-(\d{4}-\d{2}-\d{2})-")
RE_XMLNS = re.compile(rb"<mediawiki[^>]*?xmlns=\"([^\"]+)\"")

def data_do_arquivo(nome: str) -> str:
    m = RE_DATA.search(nome)
    return m.group(1) if m else ""

def localname(tag) -> str:
    if not isinstance(tag, str): return ""
    return tag.rsplit("}", 1)[-1]

def detectar_tag_page(arquivo: Path) -> str:
    with bz2.open(arquivo, "rb") as fb: cabecalho = fb.read(65536)
    m = RE_XMLNS.search(cabecalho)
    if m: return "{%s}page" % m.group(1).decode("ascii", "ignore")
    return "page"

def paginas_do_dump(arquivo: Path, skip_ate_pid: int = 0):
    nome_projeto = arquivo.name.split("-")[0]
    DATA = data_do_arquivo(arquivo.name)
    tag_page = detectar_tag_page(arquivo)
    P = tag_page[:-4] if tag_page.startswith("{") else ""
    try:
        with bz2.open(arquivo, "rb") as fb:
            if HAVE_LXML and P:
                contexto = iterparse(fb, events=("end",), tag=tag_page)
            else:
                contexto = iterparse(fb, events=("end",))
            for _, elem in contexto:
                if (HAVE_LXML and P) or localname(elem.tag) == "page":
                    pid = int((elem.findtext(P + "id") or 0))
                    titulo = elem.findtext(P + "title") or ""
                    ns = int(elem.findtext(P + "ns") or 0)

                    # 0. Retomada: dumps ordenam páginas por page_id
                    if pid and pid <= skip_ate_pid:
                        elem.clear()
                        if HAVE_LXML:
                            while elem.getprevious() is not None: del elem.getparent()[0]
                        continue

                    # 1. Filtro por namespace (apenas artigos no namespace 0)
                    if APENAS_NAMESPACE_ARTIGOS and ns != 0:
                        elem.clear()
                        if HAVE_LXML:
                            while elem.getprevious() is not None: del elem.getparent()[0]
                        continue

                    # 2. Filtro de redirecionamento pela tag XML <redirect>
                    if IGNORAR_REDIRECIONAMENTOS and elem.find(P + "redirect") is not None:
                        elem.clear()
                        if HAVE_LXML:
                            while elem.getprevious() is not None: del elem.getparent()[0]
                        continue

                    rev = elem.find(P + "revision")
                    texto = (rev.findtext(P + "text") if rev is not None else "") or ""

                    # 3. Filtro de redirecionamento pelo wikitexto (#REDIRECT)
                    if IGNORAR_REDIRECIONAMENTOS and eh_redirecionamento(texto):
                        elem.clear()
                        if HAVE_LXML:
                            while elem.getprevious() is not None: del elem.getparent()[0]
                        continue

                    if pid and texto:
                        texto_limpo, categorias = formatar_texto(texto)
                        if len(texto_limpo) >= TAMANHO_MINIMO_TEXTO:
                            links = extrair_links(texto)
                            yield pid, titulo, ns, nome_projeto, DATA, arquivo.name, texto_limpo, categorias, links
                    elem.clear()
                    if HAVE_LXML:
                        while elem.getprevious() is not None: del elem.getparent()[0]
    except (OSError, EOFError):
        raise

def verificar_integridade(conn, completa=False):
    if completa:
        print("Executando checagem completa de integridade (pode demorar)...", flush=True)
        resultado = conn.execute("PRAGMA quick_check").fetchone()
    else:
        # Checagem leve e instantânea de integridade do cabeçalho/esquema
        resultado = conn.execute("PRAGMA schema_version").fetchone()
        if resultado is not None:
            resultado = ("ok",)
    if not resultado or resultado[0] != "ok":
        sys.exit(f"[FALHA] O banco {DB_PATH} está CORROMPIDO: {resultado}\n"
                 "Restaure um backup ou recrie o banco antes de continuar.")

def migrar_banco_se_necessario(cur):
    colunas = [info[1] for info in cur.execute("PRAGMA table_info(progresso)").fetchall()]
    if "arquivo" in colunas and "ultimo_page_id" not in colunas:
        cur.execute("ALTER TABLE progresso ADD COLUMN ultimo_page_id INTEGER NOT NULL DEFAULT 0")

def expurgar_paginas_inuteis_existentes(conn, forcar=False):
    cur = conn.cursor()
    condicoes = []
    if APENAS_NAMESPACE_ARTIGOS: condicoes.append("namespace != 0")
    if IGNORAR_REDIRECIONAMENTOS:
        condicoes.append("texto LIKE '#REDIRECT%' OR texto LIKE '#REDIRECIONAMENTO%'")

    if not condicoes: return 0

    if not forcar:
        probe = cur.execute(
            f"SELECT EXISTS(SELECT 1 FROM paginas WHERE ({' OR '.join(condicoes)}) LIMIT 1)"
        ).fetchone()[0]
        if probe:
            print("\n[AVISO] Há páginas inúteis residuais no banco (probe EXISTS=1).")
            print("Rode com --limpar para remover (operação lenta, varre o disco).")
        return 0

    total_inuteis = cur.execute(
        f"SELECT COUNT(*) FROM paginas WHERE ({' OR '.join(condicoes)})"
    ).fetchone()[0]
    if total_inuteis > 0:
        print(f"\n[LIMPEZA] Encontradas {total_inuteis:,} páginas inúteis residuais no banco.")
        print("Removendo páginas inúteis (redirecionamentos / namespaces inválidos)...")
        sql_delete = f"DELETE FROM paginas WHERE ({' OR '.join(condicoes)})"
        cur.execute(sql_delete)
        print(f"✓ {total_inuteis:,} páginas inúteis removidas com sucesso.")

def montar_lotes_otimizado(it_paginas, batch_size=BATCH_SIZE):
    lote_principal = []
    categorias_flat = []
    links_flat = []
    cnt = 0

    for pag in it_paginas:
        page_id, titulo, ns, projeto, data, origem, texto_limpo, categorias, links = pag
        lote_principal.append([page_id, titulo, ns, projeto, data, origem, texto_limpo])
        if categorias: categorias_flat.extend((page_id, c) for c in categorias)
        if links: links_flat.extend((page_id, lk) for lk in links)
        cnt += 1
        if cnt >= batch_size:
            yield lote_principal, categorias_flat, links_flat, page_id
            lote_principal = []
            categorias_flat = []
            links_flat = []
            cnt = 0
    if lote_principal: yield lote_principal, categorias_flat, links_flat, page_id

def parse_args():
    ap = argparse.ArgumentParser(description="Importa dumps .xml.bz2 p/ SQLite com retomada rápida.")
    ap.add_argument("--check", action="store_true",
                    help="Roda PRAGMA quick_check completo (lento, varre o .db). Padrão é checagem leve de cabeçalho.")
    ap.add_argument("--limpar", action="store_true",
                    help="Remove páginas inúteis residuais (DELETE com LIKE varre GBs; sem a flag só avisa via EXISTS).")
    ap.add_argument("--vacuum", action="store_true",
                    help="Roda VACUUM no fim (lento, reescreve o .db). Padrão: pula.")
    ap.add_argument("--status", action="store_true",
                    help="Só mostra o que falta (lê apenas a tabela progresso, sem tocar nos .bz2 nem no texto).")
    ap.add_argument("--batch", type=int, default=BATCH_SIZE, help="Páginas por commit.")
    return ap.parse_args()

def mostrar_status(arquivos, conn):
    cur = conn.cursor()
    try:
        rows = cur.execute("SELECT arquivo, completo, ultimo_page_id FROM progresso").fetchall()
    except sqlite3.OperationalError:
        rows = []
    prog = {r[0]: (r[1], r[2]) for r in rows}
    print(f"\n{'ARQUIVO':55s} {'ESTADO':22s} {'CHECKPOINT'}")
    for arq in arquivos:
        nome = arq.name
        st = prog.get(nome)
        if st and st[0]:
            estado = "[OK] completo (pula)"
        elif st:
            estado = f"[->] retomar de {st[1]:,}"
        else:
            estado = "[ ] novo (a importar)"
        print(f"{nome:55s} {estado:22s} {st[1] if st else 0:,}")
    n_ok = sum(1 for _, (c, _) in prog.items() if c)
    print(f"\n{n_ok}/{len(arquivos)} completos. Faltam {len(arquivos) - n_ok}.\n")

def main():
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    args = parse_args()
    batch_size = max(1000, args.batch)
    arquivos = sorted(PASTA_DUMPS.rglob("*.xml.bz2"))
    if not arquivos: sys.exit(f"Nenhum .xml.bz2 encontrado em {PASTA_DUMPS}")
    print(f"{len(arquivos)} dump(s) encontrado(s).", flush=True)
    t_db0 = time.time()

    conn = sqlite3.connect(DB_PATH, isolation_level=None, timeout=60.0)
    try: conn.execute("PRAGMA page_size = 4096")
    except sqlite3.DatabaseError: pass
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.execute("PRAGMA cache_size = -524288")  # ~512 MB de cache
    conn.execute("PRAGMA mmap_size = 1073741824")  # 1 GB de arquivo mapeado p/ leitura
    conn.executescript(SCHEMA_BASE)

    verificar_integridade(conn, completa=args.check)
    cur = conn.cursor()
    migrar_banco_se_necessario(cur)
    print(f"[DB] pronto em {time.time() - t_db0:.2f}s.", flush=True)

    if args.status:
        mostrar_status(arquivos, conn)
        conn.close()
        return

    if limpar:
        expurgar_paginas_inuteis_existentes(conn, forcar=args.limpar)

    sql_insert = ("INSERT OR IGNORE INTO paginas "
                  "(page_id, titulo, namespace, projeto, data, origem, texto) VALUES (?,?,?,?,?,?,?)")
    sql_insert_categoria = "INSERT OR IGNORE INTO categorias (page_id, categoria) VALUES (?,?)"
    sql_insert_link = "INSERT OR IGNORE INTO links (page_id, destino) VALUES (?,?)"

    pulados = 0
    total_dumps = len(arquivos)
    inicio = time.time()
    total_inserido = 0

    def mostrar_progresso(indice, nome_atual, processadas, checkpoint):
        pct = (indice - 1) / total_dumps * 100
        vel = processadas / max(time.time() - inicio, 1e-9)
        sys.stdout.write(
            f"\r[{indice}/{total_dumps} | {pct:5.1f}%] {nome_atual}: "
            f"{processadas:,} neste dump | checkpoint {checkpoint:,} | "
            f"{vel:,.0f} pag/s | +{total_inserido:,} nesta sessao"
        )
        sys.stdout.flush()

    try:
        for indice, arquivo in enumerate(arquivos, 1):
            nome = arquivo.name
            estado = cur.execute("SELECT completo, ultimo_page_id FROM progresso WHERE arquivo=?", (nome,)).fetchone()

            if estado and estado[0]:
                print(f"> {nome} — já completo, pulando.")
                pulados += 1
                continue

            ultimo_page_id = 0
            if estado:
                ultimo_page_id = estado[1]
                print(f"\n> {nome} — retomando do page_id {ultimo_page_id:,}")
            else:
                print(f"\n> {nome}")
                cur.execute("INSERT OR IGNORE INTO progresso (arquivo, completo, ultimo_page_id) VALUES (?,0,0)", (nome,))

            processadas = 0
            lote_checkpoint = ultimo_page_id
            if ultimo_page_id:
                print(f"  (paginas com page_id <= {ultimo_page_id:,} serao puladas direto no parse)")
            it_paginas = paginas_do_dump(arquivo, skip_ate_pid=ultimo_page_id)
            lotes = montar_lotes_otimizado(it_paginas, batch_size=batch_size)

            try:
                for lote_principal, categorias_flat, links_flat, ultimo_page_id_lote in lotes:
                    lote_checkpoint = ultimo_page_id_lote

                    if lote_principal:
                        try:
                            cur.execute("BEGIN IMMEDIATE")
                            cur.executemany(sql_insert, lote_principal)

                            if categorias_flat:
                                cur.executemany(sql_insert_categoria, categorias_flat)
                            if links_flat:
                                cur.executemany(sql_insert_link, links_flat)

                            cur.execute(
                                """
                                UPDATE progresso
                                SET ultimo_page_id = MAX(ultimo_page_id, ?)
                                WHERE arquivo = ?
                                """,
                                (lote_checkpoint, nome)
                            )
                            cur.execute("COMMIT")

                            total_inserido += len(lote_principal)
                            processadas += len(lote_principal)
                        except Exception:
                            cur.execute("ROLLBACK")
                            raise

                    mostrar_progresso(indice, nome, processadas, lote_checkpoint)

                cur.execute("BEGIN IMMEDIATE")
                cur.execute("UPDATE progresso SET completo=1 WHERE arquivo=?", (nome,))
                cur.execute("COMMIT")

                print(f"\n✓ {nome} concluído: {processadas:,} páginas processadas "
                      f"(duplicadas ignoradas pelo INSERT OR IGNORE) "
                      f"em {time.time() - inicio:,.0f}s.")
            except (OSError, EOFError) as e:
                print(f"\n[ERRO] O dump {nome} parece estar truncado ou corrompido.")
                print(f"Progresso salvo até o checkpoint: {lote_checkpoint:,}")
                print(f"Detalhe: {e}")
                break
            except Exception as e:
                print(f"\n[ERRO] Erro inesperado ao processar {nome}: {e}")
                raise
    except KeyboardInterrupt:
        print("\n[AVISO] Interrupção (Ctrl+C) detectada!")
        print("Pode executar o script novamente para continuar de onde parou.")
    except Exception as e:
        print("\n[AVISO] Erro fatal detectado.")
        raise
    finally:
        if pulados: print(f"\n{pulados} dump(s) já completos foram pulados.")
        print(f"\nSessão: +{total_inserido:,} páginas em {time.time() - inicio:,.0f}s.")

        print("\nCriando índices (se faltarem)...", flush=True)
        t_idx = time.time()
        conn.executescript(SCHEMA_INDICES)
        print(f"Índices prontos em {time.time() - t_idx:.1f}s.")
        if args.vacuum:
            print("\nExecutando VACUUM... (pode demorar)")
            conn.execute("VACUUM")
        else:
            print("\nVACUUM pulado (use --vacuum para forçar).")
        print("Verificando integridade final do banco...")
        verificar_integridade(conn, completa=args.check)
        conn.close()
        print(f"Concluído: {DB_PATH} (integridade OK)")

if __name__ == "__main__":
    main()
