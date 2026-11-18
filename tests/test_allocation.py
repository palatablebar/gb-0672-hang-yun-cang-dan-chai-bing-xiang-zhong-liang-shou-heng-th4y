from copy import deepcopy
from decimal import Decimal
import pytest
from fastapi.testclient import TestClient
from app import main
from app.config import ROOT
from app.engine import evaluate
from app.errors import BusinessError
from app.modules.allocation.rules import attribute, containers, handover
from app.modules.grouping.rules import graph
from app.modules.weight.rules import check_weights
from app.modules.archive.rules import decide

def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DB_PATH', str(tmp_path / 'test.sqlite'))
    return TestClient(main.app)

def test_clean_attribution_rows(clean):
    rows = clean['allocation']['rows']
    assert len(rows) == 6
    first = next(r for r in rows if r['container'] == 'C001')
    assert first['root'] == 'M001' and first['pieces'] == 40
    assert Decimal(first['net_kg']) == 400 and Decimal(first['gross_kg']) == 440
    assert all(r['code'] == 'ATTRIBUTED' for r in rows)

def test_no_double_count_global(clean):
    # TRAP-22 同一件货全舱只计一次：归属合计等于主单申报合计
    rows = clean['allocation']['rows']
    assert sum(r['pieces'] for r in rows) == clean['balance']['pieces'] == 240
    assert sum(Decimal(r['net_kg']) for r in rows) == Decimal(clean['balance']['net_kg']) == 2400
    assert sum(Decimal(r['gross_kg']) for r in rows) == Decimal(clean['balance']['gross_kg']) == 2640

def test_shared_container_two_masters(clean):
    rows = [dict(r, container='C001') if r['id'].startswith('M002') else dict(r) for r in clean['records']]
    att = attribute(rows, graph(rows))
    mine = [r for r in att['rows'] if r['container'] == 'C001']
    assert {(r['root'], r['pieces']) for r in mine} == {('M001', 40), ('M002', 40)}
    assert sum(r['pieces'] for r in att['rows']) == 240

def test_cross_container_subtree(clean):
    rows = [dict(r, container='C002') if r['id'].startswith('M001-P2') else dict(r) for r in clean['records']]
    att = attribute(rows, graph(rows))
    c001 = next(r for r in att['rows'] if r['container'] == 'C001' and r['root'] == 'M001')
    c002 = next(r for r in att['rows'] if r['container'] == 'C002' and r['root'] == 'M001')
    assert c001['pieces'] == 20 and c002['pieces'] == 20
    assert sum(r['pieces'] for r in att['rows'] if r['root'] == 'M001') == 40
    assert sum(r['pieces'] for r in att['rows']) == 240

def test_dirty_unattributed(sample):
    rows = sample['allocation']['rows']
    x = next(r for r in rows if r['container'] == 'X')
    assert x['code'] == 'UNATTRIBUTED' and x['root'] == '' and x['pieces'] == 1
    y = next(r for r in rows if r['container'] == 'Y')
    assert y['code'] == 'UNATTRIBUTED'

def test_container_layer_consistency(sample):
    c001 = next(c for c in sample['allocation']['containers'] if c['container'] == 'C001')
    assert Decimal(c001['declared_gross_kg']) == 441 and Decimal(c001['verified_gross_kg']) == 440
    assert c001['code'] == 'LAYER_MISMATCH'
    c002 = next(c for c in sample['allocation']['containers'] if c['container'] == 'C002')
    assert c002['code'] == 'NO_ACTUAL' and c002['declared_pieces'] == c002['verified_pieces'] == 40

def test_dirty_handover_holds(sample):
    hand = {h['container']: h for h in sample['allocation']['handover']}
    assert hand['C001']['decision'] == '暂缓交接' and hand['C001']['held'] == 'M001'
    assert 'WEIGHT_MISMATCH' in hand['C001']['detail']
    assert hand['X']['decision'] == '暂缓交接' and hand['X']['unattributed'] == 1
    assert hand['Y']['decision'] == '暂缓交接' and hand['Y']['unattributed'] == 2
    assert hand['C002']['decision'] == '整箱交接' and hand['C002']['ready'] == 'M002'

def test_partial_handover_one_held(clean):
    rows = [dict(r) for r in clean['records']]
    for r in rows:
        if r['id'].startswith('M002'): r['container'] = 'C001'
        if r['id'] == 'M002-P1-1': r['net_kg'] = '99'
    g = graph(rows)
    att = attribute(rows, g)
    summaries = containers(rows, g, att['own'], att['root_of'], clean['context'])
    decision = next(h for h in handover(rows, g, check_weights(rows, g), [], att['root_of'], summaries) if h['container'] == 'C001')
    assert decision['decision'] == '部分交接'
    assert decision['ready'] == 'M001' and decision['held'] == 'M002'

