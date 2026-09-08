import html
import re

# Detecção de páginas de redirecionamento
RE_REDIRECT = re.compile(
    r"^\s*#(?:REDIRECT|REDIRECIONAMENTO)\b",
    re.IGNORECASE
)

# Comentários e referências
RE_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
RE_REF = re.compile(r"<ref[^>]*/>|<ref[^>]*>.*?</ref>", re.DOTALL | re.IGNORECASE)
RE_REFLIST = re.compile(
    r"<(?:references|gallery)[^>]*/>|<(?:references|gallery)[^>]*>.*?</(?:references|gallery)>",
    re.DOTALL | re.IGNORECASE
)

# Tags HTML de formatação
RE_TAG_BR = re.compile(r"<(?:br|hr)\s*/?>", re.IGNORECASE)
RE_TAG_ESTILO = re.compile(r"<(?:b|strong)[^>]*>(.*?)</(?:b|strong)>", re.IGNORECASE | re.DOTALL)
RE_TAG_ITALIC = re.compile(r"<(?:i|em)[^>]*>(.*?)</(?:i|em)>", re.IGNORECASE | re.DOTALL)
RE_TAG_CODE = re.compile(r"<(?:code|tt)[^>]*>(.*?)</(?:code|tt)>", re.IGNORECASE | re.DOTALL)
RE_TAG_MATH = re.compile(r"<(?:math|chem)[^>]*>(.*?)</(?:math|chem)>", re.IGNORECASE | re.DOTALL)
RE_TAG_NOWIKI = re.compile(r"<nowiki>(.*?)</nowiki>", re.IGNORECASE | re.DOTALL)
RE_TAG_GENERICA = re.compile(r"</?[a-z][^>\n]*>", re.IGNORECASE)

# Links e categorias
RE_CATEGORIA = re.compile(r"\[\[(?:Category|Categoria):([^\]|]+)(?:\|[^\]]*)?\]\]", re.IGNORECASE)
RE_LINK_INTERNO = re.compile(r"\[\[(?:[^|\]]*\|)?([^\]|]+)\]\]")
RE_LINK_EXTERNO = re.compile(r"\[(?:https?:)?//[^\s\]]+\s+([^\]]+)\]")
RE_URL_SOLTA = re.compile(r"\[(?:https?:)?//[^\s\]]+\]")

# Cabeçalhos
RE_CABECALHO = re.compile(r"^(={2,6})\s*(.+?)\s*\1\s*$", re.M)

# Templates simples (folhas sem {{ }} interno)
RE_TEMPLATE_FOLHA = re.compile(r"\{\{([^{}]+)\}\}")

def eh_redirecionamento(texto: str) -> bool:
    if not texto: return False
    return bool(RE_REDIRECT.match(texto.strip()))

def converter_wikitable(bloco: str) -> str:
    linhas = bloco.strip().splitlines()
    caption = ""
    linhas_tabela = []
    linha_atual = []
    eh_cabecalho_atual = False

    def salvar_linha():
        nonlocal linha_atual, eh_cabecalho_atual
        if linha_atual:
            celulas_limpas = []
            for c in linha_atual:
                c = c.strip()
                if "|" in c:
                    partes = c.split("|", 1)
                    if any(attr in partes[0] for attr in ("=", "style", "class", "colspan", "rowspan", "align", "bgcolor", "width", "scope")):
                        c = partes[1].strip()
                c = re.sub(r"\s*\n\s*", " ", c).strip()
                celulas_limpas.append(c)
            if any(celulas_limpas):
                linhas_tabela.append((eh_cabecalho_atual, celulas_limpas))
            linha_atual = []

    for l in linhas:
        l_strip = l.strip()
        if not l_strip: continue
        if l_strip.startswith("{|") or l_strip.startswith("|}"): continue
        if l_strip.startswith("|+"):
            caption = l_strip[2:].strip()
            continue
        if l_strip.startswith("|-"):
            salvar_linha()
            eh_cabecalho_atual = False
            continue
        if l_strip.startswith("!"):
            if not eh_cabecalho_atual and linha_atual: salvar_linha()
            eh_cabecalho_atual = True
            conteudo = l_strip[1:]
            for cel in re.split(r"!!|\|\|", conteudo): linha_atual.append(cel)
            continue
        if l_strip.startswith("|"):
            if eh_cabecalho_atual and linha_atual: salvar_linha()
            eh_cabecalho_atual = False
            conteudo = l_strip[1:]
            for cel in re.split(r"\|\|", conteudo):
                linha_atual.append(cel)
            continue
        if linha_atual: linha_atual[-1] += " " + l_strip

    salvar_linha()
    if not linhas_tabela: return ""

    max_cols = max(len(celulas) for _, celulas in linhas_tabela)
    if max_cols == 0: return ""

    saida = []
    if caption: saida.append(f"\n### {caption}\n")

    primeira_linha = True
    for _, celulas in linhas_tabela:
        celulas = celulas + [""] * (max_cols - len(celulas))
        linha_md = "| " + " | ".join(c.replace("|", "/") for c in celulas) + " |"
        saida.append(linha_md)
        if primeira_linha:
            saida.append("| " + " | ".join(["---"] * max_cols) + " |")
            primeira_linha = False

    return "\n" + "\n".join(saida) + "\n"

