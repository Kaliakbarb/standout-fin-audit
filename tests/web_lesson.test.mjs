import test from 'node:test';
import assert from 'node:assert/strict';
import {buildLesson, localDate, sourceOptions, suggestEvidence} from '../src/fin_audit/web_assets/lesson.mjs';

function book() {
  return {
    manifest:{base_currency:'KZT',currency_exponents:{KZT:2,USD:2},timezone:'Asia/Almaty'},
    policy:{accounts:{receivable:'AR',cash:'CASH'}},
    invoices:[{invoice_id:'i1',doc_id:'d1',currency:'KZT',total_minor:5000000,issued_at:'2026-11-05T12:00:00+05:00'},
      {invoice_id:'i2',doc_id:'d2',currency:'USD',total_minor:100000,issued_at:'2026-11-04T22:00:00Z'}],
    ledger:[{row_id:'r1',invoice_id:'i1',payment_id:null,account:'AR',debit_minor:5000000,credit_minor:0},
      {row_id:'r2',invoice_id:'i1',payment_id:null,account:'AR',debit_minor:5000000,credit_minor:0},
      {row_id:'r3',invoice_id:'i2',payment_id:null,account:'AR',debit_minor:45000000,credit_minor:0,applied_fx_rate:'450'},
      {row_id:'r4',invoice_id:'i2',payment_id:null,account:'REVENUE',debit_minor:0,credit_minor:45000000}],
    payments:[],fx:[{record_id:'f4',date:'2026-11-04',source_currency:'USD',target_currency:'KZT',rate:'450'},
      {record_id:'f5',date:'2026-11-05',source_currency:'USD',target_currency:'KZT',rate:'500'}],
  };
}

test('lesson proves the two separate 50,000 KZT differences using public records',()=>{
  const source=book(),before=JSON.stringify(source),lesson=buildLesson(source);
  assert.equal(lesson.duplicate.claimed_amount_minor,5000000);
  assert.deepEqual(lesson.duplicate.affected_ids,['r1','r2']);
  assert.equal(lesson.fx.claimed_amount_minor,5000000);
  assert.equal(lesson.expected,50000000);
  assert.deepEqual(new Set(lesson.fx.evidence_ids),new Set(['policy','r3','d2','f5']));
  assert.equal(JSON.stringify(source),before);
});

test('source date uses the company timezone, including a UTC day boundary',()=>{
  assert.equal(localDate('2026-11-04T22:00:00Z','Asia/Almaty'),'2026-11-05');
});

test('a clean or unsupported book does not get a fabricated duplicate lesson',()=>{
  const source=book();source.ledger.splice(1,1);
  assert.equal(buildLesson(source),null);
  assert.equal(buildLesson({...source,invoices:[]}),null);
});

test('the lesson does not silently round unsupported fractional valuations',()=>{
  const source=book();source.fx[1].rate='500.000001';
  assert.equal(buildLesson(source),null);
});

test('source choices exclude balancing revenue rows for currency and period claims',()=>{
  assert.deepEqual(sourceOptions(book(),'fx_conversion'),['r1','r2','r3']);
  assert.deepEqual(sourceOptions(book(),'period_cutoff'),['r1','r2','r3']);
});

test('allocation evidence follows the designated invoice and every payment posting',()=>{
  const source=book();source.payments=[{payment_id:'p1',doc_id:'pd1',invoice_refs:['i2'],currency:'USD',timestamp:'2026-11-05T12:00:00+05:00'}];
  source.ledger.push({row_id:'cash',invoice_id:'i2',payment_id:'p1',account:'CASH'},
    {row_id:'unallocated',invoice_id:'i2',payment_id:'p1',account:'UNALLOCATED'});
  const links=suggestEvidence(source,'payment_allocation','p1');
  assert.deepEqual(links.affected,['p1','i2']);
  assert.deepEqual(new Set(links.evidence),new Set(['policy','pd1','d2','cash','unallocated']));
});

test('an unknown payment designation never fabricates an invoice link',()=>{
  const source=book();source.payments=[{payment_id:'p1',doc_id:'pd1',invoice_refs:[]}];
  assert.deepEqual(suggestEvidence(source,'payment_allocation','p1').affected,['p1']);
});

test('missing sources remain empty rather than borrowing unrelated documents',()=>{
  for(const type of ['duplicate_invoice','fx_conversion','period_cutoff','payment_allocation'])
    assert.deepEqual(suggestEvidence(book(),type,'absent'),{affected:[],evidence:['policy']});
});
