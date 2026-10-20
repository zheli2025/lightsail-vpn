"""限定范围的静态 Nginx 路由识别；只返回结构摘要，不返回指令值或 token。"""
from pathlib import Path
import re
import shlex

NAME=re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}')
ALLOWED={'listen','server_name','root','index','location','default_type','add_header','expires',
         'access_log','error_log','ssl_certificate','ssl_certificate_key','ssl_protocols',
         'ssl_prefer_server_ciphers','ssl_session_cache','ssl_session_timeout','ssl_ciphers',
         'try_files','return','if','alias','limit_req','limit_conn','client_max_body_size',
         'include','allow','deny','auth_basic','auth_basic_user_file','autoindex'}


def parse(text):
    # 只实现迁移需要的受限语法；复杂/不确定配置一律交人工核对。
    lex=shlex.shlex(text,posix=True,punctuation_chars='{};')
    lex.whitespace_split=True; lex.commenters='#'
    tokens=[]
    for token in lex:
        tokens.extend(list(token) if token and set(token)<=set('{};') else [token])
        if len(tokens)>50000: raise ValueError('too many tokens')
    def block(index,depth=0):
        if depth>12: raise ValueError('too deep')
        nodes=[]; words=[]
        while index<len(tokens):
            token=tokens[index]; index+=1
            if token=='}':
                if not depth or words: raise ValueError('invalid close')
                return nodes,index
            if token==';':
                if not words: raise ValueError('empty directive')
                nodes.append((words[0],words[1:],None)); words=[]
            elif token=='{':
                if not words: raise ValueError('invalid block')
                children,index=block(index,depth+1)
                nodes.append((words[0],words[1:],children)); words=[]
            else: words.append(token)
        if depth or words: raise ValueError('unterminated')
        return nodes,index
    return block(0)[0]


def route(args):
    values=list(args)
    if values and values[0] in ('=','^~'): values.pop(0)
    if len(values)!=1: return 'other'
    value=values[0]
    if value=='/': return 'root'
    if value in ('/geo','/geo/') or value.startswith('/geo/'): return 'geo'
    if value=='/.well-known/acme-challenge/': return 'acme'
    if re.fullmatch(r'/[A-Za-z0-9_.-]+\.(yaml|yml)',value): return 'subscription'
    return 'other'


def summarize(text):
    nodes=parse(text); reasons=set(); routes=set(); ports=set(); evidence=False; servers=0
    def walk(items,inside_if=False):
        nonlocal evidence
        for name,args,children in items:
            if name not in ALLOWED: reasons.add('含不支持的指令或应用代理')
            if inside_if and name!='return': reasons.add('条件块不是简单返回鉴权')
            if name=='listen':
                value=args[0] if args else ''
                match=re.search(r'(?:^|:)([0-9]+)$',value)
                if not match or int(match[1]) not in (80,443): reasons.add('监听端口不在 80/443 范围')
                else: ports.add(int(match[1]))
            if name=='server_name':
                if any(not (v in ('_','localhost','') or re.fullmatch(r'[0-9.]+',v)) for v in args):
                    reasons.add('包含域名站点，不能认定为专用 IP VPN')
            if name=='root' and (len(args)!=1 or args[0].rstrip('/')!='/var/www/html'): reasons.add('Web 根目录不是标准 VPN 目录')
            if name=='alias':
                value=args[0] if len(args)==1 else ''
                if not (value.rstrip('/')=='/var/www/html/geo' or re.fullmatch(r'/var/www/html/[A-Za-z0-9_.-]+\.(yaml|yml)',value)):
                    reasons.add('alias 目标不在标准 Geo/订阅范围')
            if name=='include' and args!=['/etc/nginx/subscription-userinfo.conf']:
                reasons.add('包含未展开的外部配置，需人工核对')
            if name=='location':
                kind=route(args); routes.add(kind)
                if kind=='other': reasons.add('包含非 Geo/订阅/ACME 的额外路由')
                if kind in ('geo','subscription'): evidence=True
            if children is not None:
                if name not in ('location','if'): reasons.add('包含不支持的嵌套配置')
                walk(children,name=='if')
    for name,args,children in nodes:
        if name=='server' and children is not None:
            servers+=1; walk(children)
        elif name not in ('limit_req_zone','limit_conn_zone') or children is not None:
            reasons.add('站点文件含额外顶层配置')
    if not servers: reasons.add('未发现 server 配置')
    if not ports: reasons.add('无法确认 HTTP/HTTPS 监听')
    if not evidence: reasons.add('未发现 Geo 或 YAML 订阅路由')
    return {'candidate':not reasons,'server_count':servers,'ports':sorted(ports),
            'routes':sorted(routes),'reasons':sorted(reasons)}


def inspect_sites(root=Path('/')):
    root=Path(root); directory=root/'etc/nginx/sites-enabled'; results=[]
    if not directory.exists(): return results
    if directory.is_symlink():
        return [{'name':'<sites-enabled>','candidate':False,'reasons':['站点目录为符号链接，需人工核对']}]
    base=(root/'etc/nginx').resolve()
    allowed=[p.resolve() for p in (root/'etc/nginx'/name for name in ('sites-enabled','sites-available','conf.d')) if p.resolve().parent==base]
    for file in sorted(directory.iterdir()):
        report={'name':file.name,'candidate':False,'reasons':[]}
        try:
            if not NAME.fullmatch(file.name): raise ValueError()
            source=file.resolve(strict=True)
            if not any(source.is_relative_to(parent) for parent in allowed): raise ValueError()
            if source.suffix.lower() in ('.key','.pem','.crt','.json') or not source.is_file() or source.stat().st_size>262144:
                raise ValueError()
            report.update(summarize(source.read_text()))
        except (OSError,ValueError,UnicodeError):
            report['reasons']=['配置路径/语法超出受限识别范围；未输出原文']
        results.append(report)
    return results