def substituir_wikitables(texto: str) -> str:
    if "{|" not in texto: return texto

    def encontrar_tabela(t, start=0):
        pos_inicio = t.find("{|", start)
        if pos_inicio == -1: return None, -1, -1
        pos = pos_inicio + 2
        nivel = 1
        n = len(t)
        while pos < n and nivel > 0:
            p_abrir = t.find("{|", pos)
            p_fechar = t.find("|}", pos)
            if p_fechar == -1: break
            if p_abrir != -1 and p_abrir < p_fechar:
                nivel += 1
                pos = p_abrir + 2
            else:
                nivel -= 1
                pos = p_fechar + 2
        if nivel == 0: return t[pos_inicio:pos], pos_inicio, pos
        return None, -1, -1

    while True:
        bloco, inicio, fim = encontrar_tabela(texto)
        if not bloco: break
        tabela_md = converter_wikitable(bloco)
        texto = texto[:inicio] + tabela_md + texto[fim:]
    return texto

def processar_template(conteudo: str) -> str:
    partes = [p.strip() for p in conteudo.split("|")]
    if not partes or not partes[0]: return ""

    nome = partes[0].lower().strip()
    args_posicionais = []
    args_nomeados = {}

    for p in partes[1:]:
        if "=" in p:
            chave, _, val = p.partition("=")
            args_nomeados[chave.strip().lower()] = val.strip()
        else:
            args_posicionais.append(p.strip())

    # 1. Metadados e navegação conhecidos para remoção completa
    METADADOS_PREFIXOS = (
        "rcat", "redirect", "r from", "short description", "use dmy", "use mdy",
        "reflist", "refbegin", "refend", "cite", "citation", "portal",
        "authority control", "navbox", "sidebar", "good article", "featured",
        "about", "for", "main", "see also", "further", "disambiguation",
        "hatnote", "defaultsort", "s-", "succession"
    )

    # Infobox e Taxobox: extrai pares chave-valor para não perder dados importantes
    if nome.startswith("infobox") or nome.startswith("taxobox"):
        linhas_info = []
        for k, v in args_nomeados.items():
            if any(ign in k for ign in ("image", "caption", "width", "style", "alt", "class", "map", "logo")):
                continue
            if v and len(v) < 200:
                nome_prop = k.replace("_", " ").capitalize()
                linhas_info.append(f"{nome_prop}: {v}")
        if linhas_info:
            return "\n" + "\n".join(linhas_info) + "\n"
        return ""

    for pref in METADADOS_PREFIXOS:
        if nome.startswith(pref): return ""

    # 2. Conversão e medidas
    if nome in ("convert", "cvt"):
        if len(args_posicionais) >= 2:
            num = args_posicionais[0]
            unidade = args_posicionais[1]
            unidade_dest = args_posicionais[2] if len(args_posicionais) > 2 and not args_posicionais[2].isdigit() else ""
            if unidade_dest: return f"{num} {unidade} ({unidade_dest})"
            return f"{num} {unidade}"
        elif args_posicionais:
            return args_posicionais[0]
        return ""

    # 3. Formatação numérica e valores
    if nome == "val":
        num = args_posicionais[0] if args_posicionais else ""
        unidade = args_nomeados.get("u") or args_nomeados.get("ul") or ""
        expoente = args_nomeados.get("e", "")
        res = num
        if expoente: res += f" × 10^{expoente}"
        if unidade: res += f" {unidade}"
        return res

    if nome in ("frac", "sfrac"):
        if len(args_posicionais) == 1:
            return f"1/{args_posicionais[0]}"
        elif len(args_posicionais) == 2:
            return f"{args_posicionais[0]}/{args_posicionais[1]}"
        elif len(args_posicionais) == 3:
            return f"{args_posicionais[0]} {args_posicionais[1]}/{args_posicionais[2]}"
        return ""

    # 4. Idiomas e transcrições
    if nome in ("lang", "transl") and len(args_posicionais) >= 2:
        return args_posicionais[1]
    if nome.startswith("lang-") and args_posicionais:
        return args_posicionais[0]
    if nome == "nihongo":
        partes_nihongo = [p for p in args_posicionais[:3] if p]
        if partes_nihongo:
            if len(partes_nihongo) > 1:
                return f"{partes_nihongo[0]} ({', '.join(partes_nihongo[1:])})"
            return partes_nihongo[0]

    # 5. Formatação de texto
    if nome in ("nowrap", "small", "mvar", "math", "sub", "sup", "b", "i", "em", "strong"):
        return args_posicionais[0] if args_posicionais else ""

    # 6. Datas
    if nome in ("circa", "c.", "ca"):
        ano = args_posicionais[0] if args_posicionais else ""
        return f"c. {ano}" if ano else ""
    if nome in ("as of", "asof"):
        ano = args_posicionais[0] if args_posicionais else ""
        return f"a partir de {ano}" if ano else ""

    # 7. Citações
    if nome in ("quote", "quotation"):
        cit = args_posicionais[0] if args_posicionais else ""
        autor = args_posicionais[1] if len(args_posicionais) > 1 else args_nomeados.get("author", "")
        if cit and autor: return f'"{cit}" — {autor}'
        return f'"{cit}"' if cit else ""

    # 8. Listas
    if nome in ("unbulleted list", "plainlist", "flatlist", "hlist"):
        itens = [p for p in args_posicionais if p]
        return ", ".join(itens)

    # 9. Coordenadas
    if nome in ("coord", "coor"): return " ".join(args_posicionais[:4])

    # Outros templates com 1 parâmetro de texto posicional
    if len(args_posicionais) == 1 and not args_nomeados:
        return args_posicionais[0]

    return ""

