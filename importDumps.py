import argparse
import bz2
import os
import multiprocessing as mp
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
BATCH_SIZE = 20000  # páginas por lote de inserção
WORKERS = 3

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

-- Tabela de categorias extraídas das páginas (WITHOUT ROWID economiza gigabytes em disco)
CREATE TABLE IF NOT EXISTS categorias (
    page_id INTEGER NOT NULL,
    categoria TEXT NOT NULL,
    PRIMARY KEY (page_id, categoria)
) WITHOUT ROWID;

-- Tabela de links internos extraídos das páginas (WITHOUT ROWID economiza gigabytes em disco)
CREATE TABLE IF NOT EXISTS links (
    page_id INTEGER NOT NULL,
    destino TEXT NOT NULL,
    PRIMARY KEY (page_id, destino)
) WITHOUT ROWID;
"""

DROP_INDICES = """
DROP INDEX IF EXISTS idx_categorias_page;
DROP INDEX IF EXISTS idx_categorias_nome;
DROP INDEX IF EXISTS idx_links_page;
DROP INDEX IF EXISTS idx_links_destino;
"""

SCHEMA_INDICES = """
CREATE INDEX IF NOT EXISTS idx_categorias_nome ON categorias(categoria);
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

def paginas_do_dump(arquivo: Path, skip_ate_pid: int = 0, existing_ids: set = None):
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

                    if pid and pid <= skip_ate_pid:
                        elem.clear()

                        if HAVE_LXML:
                            while elem.getprevious() is not None: del elem.getparent()[0]

                        continue

                    # Já existe no banco — pula antes de qualquer processamento caro
                    if existing_ids is not None and pid in existing_ids:
                        elem.clear()

                        if HAVE_LXML:
                            while elem.getprevious() is not None: del elem.getparent()[0]

                        continue

                    # Namespace
                    if APENAS_NAMESPACE_ARTIGOS and ns != 0:
                        elem.clear()

                        if HAVE_LXML:
                            while elem.getprevious() is not None: del elem.getparent()[0]

                        continue

                    # Redirecionamento XML
                    if (
                        IGNORAR_REDIRECIONAMENTOS
                        and elem.find(P + "redirect") is not None
                    ):
                        elem.clear()

                        if HAVE_LXML:
                            while elem.getprevious() is not None: del elem.getparent()[0]
                        continue

                    # Texto
                    rev = elem.find(P + "revision")
                    texto = (rev.findtext(P + "text") if rev is not None else "") or ""

                    # Redirecionamento no wikitexto
                    if IGNORAR_REDIRECIONAMENTOS and eh_redirecionamento(texto):
                        elem.clear()
                        if HAVE_LXML:
                            while elem.getprevious() is not None: del elem.getparent()[0]
                        continue

                    # Processamento
                    if pid and len(texto) >= TAMANHO_MINIMO_TEXTO:

                        texto_limpo, categorias = formatar_texto(texto)

                        if len(texto_limpo) >= TAMANHO_MINIMO_TEXTO:

                            links = extrair_links(texto)

                            yield (
                                pid,
                                titulo,
                                ns,
                                nome_projeto,
                                DATA,
                                arquivo.name,
                                texto_limpo,
                                categorias,
                                links
                            )

                    # Libera memória
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

def worker_parse_dump(task_queue, result_queue, batch_size, existing_ids):
    while True:
        try:
            task = task_queue.get()
            if task is None: break
            arquivo_path, skip_ate_pid = task
            nome = arquivo_path.name
            it_paginas = paginas_do_dump(arquivo_path, skip_ate_pid=skip_ate_pid, existing_ids=existing_ids)
            lotes = montar_lotes_otimizado(it_paginas, batch_size=batch_size)
            for lote_principal, categorias_flat, links_flat, checkpoint in lotes:
                result_queue.put(("BATCH", nome, lote_principal, categorias_flat, links_flat, checkpoint, len(lote_principal)))
            result_queue.put(("FILE_DONE", nome, 0))
        except (OSError, EOFError) as e:
            result_queue.put(("FILE_CORRUPT", nome, str(e)))
        except Exception as e:
            result_queue.put(("FILE_ERROR", nome, str(e)))

