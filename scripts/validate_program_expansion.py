#!/usr/bin/env python3
"""Validate candidate plans and scope manifests; never approve or dispatch them.

Usage: python scripts/validate_program_expansion.py [--repo PATH] [--workspace PATH]
--workspace points to sibling clones of all four repositories for cross-repo DAG checks.
"""
from pathlib import Path
import argparse
import hashlib
import json
import re

SAFE=re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}')

def canon(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def read(path):
    def pairs(items):
        out={}
        for k,v in items:
            if k in out:raise ValueError('duplicate JSON key: '+k)
            out[k]=v
        return out
    return json.loads(path.read_text(),object_pairs_hook=pairs)

def need(condition,message):
    if not condition:raise ValueError(message)

def dag(graph):
    left={k:set(v) for k,v in graph.items()};done=set();waves=0
    while left:
        ready={k for k,v in left.items() if v<=done}
        need(bool(ready),'cycle or dangling dependency: '+str(list(left)[:12]))
        done.update(ready)
        for k in ready:del left[k]
        waves+=1
    return waves

def verify(root):
    plan=read(root/'.aiops/program.json');m=read(root/'docs/aiops/REGISTRATION_SCOPE_DRAFT.json')
    need(plan['schema_version']==1,'unsupported plan schema')
    need('PENDING' in plan['approval_pointer'],'candidate must remain non-executable')
    need('registration_scope' not in plan,'do not invent a registration reader')
    need(plan['repository']==m['repository'] and plan['program']==m['program'],'manifest identity mismatch')
    need((root/'.aiops/program.json').read_bytes()==(root/m['plan_definition']['path']).read_bytes(),'candidate mirror differs from source draft')
    definition={k:v for k,v in plan.items() if k not in m['plan_definition']['excluded_root_fields']}
    need(canon(definition)==m['plan_definition']['definition_sha256'],'plan definition digest mismatch')
    need(len(plan['nodes'])==m['plan_definition']['node_count'],'local count mismatch')
    local=plan['nodes'];pending=[]
    for e in m['pending_catalogues']:
        cat=read(root/e['path']);need(canon(cat)==e['definition_sha256'],'pending digest mismatch: '+e['path'])
        need(len(cat['nodes'])==e['node_count'],'pending count mismatch')
        need(cat['repository']==plan['repository'] and cat['program']==plan['program'],'pending identity mismatch')
        pending+=cat['nodes']
    nodes=local+pending;by={n['id']:n for n in nodes}
    need(len(by)==len(nodes),'duplicate ID across local/pending')
    need(len({k.upper() for k in by})==len(by),'case-fold task ID collision')
    need(len(nodes)==m['candidate_node_count'],'total denominator mismatch')
    recorded={x['id']:x['definition_sha256'] for x in m['node_definitions']}
    need(len(recorded)==len(m['node_definitions']) and set(recorded)==set(by),'manifest node inventory mismatch')
    for ident,n in by.items():
        need(bool(SAFE.fullmatch(ident)),'unsafe node ID: '+ident)
        need(isinstance(n.get('title'),str) and bool(n['title'].strip()),'empty title')
        need(isinstance(n.get('spec'),str) and bool(n['spec'].strip()),'empty spec')
        need(canon(n)==recorded[ident],'node definition mismatch: '+ident)
        need(n.get('audit_floor','A1') in ('A0','A1','A2','A3'),'invalid audit floor')
        need(n.get('astra_gate','NONE') in ('NONE','MILESTONE','ARCHITECTURE','RELEASE'),'invalid gate')
        need(n.get('deliverable_mode','PR')=='PR','unsupported delivery')
        for flag in ('user_merge','astra_auto_merge'):
            need(flag not in n or type(n[flag]) is bool,'invalid boolean flag')
        need(not(n.get('user_merge') and n.get('astra_auto_merge')),'conflicting merge flags')
        need(not(n.get('astra_gate')=='RELEASE' and n.get('astra_auto_merge')),'release cannot delegate merge')
        deps=n.get('depends_on',[])
        need(isinstance(deps,list) and len(deps)==len(set(deps)),'invalid/repeated dependency')
        need(all(d in by and d!=ident for d in deps),'missing local dependency: '+ident)
        for e in n.get('depends_on_external',[]):
            need(set(e)=={'repository','program','node'},'external reference fields')
            need(e['repository']!=plan['repository'],'same-repo edge must use depends_on')
            need(bool(SAFE.fullmatch(e['node'])),'unsafe external node')
    local_ids={n['id'] for n in local}
    for n in local:
        need('depends_on_external' not in n,'schema-v1 does not support external dependency fields')
        need(set(n.get('depends_on',[]))<=local_ids,'local node depends on pending definition')
    need(set(m['user_only_nodes'])=={n['id'] for n in nodes if n.get('user_merge') is True},'user-only inventory mismatch')
    for name,expected in m['definition_document_sha256'].items():
        need(hashlib.sha256((root/name).read_bytes()).hexdigest()==expected,'document hash mismatch: '+name)
    subset=m.get('finance_subset')
    if subset:
        f=read(root/subset['path']);need(canon(f)==subset['definition_sha256'],'finance subset digest')
        refs=f.get('nodes',[])
        for n in refs:need(n['id'] in by and n==by[n['id']],'finance alias differs: '+n['id'])
        need({x['id']:x['definition_sha256'] for x in subset['nodes']}=={n['id']:canon(n) for n in refs},'finance alias inventory')
    depth=dag({k:set(n.get('depends_on',[])) for k,n in by.items()})
    return plan,nodes,{'repository':plan['repository'],'local_candidate':len(local),'pending':len(pending),'total':len(nodes),'local_and_pending_dag_waves':depth,'status':'VALID_CANDIDATE_NOT_APPROVED'}

def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--repo',type=Path,default=Path(__file__).resolve().parents[1]);ap.add_argument('--workspace',type=Path);args=ap.parse_args()
    roots=[args.repo]
    if args.workspace:roots=[args.workspace/x for x in ('ZARI','film-unit-mv-studio','kix-protocol','kix-commerce-apps')]
    inventory={};reports=[]
    for root in roots:
        plan,nodes,report=verify(root);reports.append(report)
        for n in nodes:inventory[(plan['repository'],plan['program'],n['id'])]=n
    graph={};unresolved=[]
    for key,n in inventory.items():
        deps={(key[0],key[1],d) for d in n.get('depends_on',[])}
        for e in n.get('depends_on_external',[]):
            dep=(e['repository'],e['program'],e['node'])
            if dep not in inventory:unresolved.append({'from':key,'to':dep})
            else:deps.add(dep)
        graph[key]=deps
    if args.workspace:need(not unresolved,'dangling cross-repo references: '+str(unresolved))
    waves=dag(graph)
    print(json.dumps({'repositories':reports,'combined_nodes':len(inventory),'combined_dag_waves':waves,'external_graph': 'CHECKED' if args.workspace else 'NOT_CHECKED_PEER_REPOS_REQUIRED','unresolved_external':unresolved,'authority':'STRUCTURAL_CHECK_NOT_AUDIT_OR_RUNTIME_QUALIFICATION'},ensure_ascii=False,indent=2))

if __name__=='__main__':main()
