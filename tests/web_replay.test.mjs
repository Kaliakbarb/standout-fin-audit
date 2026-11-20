import test from 'node:test';
import assert from 'node:assert/strict';
import {replayFrames, recognitionTotal} from '../src/fin_audit/web_assets/replay.mjs';

test('replay describes only the actions actually present in the trajectory',()=>{
  const reference={trajectory:[
    {step:0,action:null,observation:{manifest:{}}},
    {step:1,action:{action:'list_records',args:{table:'ledger'}},observation:{result:{records:[{},{}]}}},
    {step:2,action:{action:'submit',args:{findings:{findings:[{type:'fx_conversion'}]}}},observation:{result:{submitted:true}},terminated:true}
  ]};
  const before=JSON.stringify(reference),frames=replayFrames(reference);
  assert.equal(frames.length,3);
  assert.equal(frames[1].table,'ledger');
  assert.match(frames[1].detail,/2/);
  assert.equal(frames[2].terminal,true);
  assert.match(frames[2].detail,/1/);
  assert.equal(JSON.stringify(reference),before);
});

test('no completed trajectory means no invented actions',()=>{
  assert.deepEqual(replayFrames(null),[]);
  assert.deepEqual(replayFrames({}),[]);
});

test('visible revenue is a projection of revenue postings, not all debit balances',()=>{
  const book={policy:{accounts:{revenue:'REV'}},ledger:[
    {account:'REV',debit_minor:0,credit_minor:55000000},
    {account:'REV',debit_minor:5000000,credit_minor:0},
    {account:'AR',debit_minor:55000000,credit_minor:0},
    {account:'CASH',debit_minor:123456789,credit_minor:0}
  ]};
  assert.equal(recognitionTotal(book),50000000);
});
