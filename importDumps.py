import bz2
import re
import sqlite3
import sys
import time
from pathlib import Path

from limpeza import formatar_texto, eh_redirecionamento
try:
    from lxml.etree import iterparse  # mais rápido, se disponível
    HAVE_LXML = True
except ImportError:
    from xml.etree.ElementTree import iterparse
    HAVE_LXML = False

PASTA_DUMPS = Path(r"F:\wikipedia\dump") # pasta com os .xml.bz2
DB_PATH = Path(r"F:\wikipedia\wikipedia.db") # destino do .db
BATCH_SIZE = 2000  # páginas por lote de inserção

# Configurações de filtro de páginas inúteis
IGNORAR_REDIRECIONAMENTOS = True  # Pula páginas de #REDIRECT / #REDIRECIONAMENTO
APENAS_NAMESPACE_ARTIGOS = True # Importa apenas namespace 0 (artigos de conteúdo)
TAMANHO_MINIMO_TEXTO = 20 # Descarta páginas cujo texto formatado fique menor que o limite

SCHEMA = """
PRAGMA page_size = 4096;
-- Segurança contra corrupção do .db:
-- WAL: grava em journal à parte e recupera automaticamente após queda de energia/crash.
-- synchronous=FULL: fsync a cada commit (garante que o checkpoint esteja realmente no disco).
PRAGMA journal_mode = WAL;
PRAGMA synchronous = FULL;
PRAGMA temp_store = MEMORY;
PRAGMA wal_autocheckpoint = 2000; -- checkpoint automático do WAL (~8MB) p/ limitar crescimento
CREATE TABLE IF NOT EXISTS paginas (
    page_id INTEGER PRIMARY KEY, -- id oficial da página
    titulo TEXT NOT NULL,
    namespace INTEGER NOT NULL,
    projeto TEXT NOT NULL, -- enwiki, enwikibooks, enwikivoyage...
    data TEXT NOT NULL, -- ex: 2026-07-01 (do nome do arquivo)
    origem TEXT NOT NULL, -- nome do arquivo .bz2 de onde veio
    texto TEXT -- wikitexto formatado (legível)
) WITHOUT ROWID;

-- controle de progresso p/ retomar importação interrompida.
CREATE TABLE IF NOT EXISTS progresso (
    arquivo TEXT PRIMARY KEY, -- nome do .bz2
    completo INTEGER NOT NULL DEFAULT 0, -- 1 = importado até o fim
    ultimo_page_id INTEGER NOT NULL DEFAULT 0 -- checkpoint de retomada
) WITHOUT ROWID;
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

def paginas_do_dump(arquivo: Path):
    nome_projeto = arquivo.name.split("-")[0]
    DATA = data_do_arquivo(arquivo.name)
    tag_page = detectar_tag_page(arquivo)
    P = tag_page[:-4] if tag_page.startswith("{") else ""
    try:
        with bz2.open(arquivo, "rb") as fb:
            # lxml usa 'tag' (singular); 'tags' não existe e causa TypeError.
            # Só filtra se o namespace foi detectado; senão, parseia tudo
            # e confia no filtro por localname abaixo.
            if HAVE_LXML and P:
                contexto = iterparse(fb, events=("end",), tag=tag_page)
            else:
                contexto = iterparse(fb, events=("end",))
            for _, elem in contexto:
                if (HAVE_LXML and P) or localname(elem.tag) == "page":
                    pid = int((elem.findtext(P + "id") or 0))
                    titulo = elem.findtext(P + "title") or ""
                    ns = int(elem.findtext(P + "ns") or 0)

                    # 1. Filtro por namespace (apenas artigos no namespace 0)
                    if APENAS_NAMESPACE_ARTIGOS and ns != 0:
                        elem.clear()
                        continue

                    # 2. Filtro de redirecionamento pela tag XML <redirect>
                    if IGNORAR_REDIRECIONAMENTOS and elem.find(P + "redirect") is not None:
                        elem.clear()
                        continue

                    rev = elem.find(P + "revision")
                    texto = (rev.findtext(P + "text") if rev is not None else "") or ""

                    # 3. Filtro de redirecionamento pelo wikitexto (#REDIRECT)
                    if IGNORAR_REDIRECIONAMENTOS and eh_redirecionamento(texto):
                        elem.clear()
                        continue

                    if pid and texto.strip():
                        texto_limpo = formatar_texto(texto)
                        # Só emite se restou conteúdo substancial
                        if len(texto_limpo) >= TAMANHO_MINIMO_TEXTO:
                            yield pid, titulo, ns, nome_projeto, DATA, arquivo.name, texto_limpo
                    elem.clear()
                    if HAVE_LXML:
                        # Libera os irmãos anteriores já processados para não
                        # acumular milhões de elementos vazios em dumps gigantes.
                        while elem.getprevious() is not None:
                            del elem.getparent()[0]
    except (OSError, EOFError):
        # Erro de descompressão BZ2 / EOF inesperado = arquivo truncado ou corrompido.
        raise

def verificar_integridade(conn):
    resultado = conn.execute("PRAGMA quick_check").fetchone()
    if not resultado or resultado[0] != "ok":
        sys.exit(f"[FALHA] O banco {DB_PATH} está CORROMPIDO: {resultado}\n"
                 "Restaure um backup ou recrie o banco antes de continuar.")
    # Se houver WAL pendente de uma execução anterior, isso força a recuperação agora.
    conn.execute("SELECT COUNT(*) FROM paginas").fetchone()

def migrar_banco_se_necessario(cur):
    colunas = [info[1] for info in cur.execute("PRAGMA table_info(progresso)").fetchall()]
    if "arquivo" in colunas and "ultimo_page_id" not in colunas:
        cur.execute("ALTER TABLE progresso ADD COLUMN ultimo_page_id INTEGER NOT NULL DEFAULT 0")

def expurgar_paginas_inuteis_existentes(conn):
    cur = conn.cursor()
    condicoes = []
    if APENAS_NAMESPACE_ARTIGOS: condicoes.append("namespace != 0")
    if IGNORAR_REDIRECIONAMENTOS:
        condicoes.append("texto LIKE '#REDIRECT%' OR texto LIKE '#REDIRECIONAMENTO%'")

    if not condicoes: return

    sql_check = f"SELECT COUNT(*) FROM paginas WHERE ({' OR '.join(condicoes)})"
    total_inuteis = cur.execute(sql_check).fetchone()[0]
    if total_inuteis > 0:
        print(f"\n[LIMPEZA] Encontradas {total_inuteis:,} páginas inúteis residuais no banco.")
        print("Removendo páginas inúteis (redirecionamentos / namespaces inválidos)...")
        sql_delete = f"DELETE FROM paginas WHERE ({' OR '.join(condicoes)})"
        cur.execute(sql_delete)
        conn.commit()
        print(f"✓ {total_inuteis:,} páginas inúteis removidas com sucesso.")

def main():
    arquivos = sorted(PASTA_DUMPS.rglob("*.xml.bz2"))
    if not arquivos: sys.exit(f"Nenhum .xml.bz2 encontrado em {PASTA_DUMPS}")
    print(f"{len(arquivos)} dump(s) encontrado(s).")

    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)  # inclui PRAGMAs de durabilidade (WAL + synchronous=FULL)
    # WAL persiste no arquivo, mas synchronous é por conexão: reaplica sempre.
    conn.execute("PRAGMA synchronous = FULL")
    verificar_integridade(conn)
    cur = conn.cursor()
    migrar_banco_se_necessario(cur)
    conn.commit()
    expurgar_paginas_inuteis_existentes(conn)
    conn.execute("BEGIN")

    # Usamos INSERT OR IGNORE para que a retomada seja segura e sem duplicações
    sql_insert = ("INSERT OR IGNORE INTO paginas "
                  "(page_id, titulo, namespace, projeto, data, origem, texto) VALUES (?,?,?,?,?,?,?)")

    pulados = 0
    total_dumps = len(arquivos)
    inicio = time.time()

    def mostrar_progresso(indice: int, nome_atual: str, processadas: int, checkpoint: int, is_final=False):
        total_db = cur.execute("SELECT COUNT(*) FROM paginas").fetchone()[0]
        pct = (indice - 1) / total_dumps * 100
        vel = processadas / max(time.time() - inicio, 1e-9)

        # ANSI para limpar linhas
        texto_limpo = f"\r[{indice}/{total_dumps} dumps | {pct:5.1f}%] {nome_atual}:"
        if not is_final:
            texto_limpo += f"\n  {processadas:,} páginas processadas\n  checkpoint: {checkpoint:,}\n  velocidade: {vel:,.0f} pág/s\n  total no DB: {total_db:,}\033[4A"

        sys.stdout.write(texto_limpo)
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
                conn.commit()
                conn.execute("BEGIN")

            lote, processadas = [], 0
            # Guardamos o último page_id de cada lote para ser comitado de forma segura
            lote_checkpoint = ultimo_page_id

            try:
                for linha in paginas_do_dump(arquivo):
                    page_id = linha[0]
                    # Ignora páginas até atingir o checkpoint
                    if page_id <= ultimo_page_id:
                        existe = cur.execute(
                            "SELECT 1 FROM paginas WHERE page_id=?", (page_id,)).fetchone()
                        if existe:
                            continue
                        # Página faltante no meio: recupera inserindo normalmente.

                    lote.append(linha)
                    if len(lote) >= BATCH_SIZE:
                        lote_checkpoint = lote[-1][0] # O page_id do último item deste lote
                        cur.executemany(sql_insert, lote)
                        # MAX() garante checkpoint monotônico: nunca regride,
                        # mesmo que o lote contenha páginas faltantes recuperadas.
                        cur.execute("UPDATE progresso SET ultimo_page_id=MAX(ultimo_page_id,?) WHERE arquivo=?",
                                    (lote_checkpoint, nome))
                        conn.commit() # Confirma as páginas e o checkpoint numa transação só
                        conn.execute("BEGIN")

                        processadas += len(lote)
                        lote.clear()
                        mostrar_progresso(indice, nome, processadas, lote_checkpoint)

                if lote:
                    lote_checkpoint = lote[-1][0]
                    cur.executemany(sql_insert, lote)
                    cur.execute("UPDATE progresso SET ultimo_page_id=MAX(ultimo_page_id,?) WHERE arquivo=?",
                                (lote_checkpoint, nome))
                    processadas += len(lote)

                # Marca arquivo como concluído
                cur.execute("UPDATE progresso SET completo=1 WHERE arquivo=?", (nome,))
                conn.commit()
                conn.execute("BEGIN")

                # Move o cursor de terminal para baixo das 4 linhas do progresso
                if processadas > 0: sys.stdout.write("\n\n\n\n")
                sys.stdout.flush()
                print(f"✓ {nome} concluído: {processadas:,} novas páginas em {time.time() - inicio:,.0f}s.")
            except (OSError, EOFError) as e:
                # Trata erros de corrupção ou fim inesperado do dump .bz2
                conn.rollback() # Cancela o lote atual
                conn.execute("BEGIN")
                if processadas > 0: sys.stdout.write("\n\n\n\n")
                sys.stdout.flush()
                print(f"\n[ERRO] O dump {nome} parece estar truncado ou corrompido.")
                print(f"Progresso salvo até o checkpoint: {lote_checkpoint:,}")
                print(f"Detalhe: {e}")
                # Interrompe o programa para evitar ignorar erros; o usuário pode arrumar o dump e retomar
                break
            except Exception as e:
                conn.rollback()
                conn.execute("BEGIN")
                if processadas > 0: sys.stdout.write("\n\n\n\n")
                sys.stdout.flush()
                print(f"\n[ERRO] Erro inesperado ao processar {nome}: {e}")
                raise
    except KeyboardInterrupt:
        if 'processadas' in locals() and processadas > 0: sys.stdout.write("\n\n\n\n")
        sys.stdout.flush()
        print("\n[AVISO] Interrupção (Ctrl+C) detectada!")
        print("Fazendo rollback do lote atual para preservar a integridade...")
        conn.rollback() # Limpa as páginas do lote atual que não foram comitadas com o checkpoint
        print("Pode executar o script novamente para continuar de onde parou.")
    except Exception as e:
        sys.stdout.write("\n\n\n\n")
        sys.stdout.flush()
        print("\n[AVISO] Erro fatal detectado. Rollback do lote atual.")
        conn.rollback()
        raise
    finally:
        # Só garantimos um commit final das outras coisas se estiver numa transação ativa
        if conn.in_transaction: conn.commit()
        if pulados: print(f"\n{pulados} dump(s) já completos foram pulados.")

        print("\nExecutando VACUUM... (Isso pode demorar um pouco)")
        conn.execute("VACUUM")
        # Verificação final de integridade: garante que o .db não foi corrompido.
        print("Verificando integridade final do banco...")
        check = conn.execute("PRAGMA quick_check").fetchone()
        conn.close()
        if check and check[0] == "ok":
            print(f"Concluído: {DB_PATH} (integridade OK)")
        else:
            print(f"[FALHA] Integridade do banco comprometida: {check}")
            sys.exit(1)

if __name__ == "__main__":
    main()
