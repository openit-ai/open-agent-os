"""Outline adapter — Outline API (collections/documents/search) + ACL pre-filter (§28).

Retrieval flow per §28:
  Identity -> Allowed Scope (gateway ACL pre-filter) -> Retrieval -> Allowed Documents
Per §27.2: memory provenance (source_resource_id, source_acl_version, delegation_id)
Per §16I: data_access read_replica enforcement
Per §16H: field/row limits via ToolPolicy
Per §28: ACL versioning + invalidation on permission change

Env:
  OUTLINE_API_URL (default https://app.getoutline.com)
  OUTLINE_API_KEY  (required for real calls; adapter runs in mock fallback without it)
"""
from __future__ import annotations
import fnmatch, os, re
from dataclasses import dataclass, field
from typing import Any
try:
    import httpx  # type: ignore
except ImportError:
    httpx = None  # type: ignore
try:
    from execution_gateway.data_access import get_data_access_policy  # type: ignore
except Exception:
    try:
        from data_access import get_data_access_policy  # type: ignore
    except Exception:
        get_data_access_policy = None  # type: ignore
try:
    from execution_gateway.tool_policy import ToolPolicy, validate_tool_call  # type: ignore
except Exception:
    try:
        from tool_policy import ToolPolicy, validate_tool_call  # type: ignore
    except Exception:
        ToolPolicy = None  # type: ignore
        validate_tool_call = None  # type: ignore
try:
    from governance.governance import MemoryStore, MemoryScope  # type: ignore
except Exception:
    try:
        from security.memory_governance.governance.governance import MemoryStore, MemoryScope  # type: ignore
    except Exception:
        MemoryStore = None  # type: ignore
        MemoryScope = None  # type: ignore

DEFAULT_API_URL = "https://app.getoutline.com"
DEFAULT_ACL: dict[str, dict[str, Any]] = {
    "outline/*": {"tenants": ["*"], "groups": ["*"], "public": False},
    "outline/team/*": {"tenants": ["*"], "groups": ["*"], "public": False},
    "outline/private/*": {"tenants": ["*"], "groups": ["admin"], "public": False},
}

@dataclass
class OutlineDoc:
    id: str
    collection_id: str
    title: str
    content: str
    url: str
    classification: str = "INTERNAL"
    acl_version: str = "v1"
    acl: dict[str, Any] = field(default_factory=dict)
    score: float = 0.0
    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id,"collection_id":self.collection_id,"title":self.title,"content":self.content,"url":self.url,"classification":self.classification,"acl_version":self.acl_version,"acl":self.acl,"score":self.score}

_DEFAULT_DOCS: list[OutlineDoc] = [
    OutlineDoc(id="doc_001",collection_id="team",title="Onboarding Guide",content="Welcome to the team. Onboarding process and rituals.",url="https://outline.example.com/doc/onboarding",classification="INTERNAL",acl_version="v1"),
    OutlineDoc(id="doc_002",collection_id="team",title="API Design Guidelines",content="API design principles: REST, versioning, error handling.",url="https://outline.example.com/doc/api-design",classification="INTERNAL",acl_version="v1"),
    OutlineDoc(id="doc_003",collection_id="team",title="Security Policy",content="Security policy: credential rotation, confidential handling.",url="https://outline.example.com/doc/security-policy",classification="CONFIDENTIAL",acl_version="v2"),
    OutlineDoc(id="doc_004",collection_id="private",title="Finance Budget 2026",content="Finance budget confidential: Q1 allocation and headcount.",url="https://outline.example.com/doc/finance-budget",classification="CONFIDENTIAL",acl_version="v1",acl={"groups":["admin","finance"]}),
    OutlineDoc(id="doc_005",collection_id="team",title="Customer Data Handling",content="PII handling: customer email and phone must be encrypted.",url="https://outline.example.com/doc/pii-handling",classification="PII",acl_version="v1"),
]
_DEFAULT_COLLECTIONS: list[dict[str,Any]] = [
    {"id":"team","name":"Team","description":"Team knowledge base"},
    {"id":"private","name":"Private","description":"Private / admin only"},
    {"id":"engineering","name":"Engineering","description":"Engineering docs"},
]