def parse_args():
    ap = argparse.ArgumentParser(description="Importa dumps .xml.bz2 p/ SQLite com retomada rápida e multiprocessing.")
    ap.add_argument("--check", action="store_true",
                    help="Roda PRAGMA quick_check completo (lento, varre o .db). Padrão é checagem leve de cabeçalho.")
    ap.add_argument("--limpar", action="store_true",
                    help="Remove páginas inúteis residuais (DELETE com LIKE varre GBs; sem a flag só avisa via EXISTS).")
    ap.add_argument("--vacuum", action="store_true",
                    help="Roda VACUUM no fim (lento, reescreve o .db). Padrão: pula.")
    ap.add_argument("--status", action="store_true",
                    help="Só mostra o que falta (lê apenas a tabela progresso, sem tocar nos .bz2 nem no texto).")
    ap.add_argument("--batch", type=int, default=BATCH_SIZE, help="Páginas por commit.")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 4) - 1),
                    help="Número de processos paralelos para descompactação e formatação (default: CPU cores - 1).")
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
    num_workers = max(1, WORKERS)
    arquivos = sorted(PASTA_DUMPS.rglob("*.xml.bz2"))
    if not arquivos: sys.exit(f"Nenhum .xml.bz2 encontrado em {PASTA_DUMPS}")
    print(f"{len(arquivos)} dump(s) encontrado(s). Usando {num_workers} processo(s) worker.", flush=True)
    t_db0 = time.time()

    conn = sqlite3.connect(DB_PATH, isolation_level=None, timeout=60.0)
    try: conn.execute("PRAGMA page_size = 16384")
    except sqlite3.DatabaseError: pass
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.execute("PRAGMA cache_size = -524288")  # ~512 MB de cache
    conn.execute("PRAGMA mmap_size = 2147483648")  # 2 GB de arquivo mapeado p/ leitura
    conn.execute("PRAGMA journal_size_limit = 67108864")  # Limita WAL a 64 MB
    conn.execute("PRAGMA wal_autocheckpoint = 10000")
    conn.executescript(SCHEMA_BASE)

    verificar_integridade(conn, completa=args.check)
    cur = conn.cursor()
    migrar_banco_se_necessario(cur)
    print(f"[DB] pronto em {time.time() - t_db0:.2f}s.", flush=True)

    if args.status:
        mostrar_status(arquivos, conn)
        conn.close()
        return

    print("[DB] garantindo que índices secundários estejam desativados durante a inserção...", flush=True)
    conn.executescript(DROP_INDICES)

    if limpar:
        expurgar_paginas_inuteis_existentes(conn, forcar=args.limpar)

    sql_insert = ("INSERT OR IGNORE INTO paginas "
                  "(page_id, titulo, namespace, projeto, data, origem, texto) VALUES (?,?,?,?,?,?,?)")
    sql_insert_categoria = "INSERT OR IGNORE INTO categorias (page_id, categoria) VALUES (?,?)"
    sql_insert_link = "INSERT OR IGNORE INTO links (page_id, destino) VALUES (?,?)"

    # Identifica tarefas pendentes
    tarefas = []
    pulados = 0
    for arq in arquivos:
        nome = arq.name
        estado = cur.execute("SELECT completo, ultimo_page_id FROM progresso WHERE arquivo=?", (nome,)).fetchone()
        if estado and estado[0]:
            pulados += 1
            continue
        ultimo_pid = estado[1] if estado else 0
        if not estado:
            cur.execute("INSERT OR IGNORE INTO progresso (arquivo, completo, ultimo_page_id) VALUES (?,0,0)", (nome,))
        tarefas.append((arq, ultimo_pid))

    if not tarefas:
        print(f"\nTodos os {len(arquivos)} dumps já estão concluídos no banco!")
        conn.close()
        return

    print(f"\n{len(tarefas)} dump(s) pendentes para processar. ({pulados} já completos pulados)", flush=True)

    # Carrega page_ids existentes p/ pular antes de formatar_texto/extrair_links
    print("[DB] Carregando page_ids existentes para skip rápido...", flush=True)
    t_ids = time.time()
    existing_ids = frozenset(row[0] for row in cur.execute("SELECT page_id FROM paginas"))
    print(f"[DB] {len(existing_ids):,} page_ids carregados em {time.time() - t_ids:.1f}s.", flush=True)

    task_queue = mp.Queue()
    result_queue = mp.Queue(maxsize=num_workers * 4)

    for task in tarefas: task_queue.put(task)
    for _ in range(num_workers): task_queue.put(None)

    workers = []
    for _ in range(num_workers):
        p = mp.Process(target=worker_parse_dump, args=(task_queue, result_queue, batch_size, existing_ids))
        p.daemon = True
        p.start()
        workers.append(p)

    total_dumps = len(tarefas)
    concluidos_cnt = 0
    total_inserido = 0
    inicio = time.time()

    try:
        while concluidos_cnt < total_dumps:
            msg = result_queue.get()
            kind = msg[0]

            if kind == "BATCH":
                _, nome, lote_principal, categorias_flat, links_flat, checkpoint, cnt = msg
                if lote_principal:
                    try:
                        cur.execute("BEGIN IMMEDIATE")
                        cur.executemany(sql_insert, lote_principal)
                        if categorias_flat:
                            cur.executemany(sql_insert_categoria, categorias_flat)
                        if links_flat:
                            cur.executemany(sql_insert_link, links_flat)
                        cur.execute(
                            "UPDATE progresso SET ultimo_page_id = MAX(ultimo_page_id, ?) WHERE arquivo = ?",
                            (checkpoint, nome)
                        )
                        cur.execute("COMMIT")
                        total_inserido += cnt
                    except Exception:
                        cur.execute("ROLLBACK")
                        raise

                pct = concluidos_cnt / total_dumps * 100
                vel = total_inserido / max(time.time() - inicio, 1e-9)
                sys.stdout.write(
                    f"\r[{concluidos_cnt}/{total_dumps} | {pct:5.1f}%] {nome}: "
                    f"checkpoint {checkpoint:,} | {vel:,.0f} pag/s | +{total_inserido:,} nesta sessao"
                )
                sys.stdout.flush()
            elif kind == "FILE_DONE":
                _, nome, _ = msg
                cur.execute("BEGIN IMMEDIATE")
                cur.execute("UPDATE progresso SET completo=1 WHERE arquivo=?", (nome,))
                cur.execute("COMMIT")
                concluidos_cnt += 1
                try: conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
                except Exception: pass
                print(f"\n✓ {nome} concluído. ({concluidos_cnt}/{total_dumps})", flush=True)
            elif kind == "FILE_CORRUPT":
                _, nome, err = msg
                print(f"\n[ERRO] Dump {nome} corrompido/truncado: {err}", flush=True)
                concluidos_cnt += 1

            elif kind == "FILE_ERROR":
                _, nome, err = msg
                print(f"\n[ERRO] Erro no worker para {nome}: {err}", flush=True)
                concluidos_cnt += 1

    except KeyboardInterrupt:
        print("\n[AVISO] Interrupção (Ctrl+C) detectada! Encerrando workers...")
    finally:
        for p in workers:
            p.terminate()
            p.join(timeout=1.0)

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

