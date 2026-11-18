from collections import defaultdict
from decimal import Decimal
from app.coerce import number
from app.errors import BusinessError

FIELDS = ('pieces', 'net_kg', 'gross_kg')

def _zero():
    return {'pieces': 0, 'net_kg': Decimal(0), 'gross_kg': Decimal(0)}

def _add(acc, qty):
    for field in FIELDS: acc[field] += qty[field]

def _roots(rows):
    nodes = {r['id']: r for r in rows}
    root_of = {}
    for rid in nodes:
        seen, current = {rid}, rid
        while True:
            parent = nodes[current]['parent'].strip()
            if not parent:
                root_of[rid] = current; break
            if parent not in nodes or parent in seen:
                root_of[rid] = None; break
            seen.add(parent); current = parent
    return root_of

def _own(rows, graph):
    nodes = {r['id']: r for r in rows}
    children = graph['children']
    own = {}
    for r in rows:
        kids = children.get(r['id'], [])
        own[r['id']] = {
            'pieces': r['pieces'] - sum(nodes[k]['pieces'] for k in kids),
            'net_kg': Decimal(r['net_kg']) - sum((Decimal(nodes[k]['net_kg']) for k in kids), Decimal(0)),
            'gross_kg': Decimal(r['gross_kg']) - sum((Decimal(nodes[k]['gross_kg']) for k in kids), Decimal(0)),
        }
    return own

def attribute(rows, graph):
    """箱×主单归属。每节点只计自有量(申报减直接子节点合计)，同一件货全舱只计一次。"""
    root_of = _roots(rows)
    own = _own(rows, graph)
    counts = defaultdict(int)
    quantities = defaultdict(_zero)
    for r in rows:
        key = (r['container'].strip(), root_of[r['id']])
        counts[key] += 1
        _add(quantities[key], own[r['id']])
    result = []
    for (container, root), qty in sorted(quantities.items(), key=lambda kv: (kv[0][0], kv[0][1] or '')):
        result.append({'container': container, 'root': root or '',
                       'pieces': qty['pieces'], 'net_kg': str(qty['net_kg']), 'gross_kg': str(qty['gross_kg']),
                       'code': 'ATTRIBUTED' if root else 'UNATTRIBUTED'})
    return {'rows': result, 'root_of': root_of, 'own': own}

def _actual(context):
    raw = context.get('container_actual') or {}
    if not isinstance(raw, dict): raise BusinessError('BAD_CONTEXT', '箱体实测须为对象')
    parsed = {}
    for cid, values in raw.items():
        if not isinstance(values, dict): raise BusinessError('BAD_CONTEXT', '箱体实测须为对象')
        entry = {}
        for field in FIELDS:
            if values.get(field) is not None:
                value = number(values[field])
                if value < 0 or (field == 'pieces' and value % 1):
                    raise BusinessError('INVALID_RANGE', '箱体实测数值越界')
                entry[field] = value
        if 'net_kg' in entry and 'gross_kg' in entry and entry['gross_kg'] < entry['net_kg']:
            raise BusinessError('INVALID_RANGE', '箱体实测毛重不得低于净重')
        parsed[str(cid)] = entry
    return parsed

def containers(rows, graph, own, root_of, context):
    """箱体总量=各主单归属之和；核验量=箱内叶子合计，与逐层核验同口径对照。"""
    actual = _actual(context)
    children = graph['children']
    declared = defaultdict(_zero)
    verified = defaultdict(_zero)
    unattributed = defaultdict(int)
    for r in rows:
        cid = r['container'].strip()
        _add(declared[cid], own[r['id']])
        if not children.get(r['id']):
            _add(verified[cid], {'pieces': r['pieces'], 'net_kg': Decimal(r['net_kg']), 'gross_kg': Decimal(r['gross_kg'])})
        if root_of[r['id']] is None: unattributed[cid] += 1
    result = []
    for cid in sorted(set(declared) | set(actual)):
        d, v = declared[cid], verified[cid]
        row = {'container': cid,
               'declared_pieces': d['pieces'], 'declared_net_kg': str(d['net_kg']), 'declared_gross_kg': str(d['gross_kg']),
               'verified_pieces': v['pieces'], 'verified_net_kg': str(v['net_kg']), 'verified_gross_kg': str(v['gross_kg']),
               'unattributed': unattributed[cid]}
        layer_mismatch = any(d[f] != v[f] for f in FIELDS)
        act = actual.get(cid)
        if act is None:
            row.update({'actual_pieces': '', 'actual_net_kg': '', 'actual_gross_kg': '',
                        'difference_pieces': '', 'difference_net_kg': '', 'difference_gross_kg': ''})
            row['code'] = 'LAYER_MISMATCH' if layer_mismatch else 'NO_ACTUAL'
        else:
            mismatch = False
            for field in FIELDS:
                value = act.get(field)
                row['actual_' + field] = str(value) if value is not None else ''
                difference = (d[field] - value) if value is not None else None
                row['difference_' + field] = str(difference) if difference is not None else ''
                if difference: mismatch = True
            row['code'] = 'CONTAINER_MISMATCH' if mismatch else ('LAYER_MISMATCH' if layer_mismatch else 'CONTAINER_BALANCED')
        result.append(row)
    return result

