const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const nodes = new Map();
function node() { return { textContent: '', value: '', disabled: false, children: [], classList: {toggle(){}, add(){}, remove(){}}, setAttribute(){}, addEventListener(){}, append(...items){this.children.push(...items)}, replaceChildren(...items){this.children = items}, focus(){} }; }
const docs = {a: {id:'a',name:'A.pdf',page_count:1,pages:[]}, b: {id:'b',name:'B.pdf',page_count:1,pages:[]}};
let pending;
const searchRequests = [];
const context = vm.createContext({
  document: {getElementById(id) {if(!nodes.has(id)) nodes.set(id,node()); return nodes.get(id)}, createElement:node, querySelectorAll(){return []}},
  window: {confirm(){return true}},
  fetch: async path => {
    if(path === '/api/documents') return {ok:true,json: async()=>[]};
    if(path === '/api/health') return {ok:true,json:async()=>({model:{ready:true}})};
    if(path.endsWith('/summary') || path.endsWith('/answer')) return await new Promise(resolve=>{pending=resolve});
    if(path.includes('/search?')) {
      searchRequests.push(path);
      return {ok:true,json:async()=>({query:'design revisions',results:[{id:'p2-s1',page:2,text:'Revisions are kept for 18 days.'}]})};
    }
    const id=path.split('/').at(-1);
    return {ok:true,json:async()=>docs[id]};
  }
});
vm.runInContext(fs.readFileSync('pdfdesk/web/app.js','utf8'),context);
const run = code => vm.runInContext(code,context);
(async()=>{
  await new Promise(resolve=>setImmediate(resolve));
  await run('selectDocument("a")');
  const summary = run('generateSummary()');
  assert(nodes.get('generate-summary').disabled);
  await run('selectDocument("b")');
  assert(!nodes.get('generate-summary').disabled);
  await run('selectDocument("a")');
  assert(nodes.get('generate-summary').disabled, 'summary remains busy after reselect');
  assert(nodes.get('summary-status').textContent.includes('Reading'));
  pending({ok:true,json:async()=>({pages:[{page:1,summary:'Completed summary.',key_points:[]}]})});
  await summary;
  assert(!nodes.get('generate-summary').disabled);
  assert.equal(nodes.get('summary-status').textContent,'');
  assert(nodes.get('summary-results').children.length > 0, 'completed summary renders after switching away and back');
  nodes.get('question').value='What is the launch date?';
  const answer = run('askQuestion({preventDefault(){}})');
  await run('selectDocument("b")');
  await run('selectDocument("a")');
  assert(nodes.get('ask-question').disabled);
  assert.equal(nodes.get('question').value,'What is the launch date?');
  pending({ok:false,json:async()=>({error:'Model unavailable.'})});
  await answer;
  assert.equal(nodes.get('answer-error').textContent,'Model unavailable.');
  assert(!nodes.get('ask-question').disabled);
  await run('selectDocument("b")');
  assert.equal(nodes.get('answer-error').textContent,'');
  await run('selectDocument("a")');
  assert.equal(nodes.get('answer-error').textContent,'Model unavailable.', 'error survives document reselection');
  assert(run('state.summaries.has("a")'), 'existing summary stays cached');
  nodes.get('source-query').value='design revisions';
  await run('searchSource({preventDefault(){}})');
  assert.equal(searchRequests[0],'/api/documents/a/search?q=design%20revisions');
  assert(nodes.get('search-results').children.length > 1, 'search renders page-linked passages');
  await run('selectDocument("b")');
  assert.equal(nodes.get('source-query').value,'');
  await run('selectDocument("a")');
  assert.equal(nodes.get('source-query').value,'design revisions');
  assert(nodes.get('search-results').children.length > 1, 'search results survive returning to a document');
  console.log('PASS: summary completion, pending controls/question, document-specific errors, and cached results survive switching.');
  console.log('PASS: source search encodes queries and preserves document-specific results.');
})().catch(error=>{console.error(error);process.exitCode=1});