def resolver_templates(texto: str) -> str:
    if "{{" not in texto: return texto
    for _ in range(15):
        if "{{" not in texto: break
        novo_texto = RE_TEMPLATE_FOLHA.sub(lambda m: processar_template(m.group(1)), texto)
        if novo_texto == texto: break
        texto = novo_texto
    texto = re.sub(r"\{\{[^{}]*$", "", texto)
    return texto
def extrair_legendas_imagens(texto: str) -> str:
    PREFIXOS = ("file:", "image:", "imagem:", "ficheiro:", "arquivo:")
    MODIFICADORES_TECNICOS = {
        "thumb", "thumbnail", "frame", "framed", "frameless", "border",
        "left", "right", "center", "none", "esquerda", "direita", "centro",
        "baseline", "sub", "super", "top", "text-top", "middle", "bottom", "text-bottom"
    }

    if not any(pref in texto.lower() for pref in PREFIXOS):
        return texto

    def processar_tag_imagem(conteudo: str) -> str:
        partes = []
        colchetes = 0
        atual = []
        for ch in conteudo:
            if ch == "[":
                colchetes += 1
                atual.append(ch)
            elif ch == "]":
                colchetes -= 1
                atual.append(ch)
            elif ch == "|" and colchetes == 0:
                partes.append("".join(atual).strip())
                atual = []
            else:
                atual.append(ch)
        if atual: partes.append("".join(atual).strip())

        if not partes: return ""

        legendas = []
        for p in partes[1:]:
            p_lower = p.lower()
            if p_lower in MODIFICADORES_TECNICOS: continue
            if re.match(r"^(?:\d+px|\d+x\d+px|upright(?:=[\d.]+)?)$", p_lower):
                continue
            if re.match(r"^(?:alt|link|page|class|lang)=.*", p_lower):
                continue
            if p: legendas.append(p)

        if legendas:
            legenda_texto = legendas[-1].strip()
            if legenda_texto: return f"\n\n{legenda_texto}\n\n"
        return " "

    resultado = []
    i = 0
    n = len(texto)
    while i < n:
        if texto[i:i+2] == "[[":
            start = i
            i += 2
            profundidade = 1
            while i < n and profundidade > 0:
                if texto[i:i+2] == "[[":
                    profundidade += 1
                    i += 2
                elif texto[i:i+2] == "]]":
                    profundidade -= 1
                    i += 2
                else:
                    i += 1
            bloco_completo = texto[start:i]
            conteudo = bloco_completo[2:-2]
            conteudo_lower = conteudo.strip().lower()
            if any(conteudo_lower.startswith(pref) for pref in PREFIXOS):
                resultado.append(processar_tag_imagem(conteudo))
            else:
                resultado.append(bloco_completo)
        else:
            resultado.append(texto[i])
            i += 1

    return "".join(resultado)