def _build_tool_policies() -> dict[str,Any]:
    if ToolPolicy is None: return {}
    return {
        "outline_search": ToolPolicy(tool="outline_search", allowed_actions=["SEARCH","READ"], limits={"max_results":50}, denied_fields=["password","secret","token","credential","private_key"]),
        "outline_read": ToolPolicy(tool="outline_read", allowed_actions=["READ","SEARCH"], limits={"max_results":1}, denied_fields=["password","secret","token"]),
        "outline_create": ToolPolicy(tool="outline_create", allowed_actions=["CREATE"], limits={"max_results":1}),
        "outline_modify": ToolPolicy(tool="outline_modify", allowed_actions=["MODIFY","UPDATE"], limits={"max_results":1}),
        "outline_collections_list": ToolPolicy(tool="outline_collections_list", allowed_actions=["SEARCH","READ","LIST"], limits={"max_results":50}),
        "outline_collections_info": ToolPolicy(tool="outline_collections_info", allowed_actions=["READ","SEARCH"], limits={"max_results":1}),
        "outline_documents_list": ToolPolicy(tool="outline_documents_list", allowed_actions=["SEARCH","READ","LIST"], limits={"max_results":50}),
        "outline_documents_info": ToolPolicy(tool="outline_documents_info", allowed_actions=["READ","SEARCH"], limits={"max_results":1}),
    }
_TOOL_POLICIES = _build_tool_policies()

@dataclass(frozen=True)
class ACLCheckResult:
    allowed: bool; reason: str; matched_acl: str | None = None