def test_container_actual_mismatch():
    state = evaluate({'dataset': 'clean', 'overrides': {'container_actual': {'C001': {'pieces': 40, 'net_kg': '400', 'gross_kg': '439'}}}})
    c001 = next(c for c in state['allocation']['containers'] if c['container'] == 'C001')
    assert c001['code'] == 'CONTAINER_MISMATCH' and Decimal(c001['difference_gross_kg']) == 1
    hand = next(h for h in state['allocation']['handover'] if h['container'] == 'C001')
    assert hand['decision'] == '部分交接' and hand['ready'] == 'M001'
    assert state['archive']['status'] == '待处理'

def test_container_actual_balanced():
    state = evaluate({'dataset': 'clean', 'overrides': {'container_actual': {'C001': {'pieces': 40, 'net_kg': '400', 'gross_kg': '440'}}}})
    c001 = next(c for c in state['allocation']['containers'] if c['container'] == 'C001')
    assert c001['code'] == 'CONTAINER_BALANCED'
    assert next(h for h in state['allocation']['handover'] if h['container'] == 'C001')['decision'] == '整箱交接'
    assert state['archive']['status'] == '可封存'

def test_bad_container_actual_rejected():
    with pytest.raises(BusinessError):
        evaluate({'dataset': 'clean', 'overrides': {'container_actual': {'C001': {'gross_kg': 'abc'}}}})
    with pytest.raises(BusinessError):
        evaluate({'dataset': 'clean', 'overrides': {'container_actual': {'C001': {'net_kg': '500', 'gross_kg': '400'}}}})

def test_archive_counts_held_containers(sample):
    assert sample['archive']['blocked'] == 13

def test_seal_tracks_attribution(clean):
    state = deepcopy(clean)
    state['allocation']['handover'][0]['decision'] = '暂缓交接'
    assert decide(state)['seal'] != clean['archive']['seal']
    assert decide(state)['blocked'] == 1

def test_attribution_change_recomputes_results():
    base = evaluate({'dataset': 'clean'})
    moved = evaluate({'dataset': 'clean', 'overrides': {'container_actual': {'C001': {'pieces': 40, 'net_kg': '400', 'gross_kg': '439'}}}})
    assert base['allocation']['handover'] != moved['allocation']['handover']
    assert base['archive']['seal'] != moved['archive']['seal']
    assert base['archive']['status'] == '可封存' and moved['archive']['status'] == '待处理'

def test_retry_same_plan_no_second_attribution(tmp_path, monkeypatch):
    # TRAP-24 重试同一拼箱方案不新增第二套归属
    c = client(tmp_path, monkeypatch)
    shared = (ROOT / 'sample_data/clean.csv').read_text().replace(',C002,1', ',C001,1')
    a = c.post('/api/runs', json={'csv_text': shared}).json()
    b = c.post('/api/runs', json={'csv_text': shared}).json()
    assert a['manifest']['run_id'] == b['manifest']['run_id']
    assert len(c.get('/api/runs').json()) == 1
    assert b['allocation']['rows'] == a['allocation']['rows']
    mine = [r for r in a['allocation']['rows'] if r['container'] == 'C001']
    assert {(r['root'], r['pieces']) for r in mine} == {('M001', 40), ('M002', 40)}
    again = c.post('/api/runs/' + a['manifest']['run_id'] + '/rerun', json={}).json()
    assert again['manifest']['run_id'] == a['manifest']['run_id']
    assert again['allocation'] == a['allocation']
    assert len(c.get('/api/runs').json()) == 1

def test_failed_confirm_keeps_original(tmp_path, monkeypatch):
    # TRAP-25 确认失败恢复旧版结果并保留原方案
    c = client(tmp_path, monkeypatch)
    run = c.post('/api/runs', json={}).json()
    rid = run['manifest']['run_id']
    assert c.post(f'/api/runs/{rid}/review', json={'revision': 1}).status_code == 200
    assert c.post(f'/api/runs/{rid}/review', json={'revision': 1}).status_code == 409
    kept = c.get(f'/api/runs/{rid}').json()
    assert kept['revision'] == 2 and kept['status'] == '已复核'
    assert kept['allocation'] == run['allocation']
    assert kept['input_snapshot'] == run['input_snapshot']

def test_allocation_api_and_page(tmp_path, monkeypatch):
    c = client(tmp_path, monkeypatch)
    result = c.post('/api/allocation', json={}).json()
    assert 'allocation' in result and 'weight_checks' in result and 'balance' not in result
    run = c.post('/api/runs', json={}).json()
    page = c.get('/allocation?run_id=' + run['manifest']['run_id'])
    assert page.status_code == 200 and '拼箱归属' in page.text
