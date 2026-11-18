import hashlib,json

def decide(state):
    b=state['balance'];handover=state.get('allocation',{}).get('handover',[])
    held=sum(h['decision']!='整箱交接' for h in handover)
    blocked=b['graph_errors']+b['weight_errors']+len(b['declaration_differences'])+len(state['hazard_issues'])+len(state['quarantine'])+held
    evidence={'roots':state['graph']['roots'],'weights':state['weight_checks'],'hazards':state['hazard_issues'],'handover':handover}
    return {'status':'可封存' if b['row_balanced'] and not blocked else '待处理','blocked':blocked,'seal':hashlib.sha256(json.dumps(evidence,sort_keys=True).encode()).hexdigest()}
def apply(state):
    state['archive']=decide(state);state['views']['archive']={'title':'版本封存','rows':[{'id':r['id'],'revision':r['revision'],'parent':r['parent']} for r in state['records']],'metrics':{'主单':len(state['graph']['roots']),'状态':state['archive']['status'],'阻断':state['archive']['blocked'],'暂缓箱':sum(h['decision']!='整箱交接' for h in state.get('allocation',{}).get('handover',[]))}}