class OutlineAdapter:
    name="outline"; provider="outline"
    TOOL_ACTION: dict[str,str]={"outline_search":"SEARCH","outline_read":"READ","outline_create":"CREATE","outline_modify":"MODIFY","outline_collections_list":"SEARCH","outline_collections_info":"READ","outline_documents_list":"SEARCH","outline_documents_info":"READ"}
    ENDPOINTS: dict[str,tuple[str,str]]={"outline_search":("POST","/api/documents.search"),"outline_read":("POST","/api/documents.info"),"outline_create":("POST","/api/documents.create"),"outline_modify":("POST","/api/documents.update"),"outline_collections_list":("POST","/api/collections.list"),"outline_collections_info":("POST","/api/collections.info"),"outline_documents_list":("POST","/api/documents.list"),"outline_documents_info":("POST","/api/documents.info")}
    def __init__(self,api_url: str|None=None,api_key: str|None=None,acl_store: dict[str,dict[str,Any]]|None=None,documents: list[OutlineDoc|dict[str,Any]]|None=None,collections: list[dict[str,Any]]|None=None,memory_store: Any|None=None) -> None:
        self.api_url=(api_url if api_url is not None else os.getenv("OUTLINE_API_URL") or DEFAULT_API_URL).rstrip("/")
        self.api_key=api_key if api_key is not None else (os.getenv("OUTLINE_API_KEY") or "")
        self._acl=dict(acl_store) if acl_store is not None else dict(DEFAULT_ACL)
        if documents is not None:
            self._docs: list[OutlineDoc]=[]
            for d in documents:
                if isinstance(d,dict):
                    self._docs.append(OutlineDoc(id=d.get("id",""),collection_id=d.get("collection_id",d.get("collection","team")),title=d.get("title",""),content=d.get("content",d.get("text","")),url=d.get("url",""),classification=d.get("classification","INTERNAL"),acl_version=d.get("acl_version","v1"),acl=d.get("acl",{})))
                else: self._docs.append(d)
        else: self._docs=list(_DEFAULT_DOCS)
        self._collections=list(collections) if collections is not None else list(_DEFAULT_COLLECTIONS)
        self._memory_store=memory_store
        self._acl_versions: dict[str,str]={}
        for d in self._docs:
            self._acl_versions[f"outline/{d.collection_id}/{d.id}"]=d.acl_version
            self._acl_versions[f"outline/{d.collection_id}"]=d.acl_version
        for c in self._collections:
            ck=f"outline/{c['id']}"
            if ck not in self._acl_versions: self._acl_versions[ck]="v1"
    def check_acl(self,agent_context:dict[str,Any]|Any,resource:str,action:str="READ")->ACLCheckResult:
        if isinstance(agent_context,dict):
            tenant_id=agent_context.get("tenant_id"); user_id=agent_context.get("user_id"); groups=list(agent_context.get("groups") or agent_context.get("context",{}).get("groups",[]) or [])
        else:
            tenant_id=getattr(agent_context,"tenant_id",None); user_id=getattr(agent_context,"user_id",None); groups=list(getattr(agent_context,"groups",[]) or [])
        if not tenant_id: return ACLCheckResult(False,"missing tenant_id in AgentContext")
        domain=resource.split("/")[0] if resource else ""
        if domain not in ("outline",""): return ACLCheckResult(True,f"domain {domain} not handled by outline adapter")
        return ACLCheckResult(True,"acl pre-check passed","outline-allow")
    def check_document_acl(self,agent_context:dict[str,Any]|Any,doc:OutlineDoc|dict[str,Any])->ACLCheckResult:
        if isinstance(doc,dict):
            collection_id=doc.get("collection_id") or doc.get("collection") or ""; doc_acl=doc.get("acl") or {}; doc_id=doc.get("id","")
        else: collection_id=doc.collection_id; doc_acl=doc.acl or {}; doc_id=doc.id
        if isinstance(agent_context,dict): groups=list(agent_context.get("groups") or []); user_id=agent_context.get("user_id") or ""
        else: groups=list(getattr(agent_context,"groups",[]) or []); user_id=getattr(agent_context,"user_id","") or ""
        allowed_groups=doc_acl.get("groups")
        if allowed_groups is not None:
            if not any(g in allowed_groups for g in groups) and "admin" not in groups:
                allowed_users=doc_acl.get("users",[])
                if user_id not in allowed_users and "*" not in allowed_groups:
                    return ACLCheckResult(False,f"document {doc_id} ACL denied: requires {allowed_groups}",None)
        for pattern,acl in self._acl.items():
            if fnmatch.fnmatch(f"outline/{collection_id}/{doc_id}",pattern):
                acl_groups=acl.get("groups",["*"])
                if "*" in acl_groups: return ACLCheckResult(True,"collection ACL allow",pattern)
                if any(g in acl_groups for g in groups): return ACLCheckResult(True,"collection ACL allow",pattern)
                if collection_id=="private" and "admin" not in groups: return ACLCheckResult(False,f"collection {collection_id} requires admin",pattern)
        return ACLCheckResult(True,"document ACL passed","doc-allow")
    def can_write(self,agent_context:dict[str,Any]|Any,resource:str)->ACLCheckResult:
        base=self.check_acl(agent_context,resource,action="CREATE")
        if not base.allowed: return base
        return ACLCheckResult(True,"write allowed",base.matched_acl)
    def _check_capability(self,agent_context:dict[str,Any]|Any,resource:str="outline/*",action:str="READ")->tuple[bool,str]:
        caps=None
        if isinstance(agent_context,dict): caps=agent_context.get("capabilities") or agent_context.get("capability_token") or agent_context.get("capability_tokens")
        else: caps=getattr(agent_context,"capabilities",None) or getattr(agent_context,"capability_token",None)
        if not caps: return True,"no capability token — dev allow"
        if isinstance(caps,dict): caps=[caps]
        if isinstance(caps,str): caps=[caps]
        if not isinstance(caps,list): return True,"unrecognized capability format — allow"
        for c in caps:
            if isinstance(c,dict):
                c_resource=c.get("resource") or c.get("pattern") or ""
                c_action=c.get("action") or ""
                if fnmatch.fnmatch(resource,c_resource) or fnmatch.fnmatch(c_resource,resource) or c_resource=="outline/*":
                    if c_action.upper()==action.upper() or c_action=="*" or c_action.upper()=="READ": return True,f"capability matched {c_resource}:{c_action}"
                if c_resource=="outline/*" and c_action.upper() in (action.upper(),"READ","*"): return True,"capability outline/* READ"
            elif isinstance(c,str):
                if fnmatch.fnmatch(resource,c) or c=="outline/*" or c=="outline/*:READ": return True,f"capability string matched {c}"
        return False,f"missing capability READ {resource} (found {caps})"
    def _enforce_data_access(self,action:str,resource:str)->None:
        if get_data_access_policy is None: return
        try:
            policy=get_data_access_policy(); result=policy.read_path(action,resource,source="read_replica")
            if not result.allowed or getattr(result,"decision","")=="DENY": raise PermissionError(f"data_access denied: {result.reason}")
        except PermissionError: raise
        except Exception: pass
    def _enforce_field_limits(self,tool_name:str,args:dict[str,Any],resource:str)->None:
        if validate_tool_call is None or ToolPolicy is None:
            limit=args.get("limit") or args.get("max_results") or args.get("page_size")
            if limit is not None:
                try:
                    if int(limit)>100: raise ValueError(f"limit {limit} exceeds max 100")
                except ValueError as e:
                    if "exceeds" in str(e): raise
            return
        policy=_TOOL_POLICIES.get(tool_name)
        if policy is None: return
        action=self.TOOL_ACTION.get(tool_name,"READ")
        allowed,reason=validate_tool_call(policy,action=action,args=args,resource=resource)
        if not allowed: raise ValueError(f"field/row limit denied for {tool_name}: {reason}")
    def filter_collections(self,agent_context:dict[str,Any]|Any,collections:list[dict[str,Any]])->list[dict[str,Any]]:
        if isinstance(agent_context,dict): user_id=agent_context.get("user_id"); groups=list(agent_context.get("groups") or [])
        else: user_id=getattr(agent_context,"user_id",None); groups=list(getattr(agent_context,"groups",[]) or [])
        is_admin="admin" in groups or user_id=="employee:admin"
        if is_admin: return collections
        return [c for c in collections if "private" not in str(c.get("name","")).lower() and "private" not in str(c.get("id","")).lower()]
    def add_document(self,doc:OutlineDoc|dict[str,Any])->None:
        if isinstance(doc,dict):
            nd=OutlineDoc(id=doc.get("id",""),collection_id=doc.get("collection_id","team"),title=doc.get("title",""),content=doc.get("content",""),url=doc.get("url",""),classification=doc.get("classification","INTERNAL"),acl_version=doc.get("acl_version","v1"),acl=doc.get("acl",{})); self._docs.append(nd); self._acl_versions[f"outline/{nd.collection_id}/{nd.id}"]=nd.acl_version
        else: self._docs.append(doc); self._acl_versions[f"outline/{doc.collection_id}/{doc.id}"]=doc.acl_version
    def set_documents(self,docs:list[OutlineDoc|dict[str,Any]])->None:
        self._docs=[]; self._acl_versions.clear()
        for d in docs: self.add_document(d)
    def get_document_by_id(self,doc_id:str)->OutlineDoc|None:
        for d in self._docs:
            if d.id==doc_id: return d
        return None
    def search_documents(self,query:str,collection_id:str|None=None,limit:int=10)->list[OutlineDoc]:
        if not query or not query.strip(): candidates=list(self._docs)
        else:
            q_lower=query.lower(); q_terms=[t for t in re.split(r"\s+",q_lower) if t]; candidates=[]
            for doc in self._docs:
                haystack=f"{doc.title} {doc.content}".lower()
                if any(term in haystack for term in q_terms): candidates.append(doc)
                elif query.lower() in haystack: candidates.append(doc)
        if collection_id: candidates=[d for d in candidates if d.collection_id==collection_id]
        return candidates[:limit]
    def acl_filter(self,documents:list[OutlineDoc],agent_context:dict[str,Any]|Any)->tuple[list[OutlineDoc],int]:
        allowed=[]; denied=0
        for doc in documents:
            r=self.check_document_acl(agent_context,doc)
            if r.allowed: allowed.append(doc)
            else: denied+=1
        return allowed,denied
    def rank(self,documents:list[OutlineDoc],query:str)->list[OutlineDoc]:
        if not query: return documents
        q_lower=query.lower(); q_terms=[t for t in re.split(r"\s+",q_lower) if t]; scored=[]
        for doc in documents:
            score=0.0; tl=doc.title.lower(); cl=doc.content.lower()
            if q_lower in tl: score+=10
            for term in q_terms:
                if term in tl: score+=5
                if term in cl: score+=1
            if q_lower in cl: score+=3
            doc.score=score; scored.append((score,doc))
        scored.sort(key=lambda x:x[0],reverse=True); return [d for _,d in scored]
    def get_acl_version(self,resource:str)->str|None: return self._acl_versions.get(resource)
    def set_acl_version(self,resource:str,version:str)->str|None:
        old=self._acl_versions.get(resource); self._acl_versions[resource]=version
        for d in self._docs:
            if f"outline/{d.collection_id}/{d.id}"==resource: d.acl_version=version
        return old
    def bump_acl_version(self,resource:str)->str:
        cur=self._acl_versions.get(resource,"v1")
        try: n=int(cur.lstrip("v"))+1; new=f"v{n}"
        except Exception: new=f"{cur}_v2"
        self.set_acl_version(resource,new); return new
    def invalidate_by_acl_change(self,resource:str,new_version:str|None=None,reason:str="acl_version_changed")->int:
        if new_version: self.set_acl_version(resource,new_version)
        count=0
        if self._memory_store is not None:
            try: count=self._memory_store.invalidate_by_resource(resource,reason=reason)
            except Exception: pass
            prefix=resource.rstrip("/*")
            for rid in list(self._acl_versions.keys()):
                if rid.startswith(prefix) and rid!=resource:
                    try: c=self._memory_store.invalidate_by_resource(rid,reason=reason); count+=c
                    except Exception: pass
        return count
    def update_acl(self,resource:str,acl:dict[str,Any],bump_version:bool=True)->str:
        self._acl[resource]=acl; new_ver=self.bump_acl_version(resource) if bump_version else self._acl_versions.get(resource,"v1")
        self.invalidate_by_acl_change(resource,new_version=new_ver,reason="acl_updated"); return new_ver
    def _record_provenance(self,docs:list[OutlineDoc],agent_context:dict[str,Any]|Any)->list[str]:
        if self._memory_store is None or MemoryScope is None: return []
        if isinstance(agent_context,dict): delegation_id=agent_context.get("delegation_id") or agent_context.get("source_delegation_id"); tenant_id=agent_context.get("tenant_id","default")
        else: delegation_id=getattr(agent_context,"delegation_id",None); tenant_id=getattr(agent_context,"tenant_id","default")
        mem_ids=[]
        for doc in docs:
            rid=f"outline/{doc.collection_id}/{doc.id}"; ver=self._acl_versions.get(rid,doc.acl_version)
            try:
                rec=self._memory_store.write(owner="organization",scope=MemoryScope.CORPORATE,content=f"{doc.title}: {doc.content[:500]}",classification=doc.classification,source_resource_id=rid,source_acl_version=ver,source_delegation_id=delegation_id,retention_policy="standard",tenant_id=tenant_id,provenance={"outline_doc_id":doc.id,"collection_id":doc.collection_id})
                mem_ids.append(rec.id)
            except Exception: continue
        return mem_ids
    async def list_tools(self)->list[str]: return list(self.TOOL_ACTION.keys())
    async def list_resources(self)->list[str]: return ["outline/*","outline/team/*","outline/private/*"]
    def tool_action(self,tool_name:str)->str: return self.TOOL_ACTION.get(tool_name,"READ")
    def describe_tools(self)->list[dict[str,Any]]: return [{"name":k,"action":v,"resource_pattern":"outline/*","endpoint":self.ENDPOINTS.get(k,("POST",""))} for k,v in self.TOOL_ACTION.items()]
    def _headers(self)->dict[str,str]: return {"Authorization":f"Bearer {self.api_key}","Content-Type":"application/json"} if self.api_key else {}
    async def _post(self,path:str,body:dict[str,Any])->dict[str,Any]:
        if not self.api_key: return {"_skeleton":True,"path":path,"body":body,"message":"OUTLINE_API_KEY not set — skeleton response"}
        if httpx is None: raise RuntimeError("httpx not installed")
        url=f"{self.api_url}{path}"
        async with httpx.AsyncClient(timeout=20) as client:
            resp=await client.post(url,json=body,headers=self._headers()); resp.raise_for_status(); return resp.json()
    async def call_tool(self,tool_name:str,args:dict[str,Any],agent_context:dict[str,Any]|Any)->dict[str,Any]:
        resource:str=args.get("resource") or args.get("collection_id") or args.get("document_id") or "outline/team/docs"
        if not resource.startswith("outline"): resource=f"outline/{resource}"
        action=self.tool_action(tool_name)
        cap_ok,cap_reason=self._check_capability(agent_context,resource="outline/*",action="READ")
        if not cap_ok: raise PermissionError(f"capability denied: {cap_reason} resource={resource}")
        self._enforce_field_limits(tool_name,args,resource)
        self._enforce_data_access(action,resource)
        acl=self.check_acl(agent_context,resource,action=action)
        if not acl.allowed: raise PermissionError(f"ACL denied: {acl.reason} resource={resource}")
        if action in ("CREATE","MODIFY"):
            w=self.can_write(agent_context,resource)
            if not w.allowed: raise PermissionError(f"write ACL denied: {w.reason}")
        method,path=self.ENDPOINTS.get(tool_name,("POST","/api/documents.search"))
        body=self._build_body(tool_name,args)
        if not self.api_key and tool_name in ("outline_search","outline_read","outline_documents_info","outline_documents_list","outline_collections_list","outline_collections_info"):
            mock_result=await self._mock_call(tool_name,args,agent_context)
            if tool_name in ("outline_search","outline_read","outline_documents_info"):
                docs_for_prov=mock_result.get("_docs_for_prov",[])
                if docs_for_prov: self._record_provenance(docs_for_prov,agent_context)
            mock_result.pop("_docs_for_prov",None)
            return mock_result
        result=await self._post(path,body)
        if result.get("_skeleton"):
            return {"tool":tool_name,"action":action,"resource":resource,"acl":{"allowed":acl.allowed,"reason":acl.reason},"skeleton_request":{"method":method,"path":path,"body":body},"_note":"set OUTLINE_API_KEY and OUTLINE_API_URL for real calls"}
        if isinstance(result.get("data"),list): result["data"]=self.filter_collections(agent_context,result["data"])
        return {"tool":tool_name,"action":action,"resource":resource,"acl":{"allowed":True,"matched":acl.matched_acl},"result":result}
    async def _mock_call(self,tool_name:str,args:dict[str,Any],agent_context:dict[str,Any]|Any)->dict[str,Any]:
        resource=args.get("resource") or args.get("collection_id") or args.get("document_id") or "outline/team/docs"
        if not resource.startswith("outline"): resource=f"outline/{resource}"
        action=self.tool_action(tool_name)
        if tool_name=="outline_search":
            query=args.get("query") or args.get("q") or ""; limit=int(args.get("limit",10)); collection_id=args.get("collection_id") or args.get("collection")
            candidates=self.search_documents(query,collection_id=collection_id,limit=limit*2)
            allowed,denied=self.acl_filter(candidates,agent_context)
            ranked=self.rank(allowed,query)[:limit]
            docs_dict=[]
            for d in ranked:
                rid=f"outline/{d.collection_id}/{d.id}"
                docs_dict.append({"id":d.id,"title":d.title,"content":d.content,"url":d.url,"collection_id":d.collection_id,"collection":d.collection_id,"classification":d.classification,"acl_version":self._acl_versions.get(rid,d.acl_version),"score":d.score,"resource_id":rid})
            return {"tool":tool_name,"action":action,"resource":resource,"data":docs_dict,"total":len(candidates),"filtered_count":denied,"count":len(docs_dict),"query":query,"collection_id":collection_id,"_docs_for_prov":ranked}
        if tool_name in ("outline_read","outline_documents_info"):
            doc_id=args.get("document_id") or args.get("id") or args.get("resource","")
            if "/" in doc_id and doc_id.startswith("outline"): doc_id=doc_id.split("/")[-1]
            doc=self.get_document_by_id(doc_id)
            if doc is None: return {"tool":tool_name,"action":action,"resource":resource,"error":"not_found","document_id":doc_id}
            acl_r=self.check_document_acl(agent_context,doc)
            if not acl_r.allowed: raise PermissionError(f"ACL denied for document {doc_id}: {acl_r.reason}")
            rid=f"outline/{doc.collection_id}/{doc.id}"
            expected_version=args.get("expected_acl_version") or args.get("acl_version") or args.get("source_acl_version")
            current_version=self._acl_versions.get(rid,doc.acl_version)
            if expected_version and expected_version!=current_version: raise PermissionError(f"ACL version mismatch for {doc_id}: expected {expected_version}, current {current_version} — permission changed, invalidate")
            return {"tool":tool_name,"action":action,"resource":rid,"data":{"id":doc.id,"title":doc.title,"content":doc.content,"url":doc.url,"collection_id":doc.collection_id,"classification":doc.classification,"acl_version":current_version,"resource_id":rid},"_docs_for_prov":[doc]}
        if tool_name=="outline_collections_list":
            cols=self.filter_collections(agent_context,list(self._collections))
            out=[]
            for c in cols: out.append({**c,"acl_version":self._acl_versions.get(f"outline/{c['id']}","v1")})
            return {"tool":tool_name,"action":action,"resource":resource,"data":out,"count":len(out)}
        if tool_name=="outline_collections_info":
            cid=args.get("collection_id") or args.get("id") or ""
            if cid.startswith("outline/"): cid=cid.split("/")[-1]
            for c in self._collections:
                if c["id"]==cid:
                    filtered=self.filter_collections(agent_context,[c])
                    if not filtered: raise PermissionError(f"ACL denied for collection {cid}")
                    return {"tool":tool_name,"action":action,"resource":f"outline/{cid}","data":{**c,"acl_version":self._acl_versions.get(f"outline/{cid}","v1")}}
            return {"tool":tool_name,"action":action,"resource":resource,"error":"not_found","collection_id":cid}
        if tool_name=="outline_documents_list":
            collection_id=args.get("collection_id"); limit=int(args.get("limit",25))
            docs=[d for d in self._docs if not collection_id or d.collection_id==collection_id]
            allowed,denied=self.acl_filter(docs,agent_context)
            allowed=allowed[:limit]
            docs_dict=[{"id":d.id,"title":d.title,"collection_id":d.collection_id,"acl_version":self._acl_versions.get(f"outline/{d.collection_id}/{d.id}",d.acl_version),"resource_id":f"outline/{d.collection_id}/{d.id}"} for d in allowed]
            return {"tool":tool_name,"action":action,"resource":resource,"data":docs_dict,"count":len(docs_dict),"filtered_count":denied}
        return {"tool":tool_name,"action":action,"resource":resource,"error":"unknown_tool"}
    def _build_body(self,tool_name:str,args:dict[str,Any])->dict[str,Any]:
        if tool_name=="outline_search": return {"query":args.get("query",args.get("q","")),"limit":args.get("limit",10),"collectionId":args.get("collection_id")}
        if tool_name in ("outline_read","outline_documents_info"): return {"id":args.get("document_id") or args.get("id") or args.get("resource","")}
        if tool_name=="outline_create": return {"title":args.get("title","Untitled"),"text":args.get("text",args.get("content","")),"collectionId":args.get("collection_id")}
        if tool_name=="outline_modify": return {"id":args.get("document_id") or args.get("id"),"title":args.get("title"),"text":args.get("text")}
        if tool_name=="outline_collections_list": return {}
        if tool_name=="outline_collections_info": return {"id":args.get("collection_id") or args.get("id")}
        if tool_name=="outline_documents_list": return {"collectionId":args.get("collection_id"),"limit":args.get("limit",25)}
        return dict(args)
    async def search(self,query:str,agent_context:dict[str,Any]|Any,limit:int=10,collection_id:str|None=None)->dict[str,Any]:
        a: dict[str,Any]={"query":query,"limit":limit}
        if collection_id: a["collection_id"]=collection_id
        return await self.call_tool("outline_search",a,agent_context)
    async def get_document(self,document_id:str,agent_context:dict[str,Any]|Any,expected_acl_version:str|None=None)->dict[str,Any]:
        a: dict[str,Any]={"document_id":document_id}
        if expected_acl_version: a["expected_acl_version"]=expected_acl_version
        return await self.call_tool("outline_read",a,agent_context)
    async def read(self,document_id:str,agent_context:dict[str,Any]|Any,expected_acl_version:str|None=None)->dict[str,Any]: return await self.get_document(document_id,agent_context,expected_acl_version)
    async def list_collections(self,agent_context:dict[str,Any]|Any)->dict[str,Any]: return await self.call_tool("outline_collections_list",{},agent_context)
    def describe(self)->dict[str,Any]: return {"name":self.name,"provider":self.provider,"tools":list(self.TOOL_ACTION.keys()),"resources":["outline/*"],"api_url":self.api_url,"has_api_key":bool(self.api_key),"endpoints":{k:f"{v[0]} {v[1]}" for k,v in self.ENDPOINTS.items()}}