def limpar_secoes_vazias(texto: str) -> str:
    linhas = texto.splitlines()
    novas_linhas = []
    i = 0
    n = len(linhas)
    while i < n:
        l = linhas[i].strip()
        if l.startswith("##"):
            proxima = ""
            for j in range(i + 1, n):
                if linhas[j].strip():
                    proxima = linhas[j].strip()
                    break
            if (not proxima or proxima.startswith("##")) and any(
                termo in l.lower() for termo in (
                    "reference", "referência", "see also", "ver também",
                    "external link", "ligações externa"
                )
            ):
                i += 1
                continue
        novas_linhas.append(linhas[i])
        i += 1
    return "\n".join(novas_linhas)

def formatar_texto(wikitexto: str) -> str:
    if not wikitexto: return ""

    t = wikitexto

    # 1. Comentários HTML
    t = RE_COMMENT.sub(" ", t)

    # 2. Tags de referências e citações de rodapé
    t = RE_REF.sub(" ", t)
    t = RE_REFLIST.sub(" ", t)

    # 3. Tags HTML de formatação inline
    t = RE_TAG_BR.sub("\n", t)
    t = RE_TAG_ESTILO.sub(r"**\1**", t)
    t = RE_TAG_ITALIC.sub(r"*\1*", t)
    t = RE_TAG_CODE.sub(r"`\1`", t)
    t = RE_TAG_MATH.sub(r"\1", t)
    t = RE_TAG_NOWIKI.sub(r"\1", t)
    t = RE_TAG_GENERICA.sub(" ", t)

    # 4. Tabelas Wikitext -> Markdown
    t = substituir_wikitables(t)

    # 5. Templates e Predefinições
    t = resolver_templates(t)

    # 6. Imagens e Arquivos (preserva legendas úteis)
    t = extrair_legendas_imagens(t)

    # 7. Categorias
    categorias = RE_CATEGORIA.findall(t)
    t = RE_CATEGORIA.sub("", t)

    # 8. Links internos e externos
    t = RE_LINK_INTERNO.sub(r"\1", t)
    t = RE_LINK_EXTERNO.sub(r"\1", t)
    t = RE_URL_SOLTA.sub(" ", t)

    # 9. Cabeçalhos
    def ajustar_cabecalho(m):
        nivel = len(m.group(1))
        prefixo = "#" * max(2, nivel)
        return f"\n{prefixo} {m.group(2).strip()}\n"
    t = RE_CABECALHO.sub(ajustar_cabecalho, t)

    # 10. Formatação de negrito e itálico do wikitexto
    t = re.sub(r"'{5}(.+?)'{5}", r"***\1***", t)
    t = re.sub(r"'{3}(.+?)'{3}", r"**\1**", t)
    t = re.sub(r"'{2}(.+?)'{2}", r"*\1*", t)

    # 11. Decodificação de entidades HTML (&nbsp;, &ndash;, etc.)
    t = html.unescape(t)

    # 12. Limpeza de resíduos de pontuação e parênteses vazios
    t = re.sub(r"\(\s*[;,]?\s*\)", "", t)
    t = re.sub(r"\(\s*;\s*", "(", t)
    t = re.sub(r"\s+([,.;:!?])", r"\1", t)

    # 13. Limpar seções vazias de rodapé
    t = limpar_secoes_vazias(t)

    # 14. Anexar categorias organizadas no final do texto
    if categorias:
        cats_limpas = [c.strip() for c in categorias if c.strip()]
        if cats_limpas:
            t = t.rstrip() + "\n\nCategorias: " + ", ".join(cats_limpas)

    # 15. Normalização de espaçamento
    t = re.sub(r"[ \t]+$", "", t, flags=re.M)
    t = re.sub(r"\n{3,}", "\n\n", t)

    return t.strip()
def eh_pagina_util(
    titulo: str,
    namespace: int,
    wikitexto: str,
    apenas_artigos: bool = True,
    filtrar_redirecionamentos: bool = True,
    tamanho_minimo: int = 20,
) -> bool:
    if not titulo or not wikitexto: return False
    if apenas_artigos and namespace != 0: return False
    if filtrar_redirecionamentos and eh_redirecionamento(wikitexto):
        return False

    # Verificação rápida se o texto bruto é puramente um redirect disfarçado
    texto_inicio = wikitexto.lstrip()[:100].upper()
    if filtrar_redirecionamentos and ("#REDIRECT" in texto_inicio or "#REDIRECIONAMENTO" in texto_inicio):
        return False

    return True