def _subtree(root, children):
    seen, stack = set(), [root]
    while stack:
        node = stack.pop()
        if node in seen: continue
        seen.add(node)
        stack.extend(children.get(node, []))
    return seen

def handover(rows, graph, weight_checks, hazard_issues, root_of, container_rows):
    """按可被证据证明的部分裁决交接：主单子树逐层平衡、无关系错误、无危险品问题、
    所在箱无混装禁忌方可交接；全部可交接且无未归属、实测相符为整箱交接。"""
    children = graph['children']
    bad_weight = defaultdict(set)
    for check in weight_checks:
        if check['code'] != 'BALANCED': bad_weight[check['id']].add(check['code'])
    hazard_by_id = defaultdict(set)
    for issue in hazard_issues: hazard_by_id[issue['id']].add(issue['code'])
    graph_errors = graph['errors']
    container_row = {c['container']: c for c in container_rows}
    by_container = defaultdict(set)
    for r in rows:
        root = root_of[r['id']]
        if root: by_container[r['container'].strip()].add(root)
    result = []
    for cid in sorted(set(by_container) | set(container_row)):
        ready, held, reasons = [], [], {}
        container_hazard = hazard_by_id.get(cid, set())
        for root in sorted(by_container.get(cid, set())):
            subtree = _subtree(root, children)
            codes = set(container_hazard)
            for nid in subtree:
                codes |= bad_weight.get(nid, set()) | hazard_by_id.get(nid, set())
            for error in graph_errors:
                if error['id'] in subtree or any(p in subtree for p in error.get('path', [])):
                    codes.add(error['code'])
            if codes: held.append(root); reasons[root] = sorted(codes)
            else: ready.append(root)
        summary = container_row.get(cid)
        mismatch = bool(summary) and summary['code'] == 'CONTAINER_MISMATCH'
        unattributed = summary['unattributed'] if summary else 0
        if not held and not unattributed and not mismatch: decision = '整箱交接'
        elif ready: decision = '部分交接'
        else: decision = '暂缓交接'
        detail = '; '.join([f"{r}: {','.join(reasons[r])}" for r in held]
                           + ([f'未归属节点 {unattributed}'] if unattributed else [])
                           + (['箱体实测不符'] if mismatch else []))
        result.append({'container': cid, 'decision': decision,
                       'ready': ','.join(ready), 'held': ','.join(held),
                       'unattributed': unattributed, 'detail': detail})
    return result

def apply(state):
    rows, graph = state['records'], state['graph']
    att = attribute(rows, graph)
    container_rows = containers(rows, graph, att['own'], att['root_of'], state['context'])
    handover_rows = handover(rows, graph, state['weight_checks'], state['hazard_issues'], att['root_of'], container_rows)
    state['allocation'] = {'rows': att['rows'], 'containers': container_rows, 'handover': handover_rows}
    state['views']['allocation'] = {'title': '拼箱归属', 'rows': att['rows'], 'metrics': {
        '归属行': len(att['rows']), '箱数': len(container_rows),
        '整箱交接': sum(h['decision'] == '整箱交接' for h in handover_rows),
        '暂缓交接': sum(h['decision'] == '暂缓交接' for h in handover_rows)}}
